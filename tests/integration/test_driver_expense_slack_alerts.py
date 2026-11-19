"""Slack for driver cash expenses (spec 2026-11-17 §6.7): one post per new claim, and the daily digest.

The claim goes in through the route the staff bot calls; the posts are read from the Slack recorder, with the
seeded English category rows in the translations table.
"""

import importlib.util
import inspect
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock
from unittest.mock import Mock

import pytest
from celery.schedules import crontab

from business_app.models.translation import Translation
from business_app.tasks.staff_tasks import alert_driver_expense_claimed, driver_expense_digest
from shared.staff_constants import DRIVER_EXPENSE_CATEGORIES
from tests.integration._driver_expense_helpers import claim, insert_expense
from tests.integration._prepayment_payout_helpers import collect_on_trip
from tests.unit.test_delivery_service_business_rules import _unshadow_task_publish

pytestmark = pytest.mark.integration

_spec = importlib.util.spec_from_file_location(
    "seed_staff_translations_expense_alerts",
    Path(__file__).resolve().parents[2] / "scripts" / "seed_staff_translations.py",
)
_SEED = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_SEED)

CLAIMED = "staff.alert_driver_expense_claimed"


@pytest.fixture
def enqueued(monkeypatch):
    """Every Celery publish as `(task name, args, kwargs)`, each bound against the task's own signature."""
    from celery.app.task import Task

    _unshadow_task_publish(monkeypatch)
    calls = []

    def _record(task, args, kwargs):
        inspect.signature(task.run).bind(*args, **kwargs)
        calls.append((task.name, tuple(args), dict(kwargs)))
        return Mock(id="mock-task-id")

    monkeypatch.setattr(Task, "delay", lambda task, *a, **k: _record(task, a, k))
    monkeypatch.setattr(Task, "apply_async", lambda task, args=None, kwargs=None, **o: _record(task, args or (), kwargs or {}))
    return calls


@pytest.fixture
def posts(monkeypatch):
    sent = []
    monkeypatch.setattr("business_app.utils.slack_alerts.send_slack_alert", lambda text: sent.append(text) or True)
    return sent


@pytest.fixture
def labels(app, db):
    """The seeded English category rows, written the way the seed script writes them."""
    for category in DRIVER_EXPENSE_CATEGORIES:
        key = f"staff.expense.category.{category}"
        db.session.add(Translation(key=key, language="en", value=_SEED._curated_value(key, "en"), category="staff_bot"))
    db.session.commit()


def _trip(client, admin_claim_headers, sample_user, driver):
    return collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 420000)


def test_a_new_claim_publishes_one_alert_after_commit_and_a_resend_none(
    app, client, db, enqueued, admin_claim_headers, sample_user, second_delivery_driver
):
    _trip(client, admin_claim_headers, sample_user, second_delivery_driver)

    status, body = claim(client, app, second_delivery_driver, 150000, key="k1")
    claim(client, app, second_delivery_driver, 150000, key="k1")

    assert status == 201, body
    assert [c for c in enqueued if c[0] == CLAIMED] == [(CLAIMED, (body["data"]["expense"]["id"],), {})]


def test_the_claim_post_names_driver_trip_amount_and_link(
    app, client, db, labels, posts, monkeypatch, admin_claim_headers, sample_user, second_delivery_driver
):
    monkeypatch.setitem(app.config, "ADMIN_UI_BASE_URL", "https://admin.example/")
    sid = _trip(client, admin_claim_headers, sample_user, second_delivery_driver)
    _, body = claim(client, app, second_delivery_driver, 150000, key="k1")
    expense_id = body["data"]["expense"]["id"]

    assert alert_driver_expense_claimed.run(expense_id) is True

    assert len(posts) == 1
    lines = posts[0].split("\n")
    assert lines[0] == f"💵 *Driver expense waiting* — #{expense_id}"
    assert lines[1] == f"Driver: Second Driver | Trip #{sid}"
    assert lines[2] == "Fuel: 150,000 so'm"
    assert lines[3].startswith("Time: ") and " | Env: " in lines[3]
    assert lines[4] == (
        f"Action: decide it on the admin Driver expenses page: https://admin.example/delivery/expenses?id={expense_id}"
    )
    assert posts[0].endswith(f"https://admin.example/delivery/expenses?id={expense_id}")


