"""Canonical customer link resolution (SSOT).

Every cluster-aware read/enforcement path in Phase 1 funnels through these
resolvers. They deliberately use plain SELECTs on the FK columns (never
join(User)) so a two-FK-to-users table can't bind to the wrong FK.
"""

import hashlib
import logging
import math
from decimal import Decimal
from typing import Any, Dict, List, Optional

from flask import current_app
from sqlalchemy import func, or_
from sqlalchemy.exc import OperationalError

from business_app import db
from business_app.models.customer_link import (
    AddressGroup,
    CanonicalCustomer,
    CustomerDistinctPair,
    CustomerLinkEvent,
    PlaceSuggestionDismissal,
)
from business_app.models.loyalty import ReferralProgram
from business_app.models.order import Order
from business_app.models.user import User, UserAddress
from business_app.utils.exceptions import ValidationError
from business_app.utils.helpers import calculate_distance
from shared.enums import BottleLedgerEventType, OrderStatus, UserRole, UserStatus, UserType

logger = logging.getLogger(__name__)

# Audit convention (see the place-group section below): CustomerLinkEvent has
# no group column, so every place-group event's ``reason`` is prefixed with
# "[group <id>] ". These are the event types that carry that prefix.
PLACE_GROUP_EVENT_TYPES = (
    "create_place_group",
    "add_to_place_group",
    "remove_from_place_group",
)


