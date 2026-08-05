"""Allocation scope — the frozen WHO/WHERE universe of a cash-collection event.

Pure data container (no DB access). Plan 2b's scope-aware allocation engine in
CashCollectionService resolves a scope at post time, freezes it onto
CashCollectionEvent.scope_type / scope_snapshot, and replays corrections under
the STORED snapshot — never current topology. Spec:
docs/superpowers/specs/2026-07-24-place-groups-and-cluster-wallet-design.md §5.
"""

from dataclasses import dataclass
from typing import Optional, Tuple

SCOPE_PERSONAL = "personal"
SCOPE_CLUSTER = "cluster"
SCOPE_PLACE = "place"


@dataclass(frozen=True)
class AllocationScope:
    """One resolved allocation scope.

    - personal: orderer_cluster_user_ids == (customer_id,); everything else empty.
    - cluster:  orderer_cluster_user_ids == the wallet cluster's user ids.
    - place:    all four fields populated; rings 2-3 operate on the orderer's
      cluster (whose members need not own any group address), ring 1 on the
      group's member addresses.
    """

    scope_type: str
    group_id: Optional[int]
    address_ids: Tuple[int, ...]
    place_user_ids: Tuple[int, ...]
    orderer_cluster_user_ids: Tuple[int, ...]

    @classmethod
    def personal(cls, user_id: int) -> "AllocationScope":
        return cls(SCOPE_PERSONAL, None, (), (), (user_id,))

    @classmethod
    def cluster(cls, user_ids) -> "AllocationScope":
        return cls(SCOPE_CLUSTER, None, (), (), tuple(sorted(user_ids)))

    @classmethod
    def place(cls, group_id, address_ids, place_user_ids, orderer_cluster_user_ids) -> "AllocationScope":
        return cls(
            SCOPE_PLACE,
            group_id,
            tuple(sorted(address_ids)),
            tuple(sorted(place_user_ids)),
            tuple(sorted(orderer_cluster_user_ids)),
        )

    def to_snapshot(self) -> Optional[dict]:
        """The JSON frozen onto CashCollectionEvent.scope_snapshot (spec §4.2)."""
        if self.scope_type == SCOPE_PERSONAL:
            return None
        if self.scope_type == SCOPE_CLUSTER:
            return {"user_ids": list(self.orderer_cluster_user_ids)}
        return {
            "group_id": self.group_id,
            "address_ids": list(self.address_ids),
            "place_user_ids": list(self.place_user_ids),
            "orderer_cluster_user_ids": list(self.orderer_cluster_user_ids),
        }

    @classmethod
    def from_event(cls, event) -> "AllocationScope":
        """Rehydrate the frozen scope from a CashCollectionEvent.

        Defensive money rule: anything malformed (unknown scope_type, scoped
        event missing its snapshot) degrades to PERSONAL on the event's
        customer — never a guess at current topology. The nightly reconcile
        flags scoped events without snapshots.
        """
        scope_type = getattr(event, "scope_type", None) or SCOPE_PERSONAL
        snapshot = getattr(event, "scope_snapshot", None)
        if scope_type == SCOPE_CLUSTER and snapshot:
            return cls.cluster(snapshot.get("user_ids") or [event.customer_id])
        if scope_type == SCOPE_PLACE and snapshot:
            return cls.place(
                snapshot.get("group_id"),
                snapshot.get("address_ids") or [],
                snapshot.get("place_user_ids") or [],
                snapshot.get("orderer_cluster_user_ids") or [event.customer_id],
            )
        return cls.personal(event.customer_id)

    def covers_payment(self, payment, order) -> bool:
        """True when a payment target is inside this scope (spec §5.4).

        Cluster arm: payment.user_id in the scope's cluster ids ('user_ids' for
        CLUSTER, 'orderer_cluster_user_ids' for PLACE, the single owner for
        PERSONAL). Address arm (PLACE only): the payment's order is delivered
        to a member address of the frozen group.
        """
        if payment is not None and payment.user_id in self.orderer_cluster_user_ids:
            return True
        if self.scope_type == SCOPE_PLACE and order is not None:
            return getattr(order, "delivery_address_id", None) in self.address_ids
        return False
