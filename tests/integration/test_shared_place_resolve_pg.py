"""REAL POSTGRES: two admins resolving the same case at once book its bottle move once.

The resolve call cannot hold the case lock across `admin_adjust_balance`, which commits on its own (spec §9). So it
claims the outcome under the case row lock, re-locks the case before each keyed leg, and settles under a final lock.
Here two submits of choice D (own bottles) race: the first is held right after its first read under the case lock
(`CustomerLinkService.get_address_place_group_id`, from the screen-offer check), the second starts only then and must
wait on that lock. Both answer 200, one `in` leg is booked, and the place moves by the returned count once.

`with_for_update` compiles to nothing on SQLite, so this runs on a real, fully-migrated Postgres database (`pg_app`),
with the forced interleaving of test_bottle_transfer_races_pg_e2e.py's `_double_tap`.
"""

from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.bottle import BottleLedger
from business_app.models.shared_place_case import SharedPlaceCase
from business_app.services.customer_link_service import CustomerLinkService
from shared.enums import UserRole, UserType
from tests.integration.reorder_reminder_builders import make_customer
from tests.integration.shared_place_builders import BASE_LAT, BASE_LNG, customer, pinned_address
from tests.integration.test_bottle_transfer_races_pg_e2e import _double_tap
from tests.integration.test_shared_place_admin_review_api import _delivered
from tests.integration.test_staff_bot_place_full_e2e import _pair

pytestmark = [pytest.mark.integration, pytest.mark.e2e]

RESOLVE = "/api/v1/admin/shared-place-cases/{}/resolve"


def test_concurrent_resolves_book_one_set_of_legs(pg_app, pg_db, monkeypatch):
    admin = make_customer(pg_db, first_name="Admin", role=UserRole.ADMIN, user_type=UserType.STAFF)
    headers = {
        "Authorization": "Bearer " + create_access_token(identity=str(admin.id), additional_claims={"role": "admin"}),
        "Content-Type": "application/json",
    }
    u = customer(pg_db, "Jasur")
    address = pinned_address(pg_db, u, BASE_LAT, BASE_LNG)
    case, *_ = _delivered(pg_db, u, address, quantity=1, returned=3)
    case_id, address_id = case.id, address.id
    assert _pair(address_id) == (Decimal("-2"), Decimal("-2"))

    def resolves_d(client):
        return client.post(RESOLVE.format(case_id), json={"kind": "own_bottles", "reason": "his own"}, headers=headers)

    first, second = _double_tap(
        pg_app,
        pg_db,
        monkeypatch,
        held_method="get_address_place_group_id",
        first_tap=resolves_d,
        second_tap=resolves_d,
        owner=CustomerLinkService,
    )

    assert (first[0], second[0]) == (200, 200), (first, second)
    assert first[1]["data"]["case"]["status"] == second[1]["data"]["case"]["status"] == "resolved"
    pg_db.session.expire_all()
    legs = BottleLedger.query.filter(BottleLedger.idempotency_key.like(f"shared_place:{case_id}:%")).all()
    assert [(row.idempotency_key, row.address_id, row.quantity) for row in legs] == [
        (f"shared_place:{case_id}:in", address_id, Decimal("3"))
    ]
    assert _pair(address_id) == (Decimal("1"), Decimal("1"))  # -2 + 3, once
    row = SharedPlaceCase.query.get(case_id)
    assert (row.status, row.resolution_kind, row.resolution_payload) == (
        "resolved",
        "own_bottles",
        {"kind": "own_bottles"},
    )
