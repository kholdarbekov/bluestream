"""The session bottle counts are bounded at the SERVICE, not only over HTTP.

Wave 1 bounded the truck load-out and wave 3 bounded the end-of-shift return,
each in the two places a driver's keypad slip could reach them: the staff bot,
where the typist can still be told what was wrong with what they typed, and the
pydantic request bodies, where a direct API call, a replayed request or any
future client is refused.

Both waves stopped at the HTTP line. ``BottleTrackingService`` is what sits
BEHIND that line, and it guarded only the floor —
``open_bottle_session`` refused ``bottles_loaded <= 0`` and
``close_bottle_session`` refused ``bottles_returned_to_warehouse < 0``, with no
ceiling on either. Every caller that does not come through a serializer (a
Celery task, an admin path, a script, a fixture that graduates into production
code) therefore still put an unbounded count on a 4-byte PostgreSQL integer and
took the write down with a DataError 500 carrying no hint. A guard that only
exists at the boundary is not a backstop.

So the service reads the SAME two names the serializers and the bot read —
``MAX_BOTTLES_PER_SESSION`` for the load-out, ``BOTTLE_RETURN_COLUMN_CEILING``
for the return — and raises the SAME ``ValidationError`` its floor guards
already raise, so a caller sees one shape whichever end of the range it missed.

The return bound is a STORAGE bound and must stay one: over-returning is
legitimate business behaviour on this side (``tests/unit/
test_staff_bot_over_returned.py`` pins an over-returned place as a first-class
state), so the tests below assert that a wildly over-returned shift still
closes.
"""

import pytest

from business_app.models.bottle import DriverBottleSession
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.utils.exceptions import ValidationError
from shared.enums import DriverBottleSessionStatus, UserRole, UserType
from shared.staff_constants import BOTTLE_RETURN_COLUMN_CEILING, MAX_BOTTLES_PER_SESSION
from business_app.utils.password_security import hash_password

# The storage column's own ceiling: PostgreSQL `integer` is 4 bytes, signed.
COLUMN_CEILING = 2**31 - 1

# A typed phone number: the real shape of the slip these bounds exist for.
KEYPAD_SLIP = 40_000_000_000


