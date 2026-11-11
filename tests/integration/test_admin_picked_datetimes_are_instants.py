"""Admin date-time pickers send `dayjs.toISOString()` ("...T09:00:00.000Z"). The stored value must be that
instant, not the same digits re-read as Tashkent wall time (5 hours early, and 5 more on every re-save)."""

from datetime import UTC, datetime

import pytest

from business_app.models.loyalty import LoyaltyProgram, LoyaltyReward
from business_app.models.notification import NotificationCampaign
from business_app.models.product import PriceRule
from business_app.utils.timezone_utils import ensure_utc

REWARDS = "/api/v1/admin/loyalty/rewards"
CAMPAIGNS = "/api/v1/admin/notification-campaigns"
PRICE_RULES = "/api/v1/admin/price-rules"

PICKED_FROM = datetime(2026, 11, 30, 19, 0, tzinfo=UTC)
PICKED_UNTIL = datetime(2026, 12, 31, 18, 59, tzinfo=UTC)
PICKED_SEND = datetime(2026, 11, 30, 9, 0, tzinfo=UTC)


def _js_iso(moment):
    """Exactly what dayjs/Date.toISOString() produces."""
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _instant(value):
    return ensure_utc(datetime.fromisoformat(value))


@pytest.fixture
def program(db):
    row = LoyaltyProgram(name="Gold Club", is_active=True, is_default=True, uzs_per_point=250)
    db.session.add(row)
    db.session.commit()
    return row


# ---------------------------------------------------------------- loyalty rewards


def _reward_body(**over):
    body = {
        "name": "Fixed discount",
        "reward_type": "discount",
        "points_cost": 100,
        "discount_type": "fixed",
        "discount_value": "1000.00",
        "valid_from": _js_iso(PICKED_FROM),
        "valid_until": _js_iso(PICKED_UNTIL),
    }
    body.update(over)
    return body


def _create_reward(client, headers):
    resp = client.post(REWARDS, json=_reward_body(), headers=headers)
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["data"]["reward"]


def test_reward_create_stores_the_picked_instants(client, db, program, admin_claim_headers):
    reward = _create_reward(client, admin_claim_headers)

    row = db.session.get(LoyaltyReward, reward["id"])
    assert ensure_utc(row.valid_from) == PICKED_FROM
    assert ensure_utc(row.valid_until) == PICKED_UNTIL
    assert _instant(reward["valid_until"]) == PICKED_UNTIL


def test_reward_resave_of_the_edit_form_does_not_drift(client, db, program, admin_claim_headers):
    reward = _create_reward(client, admin_claim_headers)

    for points in (150, 200):
        shown = client.get(f"{REWARDS}/{reward['id']}", headers=admin_claim_headers).get_json()["data"]["reward"]
        resend = _js_iso(_instant(shown["valid_until"]))
        resp = client.put(
            f"{REWARDS}/{reward['id']}",
            json=_reward_body(points_cost=points, valid_from=_js_iso(PICKED_FROM), valid_until=resend),
            headers=admin_claim_headers,
        )
        assert resp.status_code == 200, resp.get_json()
        db.session.expire_all()
        row = db.session.get(LoyaltyReward, reward["id"])
        assert row.points_cost == points
        assert ensure_utc(row.valid_until) == PICKED_UNTIL
        assert ensure_utc(row.valid_from) == PICKED_FROM


def test_reward_clearing_valid_until_stores_null(client, db, program, admin_claim_headers):
    reward = _create_reward(client, admin_claim_headers)

    resp = client.put(f"{REWARDS}/{reward['id']}", json={"valid_until": None}, headers=admin_claim_headers)

    assert resp.status_code == 200, resp.get_json()
    db.session.expire_all()
    assert db.session.get(LoyaltyReward, reward["id"]).valid_until is None


# ---------------------------------------------------------------- notification campaigns


class _FakeAsyncResult:
    id = "task-1"


class _RecordingApplyAsync:
    """Same signature as Celery's apply_async, so a payload drift fails the test."""

    def __init__(self):
        self.calls = []

    def __call__(self, args=None, kwargs=None, **options):
        self.calls.append({"args": args, "kwargs": kwargs, "options": options})
        return _FakeAsyncResult()


@pytest.fixture
def apply_async(monkeypatch):
    fake = _RecordingApplyAsync()
    monkeypatch.setattr(
        "business_app.tasks.notification_tasks.execute_notification_campaign_task.apply_async", fake
    )
    return fake


