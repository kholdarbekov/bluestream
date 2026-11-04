"""delivery_promises on the SQLite suite schema (honest-ETA Phase 1, spec §4.10 and §4.12a).

SQLite enforces the CHECKs and the partial unique index built from the model, so the shape rules
are visible here too. The Postgres-only proof (the migration's own DDL: every constraint by name,
the frozen vocabulary, the index predicate) is tests/integration/test_delivery_promise_pg_constraints.py.
"""

import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from business_app.models.delivery import Delivery, DeliveryStatusHistory
from business_app.models.delivery_promise import (
    MODEL_GOOGLE_TAP,
    MODEL_OSRM_SHADOW,
    PROMISE_DAY_KINDS,
    PROMISE_STAGES,
    DeliveryPromise,
)
from business_app.utils.timezone_utils import ensure_utc
from shared.enums import DeliveryStatus

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 3, 5, 0, tzinfo=UTC)
START = NOW + timedelta(minutes=30)
END = NOW + timedelta(minutes=90)
# SQLite names the columns of a unique index it refused, never the index.
ONE_ROW_PER_MODEL_PER_TAP = re.escape(
    "UNIQUE constraint failed: delivery_promises.delivery_status_history_id, delivery_promises.model_version"
)


@pytest.fixture
def tap(db, sample_order, delivery_driver):
    """A delivery that is on its way, and the history row of the tap that put it there."""
    delivery = Delivery(
        order_id=sample_order.id,
        delivery_person_id=delivery_driver.id,
        status=DeliveryStatus.IN_TRANSIT,
        scheduled_date=NOW,
        scheduled_time_slot="09:00-12:00",
    )
    db.session.add(delivery)
    db.session.flush()
    history = DeliveryStatusHistory(
        delivery_id=delivery.id,
        old_status=DeliveryStatus.PICKED_UP,
        new_status=DeliveryStatus.IN_TRANSIT,
        changed_by=delivery_driver.id,
    )
    db.session.add(history)
    db.session.commit()
    return SimpleNamespace(delivery_id=delivery.id, history_id=history.id)


def _promise(tap, **columns):
    values = {"delivery_id": tap.delivery_id, "stage": "on_the_way", "model_version": MODEL_GOOGLE_TAP}
    return DeliveryPromise(**{**values, **columns})


def _refused_by(db, row, constraint_pattern):
    """Commit `row`, expect SQLite to name the rule it broke, and leave the session clean."""
    db.session.add(row)
    with pytest.raises(IntegrityError, match=constraint_pattern):
        db.session.commit()
    db.session.rollback()


def test_a_minimal_promise_gets_its_defaults(db, tap):
    db.session.add(_promise(tap))
    db.session.commit()

    stored = DeliveryPromise.query.one()
    assert stored.shown is False
    assert stored.computed_at is not None
    assert abs(datetime.now(UTC) - ensure_utc(stored.computed_at)) < timedelta(minutes=1)
    assert (
        stored.delivery_status_history_id,
        stored.expected_at,
        stored.window_start,
        stored.window_end,
        stored.day_kind,
        stored.stops_ahead,
        stored.day_started,
        stored.drivers_working,
        stored.source,
    ) == (None,) * 9


def test_an_unknown_stage_is_refused(db, tap):
    _refused_by(db, _promise(tap, stage="paused"), r"\bck_delivery_promises_stage\b")

    assert DeliveryPromise.query.count() == 0


def test_an_unknown_day_kind_is_refused(db, tap):
    _refused_by(db, _promise(tap, day_kind="someday"), r"\bck_delivery_promises_day_kind\b")

    assert DeliveryPromise.query.count() == 0


def test_a_window_that_ends_before_it_starts_is_refused(db, tap):
    _refused_by(db, _promise(tap, window_start=END, window_end=START), r"\bck_delivery_promises_window_order\b")

    assert DeliveryPromise.query.count() == 0


@pytest.mark.parametrize("stage", PROMISE_STAGES)
def test_every_documented_stage_commits(db, tap, stage):
    db.session.add(_promise(tap, stage=stage))
    db.session.commit()

    assert DeliveryPromise.query.one().stage == stage


@pytest.mark.parametrize("day_kind", (*PROMISE_DAY_KINDS, None), ids=lambda value: str(value))
def test_every_documented_day_kind_and_none_commits(db, tap, day_kind):
    db.session.add(_promise(tap, day_kind=day_kind))
    db.session.commit()

    assert DeliveryPromise.query.one().day_kind == day_kind


@pytest.mark.parametrize(
    "start, end",
    [(START, END), (START, START), (START, None), (None, END), (None, None)],
    ids=["ordered", "single-instant", "open-ended", "open-started", "no-window"],
)
def test_a_window_may_be_ordered_open_ended_or_a_single_instant(db, tap, start, end):
    db.session.add(_promise(tap, window_start=start, window_end=end))
    db.session.commit()

    stored = DeliveryPromise.query.one()
    assert [None if value is None else ensure_utc(value) for value in (stored.window_start, stored.window_end)] == [
        start,
        end,
    ]


def test_one_tap_records_one_row_per_model(db, tap):
    shown = _promise(tap, delivery_status_history_id=tap.history_id, model_version=MODEL_GOOGLE_TAP, shown=True)
    db.session.add(shown)
    db.session.commit()

    # The notification worker re-running the same tap must not leave a second row for that model.
    _refused_by(
        db,
        _promise(tap, delivery_status_history_id=tap.history_id, model_version=MODEL_GOOGLE_TAP),
        ONE_ROW_PER_MODEL_PER_TAP,
    )

    # The same tap's hidden shadow is another model, so it has its own row.
    db.session.add(_promise(tap, delivery_status_history_id=tap.history_id, model_version=MODEL_OSRM_SHADOW))
    db.session.commit()

    # A row that belongs to no tap is outside the index: any number of them commit.
    db.session.add_all(
        [
            _promise(tap, delivery_status_history_id=None, model_version=MODEL_GOOGLE_TAP),
            _promise(tap, delivery_status_history_id=None, model_version=MODEL_GOOGLE_TAP),
        ]
    )
    db.session.commit()

    of_the_tap = DeliveryPromise.query.filter_by(delivery_status_history_id=tap.history_id).all()
    assert sorted((row.model_version, row.shown) for row in of_the_tap) == [
        (MODEL_GOOGLE_TAP, True),
        (MODEL_OSRM_SHADOW, False),
    ]
    assert DeliveryPromise.query.filter(DeliveryPromise.delivery_status_history_id.is_(None)).count() == 2