def _make_driver(db, phone: str, first_name: str = "Driver"):
    """A COMMITTED driver.

    `@transactional` rolls the whole session back when the guard fires, so a
    merely-flushed driver would vanish with the refused write and the
    "nothing was written" assertions would pass vacuously.
    """
    from datetime import datetime, UTC

    from business_app.models.user import User

    user = User(
        phone=phone,
        first_name=first_name,
        last_name="Ceiling",
        password_hash=hash_password("TestPassword123!"),
        role=UserRole.DELIVERY_DRIVER,
        user_type=UserType.STAFF,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    db.session.add(user)
    db.session.commit()
    return user


# --------------------------------------------------------------------------- #
# 1. The load-out ceiling
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_open_bottle_session_refuses_a_load_out_the_column_cannot_hold(db):
    """No serializer in the path — the service itself must refuse it."""
    driver = _make_driver(db, "+998901700001")
    svc = BottleTrackingService()

    with pytest.raises(ValidationError):
        svc.open_bottle_session(driver.id, KEYPAD_SLIP)

    assert DriverBottleSession.query.filter_by(driver_user_id=driver.id).count() == 0, (
        "a count the storage column cannot hold reached the database anyway"
    )


@pytest.mark.unit
def test_open_bottle_session_refuses_one_bottle_past_the_shared_ceiling(db):
    """The refusal boundary is the shared constant itself, not some other number."""
    driver = _make_driver(db, "+998901700002")
    svc = BottleTrackingService()

    with pytest.raises(ValidationError):
        svc.open_bottle_session(driver.id, MAX_BOTTLES_PER_SESSION + 1)

    assert DriverBottleSession.query.filter_by(driver_user_id=driver.id).count() == 0


@pytest.mark.unit
def test_open_bottle_session_still_accepts_a_full_truck_at_the_ceiling(db):
    """This bound catches typos; it must not turn away a truck that really is full."""
    driver = _make_driver(db, "+998901700003")
    svc = BottleTrackingService()

    session = svc.open_bottle_session(driver.id, MAX_BOTTLES_PER_SESSION)

    assert session.bottles_loaded == MAX_BOTTLES_PER_SESSION
    assert session.status == DriverBottleSessionStatus.OPEN


@pytest.mark.unit
def test_open_bottle_session_floor_guard_still_holds(db):
    """Regression fence: adding the ceiling must not have loosened `<= 0`."""
    driver = _make_driver(db, "+998901700004")
    svc = BottleTrackingService()

    with pytest.raises(ValidationError):
        svc.open_bottle_session(driver.id, 0)

    assert DriverBottleSession.query.filter_by(driver_user_id=driver.id).count() == 0


# --------------------------------------------------------------------------- #
# 2. The return bound — storage, never business
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_close_bottle_session_refuses_a_return_the_column_cannot_hold(db):
    """The closing half of the same session, reached without a serializer."""
    driver = _make_driver(db, "+998901700005")
    svc = BottleTrackingService()
    svc.open_bottle_session(driver.id, 10)

    with pytest.raises(ValidationError):
        svc.close_bottle_session(driver.id, KEYPAD_SLIP)

    session = DriverBottleSession.query.filter_by(driver_user_id=driver.id).one()
    assert session.status == DriverBottleSessionStatus.OPEN, (
        "a count the storage column cannot hold closed the session anyway"
    )
    assert session.bottles_returned_to_warehouse is None


@pytest.mark.unit
def test_close_bottle_session_refuses_one_bottle_past_the_shared_bound(db):
    """The refusal boundary is `BOTTLE_RETURN_COLUMN_CEILING` itself."""
    driver = _make_driver(db, "+998901700006")
    svc = BottleTrackingService()
    svc.open_bottle_session(driver.id, 10)

    with pytest.raises(ValidationError):
        svc.close_bottle_session(driver.id, BOTTLE_RETURN_COLUMN_CEILING + 1)

    session = DriverBottleSession.query.filter_by(driver_user_id=driver.id).one()
    assert session.status == DriverBottleSessionStatus.OPEN


@pytest.mark.unit
def test_close_bottle_session_still_records_a_real_over_return(db):
    """The whole reason this bound is NOT `MAX_BOTTLES_PER_SESSION`.

    Ten bottles left the warehouse; a thousand empties came back off doorsteps.
    That is a real day, and the service must record it rather than argue with
    it — the same claim `tests/unit/test_staff_bot_over_returned.py` makes about
    a single place.
    """
    driver = _make_driver(db, "+998901700007")
    svc = BottleTrackingService()
    svc.open_bottle_session(driver.id, 10)

    session = svc.close_bottle_session(driver.id, MAX_BOTTLES_PER_SESSION * 2)

    assert session.bottles_returned_to_warehouse == MAX_BOTTLES_PER_SESSION * 2
    assert session.status == DriverBottleSessionStatus.CLOSED


@pytest.mark.unit
def test_close_bottle_session_accepts_the_bound_itself_and_the_derived_value_fits(db):
    """The bound admits everything up to it — and what the close DERIVES from
    it has to fit the same 4-byte width, or the guard has merely moved the crash
    one column to the right (that headroom is why the constant is the column
    ceiling LESS one full load-out)."""
    driver = _make_driver(db, "+998901700008")
    svc = BottleTrackingService()
    svc.open_bottle_session(driver.id, MAX_BOTTLES_PER_SESSION)

    session = svc.close_bottle_session(driver.id, BOTTLE_RETURN_COLUMN_CEILING)

    assert session.bottles_returned_to_warehouse == BOTTLE_RETURN_COLUMN_CEILING
    assert -COLUMN_CEILING - 1 <= session.discrepancy <= COLUMN_CEILING


@pytest.mark.unit
def test_close_bottle_session_floor_guard_still_holds(db):
    """Regression fence: adding the ceiling must not have loosened `< 0`.

    Zero is a real answer (a driver who sold everything); minus one is a typo
    that would become a credit.
    """
    driver = _make_driver(db, "+998901700009")
    svc = BottleTrackingService()
    svc.open_bottle_session(driver.id, 10)

    with pytest.raises(ValidationError):
        svc.close_bottle_session(driver.id, -1)

    closed = svc.close_bottle_session(driver.id, 0)
    assert closed.status == DriverBottleSessionStatus.CLOSED
    assert closed.bottles_returned_to_warehouse == 0


# --------------------------------------------------------------------------- #
# 3. The bound belongs to the FIELD, not to the caller
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_admin_force_close_writes_the_same_column_and_carries_the_same_bound(db):
    """`admin_force_close_session` writes `bottles_returned_to_warehouse` too.

    `AdminForceCloseSessionRequest` already bounds the admin's HTTP path; the
    service backstop has to cover the same field for the same reason the
    driver's does, or the third writer keeps the 500 the other two lost.
    """
    driver = _make_driver(db, "+998901700010")
    svc = BottleTrackingService()
    opened = svc.open_bottle_session(driver.id, 10)

    with pytest.raises(ValidationError):
        svc.admin_force_close_session(
            opened.id,
            driver.id,
            bottles_returned_to_warehouse=KEYPAD_SLIP,
            reason="abandoned",
        )

    session = DriverBottleSession.query.filter_by(driver_user_id=driver.id).one()
    assert session.status == DriverBottleSessionStatus.OPEN
    assert session.bottles_returned_to_warehouse is None
