"""The end-of-shift RETURN count is bounded by the COLUMN, not by the business.

Wave 1 bounded the load-out (``MAX_BOTTLES_PER_SESSION``) because a driver's
mistyped phone number overflowed ``DriverBottleSession.bottles_loaded``, a
4-byte PostgreSQL integer, and reached the depot as a DataError 500. The
closing half of the same session had the same hole: ``receive_bottles_returned``
guarded ``count < 0`` only, and ``DriverBottleSessionCloseRequest`` declared
``ge=0`` with no ceiling.

The load-out bound cannot simply be reused here. Over-returning is LEGITIMATE
on this side — a driver hands back the load they took out PLUS every empty
collected at a door, and an individual place can be over-returned all on its
own (``tests/unit/test_staff_bot_over_returned.py``) — so any
business-plausibility ceiling would eventually refuse a real shift. What is
left to refuse is only what the storage cannot carry, which is why
``BOTTLE_RETURN_COLUMN_CEILING`` is its own constant with its own name.

These tests pin: one home for the number, that it is NOT a business rule (an
over-return far past a truck-load is accepted), and that the bot's refusal is
carried at the HTTP boundary too — a bot-only bound is a bound the backend does
not have.
"""

import re
from pathlib import Path

import pytest

from business_app.models.bottle import DriverBottleSession
from shared.enums import DriverBottleSessionStatus
from shared.staff_constants import BOTTLE_RETURN_COLUMN_CEILING, MAX_BOTTLES_PER_SESSION

pytestmark = pytest.mark.integration

OPEN_URL = "/api/v1/staff/bottles/session/open"
CLOSE_URL = "/api/v1/staff/bottles/session/close"
LEGACY_RETURN_URL = "/api/v1/staff/bottles/return-to-warehouse"

# The storage column's own ceiling: PostgreSQL `integer` is 4 bytes, signed.
COLUMN_CEILING = 2**31 - 1

REPO_ROOT = Path(__file__).resolve().parents[2]
BOT_HANDLER = REPO_ROOT / "staff_bot" / "handlers" / "delivery" / "bottle_collection.py"

# A typed phone number: the real shape of the slip this bound exists for.
KEYPAD_SLIP = 40_000_000_000


def _open_session(client, headers, loaded=10):
    resp = client.post(OPEN_URL, json={"bottles_loaded": loaded}, headers=headers)
    assert resp.status_code == 201, resp.get_data(as_text=True)
    return resp


# --------------------------------------------------------------------------- #
# 1. One home, and a name nobody can mistake for a business rule
# --------------------------------------------------------------------------- #


def test_the_return_bound_is_defined_once_in_shared_staff_constants():
    """MOVED, not copied — same rule as the load-out ceiling."""
    source = BOT_HANDLER.read_text()
    assert not re.search(r"^BOTTLE_RETURN_COLUMN_CEILING\s*=", source, re.MULTILINE), (
        "the staff bot defines its own copy of the return bound — import the "
        "one in shared/staff_constants.py instead"
    )

    from staff_bot.handlers.delivery import bottle_collection

    assert bottle_collection.BOTTLE_RETURN_COLUMN_CEILING is BOTTLE_RETURN_COLUMN_CEILING


def test_the_return_bound_is_a_storage_bound_and_not_the_load_out_ceiling():
    """It must be derived from the column, and be a different number.

    Reusing ``MAX_BOTTLES_PER_SESSION`` here would refuse the ordinary case:
    everything the truck left with plus everything collected at doors comes
    back through the same field.
    """
    columns = DriverBottleSession.__table__.c
    assert columns.bottles_returned_to_warehouse.type.python_type is int
    assert columns.discrepancy.type.python_type is int, (
        "the close derives `discrepancy` from this count into another integer "
        "column — re-derive this bound if that changes"
    )

    assert BOTTLE_RETURN_COLUMN_CEILING != MAX_BOTTLES_PER_SESSION
    assert BOTTLE_RETURN_COLUMN_CEILING > MAX_BOTTLES_PER_SESSION * 1000, (
        "this reads like a business ceiling — it must be a storage bound, far "
        "beyond anything a fleet could physically return"
    )
    # Headroom for the discrepancy the close computes from it, which is stored
    # in a column of the same width.
    assert BOTTLE_RETURN_COLUMN_CEILING <= COLUMN_CEILING - MAX_BOTTLES_PER_SESSION


# --------------------------------------------------------------------------- #
# 2. It refuses NO legitimate return
# --------------------------------------------------------------------------- #


