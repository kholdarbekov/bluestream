"""The scope a bottle balance and ledger entry belong to — a PLACE, not a person.

A place is the address group when an address is grouped, and the address itself
when it is not (spec 2026-07-27 section 3). Exactly one of the two keys is set;
`bottle_balances.ck_bottle_balance_scope` enforces the same rule in the database.

`addresses.address_group_id` is deliberately NOT made NOT NULL: 16 predicate
sites across 6 backend files spell "is this address at a shared place?" as
`address_group_id IS NOT NULL`, five of them on the money path, and they fail
dangerous rather than safe (spec section 2.1).
"""

from dataclasses import dataclass
from typing import List, Optional

from business_app.models.bottle import BottleBalance, BottleLedger


@dataclass(frozen=True)
class BottleScope:
    group_id: Optional[int] = None
    address_id: Optional[int] = None

    def __post_init__(self):
        if (self.group_id is None) == (self.address_id is None):
            raise ValueError("BottleScope requires exactly one of group_id / address_id")

    @classmethod
    def for_group(cls, group_id: int) -> "BottleScope":
        return cls(group_id=group_id, address_id=None)

    @classmethod
    def for_address(cls, address_id: int) -> "BottleScope":
        return cls(group_id=None, address_id=address_id)

    @property
    def is_grouped(self) -> bool:
        return self.group_id is not None

    def balance_filter(self) -> List:
        """Criteria selecting this scope's single `bottle_balances` row."""
        if self.is_grouped:
            return [BottleBalance.address_group_id == self.group_id]
        return [
            BottleBalance.address_id == self.address_id,
            BottleBalance.address_group_id.is_(None),
        ]

    def ledger_filter(self) -> List:
        """Criteria selecting this scope's `bottle_ledger` entries.

        The ungrouped arm MUST keep `address_group_id IS NULL`. After an address
        leaves a place its rows stay stamped with the former group (spec 7.1);
        filtering on `address_id` alone would pull that whole place history back
        into the departed address, in both `reconcile_balance` and
        `get_place_ledger`.
        """
        if self.is_grouped:
            return [BottleLedger.address_group_id == self.group_id]
        return [
            BottleLedger.address_id == self.address_id,
            BottleLedger.address_group_id.is_(None),
        ]

    def balance_defaults(self) -> dict:
        """Column values identifying this scope on a new `bottle_balances` row."""
        return {"address_group_id": self.group_id, "address_id": self.address_id}

    def conflict_column(self) -> str:
        """The unique column to infer for `ON CONFLICT DO NOTHING`."""
        return "address_group_id" if self.is_grouped else "address_id"
