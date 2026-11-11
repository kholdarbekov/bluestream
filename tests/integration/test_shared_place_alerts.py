"""The shared-place Slack alert: queued once per new case after the delivery commits, posted at most once.

Driven through the two HTTP paths that deliver with returns (the driver's status PUT with the staff bot's body, the
admin Orders status PUT). Slack and the Celery publish are recorded with signature-enforcing spies; nothing real.
"""

import inspect
import logging
import os
from datetime import UTC, datetime

import pytest
from flask import current_app
from sqlalchemy import event

from business_app.models.shared_place_case import SharedPlaceCase
from business_app.services.shared_place import alerts, detection
from business_app.services.shared_place.alerts import SharedPlaceAlerts
from business_app.services.shared_place.detection import SharedPlaceDetection
from business_app.tasks import customer_link_tasks
from business_app.utils import slack_alerts
from tests.integration.shared_place_builders import (
    BASE_LAT,
    BASE_LNG,
    customer,
    driver,
    pinned_address,
    stop_for,
)
from tests.integration.reorder_reminder_builders import captured_logs
from tests.integration.test_shared_place_detection import _admin_delivers, _cases, _driver_delivers
from tests.unit.test_sales_agent_tasks import _spy

pytestmark = pytest.mark.integration


def _celery_apply_async(
    self, args=None, kwargs=None, task_id=None, producer=None, link=None, link_error=None, shadow=None, **options
):
    """celery.app.task.Task.apply_async's signature (celery 5)."""


@pytest.fixture
def published(monkeypatch):
    """Recorder of the alert task's apply_async: (args, kwargs, options) per publish; binds the real signature."""
    task = customer_link_tasks.post_shared_place_case_alert_task
    # tests/conftest.py replaces Task.apply_async with a (*args, **kwargs) mock, so the real signature is spelled out
    signature = inspect.signature(_celery_apply_async)
    calls = []

    def fake(*args, **kwargs):
        bound = signature.bind(None, *args, **kwargs)
        calls.append(
            (
                tuple(bound.arguments.get("args") or ()),
                dict(bound.arguments.get("kwargs") or {}),
                dict(bound.arguments.get("options") or {}),
            )
        )

    monkeypatch.setattr(task, "apply_async", fake)
    return calls


@pytest.fixture
def slack(monkeypatch):
    """The house signature-enforcing spy on `slack_alerts.send_slack_alert`; answers True. Returns its calls."""
    return _spy(monkeypatch, slack_alerts, "send_slack_alert", result=True)


def _driver_case(db, *, first="Jasur", last="Karimov", note=None):
    u = customer(db, first)
    u.last_name = last
    address = pinned_address(db, u, BASE_LAT, BASE_LNG)
    drv = driver(db, "Botir")
    order, delivery = stop_for(u, address, drv, quantity=1)
    response = _driver_delivers(drv, delivery, 3)
    assert response.status_code == 200, response.get_json()
    [case] = _cases()
    if note is not None:
        case.driver_note = note
        db.session.commit()
    return u, address, drv, order, delivery, case


def _expected_text(case, driver_line):
    from zoneinfo import ZoneInfo

    from shared.constants import DISPLAY_TIMEZONE

    admin_site = (current_app.config.get("ADMIN_UI_BASE_URL") or "").rstrip("/")
    now = datetime.now(ZoneInfo(DISPLAY_TIMEZONE)).strftime("%Y-%m-%d %H:%M")
    return now, (
        f"🏠 *Shared-place report* — Case #{case.id}\n"
        f"Order: {case.order.order_number} | Empties returned: {case.returned_qty}\n"
        f"Driver: {driver_line}\n"
        "Time: {time} | Env: " + os.environ.get("FLASK_ENV", "unknown") + "\n"
        "Action: review it on the admin Users page, Driver reports tab: "
        f"{admin_site}/users?tab=driver-reports&case={case.id}"
    )


# --------------------------------------------------------------------------------------------------------------------
# Queueing
# --------------------------------------------------------------------------------------------------------------------


def test_driver_case_queues_one_countdown_alert(db, published):
    *_, case = _driver_case(db)

    assert published == [((case.id,), {}, {"countdown": 900})]


def test_admin_entered_case_queues_immediate_alert(db, published, admin_claim_headers):
    u = customer(db, "Dilnoza")
    address = pinned_address(db, u, BASE_LAT, BASE_LNG)
    order, _ = stop_for(u, address, None, quantity=1)

    response = _admin_delivers(order, admin_claim_headers, 3)

    assert response.status_code == 200, response.get_json()
    [case] = _cases()
    assert case.asked_driver_id is None
    assert published == [((case.id,), {}, {"countdown": 0})]