class CustomerLinkService:
    """Resolve a user/address to its canonical cluster / address group."""

    def user_exists(self, user_id: int) -> bool:
        """Cheap existence probe for admin read routes.

        Every resolver below deliberately degrades to a singleton for an
        unlinked user, which makes a nonexistent id indistinguishable from a
        real unlinked one. Routes call this first so a bad id 404s instead of
        reporting a phantom cluster.
        """
        if user_id is None:
            return False
        return db.session.query(User.id).filter(User.id == user_id).first() is not None

    def resolve_canonical(self, user_id: int) -> Optional[int]:
        """Return the user's canonical_customer_id, or None if unlinked/missing."""
        return db.session.query(User.canonical_customer_id).filter(User.id == user_id).scalar()

    def get_cluster_user_ids(self, user_id: int) -> List[int]:
        """All user ids sharing this user's canonical customer.

        Returns [user_id] when the user is unlinked or does not exist — an
        unlinked account is its own singleton cluster, so existing per-user
        behaviour is preserved.
        """
        canonical_id = self.resolve_canonical(user_id)
        if canonical_id is None:
            return [user_id]
        rows = db.session.query(User.id).filter(User.canonical_customer_id == canonical_id).all()
        ids = sorted(r[0] for r in rows)
        return ids or [user_id]

    def get_linked_accounts(self, user_id: int) -> dict:
        """The user's cluster as an admin-facing summary: members + primary.

        One call so the API layer needs no model access: the canonical id
        (None when unlinked), the cluster's primary user id, and one row per
        member. Plain FK selects (never join(User) — multi-FK gotcha). An
        unlinked account is its own singleton cluster, so an existing user
        always reports at least itself in ``members``.
        """
        member_ids = self.get_cluster_user_ids(user_id)
        canonical_id = self.resolve_canonical(user_id)
        members = [
            {"id": m.id, "first_name": m.first_name, "last_name": m.last_name, "phone": m.phone}
            for m in User.query.filter(User.id.in_(member_ids)).all()
        ]
        primary_user_id = None
        if canonical_id is not None:
            canonical = CanonicalCustomer.query.get(canonical_id)
            primary_user_id = canonical.primary_user_id if canonical else None
        return {
            "canonical_customer_id": canonical_id,
            "primary_user_id": primary_user_id,
            "members": members,
        }

    def get_address_group_member_ids(self, address_id: int) -> List[int]:
        """All address ids sharing this address's group.

        Returns [address_id] when the address is ungrouped or missing.
        """
        group_id = db.session.query(UserAddress.address_group_id).filter(UserAddress.id == address_id).scalar()
        if group_id is None:
            return [address_id]
        rows = db.session.query(UserAddress.id).filter(UserAddress.address_group_id == group_id).all()
        ids = sorted(r[0] for r in rows)
        return ids or [address_id]

    def get_address_place_group_id(self, address_id: int) -> Optional[int]:
        """The place group this address currently belongs to.

        None for an ungrouped address AND for a missing one — callers that
        need to prove membership in a SPECIFIC group compare the returned id,
        so both cases correctly fail that comparison.
        """
        if address_id is None:
            return None
        return db.session.query(UserAddress.address_group_id).filter(UserAddress.id == address_id).scalar()

    def can_view_address_history(self, requester_user_id: int, address_id: int) -> bool:
        """Three-arm customer authorization for per-address bottle history
        (spec §7): own address OR own a member address of the same place group
        OR the address owner is in the requester's canonical cluster. Replaces
        the old silent-empty-200 behavior at the API layer (deny -> 404)."""
        address = UserAddress.query.get(address_id)
        if address is None:
            return False
        if address.user_id == requester_user_id:
            return True
        if address.address_group_id is not None:
            owns_member = (
                UserAddress.query.filter(
                    UserAddress.address_group_id == address.address_group_id,
                    UserAddress.user_id == requester_user_id,
                ).count()
                > 0
            )
            if owns_member:
                return True
        return address.user_id in self.get_cluster_user_ids(requester_user_id)

    @staticmethod
    def _is_active(user) -> bool:
        status = user.status.value if hasattr(user.status, "value") else user.status
        return status == UserStatus.ACTIVE.value

    def _refresh_primary(self, canonical_id: int) -> Optional[int]:
        """Point the canonical's primary at the oldest ACTIVE member (fallback: oldest member)."""
        members = (
            User.query.filter(User.canonical_customer_id == canonical_id)
            .order_by(User.created_at.asc(), User.id.asc())
            .all()
        )
        canonical = CanonicalCustomer.query.get(canonical_id)
        if canonical is None:
            return None
        active = [m for m in members if self._is_active(m)]
        canonical.primary_user_id = active[0].id if active else (members[0].id if members else None)
        return canonical.primary_user_id

    def _choose_survivor(self, canonical_a: int, canonical_b: int) -> tuple:
        """Return (survivor, victim): larger cluster wins; tie-break the smaller (older) id."""
        count_a = db.session.query(func.count(User.id)).filter(User.canonical_customer_id == canonical_a).scalar() or 0
        count_b = db.session.query(func.count(User.id)).filter(User.canonical_customer_id == canonical_b).scalar() or 0
        if count_a > count_b:
            return canonical_a, canonical_b
        if count_b > count_a:
            return canonical_b, canonical_a
        return (min(canonical_a, canonical_b), max(canonical_a, canonical_b))

    def _handle_intracluster_referrals(self, member_ids: list) -> dict:
        """Void pending referrals internal to the cluster; report already-awarded ones.

        A person referring their own second phone is self-referral. Loyalty stays
        separate per account, but referral bonuses must not cross a person's own
        phones. No clawback of already-awarded points (append-only ledger) — those
        are surfaced for admin discretion.
        """
        ids = set(member_ids)
        internal = ReferralProgram.query.filter(
            ReferralProgram.referrer_id.in_(ids), ReferralProgram.referee_id.in_(ids)
        ).all()
        voided, already_awarded = [], []
        for ref in internal:
            if ref.status == "pending":
                ref.status = "void"
                voided.append(ref.id)
            else:
                already_awarded.append(ref.id)
        return {"voided_referral_ids": voided, "already_awarded_referral_ids": already_awarded}

    def link_accounts(self, primary_user_id: int, secondary_user_id: int, actor_admin_id: int, reason: str) -> dict:
        """Admin-driven link of two accounts into one canonical customer cluster.

        Coalesces existing clusters (never chains a new canonical on top of one
        that already exists), is hard-blocked by any asserted CustomerDistinctPair
        that would end up in the resulting cluster, is individuals-only, and is
        idempotent when both accounts already share a canonical.
        """
        primary = User.query.get(primary_user_id)
        secondary = User.query.get(secondary_user_id)
        if primary is None or secondary is None:
            raise ValidationError("User not found", error_code="CUSTOMER_LINK_USER_NOT_FOUND")
        if primary.id == secondary.id:
            raise ValidationError("Cannot link an account to itself", error_code="CUSTOMER_LINK_SELF")

        # Grocery accounts must never join a wallet cluster (spec §3/§5.8 layer 1).
        # Defensive: grocery is currently a subset of ENTITY (which the next loop
        # rejects), but the specific code must survive any subtype remodeling.
        for u in (primary, secondary):
            if u.is_grocery_store:
                raise ValidationError(
                    "Grocery-store accounts cannot be linked",
                    error_code="CUSTOMER_LINK_GROCERY_ACCOUNT",
                )

        for u in (primary, secondary):
            u_type = u.user_type.value if hasattr(u.user_type, "value") else u.user_type
            u_role = u.role.value if hasattr(u.role, "value") else u.role
            if u_type != UserType.INDIVIDUAL.value or u_role != UserRole.CUSTOMER.value:
                raise ValidationError(
                    "Only individual customer accounts can be linked",
                    error_code="CUSTOMER_LINK_NOT_INDIVIDUAL",
                )

        # Resulting cluster = union of both sides' clusters.
        resulting_ids = set(self.get_cluster_user_ids(primary.id)) | set(self.get_cluster_user_ids(secondary.id))

        # HARD distinct-pair block (incl. transitive over-merge): if any asserted-distinct
        # pair would end up together, refuse.
        conflict = CustomerDistinctPair.query.filter(
            CustomerDistinctPair.user_id_low.in_(resulting_ids),
            CustomerDistinctPair.user_id_high.in_(resulting_ids),
        ).first()
        if conflict is not None:
            raise ValidationError(
                "These accounts are marked as different people",
                error_code="CUSTOMER_LINK_DISTINCT_CONFLICT",
            )

        ca, cb = primary.canonical_customer_id, secondary.canonical_customer_id
        already_linked = False
        if ca and cb and ca == cb:
            canonical_id = ca
            already_linked = True
        elif ca and cb and ca != cb:
            survivor, victim = self._choose_survivor(ca, cb)
            User.query.filter(User.canonical_customer_id == victim).update(
                {User.canonical_customer_id: survivor}, synchronize_session=False
            )
            # Place groups are canonical-agnostic (ownerless): never repointed.
            canonical_id = survivor
        elif ca and not cb:
            secondary.canonical_customer_id = ca
            canonical_id = ca
        elif cb and not ca:
            primary.canonical_customer_id = cb
            canonical_id = cb
        else:
            canonical = CanonicalCustomer(created_by_admin_id=actor_admin_id)
            db.session.add(canonical)
            db.session.flush()
            primary.canonical_customer_id = canonical.id
            secondary.canonical_customer_id = canonical.id
            canonical_id = canonical.id

        db.session.flush()
        primary_user = self._refresh_primary(canonical_id)
        member_ids = sorted(
            r[0] for r in db.session.query(User.id).filter(User.canonical_customer_id == canonical_id).all()
        )

        referral_outcome = self._handle_intracluster_referrals(member_ids)
        if not already_linked and (
            referral_outcome["voided_referral_ids"] or referral_outcome["already_awarded_referral_ids"]
        ):
            reason = (
                f"{reason or ''} | referrals voided={referral_outcome['voided_referral_ids']} "
                f"already_awarded={referral_outcome['already_awarded_referral_ids']}"
            ).strip()

        if not already_linked:
            db.session.add(
                CustomerLinkEvent(
                    event_type="link",
                    canonical_customer_id=canonical_id,
                    acting_admin_id=actor_admin_id,
                    member_user_ids=member_ids,
                    reason=reason or "",
                )
            )
        db.session.commit()

        return {
            "canonical_customer_id": canonical_id,
            "member_user_ids": member_ids,
            "primary_user_id": primary_user,
            "already_linked": already_linked,
        }

    # Orders that still resolve against the cluster at unlink time — surfaced for
    # admin acknowledgment (not a hard block). This is the terminal-complement of
    # OrderStatus: everything except DELIVERED / CANCELLED / RETURNED.
    _NON_TERMINAL_ORDER_STATUSES = (
        OrderStatus.PENDING,
        OrderStatus.CONFIRMED,
        OrderStatus.PREPARING,
        OrderStatus.OUT_FOR_DELIVERY,
    )

    def unlink_account(self, user_id: int, actor_admin_id: int, reason: str) -> dict:
        """Detach a user from its canonical cluster.

        Phase 2: identity and geography are independent — unlink NEVER changes
        AddressGroup membership and NEVER moves bottle balances. Bottles stay
        with the PLACE; the only way to move any out is the deliberate,
        admin-chosen `bottles_leaving` split on `remove_address_from_group`
        (spec §7.1). Keeps: primary promotion, non-terminal
        cluster-order snapshot for admin acknowledgment, canonical tombstone
        retention (row kept even at 0 members), audit event. Releases
        now-out-of-scope money reservations via the
        _release_out_of_scope_reservations hook (spec §5.7). Does not unwind any
        money history — applied allocations are immutable. No-ops (empty shape,
        no error) if the user is not currently linked.
        """
        user = User.query.get(user_id)
        if user is None:
            raise ValidationError("User not found", error_code="CUSTOMER_LINK_USER_NOT_FOUND")
        canonical_id = user.canonical_customer_id
        if canonical_id is None:
            return {
                "canonical_customer_id": None,
                "remaining_member_ids": [],
                "new_primary_user_id": None,
                "non_terminal_orders": [],
            }

        # Snapshot in-flight orders for the whole cluster BEFORE detaching, so the
        # admin can be told what is mid-flight.
        cluster_ids = self.get_cluster_user_ids(user_id)
        non_terminal = Order.query.filter(
            Order.user_id.in_(cluster_ids),
            Order.status.in_(self._NON_TERMINAL_ORDER_STATUSES),
        ).all()
        non_terminal_orders = [
            {
                "order_id": o.id,
                "order_number": o.order_number,
                "user_id": o.user_id,
                "status": o.status.value if hasattr(o.status, "value") else o.status,
            }
            for o in non_terminal
        ]

        # Detach the user. Place groups are NOT touched.
        user.canonical_customer_id = None
        db.session.flush()

        remaining = sorted(
            r[0] for r in db.session.query(User.id).filter(User.canonical_customer_id == canonical_id).all()
        )
        new_primary = self._refresh_primary(canonical_id)  # canonical retained even at 0 members

        self._release_out_of_scope_reservations([user_id], remaining)

        db.session.add(
            CustomerLinkEvent(
                event_type="unlink",
                canonical_customer_id=canonical_id,
                acting_admin_id=actor_admin_id,
                member_user_ids=[user_id],
                reason=reason or "",
            )
        )
        db.session.commit()

        return {
            "canonical_customer_id": canonical_id,
            "remaining_member_ids": remaining,
            "new_primary_user_id": new_primary,
            "non_terminal_orders": non_terminal_orders,
        }

    def _release_out_of_scope_reservations(self, leaving_user_ids: List[int], remaining_user_ids: List[int]) -> int:
        """Release prepaid-credit reservations made out-of-scope by an unlink.

        Delegates to the money engine (spec §5.7): a reservation funded by one
        side of the split and parked on a pending order of the other side no
        longer resolves, so the credit is returned to its owner's unapplied
        balance and the affected payments are re-projected. Applied allocations
        are immutable history and are never touched. Returns the number of
        reservations released.
        """
        from business_app.services.cash_collection_service import CashCollectionService

        return CashCollectionService().release_out_of_scope_reservations(leaving_user_ids, remaining_user_ids)

    def dismiss_suggestion(
        self, user_id_a: int, user_id_b: int, actor_admin_id: int, signal_fingerprint: str = None
    ) -> dict:
        """Sticky "these are different people" assertion. Idempotent upsert on the
        normalized (low, high) pair — a repeat dismiss updates the existing row
        rather than erroring or duplicating.
        """
        low, high = sorted([user_id_a, user_id_b])
        existing = CustomerDistinctPair.query.filter_by(user_id_low=low, user_id_high=high).first()
        if existing is None:
            db.session.add(
                CustomerDistinctPair(
                    user_id_low=low,
                    user_id_high=high,
                    dismissed_by_admin_id=actor_admin_id,
                    signal_fingerprint=signal_fingerprint,
                )
            )
        else:
            existing.dismissed_by_admin_id = actor_admin_id
            existing.signal_fingerprint = signal_fingerprint
        db.session.add(
            CustomerLinkEvent(
                event_type="dismiss",
                canonical_customer_id=None,
                acting_admin_id=actor_admin_id,
                member_user_ids=[low, high],
                reason="not the same person",
            )
        )
        db.session.commit()
        return {"user_id_low": low, "user_id_high": high}

    # ------------------------------------------------------------------ #
    # Place groups (Phase 2): ownerless "same physical place" groups that
    # may span customers. Identity and geography are independent — linking/
    # unlinking never changes groups; grouping/ungrouping never changes
    # clusters. CustomerDistinctPair gates LINK only and is never consulted
    # here. Audit convention: every place-group event's reason starts with
    # "[group <id>] " (CustomerLinkEvent has no group column).
    # ------------------------------------------------------------------ #

    def get_place_group_user_ids(self, group_id: int) -> List[int]:
        """Sorted distinct owner user ids of a place group's member addresses.

        Plain FK SELECT (never join(User) — multi-FK gotcha). Returns [] for a
        missing/empty group.
        """
        if group_id is None:
            return []
        rows = db.session.query(UserAddress.user_id).filter(UserAddress.address_group_id == group_id).distinct().all()
        return sorted(r[0] for r in rows)

    def get_place_group_address_ids(self, group_id: int) -> List[int]:
        """Sorted member address ids of a place group ([] when missing/empty)."""
        if group_id is None:
            return []
        rows = db.session.query(UserAddress.id).filter(UserAddress.address_group_id == group_id).all()
        return sorted(r[0] for r in rows)

    def get_place_group_events(self, group_id: int, limit: int = 200) -> list:
        """This place group's audit trail, newest first.

        ``CustomerLinkEvent`` has NO group column: create/add/remove all write
        their ``reason`` as ``"[group <id>] ..."``, so that prefix IS the scope
        key. Filtering by member user ids instead would be wrong twice over —
        a ``remove_from_place_group`` event carries only the REMOVED owner,
        who by then is no longer a member (the single most audit-relevant
        record would vanish), and a member who also belongs to another group
        would drag that group's events in. The limit is applied AFTER the
        filter, never before, so a busy system cannot render one group's trail
        empty. ``created_at`` ties break on id so paging is deterministic.
        """
        if group_id is None:
            return []
        events = (
            CustomerLinkEvent.query.filter(
                CustomerLinkEvent.event_type.in_(PLACE_GROUP_EVENT_TYPES),
                CustomerLinkEvent.reason.like(f"[group {int(group_id)}]%"),
            )
            .order_by(CustomerLinkEvent.created_at.desc(), CustomerLinkEvent.id.desc())
            .limit(max(1, int(limit or 200)))
            .all()
        )
        return [
            {
                "id": event.id,
                "event_type": event.event_type,
                "acting_admin_id": event.acting_admin_id,
                "member_user_ids": event.member_user_ids,
                "reason": event.reason,
                "created_at": event.created_at.isoformat() if event.created_at else None,
            }
            for event in events
        ]

    def get_place_group_detail(self, group_id: int) -> Optional[dict]:
        """Admin-facing place-group detail: members, owners, bottles, audit.

        The only bottle BALANCE is ``place_balance`` — the whole place's single
        pool. Members carry no balance of their own (spec decision 4): the pool
        is not divisible per coworker, and the ledger remains the attributed
        view. Each member does carry ``suggested_bottles_leaving``, which is a
        pre-fill for the remove dialog and not a slice of the pool — nothing
        holds it, it is only what the admin is offered as a starting number.

        Returns None when the group does not exist so the API layer can 404
        without touching a model. Bottle quantities stay ``Decimal`` here —
        they are floated only at the serializer/API boundary. Owner rows are
        resolved by an id-filtered SELECT, never a join from a multi-FK table.
        """
        from business_app.services.bottle_tracking_service import BottleTrackingService

        group = AddressGroup.query.get(group_id)
        if group is None:
            return None

        addresses = (
            UserAddress.query.filter(UserAddress.address_group_id == group_id).order_by(UserAddress.id.asc()).all()
        )
        owner_ids = sorted({a.user_id for a in addresses})
        owners = {u.id: u for u in User.query.filter(User.id.in_(owner_ids)).all()} if owner_ids else {}

        # One row per place: any member address resolves to the same scope, so
        # the first one is enough (the resolver expands it back to the group).
        # Read ONCE, before the loop: every member clamps its suggestion against
        # this same number, and re-reading it per member (each re-resolving the
        # scope) is pure N+1.
        place_balance = BottleTrackingService.get_place_balance(addresses[0].id) if addresses else Decimal("0.00")

        members = []
        for addr in addresses:
            owner = owners.get(addr.user_id)
            # No per-member balance: the place holds ONE pool of bottles and it
            # cannot be sliced per coworker (spec decision 4). `place_balance`
            # below is the only bottle number this panel can honestly show as a
            # holding; the per-member number attached here is a SUGGESTION for
            # the remove dialog, deliberately named so it cannot read as one.
            members.append(
                {
                    "address_id": addr.id,
                    "address_title": addr.title,
                    "full_address": addr.full_address,
                    # NOT a per-member balance (there is none) — the pre-fill the
                    # remove dialog offers for `bottles_leaving`, derived from
                    # this address's own attributed entries and clamped to what
                    # the place actually holds (spec §7.1).
                    "suggested_bottles_leaving": BottleTrackingService.suggested_bottles_leaving(
                        group_id, addr.id, place_balance=place_balance
                    ),
                    "owner": {
                        "id": addr.user_id,
                        "first_name": owner.first_name if owner else None,
                        "last_name": owner.last_name if owner else None,
                        "phone": owner.phone if owner else None,
                    },
                }
            )

        return {
            "place_group_id": group.id,
            "label": group.label,
            "place_balance": place_balance,
            "members": members,
            "events": self.get_place_group_events(group_id),
        }

    def list_place_groups(self, page: int = 1, per_page: int = 20, search: Optional[str] = None) -> Dict[str, Any]:
        """Every place group, paginated, with the exposure it carries.

        The estate-wide counterpart to ``get_place_group_detail``. Until now the
        only way to reach a group was through one customer's detail modal
        (``PlaceGroupPanel`` lives inside the Users detail drawer), so an admin
        could not see what had already been grouped without knowing whom to look
        up first. This is the reader behind the "Grouped Addresses" tab.

        🔴 READ-ONLY. Nothing here creates, edits or dissolves a group. Grouping
        stays a deliberate admin act through ``create_place_group`` /
        ``add_addresses_to_group``, both of which demand a ``reason`` (spec
        §2.1: auto-grouping fails dangerously in seven distinct ways).

        ``member_count`` is the number of distinct address OWNERS — the same
        definition ``get_place_cod_statement`` and ``get_place_cod_debtor_rows``
        use — NOT the number of addresses, or a 3-person office where one person
        contributed two addresses renders "(4 members)". ``address_count`` is
        the separate figure, reported alongside it rather than conflated.

        COD exposure comes from the PUBLIC reader
        ``CashCollectionService.get_place_cod_debtor_rows``: ONE estate-wide
        grouped query yielding both figures, instead of an N+1 of
        ``get_place_open_cod_debt_total`` per rendered row. Both readers select
        on exactly the same predicate (CASH payment, outstanding > 0, DELIVERED
        order, ``delivery_address_id`` in the group), so a place absent from it
        genuinely has no open delivered COD debt and renders 0.0 / 0.

        ``bottle_exposure`` is the OTHER half of what a place carries: what it
        HOLDS, beside what it OWES. Grouping two addresses pools their bottles
        into one indivisible place balance exactly as it pools their COD debt,
        so an admin who can only see the money is being shown half the
        consequence of the act — which is why the plan calls both figures the
        mitigation rather than decoration. It comes from
        ``BottleTrackingService.get_place_balances_by_group``: again ONE grouped
        query for the whole page, not a ``get_place_balance`` per rendered row.
        A place with no balance row has never moved a bottle and renders 0.0.

        Money crosses this boundary as a FLOAT (the reader already casts it):
        Flask renders a bare ``Decimal`` as the STRING "35000.00", which turns
        the admin UI's arithmetic into ``NaN``. The bottle figure is a quantity,
        not money, but it is a ``Decimal`` in the ledger and so gets the same
        treatment — and it must be cast HERE, because the route hands this
        payload to ``success_response`` untouched (unlike the detail route,
        which floats bottle quantities at the API boundary itself).
        """
        from business_app.services.bottle_tracking_service import BottleTrackingService
        from business_app.services.cash_collection_service import CashCollectionService

        # Project convention (precedent: paginate_users_with_open_cod_debts).
        safe_page = max(1, int(page or 1))
        safe_per_page = max(1, min(int(per_page or 20), 100))

        query = AddressGroup.query
        text_query = (search or "").strip()
        if text_query:
            query = query.filter(AddressGroup.label.ilike(f"%{text_query}%"))

        total = int(query.count())
        pages = (total + safe_per_page - 1) // safe_per_page
        pagination = {
            "page": safe_page,
            "per_page": safe_per_page,
            "total": total,
            "pages": pages,
        }

        # Newest first. ``id`` rather than ``created_at``: it is monotonic and a
        # strict total order, so paging stays stable where bulk-created groups
        # share a timestamp.
        groups = (
            query.order_by(AddressGroup.id.desc()).limit(safe_per_page).offset((safe_page - 1) * safe_per_page).all()
        )
        if not groups:
            return {"items": [], "pagination": pagination}

        group_ids = [g.id for g in groups]
        # One grouped query for BOTH counts across the whole page.
        counts = {
            row[0]: (int(row[1] or 0), int(row[2] or 0))
            for row in db.session.query(
                UserAddress.address_group_id,
                func.count(func.distinct(UserAddress.user_id)),
                func.count(UserAddress.id),
            )
            .filter(UserAddress.address_group_id.in_(group_ids))
            .group_by(UserAddress.address_group_id)
            .all()
        }
        # The reader clamps its own limit at 1000 and sorts by outstanding
        # DESC, so a hypothetical estate with >1000 simultaneously-indebted
        # places would render its smallest debts as 0. That ceiling belongs to
        # the reader, which this task may not modify (engine firewall).
        exposure = {row["place_group_id"]: row for row in CashCollectionService().get_place_cod_debtor_rows(limit=1000)}
        # ...and one more for the bottles the page's places are holding. Scoped
        # to THIS page's ids, so unlike the COD reader above it carries no
        # estate-wide ceiling.
        bottles = BottleTrackingService.get_place_balances_by_group(group_ids)

        items = []
        for group in groups:
            member_count, address_count = counts.get(group.id, (0, 0))
            cod = exposure.get(group.id)
            items.append(
                {
                    "id": group.id,
                    "label": group.label,
                    "member_count": member_count,
                    "address_count": address_count,
                    # Already a float on the reader's side — do NOT re-wrap.
                    "place_open_cod_debt_total": cod["total_outstanding_amount"] if cod else 0.0,
                    "active_cod_debt_count": cod["active_cod_debt_count"] if cod else 0,
                    # What the place HOLDS, beside what it OWES. A ``Decimal``
                    # in the ledger, a JSON number here, and the miss is 0.0
                    # rather than a minted balance row.
                    "bottle_exposure": float(bottles.get(group.id, Decimal("0.00"))),
                    "created_at": group.created_at.isoformat() if group.created_at else None,
                }
            )
        return {"items": items, "pagination": pagination}

    def search_addresses(self, query_text: str, limit: int = 20, exclude_grouped: bool = True) -> list:
        """Cross-user address search powering the manual place-group picker.

        Matches phone / first name / last name / address title / address text.
        ``addresses`` has a SINGLE FK to ``users``, so the join pins its ON
        clause explicitly (never a bare ``join(User)`` — the multi-FK gotcha).
        Queries shorter than two characters return [] so an empty picker box
        never scans the table.
        """
        text_query = (query_text or "").strip()
        if len(text_query) < 2:
            return []
        row_limit = max(1, min(int(limit or 20), 50))
        pattern = f"%{text_query}%"
        query = (
            db.session.query(UserAddress, User)
            .join(User, User.id == UserAddress.user_id)
            .filter(
                or_(
                    User.phone.ilike(pattern),
                    User.first_name.ilike(pattern),
                    User.last_name.ilike(pattern),
                    UserAddress.full_address.ilike(pattern),
                    UserAddress.title.ilike(pattern),
                )
            )
        )
        if exclude_grouped:
            query = query.filter(UserAddress.address_group_id.is_(None))
        rows = query.order_by(UserAddress.id.desc()).limit(row_limit).all()
        return [
            {
                "address_id": addr.id,
                "title": addr.title,
                "full_address": addr.full_address,
                "address_group_id": addr.address_group_id,
                "owner": {
                    "id": owner.id,
                    "first_name": owner.first_name,
                    "last_name": owner.last_name,
                    "phone": owner.phone,
                },
            }
            for addr, owner in rows
        ]

    @staticmethod
    def _lock_place_group(group_id: int):
        """RUNG 0 — the MEMBERSHIP MUTEX on `address_groups`.

        `FOR NO KEY UPDATE` (`key_share=True`), taken by any transaction that
        changes a place's membership or depends on its member SET being stable.
        It blocks a writer's `FOR SHARE` on member addresses, blocks another
        lifecycle acquirer, and blocks the membership `UPDATE` — while leaving
        order/subscription creation at member addresses alone, which plain
        `FOR UPDATE` would not.

        `populate_existing()` is mandatory for the same reason it is on
        `_load_addresses`: without it a group already in the identity map is
        re-read, locked, and its columns thrown away.

        Returns the locked `AddressGroup`, or None when the id does not exist.
        """
        from business_app.services.bottle_tracking_service import BottleTrackingService

        group = (
            AddressGroup.query.filter(AddressGroup.id == group_id)
            .populate_existing()
            .with_for_update(key_share=True)
            .first()
        )
        if group is not None:
            BottleTrackingService.register_scope_lock(group_ids=[group.id])
        return group

    @staticmethod
    def _raise_place_busy(group_id: int, exc: BaseException) -> None:
        """Turn a 55P03 on the lifecycle ladder into a NAMED, retryable refusal.

        The twin of `BottleTrackingService._raise_scope_busy`, which does this
        for the DRIVER side of the same contention, and it carries the same
        `BOTTLE_SCOPE_LOCK_TIMEOUT` code because it IS the same event seen from
        the other end: someone else holds this place, come back in a moment.

        `ValidationError`, not `ConflictError`, and that is a routing fact
        rather than a taxonomy claim: `api/admin.py`'s place-group routes map
        `ValidationError` to a 400 CARRYING `error_code` and everything else to
        `internal_error_response`, so a `ConflictError` here would reach the
        admin panel as the very 500 this exists to remove.

        The rollback is MANDATORY: Postgres has already aborted the transaction,
        so every later statement on this session fails with 25P02 and would
        replace the real cause with an unrelated one.
        """
        logger.warning("[PLACE] lifecycle lock timed out for group=%s — %s", group_id, exc.__class__.__name__)
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001 — the session is already doomed
            logger.exception("[PLACE] rollback after lifecycle lock timeout failed")
        busy = ValidationError(
            "This place is being edited by another administrator right now "
            "(lock timeout); nothing was saved. Please try again in a moment.",
            error_code="BOTTLE_SCOPE_LOCK_TIMEOUT",
            details={"place_group_id": group_id},
        )
        # Carry the DBAPI cause so the SQLSTATE stays introspectable — a caller
        # asking "was this really 55P03?" must not have to parse a sentence.
        busy.orig = getattr(exc, "orig", None)
        raise busy from exc

    def _assert_place_group_eligible(self, addresses: list) -> None:
        """Shared create/add fences.

        No grocery-flagged owner (protects the corporate-contract mirror,
        spec §5.8 layer 1), no entity accounts, and an address may belong to
        at most one place group (move = explicit remove + add).
        """
        owner_ids = {a.user_id for a in addresses}
        for owner in User.query.filter(User.id.in_(owner_ids)).all():
            if owner.is_grocery_store:
                raise ValidationError(
                    "Grocery-store accounts cannot join place groups",
                    error_code="PLACE_GROUP_GROCERY_MEMBER",
                )
            o_type = owner.user_type.value if hasattr(owner.user_type, "value") else owner.user_type
            o_role = owner.role.value if hasattr(owner.role, "value") else owner.role
            if o_type != UserType.INDIVIDUAL.value or o_role != UserRole.CUSTOMER.value:
                raise ValidationError(
                    "Only individual customer addresses can join place groups",
                    error_code="PLACE_GROUP_ENTITY_MEMBER",
                )
        for addr in addresses:
            if addr.address_group_id is not None:
                raise ValidationError(
                    f"Address {addr.id} is already in a place group",
                    error_code="PLACE_GROUP_ADDRESS_ALREADY_GROUPED",
                )

    def _load_addresses(self, address_ids: list) -> list:
        """RUNG 1, lifecycle mode: every target `addresses` row, LOCKED.

        Three properties, each load-bearing and none decoration:

        * `FOR NO KEY UPDATE` (`key_share=True`), not `FOR UPDATE`. It blocks a
          writer's `FOR SHARE`, blocks another lifecycle acquirer and blocks the
          membership `UPDATE` — but does NOT block INSERTs into `addresses`'
          six FK children (`orders`, `subscriptions`, `bottle_balances`,
          `bottle_ledger`, `bottle_fines`, `place_suggestion_dismissals`), which
          plain `FOR UPDATE` does. Verified on this project's Postgres 17: plain
          `FOR UPDATE` here would stall order and subscription creation at every
          member address for the whole admin transaction.
        * `ORDER BY id` in ONE statement. `LockRows` sits above `Sort` in the
          plan, so ordering the query orders the LOCK ACQUISITION. Without it,
          two joins over {A,B} and {B,A} are a textbook ABBA.
        * `populate_existing()`, and this is the difference between a fix and a
          NO-OP. `with_for_update()` does NOT imply it: SQLAlchemy re-reads the
          row in the database, acquires the lock correctly, and then DISCARDS
          the columns when the object is already in the identity map (and
          `Session.get()` emits no SQL at all). `_assert_place_group_eligible`
          would then evaluate `addr.address_group_id is not None` on the
          PRE-IMAGE — every lock taken, the very defect untouched. Verified.
        """
        from business_app.services.bottle_tracking_service import BottleTrackingService

        unique_ids = list(dict.fromkeys(address_ids))
        addresses = (
            UserAddress.query.filter(UserAddress.id.in_(unique_ids))
            .order_by(UserAddress.id.asc())
            .populate_existing()
            .with_for_update(key_share=True)
            .all()
        )
        if len(addresses) != len(unique_ids):
            raise ValidationError("Address not found", error_code="CUSTOMER_LINK_ADDRESS_NOT_FOUND")
        BottleTrackingService.register_scope_lock(address_ids=[a.id for a in addresses])
        return addresses

    @staticmethod
    def _absorb_joiners_into_group(group, addresses: list) -> list:
        """Attach `addresses` to `group` and RE-SCOPE their bottle history (§7.2).

        The ordering below is load-bearing; both join entry points share this one
        copy because five ordered steps written twice will drift.

        Joining is the exact inverse of Task 2's leave: bottles are MOVED, never
        minted or destroyed. Whatever each joiner's own place held becomes part
        of what the group holds, and the sum over the distinct places is
        unchanged by the join.

        LOCK ORDERING (spec §5.2, revised). DEADLOCK-FREEDOM HERE RESTS ON
        ORDERING ALONE, NEVER ON A FENCE. Every transaction acquires a prefix of
        one total order:

            rung 0  `address_groups` row      FOR NO KEY UPDATE  (lifecycle)
            rung 1  `addresses` rows          FOR SHARE for a writer that only
                                              READS membership, FOR NO KEY UPDATE
                                              for the lifecycle; ascending id,
                                              ONE statement
            rung 2  `bottle_balances` GROUP row    FOR UPDATE
            rung 3  `bottle_balances` ADDRESS row  FOR UPDATE

        The whole ladder sits BELOW the payment/settlement locks
        (`staff_service.py`'s `lock_order_settlement_candidates`,
        `order_edit_service.py`'s cash-before-bottles rule).

        This method is entered holding rungs 0 and 1: its callers take the
        destination `address_groups` row (`add_addresses_to_group` via
        `_lock_place_group`; `create_place_group` by EXISTENCE — it INSERTs the
        row, and a row that does not exist cannot be locked by anyone) and then
        every joining `addresses` row via `_load_addresses`. Step 1's membership
        write below is therefore a LOCK UPGRADE on rows already held, not a
        fresh acquisition.

        The three two-balance-row acquirers — this method,
        `_split_bottles_out_of_place` and
        `BottleTrackingService.release_group_history_to_address` — are all
        lifecycle operations on ONE place, and all three hold that place's
        `address_groups` row first. They are therefore MUTUALLY EXCLUSIVE, and
        the late-create branch below (`absorbed != 0` with no pre-existing group
        row) can no longer race anything. Every other bottle path — delivery,
        return, standalone collection, admin adjustment, fine, reconcile,
        order-edit cascade — takes exactly ONE balance row and so cannot be half
        of an ABBA cycle.

        THE OLD ARGUMENT IN THIS DOCSTRING RESTED ON "of two concurrent
        transactions on one address, exactly one passes its fence." That is
        FALSE for two JOINS, and it was false for a structural reason: under
        READ COMMITTED both joins read the same pre-image, so a READ-BASED test
        can never serialise anything. It is REPLACED, not repaired. The
        membership check in `_assert_place_group_eligible` survives as a
        CORRECTNESS check only — and it is now TRUE as a CONSEQUENCE of the
        `addresses` row lock rather than as its justification: the loser of a
        join race BLOCKS on rung 1, and Postgres's EvalPlanQual re-check hands
        it the winner's COMMITTED `address_group_id` when it wakes (verified on
        this project's Postgres). That requires `populate_existing()` on the
        locking load — without it SQLAlchemy re-reads the row and DISCARDS the
        columns, and the check evaluates the pre-image again.

        WHAT WOULD FALSIFY THIS: a lock acquired out of rung order; a write to
        `bottle_balances`/`bottle_ledger` for an address whose row lock is not
        held; or a write to `addresses.address_group_id` by any path — script,
        data fix, admin tool, new route — that does not first hold the target
        `address_groups` row. The last is an invariant on the TABLE, not on this
        method.

        The group row is locked but NOT created if absent, because a place that
        has never moved a bottle must not gain a 0.00 row just for being grouped
        — callers hand-insert that row (and `uq_bottle_balance_group` would
        reject a second one).

        Returns the sorted ids of every re-scoped ledger entry, for the audit
        event's `event_metadata`.
        """
        from business_app.models.bottle import BottleBalance
        from business_app.services.bottle_scope import BottleScope
        from business_app.services.bottle_tracking_service import BottleTrackingService

        if not addresses:
            # Unreachable from either caller today (both guard on an empty
            # address list), but this is the shared surface a third join path
            # would use, and `addresses[0]` below would be an uncaught 500.
            return []

        # 1. Membership pointer FIRST, and FLUSHED — every bottle read below
        #    resolves scope from `addresses.address_group_id`.
        for addr in addresses:
            addr.address_group_id = group.id
        db.session.flush()

        bottles = BottleTrackingService()
        place_scope = BottleScope.for_group(group.id)
        # Read before the expire_all in step 4, so re-reading it in step 5 costs
        # no extra SELECT. `get_or_create_balance` ignores the address entirely
        # when an explicit `scope` is passed; it only satisfies the signature.
        anchor_address_id = addresses[0].id

        # 2. Take the PLACE's row FOR UPDATE **before** any joiner's, and hold
        #    it to the end of the transaction — see LOCK ORDERING above.
        place_row = BottleBalance.query.filter(*place_scope.balance_filter()).with_for_update().first()

        # 3. Absorb each joiner's own-scope history into the group, each under
        #    its own address row's lock. Nothing is credited yet: the place's
        #    row is written exactly once, in step 5.
        rescoped = []
        absorbed = Decimal("0.00")
        last_delivery_at = last_return_at = None
        for addr in sorted(addresses, key=lambda a: a.id):
            moved = bottles.absorb_address_into_group(addr.id, group.id)
            rescoped.extend(moved["entry_ids"])
            absorbed += moved["absorbed_balance"]
            # The deleted rows' provenance travels with their figures — nothing
            # rebuilds these two columns, so this is the only chance to keep them.
            last_delivery_at = BottleTrackingService.latest_timestamp(last_delivery_at, moved["last_delivery_at"])
            last_return_at = BottleTrackingService.latest_timestamp(last_return_at, moved["last_return_at"])

        # 4. The bulk ledger UPDATE above ran with synchronize_session=False, so
        #    the identity map still holds the pre-move column values. Expire
        #    AFTER the flush in step 1 — expiring with pending changes would
        #    discard them. `place_row` is merely expired, not unlocked: the row
        #    lock lives until the transaction ends.
        db.session.expire_all()

        # 5. Credit what the joiners' own places held onto the PLACE's single
        #    row. This is a transfer, not a recomputation: rebuilding the figure
        #    from the merged ledger instead would destroy the balance of any
        #    place whose row is not ledger-derived (seeded before the ledger, or
        #    already drifted) — and repairing drift is `reconcile_balance`'s job,
        #    not a membership edit's.
        #    The joiners' `last_delivery_at` / `last_return_at` ride along, for
        #    the same reason: `absorb_address_into_group` DELETES the rows that
        #    held them and no later pass rebuilds them, so a place assembled from
        #    two customers served yesterday would read NEVER SERVED on the admin
        #    bottle table and in the customer drawer.
        #
        #    THEY NEVER MINT A ROW OF THEIR OWN. "A place that has never moved a
        #    bottle must not gain a 0.00 row just for being grouped" outranks
        #    them: an unnecessary row is a `stranded_address_balances` /
        #    `orphaned_place_balances` shape the sweep exists to chase, while a
        #    lost date is a display fact on a place currently holding nothing.
        #    So the ONE corner still dropped is a joiner whose figure nets to
        #    exactly zero into a place that has no row yet.
        carried_provenance = last_delivery_at is not None or last_return_at is not None
        if absorbed != 0 or (place_row is not None and carried_provenance):
            if place_row is None:
                place_row = bottles.get_or_create_balance(anchor_address_id, scope=place_scope)
            place_row.balance = (place_row.balance or Decimal("0.00")) + absorbed
            place_row.last_delivery_at = BottleTrackingService.latest_timestamp(
                place_row.last_delivery_at, last_delivery_at
            )
            place_row.last_return_at = BottleTrackingService.latest_timestamp(place_row.last_return_at, last_return_at)
            db.session.flush()

        # 6. Rebuild the running snapshots over the merged timeline, so the
        #    history view walks the same numbers the summary reports.
        BottleTrackingService.recompute_balance_after(place_scope)
        return sorted(rescoped)

    # ------------------------------------------------------------------ #
    # Merge review (spec §7.4): the admin inspects the merged ledger, drops
    # entries that should never have been there, and may state the number of
    # bottles actually on site. Split deliberately into a VALIDATE half and an
    # APPLY half, because the two cannot run at the same moment:
    #
    #   * the guards must see the PRE-absorb world — after the absorb every
    #     joiner's entries carry the group and the §7.2 selector finds nothing,
    #     so the preview being validated would no longer be the admin's — and
    #     they must run before ANY write, so a rejected merge leaves no flushed
    #     `AddressGroup` or membership pointer for the next commit to adopt;
    #   * the corrections must be written AFTER it, because they are scoped to
    #     the group and the running-snapshot pass has to see them last.
    #
    # Both halves are shared verbatim by the two join entry points, so the
    # guard order still lives in exactly one place.
    # ------------------------------------------------------------------ #

    @staticmethod
    def _has_merge_review(excluded_ledger_entry_ids, resulting_balance) -> bool:
        return bool(excluded_ledger_entry_ids) or resulting_balance is not None

    @staticmethod
    def _coerce_id_list(values, field: str) -> list:
        """Ledger-entry ids as ints, rejecting garbage as a 400 rather than a 500.

        PINS WHAT AN ID IS, because `int()` swallows far more than an id and
        each thing it swallows REVERSES A REAL LEDGER ENTRY:

        * a STRING is ITERABLE. `[int(v) for v in "12"]` is `[1, 2]` — two
          entries this merge really contains, seven real bottles written out of
          the place by one malformed field. So the container must be a list or
          a tuple, checked BEFORE anything is iterated; a bare string is a 400.
        * `int(1.9)` is 1 and `int(True)` is 1. A typo'd float or a stray
          boolean therefore names entry 1, which is a real entry in almost every
          merge. Both are refused rather than truncated — `bool` FIRST, since
          `isinstance(True, int)` is True.

        The deliberate tolerance that stays: a STRING MEMBER of the list
        (`["41"]` means entry 41) — HTTP clients that stringify ids are common,
        the intent is unambiguous, and it is pinned by
        `test_a_string_member_of_excludedLedgerEntryIds_is_accepted_as_an_id`.
        `"1.9"` is not an integer literal and is refused with everything else.
        """
        if values is None:
            return []
        if not isinstance(values, (list, tuple)):
            raise ValidationError(f"{field} must be a list of ledger entry ids")
        ids = []
        for value in values:
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                raise ValidationError(f"{field} must be a list of ledger entry ids")
            try:
                ids.append(int(value))
            except (TypeError, ValueError):
                raise ValidationError(f"{field} must be a list of ledger entry ids")
        return ids

    # The widest magnitude `bottle_balances.balance` / `bottle_ledger.quantity`
    # can hold, derived from the COLUMN rather than typed out: NUMERIC(12,2)
    # means ten integer digits, so 9999999999.99. Anything larger is a client
    # error that reaches Postgres as a `numeric field overflow` DataError, which
    # is not a ValidationError — both join routes then fall through to their
    # bare `except Exception` and report a 400-shaped mistake as a 500. SQLite
    # has no numeric bound at all, so it is invisible on the fast suite.
    @staticmethod
    def _resulting_balance_limit() -> Decimal:
        from business_app.models.bottle import BottleBalance

        column_type = BottleBalance.__table__.c.balance.type
        precision = int(getattr(column_type, "precision", None) or 12)
        scale = int(getattr(column_type, "scale", None) or 2)
        return Decimal(10) ** (precision - scale) - Decimal(1).scaleb(-scale)

    @staticmethod
    def _coerce_resulting_balance(value):
        """The admin's stated bottle count, as a FINITE Decimal.

        NaN and Infinity are reachable, not theoretical: Python's own JSON
        parser accepts both literals, `Decimal("NaN")` constructs happily, and
        every comparison against NaN is False — so an unguarded NaN sails past
        `delta != 0` straight into `bottle_ledger.quantity`. Exactly the trap
        `_validated_bottles_leaving` documents for `bottles_leaving`.

        The SIGN is deliberately NOT checked, and that decision stands: a place
        can legitimately be negative (over-returned — spec §1.2/§16 keeps the
        return quantity unbounded).

        The MAGNITUDE is bounded, and that is a different question from the
        sign. `resulting_balance` becomes a `bottle_ledger.quantity` and a
        `bottle_balances.balance`, both NUMERIC(12,2); a stated 10^14 passes
        every check here and dies at the COLUMN with a Postgres `numeric field
        overflow` DataError, which is not a ValidationError, so both join routes
        fall through to their bare `except Exception` and report a plain client
        mistake as a 500. The bound is read off the column itself
        (`_resulting_balance_limit`) rather than typed out, so widening the
        column widens this with it. Invisible on SQLite, which has no numeric
        bound — verified on `pg_app`/`pg_db`.

        §13 defines no code for this, so it 400s on the message alone; the
        four §7.4 codes stay reserved for the states the spec names.
        """
        if value is None:
            return None
        try:
            stated = Decimal(str(value))
        except (ArithmeticError, ValueError):
            # decimal.InvalidOperation IS an ArithmeticError — naming both
            # would be redundant.
            raise ValidationError("resulting_balance must be a number")
        # BEFORE the magnitude test, never after: Python's `decimal` is not
        # IEEE-754 about ordering — comparing a Decimal('NaN') with `>` RAISES
        # InvalidOperation rather than returning False.
        if not stated.is_finite():
            raise ValidationError("resulting_balance must be a finite number")
        limit = CustomerLinkService._resulting_balance_limit()
        if abs(stated) > limit:
            raise ValidationError(f"resulting_balance must be between -{limit} and {limit}")
        return stated

    def _validate_merge_review(
        self,
        *,
        address_ids: list,
        group_id: Optional[int],
        reason: str,
        excluded_ledger_entry_ids: Optional[list],
        resulting_balance,
        preview_entry_ids: Optional[list],
    ) -> Optional[dict]:
        """Spec §7.4's guards, in the fixed order, all BEFORE any write.

        Returns the live preview the corrections will be measured against, or
        None when the caller asked for no review at all (the plain join path,
        which must stay byte-for-byte as it was).
        """
        from business_app.models.bottle import BottleLedger
        from business_app.services.bottle_tracking_service import (
            BALANCE_DECOUPLED_LEDGER_KEY_PREFIXES,
        )

        excluded = self._coerce_id_list(excluded_ledger_entry_ids, "excluded_ledger_entry_ids")
        preview_entry_ids = (
            None if preview_entry_ids is None else self._coerce_id_list(preview_entry_ids, "preview_entry_ids")
        )
        resulting_balance = self._coerce_resulting_balance(resulting_balance)
        if not self._has_merge_review(excluded, resulting_balance) and preview_entry_ids is None:
            return None

        # 1. A correction is an admin OVERRIDING the ledger. It is only
        #    accountable if it says why, so the reason stops being the route's
        #    business and becomes the service's. Deliberately NOT applied to a
        #    plain join: `create_address_group` still passes an empty reason.
        if self._has_merge_review(excluded, resulting_balance) and not (reason or "").strip():
            raise ValidationError(
                "A reason is required to exclude ledger entries or override the resulting balance",
                error_code="MERGE_REASON_REQUIRED",
            )

        from business_app.services.bottle_tracking_service import BottleTrackingService

        # `strict_exclusions=False` so the preview does not raise on an
        # ineligible id ahead of the staleness check: §7.4's guard order is
        # fixed, and guard 3 below is this path's own copy of that fence. Every
        # OTHER caller (the admin preview route) gets the strict default, so the
        # decision aid and the committer reject the same input.
        preview = BottleTrackingService.build_merge_preview(
            address_ids, group_id=group_id, excluded_ledger_entry_ids=excluded, strict_exclusions=False
        )

        # 2. A merge too large to render is a merge the admin could not have
        #    reviewed, so a CORRECTION against it is unfounded. Enforced here as
        #    well as at the route: the route's cap only stops the preview being
        #    fetched, and nothing prevented a client from posting an override
        #    for a merge it never managed to display. A PLAIN join of the same
        #    addresses is untouched — this method returns early above when no
        #    correction is present.
        if (
            self._has_merge_review(excluded, resulting_balance)
            and len(preview["entry_ids"]) > BottleTrackingService.MERGE_PREVIEW_MAX_ENTRIES
        ):
            raise ValidationError(
                f"This merge spans {len(preview['entry_ids'])} ledger entries, above the "
                f"{BottleTrackingService.MERGE_PREVIEW_MAX_ENTRIES} the review can display. "
                "Narrow the merge or reconcile the place first."
                # No §13 code: the spec names none for the cap, and inventing
                # one is spec-owner territory. Same call as the malformed
                # `resulting_balance` rejection — a 400 on the message alone.
            )

        # 3. The admin decided against a snapshot. If the merged set has moved
        #    since — a delivery landed, a return was posted — every figure on
        #    that screen is stale and the override would encode the wrong
        #    number. Compare as SETS: the preview is ordered by
        #    (occurred_at, id), which is not necessarily ascending id.
        if preview_entry_ids is not None and sorted(int(i) for i in preview_entry_ids) != sorted(preview["entry_ids"]):
            raise ValidationError(
                "The bottle ledger changed since this preview was generated",
                error_code="MERGE_PREVIEW_STALE",
            )

        # A BALANCE-DECOUPLED row is NOT an exclusion candidate, and this fence
        # is about the row's COUPLING, not about its id. `_apply_merge_review`
        # reverses an exclusion through `_create_ledger_entry`, which moves the
        # place's balance by `-quantity`. That is correct for every row that
        # MOVED bottles, and destructive for the one kind that did not: a
        # `merge_backfill` aligned the LEDGER to the balance the place already
        # carried (see `_apply_merge_review`, step 1) without moving a crate, so
        # reversing it coupled takes N REAL bottles out of a place that still
        # physically holds them — a place at 12 drops to -8 because an admin
        # unticked a row the panel offered them.
        #
        # Reversing it DECOUPLED instead was the other candidate fix and is
        # worse: it would leave the episode with `balance != ledger_sum`, which
        # is precisely the convergence guarantee §7.4 exists to establish, and
        # the very next preview would re-backfill the same drift for ever. The
        # honest answer is that a ledger-only alignment is not a movement an
        # admin can un-tick; the way to change what the place holds is to STATE
        # the resulting balance, which is measured post-exclusion and is exactly
        # the control for this.
        #
        # `merge_correction` is NOT in this set even though it is also
        # place-level (`PLACE_LEVEL_LEDGER_KEY_PREFIXES` covers both): it is
        # balance-COUPLED, so its reversal is arithmetically consistent and
        # stays excludable — pinned by
        # `test_a_previously_written_merge_correction_can_be_excluded_and_convergence_survives`.
        decoupled = {
            entry.id
            for entry in preview["entries"]
            if (entry.idempotency_key or "").startswith(BALANCE_DECOUPLED_LEDGER_KEY_PREFIXES)
        }
        eligible = set(preview["entry_ids"]) - decoupled
        for entry_id in excluded:
            # 4. Only entries in THIS merge's own preview may be excluded.
            #    Mirrors `build_merge_preview`'s own `strict_exclusions` fence,
            #    run here so §7.4's guard order holds.
            if entry_id in decoupled:
                raise ValidationError(
                    f"Ledger entry {entry_id} records what this place already held "
                    "(a ledger-only alignment, no bottles moved) and cannot be excluded; "
                    "state the resulting balance instead",
                    error_code="MERGE_EXCLUSION_NOT_ELIGIBLE",
                )
            if entry_id not in eligible:
                raise ValidationError(
                    f"Ledger entry {entry_id} is not part of this merge",
                    error_code="MERGE_EXCLUSION_NOT_ELIGIBLE",
                )
        if excluded:
            # 5. ...and not one that an EARLIER episode already neutralised.
            #    The idempotency key is episode-scoped
            #    (`merge_exclude:{group}:{event}:{entry}`), so it would happily
            #    write a second reversal on a re-join and destroy the bottles
            #    twice. The `:` before the id anchors the suffix, so
            #    `merge_exclude:%:41` cannot match `...:341` or `...:410`.
            already = (
                db.session.query(BottleLedger.idempotency_key)
                .filter(or_(*[BottleLedger.idempotency_key.like(f"merge_exclude:%:{i}") for i in excluded]))
                .first()
            )
            if already is not None:
                raise ValidationError(
                    "One of these ledger entries has already been excluded from a merge",
                    error_code="MERGE_EXCLUSION_NOT_ELIGIBLE",
                )
        return preview

    def _apply_merge_review(
        self,
        *,
        group_id: int,
        event,
        preview: Optional[dict],
        addresses: list,
        reason: str,
        excluded_ledger_entry_ids: Optional[list],
        resulting_balance,
        acting_admin_id: int,
    ) -> None:
        """Spec §7.4's writes. Runs AFTER the joiners are absorbed.

        NO historical `quantity` is rewritten. Up to THREE entries are APPENDED,
        in this order:

        1. `merge_backfill` — `stored_before - ledger_sum_before`, written
           BALANCE-DECOUPLED (`_create_ledger_backfill_entry`). Skipped when
           there is no drift.
        2. one `merge_exclude` per excluded entry — a reversing `-quantity`,
           balance-coupled.
        3. `merge_correction` — `stated - (stored_before - excluded_total)`,
           balance-coupled. Skipped when that delta is zero.

        WHY THE BACKFILL EXISTS, AND WHY IT IS THE ONLY DECOUPLED WRITE. The
        stored figure and the ledger sum are routinely different, and that is
        KNOWN and EXPECTED: addresses were manually adjusted before grouping and
        the join CARRIES those figures rather than re-deriving them (spec §7.2 —
        rebuilding from ledger sums would zero any place seeded before the
        ledger existed). Dev address 24 is the shape: stored 20.00 and ZERO
        ledger rows.

        A balance-COUPLED append cannot repair that: it moves both figures by
        the same amount, so their difference is invariant under it. ALIGNING THE
        LEDGER to the balance the place already carries is the one operation
        here that is a ledger fact and not a bottle movement — no crate arrives
        or leaves — so it is written decoupled, moving the ledger onto the
        stored figure while the balance stands still. It is SIGNED and both
        directions occur: positive where the ledger recorded too little (an
        opening balance it never had), negative where it recorded too much.
        After step 1 both figures read `stored_before`,
        and steps 2 and 3 then move them TOGETHER. Stating N yields N on BOTH,
        and re-stating N is a no-op: a sequence of previews converges.

        Writing the drift the other way round — a coupled `-drift` — was the
        previous attempt and is wrong twice over: on address 24 it asserts that
        twenty bottles LEFT the place on a day nothing left, and it drives the
        ledger to -8 while the balance reads 12, so the admin panel's Reconcile
        button (`api/admin_bottles.py`) would then set the balance to -8 and
        DESTROY the admin's number.

        The §7.4 order is unchanged and must not be inverted: the exclusions are
        applied before the override, and the override is measured against the
        POST-exclusion figure, never against the pre-exclusion one — measuring
        it against the latter would double-count every exclusion. Which
        post-exclusion figure is the one refinement: `stored_before -
        excluded_total`, the number the place will actually hold, rather than
        `computed_balance - excluded_total`. The two are IDENTICAL whenever the
        ledger already explains the stored figure, i.e. everywhere §7.4's rule
        was ever meaningful; on a drifted place, measuring against a figure the
        place does not hold is exactly what made an admin stating 10 get 15.

        WHAT STOPS THIS MINTING OR DESTROYING BOTTLES. Nothing stops the total
        from changing, and nothing should: an exclusion and an override are
        authoritative corrections — the admin has counted the crates and the
        ledger has not. What is guaranteed is that BALANCES move only through
        the COUPLED entries, each carrying its delta as its `quantity`, so
        `Σ balances after − Σ balances before` equals the sum of the coupled
        quantities and nothing else. The backfill is deliberately outside that
        sum: it moves no balance and is not a bottle movement. Both halves are
        pinned separately in `tests/unit/test_place_merge_review.py`.

        And the guarantee this now buys, which was impossible under the coupled
        design: after a reviewed merge `get_place_balance(...) == ledger_sum`
        for the place. That single equality is the strongest guard on the
        feature, and `reconcile_balance` — still never called here — becomes a
        no-op on the result instead of a destroyer of it.

        LOCK ORDERING (spec §5.2): this acquires exactly ONE `bottle_balances`
        row — the destination GROUP's, via `_create_ledger_entry`'s
        `get_or_create_balance` under the explicit group scope. It is NOT a
        fourth two-row acquirer, and it takes no address row, so it adds no
        edge to the wait-for graph. The caller already holds that same group row
        in every case where the place carried one; the only case where it is
        acquired here for the first time is the late-acquisition branch
        `_absorb_joiners_into_group` already documents, whose safety rests on
        the membership fence (a join requires each joiner UNGROUPED, a removal
        requires it GROUPED) and on the `addresses` row write-lock the absorb
        takes first, which serialises two concurrent joins of the same address
        before either reaches a bottle row.
        """
        from business_app.models.bottle import BottleBalance, BottleLedger
        from business_app.services.bottle_scope import BottleScope
        from business_app.services.bottle_tracking_service import BottleTrackingService

        if preview is None:
            return
        # Re-coerced, not re-validated: `_validate_merge_review` already
        # rejected anything unusable, and both halves must read the SAME number
        # (a raw float here and a Decimal there is how "5" becomes 4.999...).
        excluded = self._coerce_id_list(excluded_ledger_entry_ids, "excluded_ledger_entry_ids")
        resulting_balance = self._coerce_resulting_balance(resulting_balance)
        if not self._has_merge_review(excluded, resulting_balance):
            return

        bottles = BottleTrackingService()
        place_scope = BottleScope.for_group(group_id)
        by_id = {e.id: e for e in preview["entries"]}
        anchor_user_id, anchor_address_id = self._place_correction_anchor(preview, addresses)

        # STEP 1 — BACKFILL: align the ledger to the balance the place carries, so
        # steps 2 and 3 move two figures that already agree. Read under the
        # GROUP row's own FOR UPDATE (the same single row `_create_ledger_entry`
        # takes below — still no address row, still not a two-row acquirer).
        # Not created if absent: a place that has never moved a bottle must not
        # gain a 0.00 row here, and `stored_before` is then correctly 0.
        place_row = BottleBalance.query.filter(*place_scope.balance_filter()).with_for_update().first()
        # Quantized to the column's own scale so the two figures are comparable
        # and so the audit metadata below cannot read "0" on one place and
        # "0.00" on the next — SQLite renders an empty SUM's coalesce default
        # without scale, and an audit record that changes shape is a bad record.
        cents = Decimal("0.01")
        stored_before = (Decimal(str(place_row.balance or 0)) if place_row is not None else Decimal("0.00")).quantize(
            cents
        )
        ledger_sum_before = Decimal(
            str(
                db.session.query(func.coalesce(func.sum(BottleLedger.quantity), Decimal("0.00")))
                .filter(*place_scope.ledger_filter())
                .scalar()
                or 0
            )
        ).quantize(cents)
        backfill = stored_before - ledger_sum_before
        if backfill != 0:
            # BALANCE-DECOUPLED, and the only such write in the codebase: this
            # records what the place ALREADY holds, so moving the balance too
            # would mint (or destroy) the drift a second time. Signed — a
            # negative backfill retires a surplus the ledger over-recorded.
            # See `BottleTrackingService._create_ledger_backfill_entry`; this
            # is its ONLY call site, pinned by
            # tests/integration/test_bottle_place_lock_order.py.
            bottles._create_ledger_backfill_entry(
                scope=place_scope,
                user_id=anchor_user_id,
                address_id=anchor_address_id,
                quantity=backfill,
                actor_user_id=acting_admin_id,
                # SIGN-NEUTRAL: the ledger may have recorded too little (an
                # opening balance it never had) or too much (a surplus). A
                # note that assumes one direction is false in an audit of the
                # other, and this entry is written for both.
                notes="Place ledger aligned to the balance the place carries, during merge review",
                idempotency_key=f"merge_backfill:{group_id}:{event.id}",
                metadata={
                    "source": "merge_backfill",
                    "acting_admin_id": acting_admin_id,
                    "reason": reason,
                    # Everything needed to reconstruct what was backfilled.
                    "stored_before": str(stored_before),
                    "ledger_sum_before": str(ledger_sum_before),
                    "stated_resulting_balance": (None if resulting_balance is None else str(resulting_balance)),
                },
            )

        for entry_id in sorted(excluded):
            source = by_id[entry_id]
            quantity = Decimal(str(source.quantity or 0))
            bottles._create_ledger_entry(
                user_id=source.user_id,
                address_id=source.address_id,
                event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT,
                quantity=-quantity,
                actor_user_id=acting_admin_id,
                notes="Ledger entry excluded during place merge review",
                scope=place_scope,
                idempotency_key=f"merge_exclude:{group_id}:{event.id}:{entry_id}",
                metadata={
                    "source": "merge_exclude",
                    "excluded_ledger_entry_id": entry_id,
                    "acting_admin_id": acting_admin_id,
                    "reason": reason,
                },
            )

        if resulting_balance is not None:
            stated = resulting_balance
            # Measured against what the place WILL hold once the exclusions have
            # landed — `stored_before - excluded_total`. Each exclusion above is
            # balance-coupled and moves the stored figure by exactly `-quantity`,
            # so this is the post-exclusion figure by arithmetic rather than by
            # a re-read. Identical to §7.4's `computed_balance - excluded_total`
            # on any place whose ledger already explains its figure; on a
            # drifted one, that older basis is what made stating 10 give 15.
            post_exclusion = stored_before - preview["excluded_total"]
            delta = stated - post_exclusion
            if delta != 0:
                bottles._create_ledger_entry(
                    user_id=anchor_user_id,
                    address_id=anchor_address_id,
                    event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT,
                    quantity=delta,
                    actor_user_id=acting_admin_id,
                    notes="Resulting bottle balance corrected during place merge review",
                    scope=place_scope,
                    idempotency_key=f"merge_correction:{group_id}:{event.id}",
                    metadata={
                        "source": "merge_correction",
                        "acting_admin_id": acting_admin_id,
                        "reason": reason,
                        "stored_before": str(stored_before),
                        "ledger_sum_before": str(ledger_sum_before),
                        "post_exclusion_balance": str(post_exclusion),
                        "preview_resulting_balance": str(preview["resulting_balance"]),
                        "stated_resulting_balance": str(stated),
                    },
                )

        # The adjustments land after the absorb, so the snapshot pass runs LAST.
        BottleTrackingService.recompute_balance_after(place_scope)
        logger.info(
            "[PLACE] merge review applied group=%s event=%s backfill=%s excluded=%s "
            "resulting_balance=%s stored_before=%s ledger_sum_before=%s admin=%s",
            group_id,
            event.id,
            backfill,
            excluded,
            resulting_balance,
            stored_before,
            ledger_sum_before,
            acting_admin_id,
        )

    @staticmethod
    def _place_correction_anchor(preview: dict, addresses: list) -> tuple:
        """`(user_id, address_id)` to stamp on a PLACE-LEVEL correction entry.

        `bottle_ledger.(user_id, address_id)` are NOT NULL — decision 4's named
        ledger rests on them — but a `merge_backfill` / `merge_correction` belongs
        to the place, not to a member. The lowest-id entry in the merged set is
        chosen so the stamp is deterministic; when the merge has no history at
        all (an admin stating a figure for a place that has never moved a
        bottle) it falls back to the lowest-id JOINING ADDRESS, which is the
        case a bare `min(entries)` would 500 on.

        The borrowed stamp is neutralised everywhere it could be mistaken for
        an attribution: `BottleTrackingService.suggested_bottles_leaving` skips
        these keys, and `serialize_customer_place_ledger_entry` drops
        `member_name` and forces `is_own` False for these sources.
        """
        if preview["entries"]:
            oldest = min(preview["entries"], key=lambda e: e.id)
            return oldest.user_id, oldest.address_id
        first = min(addresses, key=lambda a: a.id)
        return first.user_id, first.id

    def _merge_review_metadata(self, excluded_ledger_entry_ids, resulting_balance) -> dict:
        """The §7.4 corrections, on the join event's `event_metadata` (Task 3's
        column). `resulting_balance` is stringified — JSON has no Decimal, and
        floating a bottle quantity into an audit record is how "5" becomes
        "4.999999999999999"."""
        payload = {}
        if excluded_ledger_entry_ids:
            payload["excluded_ledger_entry_ids"] = sorted(
                self._coerce_id_list(excluded_ledger_entry_ids, "excluded_ledger_entry_ids")
            )
        stated = self._coerce_resulting_balance(resulting_balance)
        if stated is not None:
            payload["resulting_balance"] = str(stated)
        return payload

    def create_place_group(
        self,
        address_ids: list,
        acting_admin_id: int,
        reason: str,
        label: str = None,
        excluded_ledger_entry_ids: list = None,
        resulting_balance=None,
        preview_entry_ids: list = None,
    ) -> AddressGroup:
        """Create an ownerless place group from >= 2 addresses (any owners).

        The three trailing parameters are spec §7.4's merge review; omitting
        them leaves this path exactly as it was.
        """
        from business_app.services.bottle_tracking_service import BottleTrackingService

        if len(set(address_ids)) < 2:
            raise ValidationError(
                "A place group needs at least two addresses",
                error_code="PLACE_GROUP_MIN_ADDRESSES",
            )
        addresses = self._load_addresses(address_ids)
        self._assert_place_group_eligible(addresses)

        # BEFORE the group row exists: a rejected review must not leave a
        # flushed AddressGroup behind for the next commit on this session.
        preview = self._validate_merge_review(
            address_ids=[a.id for a in addresses],
            group_id=None,
            reason=reason,
            excluded_ledger_entry_ids=excluded_ledger_entry_ids,
            resulting_balance=resulting_balance,
            preview_entry_ids=preview_entry_ids,
        )

        group = AddressGroup(canonical_customer_id=None, label=label)
        db.session.add(group)
        db.session.flush()
        # RUNG 0 is VACUOUS here, and that is an EXISTENCE argument, not an
        # argument about interleaving. The `address_groups` row does not exist
        # until this transaction INSERTs it, and a row that does not exist
        # cannot be locked by anybody — so there is nothing to take and nothing
        # to wait for. This exemption is valid ONLY while `create_place_group`
        # never ADOPTS an existing group id; `test_bottle_place_lock_order.py`
        # pins that. If a create path ever adopts one, it must take rung 0 via
        # `_lock_place_group` first.
        #
        # Phrasing this as "no other transaction will do X" is the exact shape of
        # argument that was accepted here once before and later proven false.
        BottleTrackingService.register_scope_lock(group_ids=[group.id])
        rescoped = self._absorb_joiners_into_group(group, addresses)

        event = CustomerLinkEvent(
            event_type="create_place_group",
            canonical_customer_id=None,
            acting_admin_id=acting_admin_id,
            member_user_ids=sorted({a.user_id for a in addresses}),
            reason=f"[group {group.id}] {reason or ''}".strip()[:500],
            event_metadata={
                "rescoped_ledger_entry_ids": rescoped,
                **self._merge_review_metadata(excluded_ledger_entry_ids, resulting_balance),
            },
        )
        db.session.add(event)
        # Flushed before the corrections: `event.id` is the episode handle their
        # idempotency keys are built from.
        db.session.flush()
        self._apply_merge_review(
            group_id=group.id,
            event=event,
            preview=preview,
            addresses=addresses,
            reason=(reason or "").strip(),
            excluded_ledger_entry_ids=excluded_ledger_entry_ids,
            resulting_balance=resulting_balance,
            acting_admin_id=acting_admin_id,
        )
        db.session.commit()
        return group

    def add_addresses_to_group(
        self,
        group_id: int,
        address_ids: list,
        acting_admin_id: int,
        reason: str,
        excluded_ledger_entry_ids: list = None,
        resulting_balance=None,
        preview_entry_ids: list = None,
    ) -> AddressGroup:
        """Add addresses to an existing place group (same fences as create).

        RUNG 0 BEFORE RUNG 1: the `address_groups` row is locked before any
        `addresses` row, so the member SET this call reasons about is pinned for
        the whole transaction.

        A MEMBERLESS GROUP IS REFUSED as a join target. §7.1/§7.3 deliberately
        leave a departed member's ledger rows stamped with the group they left
        (NULLing them would drop the place's history into a departed address's
        own scope and mint bottles onto someone who left with nothing), and
        `bottle_ledger.address_group_id` is a foreign key, so a DISSOLVED group
        keeps its row and its history for ever. Re-populating that id makes a
        group id denote two tenancies: `get_place_ledger` filters on
        `address_group_id` alone, so the new members inherit — and can read —
        a STRANGER'S delivery history and residual balance. Under rung 0 this
        refusal is evaluated while holding the group row, so unlike an unlocked
        existence check it is not a TOCTOU.

        This prevents NEW exposure; it does not un-mix a group already
        re-populated. The structural fix is an incarnation/epoch column on
        `address_groups`, which is a migration and is flagged separately.
        """
        group = self._lock_place_group(group_id)
        if group is None:
            raise ValidationError("Place group not found", error_code="PLACE_GROUP_NOT_FOUND")
        if not address_ids:
            return group
        if db.session.query(UserAddress.id).filter(UserAddress.address_group_id == group.id).first() is None:
            raise ValidationError(
                "This place group has no members and cannot be re-used; create a new place",
                error_code="PLACE_GROUP_DISSOLVED",
            )
        addresses = self._load_addresses(address_ids)
        self._assert_place_group_eligible(addresses)

        preview = self._validate_merge_review(
            address_ids=[a.id for a in addresses],
            group_id=group.id,
            reason=reason,
            excluded_ledger_entry_ids=excluded_ledger_entry_ids,
            resulting_balance=resulting_balance,
            preview_entry_ids=preview_entry_ids,
        )

        rescoped = self._absorb_joiners_into_group(group, addresses)

        event = CustomerLinkEvent(
            event_type="add_to_place_group",
            canonical_customer_id=None,
            acting_admin_id=acting_admin_id,
            member_user_ids=sorted({a.user_id for a in addresses}),
            reason=f"[group {group.id}] {reason or ''}".strip()[:500],
            event_metadata={
                "rescoped_ledger_entry_ids": rescoped,
                **self._merge_review_metadata(excluded_ledger_entry_ids, resulting_balance),
            },
        )
        db.session.add(event)
        db.session.flush()
        self._apply_merge_review(
            group_id=group.id,
            event=event,
            preview=preview,
            addresses=addresses,
            reason=(reason or "").strip(),
            excluded_ledger_entry_ids=excluded_ledger_entry_ids,
            resulting_balance=resulting_balance,
            acting_admin_id=acting_admin_id,
        )
        db.session.commit()
        return group

    @staticmethod
    def _validated_bottles_leaving(address, bottles_leaving) -> Decimal:
        """Spec §7.1 / §13 — one code, `PLACE_SPLIT_INVALID`, for all rejections.

        Three arms in the spec's words: negative, above the place balance, or
        non-zero when the place balance is <= 0. The third is NOT redundant with
        the second: when the place is over-returned its balance sits BELOW the
        default of 0, so "cap at the place balance" would reject the default and
        "clamp to the cap" would quietly produce a negative transfer. Capping at
        ``max(0, place)`` is the one expression that says all three at once —
        0 always passes, and nothing above what the place actually holds ever
        does.

        Out-of-range is a REJECTION, never a silent clamp: absorbing impossible
        numbers is exactly what retiring the netting mechanism was meant to stop.
        """
        from business_app.services.bottle_tracking_service import BottleTrackingService

        try:
            leaving = Decimal(str(bottles_leaving if bottles_leaving is not None else 0))
        except (ArithmeticError, ValueError):
            # The concrete one is decimal.InvalidOperation, which IS an
            # ArithmeticError — naming both would be redundant.
            raise ValidationError(
                "bottles_leaving must be a number",
                error_code="PLACE_SPLIT_INVALID",
            )
        # NaN/Infinity are reachable: Python's own JSON parser accepts both
        # literals, and EVERY comparison against NaN is False — so an unguarded
        # NaN slips past both range checks below straight into the ledger.
        if not leaving.is_finite():
            raise ValidationError(
                "bottles_leaving must be a number",
                error_code="PLACE_SPLIT_INVALID",
            )
        place = BottleTrackingService.get_place_balance(address.id)
        cap = max(Decimal("0.00"), place)
        if leaving < 0 or leaving > cap:
            raise ValidationError(
                f"bottles_leaving must be between 0 and the place balance ({place})",
                error_code="PLACE_SPLIT_INVALID",
            )
        return leaving.quantize(Decimal("0.01"))

    def remove_address_from_group(
        self, address_id: int, acting_admin_id: int, reason: str, bottles_leaving=None
    ) -> dict:
        """Remove one address from its place group (spec §7.1).

        By DEFAULT the bottles stay with the PLACE and the departing address
        starts a fresh scope at 0. Nothing is netted between people:
        `bottle_balances` has one row per place since migration a3e7d1f9c204, so
        a negative "pair" inside a non-negative place is not representable and
        the netting routine that settled it is deleted (spec §8).

        `bottles_leaving` is the deliberate, admin-chosen alternative: that many
        bottles leave WITH the address, as ONE conserving move — a paired
        ADMIN_ADJUSTMENT out of the place and into the address's brand-new
        scope, summing to zero. It defaults to 0, so the default path above is
        untouched, and an impossible quantity is rejected (`PLACE_SPLIT_INVALID`)
        rather than clamped. `BottleTrackingService.suggested_bottles_leaving`
        derives the pre-fill the admin is offered.

        The reason stays mandatory — removal changes who the place's bottles are
        physically reachable by, and the whole action lands on the audit trail.

        Spec §7.3: if this removal would leave the group with exactly ONE
        member, the place DISSOLVES onto that member in the same transaction —
        see `_dissolve_if_last_member`. `dissolved` reports whether it did.

        LOCK ORDERING (spec §5.2, revised) — the header below climbs the ladder
        in order and never descends:

          1. an UNLOCKED read of `address_group_id`, ONLY to name G;
          2. RUNG 0: `address_groups(G)` FOR NO KEY UPDATE;
          3. RUNG 1: the WHOLE member set of G, `ORDER BY id FOR NO KEY UPDATE`,
             in one statement. It necessarily contains both the departing
             address and the prospective survivor. This is sound as a predicate
             lock ONLY because rung 0 pins the set;
          4. RE-VALIDATION on the locked rows — the address exists and is still
             in G. The loser of two concurrent removals now gets a named
             `PLACE_GROUP_NOT_FOUND` instead of a 40P01 deadlock rendered to the
             admin as `ExternalServiceError("Database connection error")`;
             and if it is CANCELLED while waiting for rungs 0/1 rather than
             waking to that refusal, it gets the equally named
             `BOTTLE_SCOPE_LOCK_TIMEOUT` (see `_raise_place_busy`) — never a raw
             `OperationalError` rendered as a 500;
          5. RUNG 2: the place's `bottle_balances` row, taken BEFORE
             `_validated_bottles_leaving` so the §7.1 cap is validated under the
             very lock that guards the figure it caps;
          6. the body, unchanged.

        Step 3 is what kills the confirmed 40P01 between two concurrent
        removals. `_dissolve_if_last_member` acquires NOTHING: its member count
        is a re-read of a pinned set, and its survivor un-point is a LOCK
        UPGRADE on a row held since step 3, not a fresh `addresses` acquisition
        AFTER `bottle_balances(G)`. That relocation is the entire fix.

        Returns {'group_id': int, 'bottles_leaving': Decimal, 'dissolved': bool}.
        """
        from business_app.services.bottle_scope import BottleScope
        from business_app.services.bottle_tracking_service import BottleTrackingService

        if not (reason or "").strip():
            raise ValidationError(
                "A reason is required to remove an address from a place group",
                error_code="PLACE_GROUP_REASON_REQUIRED",
            )
        # 1. Unlocked, and used for NOTHING except naming the group to lock.
        #    Everything this decides is re-decided at step 4 on locked rows.
        named = db.session.query(UserAddress.address_group_id).filter(UserAddress.id == address_id).first()
        if named is None:
            raise ValidationError("Address not found", error_code="CUSTOMER_LINK_ADDRESS_NOT_FOUND")
        group_id = named[0]
        if group_id is None:
            raise ValidationError("Address is not in a place group", error_code="PLACE_GROUP_NOT_FOUND")

        # BOUND THE WAITER, then CONVERT what the bound produces (M2, see
        # `BottleTrackingService._apply_scope_lock_timeout`). A second admin
        # removing the other member of the same place is a WAITER on rung 0, and
        # the ladder makes that wait CERTAIN rather than incidental. Unbounded,
        # the wait is a gunicorn worker parked for the whole of the first admin's
        # transaction; unconverted, a bound turns into a raw 55P03
        # `OperationalError`, which no `except ValidationError` catches and the
        # route reports as `internal_error_response` — a 500 for "someone else
        # is editing this place right now".
        #
        # `_apply_scope_lock_timeout` never clobbers an explicit `lock_timeout`,
        # so an operator (or a concurrency test) that has stated a tighter bound
        # keeps it, and it is a no-op on SQLite where there is no lock at all.
        BottleTrackingService._apply_scope_lock_timeout()
        try:
            # 2. RUNG 0.
            if self._lock_place_group(group_id) is None:
                raise ValidationError("Place group not found", error_code="PLACE_GROUP_NOT_FOUND")

            # 3. RUNG 1 — the whole member set, ascending id, ONE statement.
            members = (
                UserAddress.query.filter(UserAddress.address_group_id == group_id)
                .order_by(UserAddress.id.asc())
                .populate_existing()
                .with_for_update(key_share=True)
                .all()
            )
        except OperationalError as exc:
            if BottleTrackingService._is_lock_not_available(exc):
                self._raise_place_busy(group_id, exc)
            raise
        BottleTrackingService.register_scope_lock(address_ids=[m.id for m in members])

        # 4. Re-validate on the LOCKED rows. Under READ COMMITTED the step-1 read
        #    saw a pre-image; this is the first read that is stable.
        address = next((m for m in members if m.id == address_id), None)
        if address is None:
            if db.session.query(UserAddress.id).filter(UserAddress.id == address_id).first() is None:
                raise ValidationError("Address not found", error_code="CUSTOMER_LINK_ADDRESS_NOT_FOUND")
            raise ValidationError("Address is not in a place group", error_code="PLACE_GROUP_NOT_FOUND")

        # 5. RUNG 2, hoisted ABOVE the cap validation. Locked, never created: a
        #    place that has never moved a bottle must not gain a 0.00 row for
        #    being removed from. Reading the cap outside this lock is what let a
        #    concurrent return push a "validated" split past it and drive the
        #    place negative.
        BottleTrackingService.get_balance_row(BottleScope.for_group(group_id))

        # Validated BEFORE anything is written, so a rejected split leaves no
        # flushed audit event behind for the next commit on this session to pick
        # up. The place balance it reads still resolves through the group — the
        # membership pointer is only cleared at the end.
        leaving = self._validated_bottles_leaving(address, bottles_leaving)

        event = CustomerLinkEvent(
            event_type="remove_from_place_group",
            canonical_customer_id=None,
            acting_admin_id=acting_admin_id,
            member_user_ids=[address.user_id],
            reason=f"[group {group_id}] {reason.strip()}"[:500],
        )
        db.session.add(event)
        # Flushed before the membership pointer moves: `event.id` is the episode
        # handle any bottle movement recorded for THIS removal keys itself to.
        db.session.flush()

        if leaving > 0:
            self._split_bottles_out_of_place(
                address=address,
                group_id=group_id,
                event_id=event.id,
                leaving=leaving,
                acting_admin_id=acting_admin_id,
                reason=reason.strip(),
            )

        # Spec §5.7: place scope creates reservations only via the cluster-keyed
        # ring-3 sweep, so ungroup has no reservations to release. If ring 3 ever
        # becomes place-keyed, release them here.

        address.address_group_id = None
        # FLUSHED before the membership count below: the dissolve expires the
        # session, and expiring a pending change would discard it.
        db.session.flush()

        dissolved = self._dissolve_if_last_member(
            group_id=group_id,
            departing_address_id=address_id,
            event=event,
            acting_admin_id=acting_admin_id,
            reason=reason.strip(),
        )

        db.session.commit()
        return {"group_id": group_id, "bottles_leaving": leaving, "dissolved": dissolved}

    @staticmethod
    def _dissolve_if_last_member(
        *, group_id: int, departing_address_id: int, event, acting_admin_id: int, reason: str
    ) -> bool:
        """Spec §7.3 — ONE rule: the LAST member out takes the place's history.

        `create_place_group` requires >= 2 addresses, so a removal that leaves
        the group with fewer than two members has produced a state the
        constructor refuses to build. Dissolve it in THIS transaction: the last
        member takes its own history back and inherits whatever the place still
        holds, and the group's `bottle_balances` row — which no address could
        resolve to any more — is deleted. That row is precisely the nightly
        `orphaned_place_balances` violation this closes.

        Two arms of the SAME rule, not two rules:

        * ONE address remaining — the survivor is the last member, and the
          departing address leaves with only what §7.1's `bottles_leaving` gave
          it.
        * ZERO remaining — the DEPARTING address is itself the last member out,
          so it is what the history is released to. Reachable because neither
          `add_addresses_to_group` nor its route enforces a minimum member
          count: a dissolved group can be repopulated to exactly one member and
          then emptied. "The bottles stay with the place" (§7.1's default) has
          no meaning once the place has nobody left who can reach them —
          stranding them behind a memberless group is not "staying", it is
          losing them. When §7.1's split ran in the same call, its `:out` half is
          attributed to that same address and is re-stamped along with the rest,
          so the two split halves simply net to zero inside the address's own
          scope: in retrospect nothing ever left.

        The `AddressGroup` row itself is KEPT in both arms. The spec's literal
        "the group is then deleted" is not implementable:
        `bottle_ledger.address_group_id` is a foreign key that every DEPARTED
        member's entries still carry, and NULLing those would drop the place's
        history into a departed address's own scope under §3.1 — minting bottles
        onto an address that left with nothing. A memberless group is inert
        (nothing resolves to it) and stays as the anchor for that history.

        Runs AFTER the §7.1 `bottles_leaving` split, so a departing member can
        take their crates AND trigger the dissolve in one call: the split writes
        while the place still has its members, then the dissolve moves what is
        left.

        LOCK ORDERING (spec §5.2, revised): THIS METHOD ACQUIRES NOTHING NEW.
        `remove_address_from_group` already holds `address_groups(G)` (rung 0)
        and every `addresses` row of G (rung 1, ascending id, one statement), so

          * the member count below is a RE-READ of a set that is already pinned
            — it can no longer see the other removal's uncommitted clear and
            miss the dissolve, and it can no longer be raced;
          * the survivor un-point is a LOCK UPGRADE on a row held since rung 1,
            not a fresh `addresses` acquisition AFTER `bottle_balances(G)`.

        That second point is the entire fix for the confirmed 40P01: the old
        shape took `addresses(A)` -> `bottle_balances(G)` -> `addresses(B)`,
        straddling rung 2 with rung 1 on both sides, and two removals from a
        two-member place were a textbook ABBA.
        """
        from business_app.services.bottle_tracking_service import BottleTrackingService

        remaining = (
            db.session.query(UserAddress.id)
            .filter(UserAddress.address_group_id == group_id)
            .order_by(UserAddress.id.asc())
            .all()
        )
        if len(remaining) > 1:
            return False

        # Zero remaining => the address that just left IS the last member out.
        survivor_id = remaining[0][0] if remaining else departing_address_id
        released = BottleTrackingService.release_group_history_to_address(
            group_id,
            survivor_id,
            acting_admin_id=acting_admin_id,
            event_id=event.id,
            reason=reason,
        )
        if remaining:
            # Nothing to un-point in the zero-remaining arm: the caller already
            # cleared the departing address's own pointer.
            db.session.query(UserAddress).filter(UserAddress.id == survivor_id).update(
                {UserAddress.address_group_id: None}, synchronize_session=False
            )
            db.session.flush()
        db.session.expire_all()

        # Record it on the SAME episode — one removal, one audit row. `reason` is
        # String(500): trim the admin's prose, never the marker.
        marker = " | place dissolved onto its last member"
        event.reason = f"{event.reason or ''}"[: 500 - len(marker)] + marker
        event.event_metadata = {
            **(event.event_metadata or {}),
            "dissolved_onto_address_id": survivor_id,
            "dissolved_inherited_bottles": str(released["inherited"]),
            "dissolved_rescoped_ledger_entry_ids": released["entry_ids"],
        }
        logger.info(
            "[PLACE] group dissolved onto its last member group=%s survivor=%s event=%s admin=%s",
            group_id,
            survivor_id,
            event.id,
            acting_admin_id,
        )
        return True

    @staticmethod
    def assert_address_not_in_place_group(address_id: int) -> None:
        """Spec §7.3 — a grouped address cannot be deleted (PLACE_GROUP_ADDRESS_NOT_DELETABLE).

        `bottle_balances.address_id NOT NULL` used to make this fail with an
        IntegrityError that all three delete paths convert into "referenced by
        existing records". A grouped address has no balance row of its own, so
        that guard weakens for exactly the members who share a pool. Remove the
        address from its place first — that routes through §7.1 and makes the
        bottle question explicit.

        Shared by all three delete entry points (`api/addresses.py`,
        `services/auth_service.py`, `api/admin.py`); a fence on two of three is
        not a fence.

        The message is TRANSLATED, in the key-then-English-fallback idiom
        `api/addresses.py` uses for every other error on that route (see
        `api.addresses.error.in_use_by_subscription`). It reaches customers: the
        Telegram bot deletes via `/api/v1/auth/addresses/<id>` and renders the
        message verbatim. `error_code` remains the machine contract and is
        unaffected by the language.
        """
        row = db.session.query(UserAddress.address_group_id).filter(UserAddress.id == address_id).first()
        if row is not None and row[0] is not None:
            from business_app.utils.translations import get_translation

            key = "api.addresses.error.in_place_group"
            message = get_translation(key)
            if message == key:
                message = "Cannot delete an address that belongs to a place group — remove it from the place first"
            raise ValidationError(message, error_code="PLACE_GROUP_ADDRESS_NOT_DELETABLE")

    @staticmethod
    def _split_bottles_out_of_place(
        *, address, group_id: int, event_id: int, leaving: Decimal, acting_admin_id: int, reason: str
    ) -> None:
        """Move `leaving` bottles from the place to the departing address (§7.1).

        ONE conserving move written as two halves that sum to zero, in the
        caller's transaction and BEFORE `address_group_id` is cleared. Both
        scopes are passed EXPLICITLY (spec §5): `resolve_scope` returns exactly
        one scope for an address and cannot express "out of the group, into the
        address" on its own.

        LOCK ORDERING (spec §5.2, revised). DEADLOCK-FREEDOM HERE RESTS ON
        ORDERING ALONE, NEVER ON A FENCE. Every transaction acquires a prefix of
        one total order:

            rung 0  `address_groups` row      FOR NO KEY UPDATE  (lifecycle)
            rung 1  `addresses` rows          FOR SHARE for a writer that only
                                              READS membership, FOR NO KEY UPDATE
                                              for the lifecycle; ascending id,
                                              ONE statement
            rung 2  `bottle_balances` GROUP row    FOR UPDATE
            rung 3  `bottle_balances` ADDRESS row  FOR UPDATE

        The whole ladder sits BELOW the payment/settlement locks
        (`staff_service.py`'s `lock_order_settlement_candidates`,
        `order_edit_service.py`'s cash-before-bottles rule).

        This is the FIRST place in the codebase to hold TWO `bottle_balances`
        rows in one transaction: the GROUP row (taken by the `:out` half) and
        then the departing ADDRESS's own row (taken by the `:in` half), in that
        order — rung 2 before rung 3. It is entered holding rungs 0 and 1:
        `remove_address_from_group` locks `address_groups(G)` and then the whole
        member set of G, ascending id, in one statement, before any bottle row.

        The three two-balance-row acquirers — this one,
        `_absorb_joiners_into_group` (join, §7.2) and
        `BottleTrackingService.release_group_history_to_address` (dissolve,
        §7.3) — are all lifecycle operations on ONE place, and all three hold
        that place's `address_groups` row first. They are therefore MUTUALLY
        EXCLUSIVE, and the join's late-create branch can no longer race
        anything. Every other bottle path — delivery, return, standalone
        collection, admin adjustment, fine, reconcile, order-edit cascade —
        takes exactly ONE balance row and so cannot be half of an ABBA cycle.

        THE OLD ARGUMENT IN THIS DOCSTRING appealed to a MEMBERSHIP FENCE — "of
        two concurrent transactions on one address, exactly one passes its
        fence." That is FALSE for two JOINS, and false for a structural reason:
        under READ COMMITTED both read the same pre-image, so a read-based test
        can never serialise anything. It is REPLACED, not repaired. There is no
        longer an ordering exception of any kind here.

        WHAT WOULD FALSIFY THIS: a lock acquired out of rung order; a write to
        `bottle_balances`/`bottle_ledger` for an address whose row lock is not
        held; or a write to `addresses.address_group_id` by any path — script,
        data fix, admin tool, new route — that does not first hold the target
        `address_groups` row. The last is an invariant on the TABLE, not on this
        method.
        """
        from business_app.services.bottle_scope import BottleScope
        from business_app.services.bottle_tracking_service import BottleTrackingService

        bottles = BottleTrackingService()
        shared = dict(
            user_id=address.user_id,
            address_id=address.id,
            event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT,
            actor_user_id=acting_admin_id,
            notes="Bottles leaving with the address on place-group removal",
            metadata={
                "source": "place_leave",
                "acting_admin_id": acting_admin_id,
                "reason": reason,
                "place_group_id": group_id,
            },
        )
        bottles._create_ledger_entry(
            quantity=-leaving,
            scope=BottleScope.for_group(group_id),
            idempotency_key=f"place_leave:{group_id}:{event_id}:{address.id}:out",
            **shared,
        )
        bottles._create_ledger_entry(
            quantity=leaving,
            scope=BottleScope.for_address(address.id),
            idempotency_key=f"place_leave:{group_id}:{event_id}:{address.id}:in",
            **shared,
        )
        logger.info(
            "[PLACE] bottles left with address on ungroup group=%s address=%s " "quantity=%s event=%s admin=%s",
            group_id,
            address.id,
            leaving,
            event_id,
            acting_admin_id,
        )

    def get_link_suggestions(self, user_id: int) -> list:
        """Rank other individual-customer accounts near this user's addresses.

        Primary (and only) automatic signal: geolocation proximity within
        CUSTOMER_LINK_SUGGESTION_RADIUS_KM. Score is dampened by how many DISTINCT
        customers share a geolocation, so shared offices/buildings rank low. Name
        matching is deliberately NOT used. Every candidate is still admin-confirmed.
        """
        target_coords = [
            (a.latitude, a.longitude)
            for a in UserAddress.query.filter(
                UserAddress.user_id == user_id,
                UserAddress.latitude.isnot(None),
                UserAddress.longitude.isnot(None),
            ).all()
        ]
        if not target_coords:
            return []

        cluster_ids = set(self.get_cluster_user_ids(user_id))

        # Dismissed pairs involving this user -> exclude the other side.
        dismissed = set()
        for pair in CustomerDistinctPair.query.filter(
            (CustomerDistinctPair.user_id_low == user_id) | (CustomerDistinctPair.user_id_high == user_id)
        ).all():
            dismissed.add(pair.user_id_high if pair.user_id_low == user_id else pair.user_id_low)

        # Candidate addresses: other individual customers with coordinates.
        candidates_query = (
            db.session.query(
                UserAddress.user_id,
                UserAddress.latitude,
                UserAddress.longitude,
                User.first_name,
                User.last_name,
                User.phone,
            )
            .join(User, User.id == UserAddress.user_id)
            .filter(
                UserAddress.latitude.isnot(None),
                UserAddress.longitude.isnot(None),
                User.user_type == UserType.INDIVIDUAL,
                User.role == UserRole.CUSTOMER,
            )
        )

        radius = current_app.config["CUSTOMER_LINK_SUGGESTION_RADIUS_KM"]
        dampen_cutoff = current_app.config["CUSTOMER_LINK_SHARED_GEO_DAMPEN_CUTOFF"]
        # Bounding-box prefilter before the O(N*M) distance scan (spec §10):
        # margin covers the radius plus the ~11 m point-rounding grid so
        # shared_geo_customer_count at in-radius points is unchanged.
        candidates_query = self._bbox_prefilter(candidates_query, target_coords, radius + 0.05)
        candidates = candidates_query.all()

        # Pass 1: nearest distance per candidate user, and count distinct customers per rounded geo point.
        best = {}  # candidate_user_id -> {min_km, first_name, last_name, phone, point}
        geo_customers = {}  # rounded (lat,lng) -> set(user_id)
        for cand_uid, lat, lng, fn, ln, phone in candidates:
            point = (round(lat, 4), round(lng, 4))  # ~11 m grid
            geo_customers.setdefault(point, set()).add(cand_uid)
            if cand_uid in cluster_ids or cand_uid in dismissed or cand_uid == user_id:
                continue
            nearest = min(calculate_distance(tlat, tlng, lat, lng) for tlat, tlng in target_coords)
            if nearest > radius:
                continue
            if cand_uid not in best or nearest < best[cand_uid]["min_km"]:
                best[cand_uid] = {"min_km": nearest, "first_name": fn, "last_name": ln, "phone": phone, "point": point}

        results = []
        for cand_uid, info in best.items():
            shared = len(geo_customers.get(info["point"], set()))
            # Closer => higher; more shared customers at the point => lower.
            proximity = 1.0 - min(info["min_km"] / radius, 1.0)
            dampen = 1.0 / max(1, shared - dampen_cutoff + 1) if shared >= dampen_cutoff else 1.0
            results.append(
                {
                    "user_id": cand_uid,
                    "first_name": info["first_name"],
                    "last_name": info["last_name"],
                    "phone": info["phone"],
                    "min_distance_km": round(info["min_km"], 4),
                    "shared_geo_customer_count": shared,
                    "score": round(proximity * dampen, 4),
                }
            )
        results.sort(key=lambda r: r["score"], reverse=True)
        return results

    # ------------------------------------------------------------------
    # Place-group suggestions (Phase 2c, spec §10) — co-location is a
    # POSITIVE signal here, the inverse of the LINK channel's dampening.
    # ------------------------------------------------------------------

    @staticmethod
    def _bbox_prefilter(query, coords, radius_km):
        """Constrain an address query to a lat/lng bounding box around coords.

        1 degree latitude ~= 111 km; longitude degrees shrink by cos(lat).
        """
        if not coords:
            return query
        lat_margin = radius_km / 111.0
        avg_lat = sum(c[0] for c in coords) / len(coords)
        lng_margin = radius_km / (111.0 * max(0.1, math.cos(math.radians(avg_lat))))
        lat_min = min(c[0] for c in coords) - lat_margin
        lat_max = max(c[0] for c in coords) + lat_margin
        lng_min = min(c[1] for c in coords) - lng_margin
        lng_max = max(c[1] for c in coords) + lng_margin
        return query.filter(
            UserAddress.latitude.between(lat_min, lat_max),
            UserAddress.longitude.between(lng_min, lng_max),
        )

    @staticmethod
    def _point_fingerprint(address_ids) -> str:
        """Stable signal fingerprint for a co-located address set.

        A new address at the point changes the fingerprint, re-surfacing
        dismissed suggestions (spec §10).
        """
        joined = ",".join(str(i) for i in sorted(address_ids))
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:64]

    @staticmethod
    def _ungrouped_individual_address_query():
        """Candidate pool shared by the suggestion engine and the dismissal
        fingerprint, so the two can never disagree about a point's membership.

        NOTE: ``User.is_grocery_store`` is a Python @property
        (``business_app/models/user.py``), NOT a column — it CANNOT appear in a
        SQL filter. The INDIVIDUAL filter already excludes every entity user,
        and grocery stores are ``user_type=ENTITY``.
        """
        return (
            db.session.query(
                UserAddress.id,
                UserAddress.user_id,
                UserAddress.latitude,
                UserAddress.longitude,
                UserAddress.title,
                UserAddress.full_address,
                User.first_name,
                User.last_name,
                User.phone,
            )
            .join(User, User.id == UserAddress.user_id)
            .filter(
                UserAddress.latitude.isnot(None),
                UserAddress.longitude.isnot(None),
                UserAddress.address_group_id.is_(None),
                User.user_type == UserType.INDIVIDUAL,
                User.role == UserRole.CUSTOMER,
            )
        )

    @staticmethod
    def _place_suggestion_radius_km() -> float:
        """The PLACE channel's co-location radius, in km.

        The config key is in METRES (shared/business_config.py) and this is the
        single point of conversion, so no call site ever carries a /1000.0 of
        its own — nor a default literal (single-default rule).
        """
        return current_app.config["PLACE_SUGGESTION_RADIUS_M"] / 1000.0

    @staticmethod
    def _cluster_addresses_by_radius(rows, radius_km: float) -> List[List[dict]]:
        """Group co-located candidate addresses into connected components.

        Replaces the old ``round(lat, 4)`` snap-to-grid (~11.1 m N-S, ~8.4 m
        E-W, fixed to the world grid, so two pins 1 m apart could straddle a
        cell boundary and never be suggested). Pairs within ``radius_km`` are
        unioned, so A-B 8 m + B-C 8 m yields ONE candidate even when A-C is
        15 m (plan E18) — an admin should see one physical place, not two
        overlapping suggestions to reconcile.

        Distance is geopy geodesic via ``calculate_distance``
        (business_app/utils/helpers.py:80-82) — never distance_matrix, which is
        the routing/ETA stack and would make suggestions non-deterministic.

        🔴 THIS IS THE SINGLE SOURCE OF CO-LOCATION. ``get_place_group_suggestions``
        and ``dismiss_place_suggestion`` must BOTH call it. If they ever compute
        membership differently, a dismissal's fingerprint stops matching the
        clusterer's and the admin's "not the same place" silently no-ops
        (plan E19) — which is exactly what the old duplicated rounding risked.

        PERFORMANCE — the coarse-cell pre-bucket is REQUIRED, not an
        optimisation. A naive pairwise geodesic scan is O(n^2); the estate-wide
        path (``user_id=None``) and the dismissal path have no bounding box at
        all, so at 5 000 ungrouped addresses that would be ~12.5 M geodesic
        calls synchronously inside a gunicorn worker. Instead each row is
        bucketed into an integer cell of ``radius_km / 80.0`` DEGREES and tested
        only against its own cell and the 8 adjacent ones.

        Why ``/ 80.0`` cannot miss a pair (the completeness argument):
        one degree of latitude is ~111.13 km everywhere; one degree of
        longitude is ``111.32 * cos(lat)`` km, ~83.6 km at Tashkent's ~41.3 N —
        the smaller of the two and therefore the binding constraint. Both
        exceed the deliberately conservative 80, so a cell is AT LEAST
        ``radius_km`` wide on BOTH axes (it is 1.39x on latitude and 1.05x on
        longitude here). Two points within ``radius_km`` therefore differ by at
        most ``cell_deg`` on each axis, hence by at most 1 cell index on each
        axis, hence the 3x3 sweep always compares them. The margin holds for
        every latitude with ``cos(lat) >= 80/111.32``, i.e. up to 44.06 deg —
        TASHKENT_POLYGON spans ~41.1-41.4 N. **Do not "optimise" 80 upward.**

        The pre-bucket is a candidate GENERATOR only: every surviving pair still
        goes through ``calculate_distance``, so it can produce false candidates
        but never false negatives. The honest remaining bound is O(m^2) in the
        number of addresses inside one cell neighbourhood — a genuinely
        co-located crowd (one large building), not the estate.

        That O(m^2) is real and it BITES, so ``m`` is first reduced to the
        number of DISTINCT coordinates: rows sharing a byte-identical
        ``(latitude, longitude)`` are unioned outright and only ONE
        representative per coordinate enters the pair sweep. This is exactly
        partition-preserving, not a heuristic — their separation is 0.0 km,
        which satisfies ``<= radius_km`` for every positive radius, and any
        third row within the radius of the representative is within the same
        distance of every other row at that coordinate (identical inputs to
        ``calculate_distance``), so the components are unchanged. It matters
        because a geocoder falling back to a building/district centroid is the
        standard way thousands of rows land on one exact float pair: measured
        in this container, 5 000 such rows cost 4.52 s of pure pair iteration
        WITHOUT this collapse and 0.004 s with it, and that cost is paid
        synchronously in a gunicorn worker on the unanchored estate-wide path.

        Returns components in a deterministic order (ids sorted within a
        component, components sorted by their id lists). ``_point_fingerprint``
        hashes the sorted id set, so ordering does not affect correctness — it
        just makes the dismissal path and the suite reproducible.
        """
        members_by_id = {}
        rows_by_coord = {}
        cells = {}
        # A non-positive radius means "co-location is off"; every address is its
        # own component. Guarded here because cell_deg would be 0 (or negative)
        # and the floor division below would raise. The exact-coordinate
        # collapse is gated on the same guard, so a 0 radius keeps yielding all
        # singletons even for byte-identical pins.
        cell_deg = (radius_km / 80.0) if radius_km > 0 else None
        for row in rows:
            if row.latitude is None or row.longitude is None:  # defensive; the query filters these
                continue
            members_by_id[row.id] = {
                "address_id": row.id,
                "user_id": row.user_id,
                "first_name": row.first_name,
                "last_name": row.last_name,
                "phone": row.phone,
                "title": row.title,
                "full_address": row.full_address,
            }
            if cell_deg is not None:
                rows_by_coord.setdefault((row.latitude, row.longitude), []).append(row)

        parent = {addr_id: addr_id for addr_id in members_by_id}

        def find(x):
            root = x
            while parent[root] != root:
                root = parent[root]
            while parent[x] != root:  # path compression
                parent[x], x = root, parent[x]
            return root

        def union(x, y):
            rx, ry = find(x), find(y)
            if rx != ry:
                parent[max(rx, ry)] = min(rx, ry)

        # Collapse byte-identical coordinates BEFORE bucketing: union each such
        # group and send a single representative into the pair sweep, so `m` is
        # the count of distinct coordinates in a neighbourhood, not of rows.
        # See the docstring for why this cannot change the partition.
        for (lat, lng), same_point in rows_by_coord.items():
            representative = same_point[0]
            for duplicate in same_point[1:]:
                union(representative.id, duplicate.id)
            cells.setdefault((int(lat // cell_deg), int(lng // cell_deg)), []).append(representative)

        for (cx, cy), bucket in cells.items():
            neighbourhood = [r for dx in (-1, 0, 1) for dy in (-1, 0, 1) for r in cells.get((cx + dx, cy + dy), ())]
            for a in bucket:
                for b in neighbourhood:
                    # `a.id < b.id` tests each pair once and makes the result
                    # independent of dict ordering. Pairs already in one
                    # component are skipped: unioning them again is a no-op, so
                    # this is exact, and it drops the number of GEODESIC CALLS
                    # for a genuinely co-located crowd from O(m^2) to O(m).
                    # It does NOT make the sweep sub-quadratic — the iteration
                    # below is still m^2 union-find lookups, which is why `m`
                    # is distinct coordinates (see the collapse above) and why
                    # `test_the_clusterer_stays_within_its_wall_clock_budget`
                    # crowds DISTINCT pins, not duplicates: a duplicate crowd
                    # short-circuits on the first `a` and pins nothing.
                    if a.id < b.id and find(a.id) != find(b.id):
                        if calculate_distance(a.latitude, a.longitude, b.latitude, b.longitude) <= radius_km:
                            union(a.id, b.id)

        components = {}
        for addr_id in members_by_id:
            components.setdefault(find(addr_id), []).append(addr_id)
        return sorted(
            ([members_by_id[i] for i in sorted(ids)] for ids in components.values()),
            key=lambda members: [m["address_id"] for m in members],
        )

    def get_place_group_suggestions(self, limit: int = 20, user_id: Optional[int] = None) -> list:
        """Co-located ungrouped address sets ranked by distinct-customer count.

        Excludes already-grouped addresses and grocery/entity owners; a
        pairwise PlaceSuggestionDismissal with the point's CURRENT fingerprint
        suppresses the point (new members change the fingerprint and
        re-surface it). NEVER reads or writes CustomerDistinctPair — "same
        place" must not imply "same/different person".

        Co-location is a true PLACE_SUGGESTION_RADIUS_M radius over connected
        components, computed by ``_cluster_addresses_by_radius`` over the FULL
        unanchored pool. ``user_id`` anchors by FILTERING the finished
        components, never by narrowing the pool: components are transitively
        unbounded, so a bounding box drawn around the anchor would truncate a
        chain and make this path disagree with ``dismiss_place_suggestion``
        about the point's membership — which silently voids the dismissal
        (plan E19).
        """
        components = self._cluster_addresses_by_radius(
            self._ungrouped_individual_address_query().all(), self._place_suggestion_radius_km()
        )

        candidate_components = [members for members in components if len({m["user_id"] for m in members}) >= 2]
        if user_id is not None:
            candidate_components = [
                members for members in candidate_components if any(m["user_id"] == user_id for m in members)
            ]
        if not candidate_components:
            return []

        all_ids = [m["address_id"] for members in candidate_components for m in members]
        dismissals = PlaceSuggestionDismissal.query.filter(
            PlaceSuggestionDismissal.address_id_low.in_(all_ids),
            PlaceSuggestionDismissal.address_id_high.in_(all_ids),
        ).all()

        results = []
        for members in candidate_components:
            address_ids = sorted(m["address_id"] for m in members)
            fingerprint = self._point_fingerprint(address_ids)
            id_set = set(address_ids)
            suppressed = any(
                d.address_id_low in id_set and d.address_id_high in id_set and d.signal_fingerprint == fingerprint
                for d in dismissals
            )
            if suppressed:
                continue
            distinct = len({m["user_id"] for m in members})
            results.append(
                {
                    "address_ids": address_ids,
                    "distinct_customer_count": distinct,
                    "score": float(distinct),
                    "signal_fingerprint": fingerprint,
                    "members": members,
                }
            )
        # Strongest signal first; address ids break ties so paging is stable.
        results.sort(key=lambda r: (-r["distinct_customer_count"], -len(r["address_ids"]), r["address_ids"]))
        return results[: max(1, int(limit or 20))]

    def dismiss_place_suggestion(
        self, address_id_a: int, address_id_b: int, acting_admin_id: int, reason: str
    ) -> "PlaceSuggestionDismissal":
        """Sticky-until-new-signal "not the same place / do not group" assertion.

        Pairwise, normalized (low, high), idempotent upsert. Stamps the
        CURRENT fingerprint of the shared co-located point so a genuinely new
        signal (new address at the point) re-surfaces the suggestion. NEVER
        writes CustomerDistinctPair (spec §10) — a place dismissal must not
        block linking two accounts as one person.
        """
        addr_a = UserAddress.query.get(address_id_a)
        addr_b = UserAddress.query.get(address_id_b)
        if addr_a is None or addr_b is None:
            raise ValidationError("Address not found", error_code="CUSTOMER_LINK_ADDRESS_NOT_FOUND")

        # Fingerprint of the shared component's current ungrouped set; fallback
        # to the pair itself when the two are not co-located.
        #
        # 🔴 This runs the SAME clusterer (_cluster_addresses_by_radius) over the
        # SAME pool (_ungrouped_individual_address_query) as
        # get_place_group_suggestions — one pool, one clusterer, two consumers.
        # The two fingerprints then cannot diverge, because they are computed
        # from the same objects. If they ever did, this dismissal would stamp a
        # fingerprint the suggestion engine never produces and the admin's "not
        # the same place" would be silently forgotten forever (plan E19).
        fingerprint = self._point_fingerprint([address_id_a, address_id_b])
        for members in self._cluster_addresses_by_radius(
            self._ungrouped_individual_address_query().all(), self._place_suggestion_radius_km()
        ):
            component_ids = [m["address_id"] for m in members]
            if address_id_a in component_ids and address_id_b in component_ids:
                fingerprint = self._point_fingerprint(component_ids)
                break

        low, high = sorted([address_id_a, address_id_b])
        existing = PlaceSuggestionDismissal.query.filter_by(address_id_low=low, address_id_high=high).first()
        if existing is None:
            existing = PlaceSuggestionDismissal(
                address_id_low=low,
                address_id_high=high,
                dismissed_by_admin_id=acting_admin_id,
                signal_fingerprint=fingerprint,
            )
            db.session.add(existing)
        else:
            existing.dismissed_by_admin_id = acting_admin_id
            existing.signal_fingerprint = fingerprint
        db.session.add(
            CustomerLinkEvent(
                event_type="dismiss_place_suggestion",
                canonical_customer_id=None,
                acting_admin_id=acting_admin_id,
                member_user_ids=sorted({addr_a.user_id, addr_b.user_id}),
                reason=(reason or "").strip()[:500],
            )
        )
        db.session.commit()
        return existing
