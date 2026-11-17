"""The price-change tick (spec §5.5): the beat task runs the switch and reports each step, never raising.

The tick calls the real switch on the real rows; a failing step is replaced by a recorder with the switch's real
signature. Tests never reach real Sentry, Slack or Telegram.
"""

import logging
from datetime import timedelta

import pytest

from business_app.models.price_change import PriceChangeStatus
from business_app.services.price_change import switch
from business_app.tasks import price_change_tasks
from business_app.tasks.price_change_tasks import price_change_tick
from business_app.utils.local_windows import local_date
from tests.integration.price_change_builders import (
    CAP_10,
    CAP_19,
    NEW_10,
    NEW_19,
    bottles,
    make_admin,
    make_price_change,
)
from tests.integration.reorder_reminder_builders import captured_logs

pytestmark = pytest.mark.integration


def test_the_tick_applies_a_due_change(app, db):
    b = bottles(db)
    change_on = local_date()
    price_change = make_price_change(
        db,
        [(b.p19, NEW_19, CAP_19, None), (b.p10, NEW_10, CAP_10, None)],
        created_by=make_admin(db),
        announce_on=change_on - timedelta(days=28),
        change_on=change_on,
        status=PriceChangeStatus.ANNOUNCED,
    )
    db.session.commit()

    result = price_change_tick()

    assert result["applied"] == [price_change.id]


def test_a_failing_step_is_reported_not_raised(app, db, monkeypatch, caplog):
    def boom(now=None):
        raise RuntimeError("database gone")

    monkeypatch.setattr(switch, "apply_due_price_changes", boom)

    with captured_logs(caplog, price_change_tasks.logger):
        result = price_change_tick()

    assert result["applied"] == "failed"
    assert [key for key, value in result.items() if key != "applied" and value == "failed"] == []
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1
    assert errors[0].getMessage() == "price_change.tick: step applied failed"