def test_failed_case_open_queues_nothing(db, published, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("the candidate search failed")

    monkeypatch.setattr(detection, "find_candidates", boom)
    u = customer(db, "Jasur")
    address = pinned_address(db, u, BASE_LAT, BASE_LNG)
    drv = driver(db, "Botir")
    order, delivery = stop_for(u, address, drv, quantity=1)

    response = _driver_delivers(drv, delivery, 3)

    assert response.status_code == 200, response.get_json()
    assert _cases() == []
    assert published == []


def test_reopening_an_existing_case_inserts_and_queues_nothing_more(db, published):
    u, address, drv, order, delivery, case = _driver_case(db)
    assert published == [((case.id,), {}, {"countdown": 900})]
    inserts = []

    def count_inserts(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("INSERT INTO SHARED_PLACE_CASES"):
            inserts.append(statement)

    event.listen(db.engine, "before_cursor_execute", count_inserts)
    try:
        again = SharedPlaceDetection.open_for_delivery(order, asked_driver_id=drv.id)
        db.session.commit()
    finally:
        event.remove(db.engine, "before_cursor_execute", count_inserts)

    assert again is not None and again.id == case.id
    assert inserts == []
    assert SharedPlaceCase.query.count() == 1
    assert published == [((case.id,), {}, {"countdown": 900})]


# --------------------------------------------------------------------------------------------------------------------
# Posting, at most once
# --------------------------------------------------------------------------------------------------------------------


def test_post_once_posts_exactly_one_text(db, slack):
    *_, case = _driver_case(db)

    first = SharedPlaceAlerts.post_once(case.id)
    second = SharedPlaceAlerts.post_once(case.id)

    assert (first, second) == (True, False)
    [((posted,), kwargs)] = slack
    assert kwargs == {}
    _, template = _expected_text(case, "Botir — didn't answer")
    prefix, suffix = template.split("{time}")
    assert posted.startswith(prefix) and posted.endswith(suffix)
    stamp = posted[len(prefix) : len(posted) - len(suffix)]
    assert datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S %z").utcoffset().total_seconds() == 5 * 3600


OFFERED = [{"ref": {"kind": "address", "id": 99999}, "first_name": "Akmal", "empties": 8, "distance_m": 14}]


@pytest.mark.parametrize(
    "answer,candidates,line",
    [
        ("picked", OFFERED, "Botir — picked a nearby place"),
        ("none", OFFERED, "Botir — none of the nearby places"),
        # The note-only screen's "none" (spec §4.4): nothing was offered, so nothing was turned down.
        ("none", [], "Botir — no nearby places"),
        ("skipped", OFFERED, "Botir — skipped"),
        ("skipped", [], "Botir — skipped"),
    ],
)
def test_driver_line_follows_the_answer(db, answer, candidates, line):
    *_, case = _driver_case(db)
    case.driver_answer = answer
    case.answered_at = datetime.now(UTC)
    case.picked_index = 0 if answer == "picked" else None
    case.candidates = candidates
    db.session.commit()

    assert f"\nDriver: {line}\n" in SharedPlaceAlerts.build_text(case)


def test_admin_entered_case_text_says_so(db, admin_claim_headers):
    u = customer(db, "Dilnoza")
    address = pinned_address(db, u, BASE_LAT, BASE_LNG)
    order, _ = stop_for(u, address, None, quantity=1)
    assert _admin_delivers(order, admin_claim_headers, 3).status_code == 200
    [case] = _cases()

    assert "\nDriver: entered by an admin\n" in SharedPlaceAlerts.build_text(case)


def test_post_once_claims_even_when_slack_refuses(db, monkeypatch):
    slack = _spy(monkeypatch, slack_alerts, "send_slack_alert", result=False)
    *_, case = _driver_case(db)

    first = SharedPlaceAlerts.post_once(case.id)
    db.session.expire_all()
    marker = SharedPlaceCase.query.get(case.id).alert_posted_at
    second = SharedPlaceAlerts.post_once(case.id)

    assert first is False and second is False
    assert marker is not None
    assert len(slack) == 1


def test_a_refused_post_is_logged_once(db, monkeypatch, caplog):
    """Spec §9: a post that Slack refuses is logged, not retried."""
    _spy(monkeypatch, slack_alerts, "send_slack_alert", result=False)
    *_, case = _driver_case(db)

    with captured_logs(caplog, alerts.logger, logging.WARNING):
        SharedPlaceAlerts.post_once(case.id)
        SharedPlaceAlerts.post_once(case.id)

    assert [(r.levelname, r.getMessage()) for r in caplog.records if r.name == alerts.logger.name] == [
        ("WARNING", f"[SHARED_PLACE] alert_post_refused case={case.id}")
    ]


def test_an_accepted_post_logs_no_refusal(db, slack, caplog):
    *_, case = _driver_case(db)

    with captured_logs(caplog, alerts.logger, logging.WARNING):
        assert SharedPlaceAlerts.post_once(case.id) is True

    assert [r for r in caplog.records if r.name == alerts.logger.name] == []


def test_text_never_carries_customer_or_note(db, slack):
    u, address, *_, case = _driver_case(db, first="Jasur", last="Karimov", note="He lives with his aunt Nodira")

    assert SharedPlaceAlerts.post_once(case.id) is True

    [((posted,), _)] = slack
    for private in ("Jasur", "Karimov", u.phone, address.full_address, "aunt Nodira", "lives with"):
        assert private not in posted


def test_countdown_after_resolution_posts_nothing(db, slack):
    *_, case = _driver_case(db)
    case.status = "closed"
    case.decided_at = datetime.now(UTC)
    db.session.commit()

    assert SharedPlaceAlerts.post_once(case.id) is False

    assert slack == []
    db.session.expire_all()
    assert SharedPlaceCase.query.get(case.id).alert_posted_at is not None


def test_task_runs_post_once(db, slack):
    *_, case = _driver_case(db)

    assert customer_link_tasks.post_shared_place_case_alert_task.run(case.id) is True
    assert len(slack) == 1