def _campaign_body(user_id, **over):
    """`normalizeCampaignPayload` in admin_ui Notifications.js."""
    body = {
        "name": "Winter promo",
        "template_id": None,
        "notification_type": "promotional",
        "channel": "in_app",
        "subject": None,
        "content": "Hi",
        "target_audience": "custom_segment",
        "target_segment_id": None,
        "specific_user_ids": [user_id],
        "priority": "normal",
        "scheduled_at": _js_iso(PICKED_SEND),
    }
    body.update(over)
    return body


def _create_campaign(client, headers, user_id):
    resp = client.post(CAMPAIGNS, json=_campaign_body(user_id), headers=headers)
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["data"]["campaign"]


def _stored_schedule(db, campaign_id):
    db.session.expire_all()
    return ensure_utc(db.session.get(NotificationCampaign, campaign_id).scheduled_at)


def test_campaign_create_stores_the_picked_instant(client, db, sample_user, admin_claim_headers):
    campaign = _create_campaign(client, admin_claim_headers, sample_user.id)

    assert _stored_schedule(db, campaign["id"]) == PICKED_SEND
    assert _instant(campaign["scheduled_at"]) == PICKED_SEND


def test_campaign_resave_does_not_drift(client, db, sample_user, admin_claim_headers):
    campaign = _create_campaign(client, admin_claim_headers, sample_user.id)

    for name in ("Renamed once", "Renamed twice"):
        shown = client.get(f"{CAMPAIGNS}/{campaign['id']}", headers=admin_claim_headers).get_json()["data"]["campaign"]
        body = _campaign_body(sample_user.id, name=name, scheduled_at=_js_iso(_instant(shown["scheduled_at"])))
        resp = client.put(f"{CAMPAIGNS}/{campaign['id']}", json=body, headers=admin_claim_headers)
        assert resp.status_code == 200, resp.get_json()
        assert _stored_schedule(db, campaign["id"]) == PICKED_SEND


def test_schedule_button_queues_with_the_picked_eta(client, db, sample_user, admin_claim_headers, apply_async):
    campaign = _create_campaign(client, admin_claim_headers, sample_user.id)

    resp = client.post(f"{CAMPAIGNS}/{campaign['id']}/send", json={"send_now": False}, headers=admin_claim_headers)

    assert resp.status_code == 200, resp.get_json()
    assert len(apply_async.calls) == 1
    call = apply_async.calls[0]
    assert call["args"] == [campaign["id"]]
    assert ensure_utc(call["options"]["eta"]) == PICKED_SEND


def test_send_now_queues_without_an_eta(client, db, sample_user, admin_claim_headers, apply_async):
    campaign = _create_campaign(client, admin_claim_headers, sample_user.id)

    resp = client.post(f"{CAMPAIGNS}/{campaign['id']}/send", json={"send_now": True}, headers=admin_claim_headers)

    assert resp.status_code == 200, resp.get_json()
    assert len(apply_async.calls) == 1
    assert apply_async.calls[0]["options"] == {}


def test_send_with_full_body_and_scheduled_at_uses_the_picked_eta(
    client, db, sample_user, admin_claim_headers, apply_async
):
    campaign = _create_campaign(client, admin_claim_headers, sample_user.id)
    later = datetime(2026, 12, 5, 7, 30, tzinfo=UTC)
    body = _campaign_body(sample_user.id, scheduled_at=_js_iso(later), send_now=False)

    resp = client.post(f"{CAMPAIGNS}/{campaign['id']}/send", json=body, headers=admin_claim_headers)

    assert resp.status_code == 200, resp.get_json()
    assert len(apply_async.calls) == 1
    assert ensure_utc(apply_async.calls[0]["options"]["eta"]) == later


# ---------------------------------------------------------------- price rules


def test_price_rule_create_stores_the_picked_instant(client, db, sample_product, admin_claim_headers):
    body = {
        "product_id": sample_product.id,
        "rule_type": "seasonal_discount",
        "name": "Winter",
        "discount_value": "10",
        "valid_from": _js_iso(PICKED_FROM),
        "valid_until": _js_iso(PICKED_UNTIL),
    }

    resp = client.post(PRICE_RULES, json=body, headers=admin_claim_headers)

    assert resp.status_code == 201, resp.get_json()
    rule = PriceRule.query.filter_by(product_id=sample_product.id).one()
    assert ensure_utc(rule.valid_from) == PICKED_FROM
    assert ensure_utc(rule.valid_until) == PICKED_UNTIL
