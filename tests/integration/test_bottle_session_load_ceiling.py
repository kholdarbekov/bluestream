"""The truck load-out ceiling is ONE rule with ONE home.

Wave 1 stopped a driver's mistyped phone number from overflowing
``DriverBottleSession.bottles_loaded`` (a 4-byte PostgreSQL integer, ceiling
2147483647) by bounding the count where the driver could still be told what was
wrong with what they typed — in the staff bot.

That guard was client-side only. ``DriverBottleSessionOpenRequest`` declared
``bottles_loaded: int = Field(..., gt=0)`` with no ceiling, so a direct API
call, a replayed request, or any future client (admin UI, a script, a second
bot) still reached the column and took the write down with a DataError 500.
A bot-only bound is a bound the backend does not have.

So the number lives in ``shared/staff_constants.py`` — the module that already
documents itself as the SSOT read by the staff bot, the backend and the admin
UI — and BOTH sides read that one name. These tests pin the home, the bound,
and the fact that the two sides cannot drift apart.
"""

import re
from pathlib import Path

import pytest

from business_app.models.bottle import DriverBottleSession
from shared.staff_constants import MAX_BOTTLES_PER_SESSION

pytestmark = pytest.mark.integration

OPEN_URL = "/api/v1/staff/bottles/session/open"
LEGACY_LOAD_URL = "/api/v1/staff/bottles/load"

# The storage column's own ceiling: PostgreSQL `integer` is 4 bytes, signed.
COLUMN_CEILING = 2**31 - 1

REPO_ROOT = Path(__file__).resolve().parents[2]
BOT_HANDLER = REPO_ROOT / "staff_bot" / "handlers" / "delivery" / "bottle_collection.py"


# --------------------------------------------------------------------------- #
# 1. One home
# --------------------------------------------------------------------------- #


def test_the_ceiling_is_defined_once_in_shared_staff_constants():
    """MOVED, not copied.

    A second literal in the bot would be a rule with two expressions: raising
    the fleet's ceiling in one place and not the other silently re-opens the
    hole on whichever side was forgotten. `shared/staff_constants.py` holds the
    number; the bot imports it.
    """
    source = BOT_HANDLER.read_text()
    assert not re.search(r"^MAX_BOTTLES_PER_SESSION\s*=", source, re.MULTILINE), (
        "the staff bot still defines its own copy of the ceiling — import the "
        "one in shared/staff_constants.py instead"
    )
    assert "from shared.staff_constants import" in source, (
        "the staff bot must read the ceiling from the shared SSOT module"
    )

    from staff_bot.handlers.delivery import bottle_collection

    assert bottle_collection.MAX_BOTTLES_PER_SESSION is MAX_BOTTLES_PER_SESSION


def test_the_ceiling_sits_far_below_what_the_column_can_hold():
    """The bound exists to catch a keypad slip, not to fence the column.

    Re-derived from the live column type so that changing the column forces
    this bound to be reconsidered rather than silently outgrown.
    """
    assert DriverBottleSession.__table__.c.bottles_loaded.type.python_type is int, (
        "bottles_loaded is no longer an integer column — re-derive this bound"
    )
    assert 0 < MAX_BOTTLES_PER_SESSION < COLUMN_CEILING


# --------------------------------------------------------------------------- #
# 2. The backend refuses what the bot refuses
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("url", [OPEN_URL, LEGACY_LOAD_URL])
def test_the_backend_refuses_a_load_out_the_column_cannot_hold(
    client, db, driver_auth_headers, delivery_driver, url
):
    """A typed phone number posted straight at the API — no bot in the path.

    Both the current endpoint and the deprecated shim share the serializer, so
    both inherit the bound.
    """
    resp = client.post(url, json={"bottles_loaded": 40_000_000_000}, headers=driver_auth_headers)

    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert DriverBottleSession.query.filter_by(driver_user_id=delivery_driver.id).count() == 0, (
        "a count the storage column cannot hold reached the database anyway"
    )


def test_the_backend_refuses_one_bottle_past_the_ceiling(
    client, db, driver_auth_headers, delivery_driver
):
    """The refusal boundary is the shared constant itself, not some other number."""
    resp = client.post(
        OPEN_URL,
        json={"bottles_loaded": MAX_BOTTLES_PER_SESSION + 1},
        headers=driver_auth_headers,
    )

    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert DriverBottleSession.query.filter_by(driver_user_id=delivery_driver.id).count() == 0


def test_a_load_out_at_the_ceiling_is_still_accepted(
    client, db, driver_auth_headers, delivery_driver
):
    """This bound catches typos; it must not turn away a truck that really is full."""
    resp = client.post(
        OPEN_URL,
        json={"bottles_loaded": MAX_BOTTLES_PER_SESSION},
        headers=driver_auth_headers,
    )

    assert resp.status_code == 201, resp.get_data(as_text=True)
    session = DriverBottleSession.query.filter_by(driver_user_id=delivery_driver.id).one()
    assert session.bottles_loaded == MAX_BOTTLES_PER_SESSION


def test_the_lower_bound_the_ceiling_was_added_beside_still_holds(
    client, db, driver_auth_headers, delivery_driver
):
    """Regression fence: adding `le=` must not have loosened `gt=0`."""
    resp = client.post(OPEN_URL, json={"bottles_loaded": 0}, headers=driver_auth_headers)

    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert DriverBottleSession.query.filter_by(driver_user_id=delivery_driver.id).count() == 0