def test_an_unknown_claim_posts_nothing(app, db, posts):
    assert alert_driver_expense_claimed.run(987654) is False
    assert posts == []


def test_the_digest_lists_only_claims_waiting_over_24_hours(
    app, client, db, labels, posts, monkeypatch, admin_claim_headers, sample_user, second_delivery_driver
):
    monkeypatch.setitem(app.config, "ADMIN_UI_BASE_URL", "https://admin.example")
    driver = second_delivery_driver
    sid = _trip(client, admin_claim_headers, sample_user, driver)
    now = datetime.now(UTC)
    old = insert_expense(db, sid, driver.id, 50000, submitted_at=now - timedelta(hours=25))
    fresh = insert_expense(db, sid, driver.id, 40000, submitted_at=now - timedelta(hours=23))
    done = insert_expense(db, sid, driver.id, 30000, status="approved", submitted_at=now - timedelta(hours=30))

    assert driver_expense_digest.run() == 1

    assert len(posts) == 1
    lines = posts[0].split("\n")
    assert lines == [
        "⏳ *Driver expenses waiting over 24 h* — 1",
        f"#{old} Second Driver · Fuel · 50,000 so'm · waiting 25 h",
        "Action: https://admin.example/delivery/expenses",
    ]
    assert f"#{fresh} " not in posts[0] and f"#{done} " not in posts[0]


def test_the_digest_lists_the_oldest_first(
    app, client, db, labels, posts, admin_claim_headers, sample_user, second_delivery_driver
):
    driver = second_delivery_driver
    sid = _trip(client, admin_claim_headers, sample_user, driver)
    now = datetime.now(UTC)
    newer = insert_expense(db, sid, driver.id, 10000, submitted_at=now - timedelta(hours=26))
    older = insert_expense(db, sid, driver.id, 20000, submitted_at=now - timedelta(hours=49))

    assert driver_expense_digest.run() == 2

    lines = posts[0].split("\n")
    assert lines[0].endswith("— 2")
    assert lines[1].startswith(f"#{older} ") and lines[1].endswith("waiting 49 h")
    assert lines[2].startswith(f"#{newer} ") and lines[2].endswith("waiting 26 h")


def test_an_empty_digest_posts_nothing(app, db, labels, posts):
    assert driver_expense_digest.run() == 0
    assert posts == []


def test_the_digest_runs_daily_at_09_30(app):
    # Importing the module builds the app itself, so it gets the test app (as test_celery_task_wiring_cleanup does).
    if "business_app.tasks.celery_app" not in sys.modules:
        with mock.patch("business_app.create_app", return_value=app):
            import business_app.tasks.celery_app  # noqa: F401
    celery = sys.modules["business_app.tasks.celery_app"].celery
    assert celery.conf.beat_schedule["driver-expense-waiting-digest"] == {
        "task": "staff.driver_expense_digest",
        "schedule": crontab(hour=9, minute=30),
    }


def test_the_slack_label_is_the_seeded_english_translation(
    app, client, db, labels, posts, admin_claim_headers, sample_user, second_delivery_driver
):
    driver = second_delivery_driver
    sid = _trip(client, admin_claim_headers, sample_user, driver)
    status, body = claim(client, app, driver, 80000, key="v1", category="vehicle")
    assert status == 201, body
    insert_expense(
        db, sid, driver.id, 60000, category="vehicle", submitted_at=datetime.now(UTC) - timedelta(hours=30)
    )

    assert alert_driver_expense_claimed.run(body["data"]["expense"]["id"]) is True
    assert driver_expense_digest.run() == 1

    assert "Car repair: 80,000 so'm" in posts[0]
    assert "vehicle" not in posts[0]
    assert " · Car repair · 60,000 so'm · waiting 30 h" in posts[1]
