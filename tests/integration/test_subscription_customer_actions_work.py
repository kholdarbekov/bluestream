"""The customer subscription actions the bot and the website offer must work end to end.

Pause / edit / change-payment-method used to commit and notify, then answer 500 (the
camelCase `SubscriptionSchema` cannot validate an ORM row). The pause call also failed
400 because both clients sent it without a body. Skip only wrote a log line, so the
order was still created on the old date. Since the single-frequency change (plan 2026-10-24
Task 2) the clock is `next_delivery_date` and the bot's "change frequency" really saves.

Driven through the HTTP endpoints with the bodies the clients actually send.
"""

import json
from datetime import timedelta
from unittest.mock import patch

import pytest

from business_app.models.order import Order
from business_app.models.subscription import Subscription, SubscriptionStatus
from business_app.models.user import UserAddress
from business_app.tasks.subscription_tasks import process_daily_subscription_billing
from business_app.utils.local_windows import local_date
from shared.enums import SubscriptionFrequency

BASE = "/api/v1/subscriptions"


@pytest.fixture(autouse=True)
def _no_external_io():
    with patch("business_app.tasks.subscription_tasks.create_subscription_delivery_task.delay"), patch(
        "business_app.api.subscriptions.get_notification_service"
    ) as notifier:
        yield notifier


@pytest.fixture
def notifier(_no_external_io):
    return _no_external_io


@pytest.fixture
def sub_id(client, auth_headers, user_address, sample_product):
    """Created exactly as the bot creates one (telegram_bot/handlers/subscriptions.py)."""
    resp = client.post(
        f"{BASE}/",
        headers=auth_headers,
        json={
            "name": "Subscription - Weekly",
            "frequency": "weekly",
            "delivery_address_id": user_address.id,
            "payment_method": "cash",
            "items": [{"product_id": sample_product.id, "quantity": 2}],
            "auto_renew": True,
            "delivery_time_slot_id": None,
        },
    )
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["data"]["subscription"]["id"]


def _sub(db, sub_id):
    db.session.expire_all()
    return db.session.get(Subscription, sub_id)


def _assert_json_ok(resp):
    assert resp.status_code != 500, resp.get_data(as_text=True)
    assert resp.is_json
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


@pytest.mark.integration
class TestPauseResume:
    def test_pause_with_the_empty_body_both_clients_send(self, client, db, auth_headers, sub_id):
        resp = client.post(f"{BASE}/{sub_id}/pause", headers=auth_headers, json={})

        body = _assert_json_ok(resp)
        assert body["data"]["subscription"]["id"] == sub_id
        assert _sub(db, sub_id).status == SubscriptionStatus.PAUSED

    def test_pause_without_any_body_is_still_a_400(self, client, db, auth_headers, sub_id):
        """Documents why the clients must send `{}`."""
        resp = client.post(f"{BASE}/{sub_id}/pause", headers=auth_headers)

        assert resp.status_code == 400
        assert _sub(db, sub_id).status == SubscriptionStatus.ACTIVE

    def test_resume_after_pause(self, client, db, auth_headers, sub_id):
        _assert_json_ok(client.post(f"{BASE}/{sub_id}/pause", headers=auth_headers, json={}))

        body = _assert_json_ok(client.post(f"{BASE}/{sub_id}/resume", headers=auth_headers))

        assert body["success"] is True
        assert _sub(db, sub_id).status == SubscriptionStatus.ACTIVE


@pytest.mark.integration
class TestEdit:
    def test_change_delivery_address(self, client, db, auth_headers, sample_user, sub_id):
        other = UserAddress(
            user_id=sample_user.id,
            full_address="2 Test St, Tashkent",
            street_address="2 Test St",
            city="Tashkent",
            latitude=41.3112,
            longitude=69.2798,
        )
        db.session.add(other)
        db.session.commit()

        resp = client.put(f"{BASE}/{sub_id}", headers=auth_headers, json={"delivery_address_id": other.id})

        _assert_json_ok(resp)
        assert _sub(db, sub_id).delivery_address_id == other.id

    def test_turn_auto_renew_off(self, client, db, auth_headers, sub_id):
        resp = client.put(f"{BASE}/{sub_id}", headers=auth_headers, json={"auto_renew": False})

        _assert_json_ok(resp)
        assert _sub(db, sub_id).auto_renew is False


@pytest.mark.integration
class TestChangePaymentMethod:
    def test_bots_exact_body(self, client, db, auth_headers, sub_id):
        """telegram_bot: `payment_data = {'payment_method': payment_method}`."""
        resp = client.post(
            f"{BASE}/{sub_id}/change-payment-method",
            headers=auth_headers,
            json={"payment_method": "click"},
        )

        _assert_json_ok(resp)
        assert _sub(db, sub_id).payment_method.value == "click"


@pytest.mark.integration
class TestSkipNextDelivery:
    def _make_due_tomorrow(self, db, sub_id):
        sub = _sub(db, sub_id)
        sub.next_delivery_date = local_date() + timedelta(days=1)
        db.session.commit()
        return _sub(db, sub_id).next_delivery_date

    def test_skip_moves_the_next_delivery_one_cycle_and_the_order_follows(
        self, app, client, db, auth_headers, sub_id, notifier
    ):
        original = self._make_due_tomorrow(db, sub_id)

        resp = client.post(f"{BASE}/{sub_id}/skip-next-delivery", headers=auth_headers, json={})

        body = _assert_json_ok(resp)
        new_date = _sub(db, sub_id).next_delivery_date
        assert new_date == original + timedelta(weeks=1)

        # The response and the notification quote the date that really happens.
        assert body["data"]["new_next_delivery_date"] == new_date.isoformat()
        sent = json.dumps(notifier.return_value.send_notification.call_args.kwargs, default=str)
        assert new_date.isoformat() in sent

        # On the original date no order is made...
        process_daily_subscription_billing()
        assert Order.query.filter_by(subscription_id=sub_id).count() == 0

        # ...on the new date one is.
        self._make_due_tomorrow(db, sub_id)
        process_daily_subscription_billing()
        assert Order.query.filter_by(subscription_id=sub_id).count() == 1

    def test_skip_on_a_paused_subscription_is_refused(self, client, db, auth_headers, sub_id):
        _assert_json_ok(client.post(f"{BASE}/{sub_id}/pause", headers=auth_headers, json={}))
        before = _sub(db, sub_id).next_delivery_date

        resp = client.post(f"{BASE}/{sub_id}/skip-next-delivery", headers=auth_headers, json={})

        assert resp.status_code == 400
        assert _sub(db, sub_id).next_delivery_date == before


@pytest.mark.integration
class TestChangeFrequencySaves:
    def test_bots_exact_frequency_body_is_saved(self, client, db, auth_headers, sub_id):
        """telegram_bot update_frequency_confirm sends exactly `{'frequency': X}`. It used to be refused
        (step 1) because nothing could save it; now it is the one frequency (plan Task 2)."""
        resp = client.put(f"{BASE}/{sub_id}", headers=auth_headers, json={"frequency": "monthly"})

        body = _assert_json_ok(resp)
        after = _sub(db, sub_id)
        assert after.frequency == SubscriptionFrequency.MONTHLY
        assert body["data"]["subscription"]["frequency"] == "monthly"

    def test_an_unknown_field_is_still_refused(self, client, db, auth_headers, sub_id):
        before = _sub(db, sub_id).updated_at

        resp = client.put(f"{BASE}/{sub_id}", headers=auth_headers, json={"billing_day": 5})

        assert resp.status_code == 400
        assert _sub(db, sub_id).updated_at == before