def test_an_over_return_far_past_the_truck_load_is_accepted(
    client, db, driver_auth_headers, delivery_driver
):
    """The whole reason this is not `MAX_BOTTLES_PER_SESSION`.

    Ten bottles left the warehouse; a thousand empties came back off doorsteps.
    That is a real day, and the close must record it rather than argue with it.
    """
    _open_session(client, driver_auth_headers, loaded=10)

    resp = client.post(
        CLOSE_URL,
        json={"bottles_returned_to_warehouse": MAX_BOTTLES_PER_SESSION * 2},
        headers=driver_auth_headers,
    )

    assert resp.status_code == 200, resp.get_data(as_text=True)
    session = DriverBottleSession.query.filter_by(driver_user_id=delivery_driver.id).one()
    assert session.bottles_returned_to_warehouse == MAX_BOTTLES_PER_SESSION * 2
    assert session.status == DriverBottleSessionStatus.CLOSED


def test_a_return_at_the_storage_bound_itself_is_still_accepted(
    client, db, driver_auth_headers, delivery_driver
):
    """The bound admits everything up to it — it fences the column, not the day."""
    _open_session(client, driver_auth_headers, loaded=MAX_BOTTLES_PER_SESSION)

    resp = client.post(
        CLOSE_URL,
        json={"bottles_returned_to_warehouse": BOTTLE_RETURN_COLUMN_CEILING},
        headers=driver_auth_headers,
    )

    assert resp.status_code == 200, resp.get_data(as_text=True)
    session = DriverBottleSession.query.filter_by(driver_user_id=delivery_driver.id).one()
    assert session.bottles_returned_to_warehouse == BOTTLE_RETURN_COLUMN_CEILING
    # The value the close DERIVES from it has to fit the same width, or the
    # bound has merely moved the crash one column to the right.
    assert -COLUMN_CEILING - 1 <= session.discrepancy <= COLUMN_CEILING


# --------------------------------------------------------------------------- #
# 3. The backend refuses what the bot refuses
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("url", [CLOSE_URL, LEGACY_RETURN_URL])
def test_the_backend_refuses_a_return_the_column_cannot_hold(
    client, db, driver_auth_headers, delivery_driver, url
):
    """A typed phone number posted straight at the API — no bot in the path.

    Both the current endpoint and the deprecated shim share the serializer, so
    both inherit the bound.
    """
    _open_session(client, driver_auth_headers)

    resp = client.post(
        url, json={"bottles_returned_to_warehouse": KEYPAD_SLIP}, headers=driver_auth_headers
    )

    assert resp.status_code == 400, resp.get_data(as_text=True)
    session = DriverBottleSession.query.filter_by(driver_user_id=delivery_driver.id).one()
    assert session.status == DriverBottleSessionStatus.OPEN, (
        "a count the storage column cannot hold closed the session anyway"
    )
    assert session.bottles_returned_to_warehouse is None


def test_the_backend_refuses_one_bottle_past_the_bound(
    client, db, driver_auth_headers, delivery_driver
):
    """The refusal boundary is the shared constant itself, not some other number."""
    _open_session(client, driver_auth_headers)

    resp = client.post(
        CLOSE_URL,
        json={"bottles_returned_to_warehouse": BOTTLE_RETURN_COLUMN_CEILING + 1},
        headers=driver_auth_headers,
    )

    assert resp.status_code == 400, resp.get_data(as_text=True)
    session = DriverBottleSession.query.filter_by(driver_user_id=delivery_driver.id).one()
    assert session.status == DriverBottleSessionStatus.OPEN


def test_the_lower_bound_the_ceiling_was_added_beside_still_holds(
    client, db, driver_auth_headers, delivery_driver
):
    """Regression fence: adding `le=` must not have loosened `ge=0`.

    Zero is a real answer (a driver who sold everything); minus one is a typo
    that would become a credit.
    """
    _open_session(client, driver_auth_headers)

    resp = client.post(
        CLOSE_URL, json={"bottles_returned_to_warehouse": -1}, headers=driver_auth_headers
    )
    assert resp.status_code == 400, resp.get_data(as_text=True)

    resp = client.post(
        CLOSE_URL, json={"bottles_returned_to_warehouse": 0}, headers=driver_auth_headers
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)


def test_the_admin_force_close_writes_the_same_column_and_carries_the_same_bound():
    """Same column, same crash — the bound belongs to the field, not the caller.

    An admin force-closing an abandoned session writes
    ``bottles_returned_to_warehouse`` through a different serializer; leaving
    that one unbounded would mean the admin UI keeps the 500 the driver no
    longer gets.
    """
    from pydantic import ValidationError as PydanticValidationError

    from business_app.serializers.bottle_serializers import AdminForceCloseSessionRequest

    with pytest.raises(PydanticValidationError):
        AdminForceCloseSessionRequest(
            bottles_returned_to_warehouse=KEYPAD_SLIP, reason="abandoned"
        )

    accepted = AdminForceCloseSessionRequest(
        bottles_returned_to_warehouse=BOTTLE_RETURN_COLUMN_CEILING, reason="abandoned"
    )
    assert accepted.bottles_returned_to_warehouse == BOTTLE_RETURN_COLUMN_CEILING
