"""One frequency, local delivery DATEs, and no trace of the old columns (plan 2026-10-24 Tasks 1 and 6;
spec 2026-10-23 §4.1-4.3, §9).

A subscription has ONE `frequency`; `next_delivery_date` / `last_delivery_date` are local DATEs and every
writer computes them through `subscription_schedule`. `billing_cycle`, `delivery_frequency`,
`next_billing_date` and `last_billing_date` are gone from the table, the model, every payload and every
request: a body that names only the old pair is refused.

Driven through the endpoints with the bodies the clients send: the bot's create body
(telegram_bot/handlers/subscriptions.py) and the admin form's (admin_ui/src/utils/subscriptionPayload.js).
The business clock is frozen on a Monday at 10:00 Tashkent. The Monday is the first one on or after
the real local date, because `start_date` still defaults to the wall clock and must not fall after it.
"""

from datetime import UTC, date, datetime, time, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from business_app.models.delivery import DeliveryTimeSlot
from business_app.models.subscription import Subscription
from shared.constants import DISPLAY_TIMEZONE
from shared.enums import SubscriptionFrequency
from tests.integration.sales_pay_builders import freeze_local_now
from tests.integration.test_subscription_action_guards import _state

TASHKENT = ZoneInfo(DISPLAY_TIMEZONE)
BASE = "/api/v1/subscriptions"
ADMIN_BASE = "/api/v1/admin/subscriptions"
THURSDAY = 3


def _first_monday_from_today() -> date:
    today = datetime.now(TASHKENT).date()
    return today + timedelta(days=(7 - today.weekday()) % 7)


@pytest.fixture(autouse=True)
def _no_external_io():
    with patch("business_app.tasks.subscription_tasks.create_subscription_delivery_task.delay"), patch(
        "business_app.api.subscriptions.get_notification_service"
    ):
        yield


@pytest.fixture
def monday(monkeypatch) -> date:
    day = _first_monday_from_today()
    freeze_local_now(monkeypatch, datetime.combine(day, time(10, 0), tzinfo=TASHKENT))
    return day


@pytest.fixture
def morning_slot(db):
    slot = DeliveryTimeSlot(
        name="Morning", start_time="10:00", end_time="12:00", is_active=True, available_days=[0, 1, 2, 3, 4, 5, 6]
    )
    db.session.add(slot)
    db.session.commit()
    return slot


def _bot_body(address_id, product_id, frequency="weekly"):
    """telegram_bot/handlers/subscriptions.py `confirm_subscription`: one choice sent as both fields."""
    return {
        "name": f"Subscription - {frequency.title()}",
        "frequency": frequency,
        "delivery_address_id": address_id,
        "payment_method": "cash",
        "items": [{"product_id": product_id, "quantity": 2}],
        "auto_renew": True,
        "delivery_time_slot_id": None,
    }


def _admin_body(user_id, address_id, product_id, **overrides):
    """`buildSubscriptionPayload(values)` for a new subscription; undefined keys are dropped by JSON."""
    body = {
        "name": "Office water",
        "description": None,
        "frequency": "monthly",
        "delivery_day_of_week": None,
        "delivery_day_of_month": None,
        "delivery_time_slot_id": None,
        "delivery_address_id": address_id,
        "payment_method": "cash",
        "auto_renew": True,
        "discount_percentage": 0,
        "loyalty_points_multiplier": None,
        "user_id": user_id,
        "items": [{"product_id": product_id, "quantity": 3}],
    }
    body.update(overrides)
    return body


def _admin_created_id(resp) -> int:
    """The admin create answers with `Subscription.to_dict()`, keyed for us by its unique number."""
    assert resp.status_code == 201, resp.get_json()
    number = resp.get_json()["data"]["subscription"]["subscription_number"]
    return Subscription.query.filter_by(subscription_number=number).one().id


def _row(db, subscription_id) -> Subscription:
    db.session.expire_all()
    return db.session.get(Subscription, subscription_id)


OLD_COLUMNS = ("billing_cycle", "delivery_frequency", "next_billing_date", "last_billing_date")


def _assert_one_frequency(row, frequency):
    assert row.frequency == frequency
    for old in OLD_COLUMNS:
        assert not hasattr(row, old), old


def _assert_next_delivery(row, day: date):
    assert type(row.next_delivery_date) is date  # a DATE column, not a datetime
    assert row.next_delivery_date == day


def _keys(value):
    """Every dict key anywhere in a JSON payload."""
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from _keys(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _keys(inner)


def _assert_no_old_keys(payload):
    assert set(OLD_COLUMNS).isdisjoint(_keys(payload))


@pytest.mark.integration
class TestCreate:
    def test_bot_body_gets_one_frequency_and_the_first_delivery_after_the_lead(
        self, client, db, auth_headers, user_address, sample_product, monday
    ):
        resp = client.post(f"{BASE}/", headers=auth_headers, json=_bot_body(user_address.id, sample_product.id))

        assert resp.status_code == 201, resp.get_json()
        row = _row(db, resp.get_json()["data"]["subscription"]["id"])
        _assert_one_frequency(row, SubscriptionFrequency.WEEKLY)
        # No day chosen (the bot has no picker yet): any day anchors, so the lead decides.
        _assert_next_delivery(row, monday + timedelta(days=1))
        assert row.last_delivery_date is None

    def test_admin_weekly_on_thursday_with_a_slot(
        self, client, db, admin_auth_headers, sample_user, user_address, sample_product, morning_slot, monday
    ):
        body = _admin_body(
            sample_user.id,
            user_address.id,
            sample_product.id,
            frequency="weekly",
            delivery_day_of_week=THURSDAY,
            delivery_time_slot_id=morning_slot.id,
        )

        resp = client.post(ADMIN_BASE, headers=admin_auth_headers, json=body)

        row = _row(db, _admin_created_id(resp))
        _assert_one_frequency(row, SubscriptionFrequency.WEEKLY)
        _assert_next_delivery(row, monday + timedelta(days=3))
        assert row.delivery_day_of_week == THURSDAY

    def test_admin_monthly_default_of_the_form(
        self, client, db, admin_auth_headers, sample_user, user_address, sample_product, monday
    ):
        resp = client.post(
            ADMIN_BASE, headers=admin_auth_headers, json=_admin_body(sample_user.id, user_address.id, sample_product.id)
        )

        row = _row(db, _admin_created_id(resp))
        _assert_one_frequency(row, SubscriptionFrequency.MONTHLY)
        _assert_next_delivery(row, monday + timedelta(days=1))

    def test_admin_start_date_in_the_future_dates_the_first_delivery_from_it(
        self, client, db, admin_auth_headers, sample_user, user_address, sample_product, monday
    ):
        start_local = datetime.combine(monday + timedelta(days=8), time(9, 0), tzinfo=TASHKENT)  # next Tuesday
        body = _admin_body(
            sample_user.id,
            user_address.id,
            sample_product.id,
            frequency="weekly",
            delivery_day_of_week=THURSDAY,
            start_date=start_local.astimezone(UTC).isoformat().replace("+00:00", "Z"),  # dayjs toISOString()
        )

        resp = client.post(ADMIN_BASE, headers=admin_auth_headers, json=body)

        row = _row(db, _admin_created_id(resp))
        _assert_next_delivery(row, monday + timedelta(days=10))

    def test_the_detail_endpoint_publishes_next_delivery_as_a_date(
        self, client, db, auth_headers, user_address, sample_product, monday
    ):
        created = client.post(f"{BASE}/", headers=auth_headers, json=_bot_body(user_address.id, sample_product.id))
        sub_id = created.get_json()["data"]["subscription"]["id"]

        resp = client.get(f"{BASE}/{sub_id}", headers=auth_headers)

        assert resp.status_code == 200, resp.get_json()
        published = resp.get_json()["data"]["subscription"]
        assert published["next_delivery_date"] == (monday + timedelta(days=1)).isoformat()
        assert published["last_delivery_date"] is None


@pytest.mark.integration
class TestStatistics:
    def test_upcoming_deliveries_is_one_per_active_subscription(
        self, client, db, auth_headers, user_address, sample_product, monday
    ):
        ids = [
            client.post(f"{BASE}/", headers=auth_headers, json=_bot_body(user_address.id, sample_product.id, freq))
            .get_json()["data"]["subscription"]["id"]
            for freq in ("weekly", "monthly")
        ]
        token = _state(client, ids[1], auth_headers)
        paused = client.post(f"{BASE}/{ids[1]}/pause", headers=auth_headers, json={"expected_state": token})
        assert paused.status_code == 200

        resp = client.get(f"{BASE}/statistics", headers=auth_headers)

        assert resp.status_code == 200, resp.get_json()
        stats = resp.get_json()["data"]["statistics"]
        assert (stats["active_subscriptions"], stats["paused_subscriptions"], stats["upcoming_deliveries"]) == (1, 1, 1)


@pytest.mark.integration
class TestAdminUpdate:
    def test_changing_the_frequency(
        self, client, db, admin_auth_headers, sample_user, user_address, sample_product, monday
    ):
        created = client.post(
            ADMIN_BASE, headers=admin_auth_headers, json=_admin_body(sample_user.id, user_address.id, sample_product.id)
        )
        sub_id = _admin_created_id(created)

        # The admin form sends the weekday with a weekly frequency: weekly without one is refused (B9).
        resp = client.put(
            f"{ADMIN_BASE}/{sub_id}", headers=admin_auth_headers, json={"frequency": "weekly", "delivery_day_of_week": 3}
        )

        assert resp.status_code == 200, resp.get_json()
        _assert_one_frequency(_row(db, sub_id), SubscriptionFrequency.WEEKLY)
        _assert_no_old_keys(resp.get_json())


@pytest.mark.unit
def test_the_deleted_features_leave_no_copy_and_no_notification_type():
    """Final review M8: templates, retry-billing, the end-of-period cancel, resume dates and renewal reminders
    are gone, so their seeded copy and notification types are too."""
    from business_app.services.email_template_service import EmailTemplateService
    from business_app.utils.constants import NotificationType
    from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

    dead = [
        key
        for key in BACKEND_TRANSLATIONS
        if key.startswith("api.subscriptions.templates.")
        or key
        in {
            "api.subscriptions.cancellation_scheduled",
            "api.subscriptions.log.cancellation_scheduled",
            "api.subscriptions.billing_retry_initiated",
            "api.subscriptions.error.get_templates_failed",
            "api.subscriptions.error.retry_billing_failed",
            "api.subscriptions.error.no_failed_billing_to_retry",
            "api.subscriptions.error.only_active_retry",
            "api.subscriptions.error.resume_date_future",
            "api.subscriptions.immediate",
            "telegram.subscription.use_template",
            "telegram.subscription.create_custom",
            "telegram.subscription.create_template_or_custom",
            "telegram.subscription.billing_retry_initiated",
        }
    ]
    assert dead == []
    assert not {"SUBSCRIPTION_RENEWAL", "SUBSCRIPTION_CANCELLATION_SCHEDULED"} & set(NotificationType.__members__)
    assert "subscription_renewal" not in EmailTemplateService.TEMPLATE_MAPPING
    assert "subscription_renewal" not in EmailTemplateService.EMAIL_SUBJECTS


@pytest.mark.integration
class TestTheOldColumnsAreGone:
    def test_the_model_has_none_of_them(self):
        columns = {column.name for column in Subscription.__table__.columns}
        assert columns.isdisjoint(OLD_COLUMNS)

    def test_no_customer_payload_carries_them(self, client, db, auth_headers, user_address, sample_product, monday):
        created = client.post(f"{BASE}/", headers=auth_headers, json=_bot_body(user_address.id, sample_product.id))
        sub_id = created.get_json()["data"]["subscription"]["id"]
        _assert_no_old_keys(created.get_json())

        for path in (f"{BASE}/", f"{BASE}/{sub_id}", f"{BASE}/{sub_id}/history"):
            resp = client.get(path, headers=auth_headers)
            assert resp.status_code == 200, (path, resp.get_json())
            _assert_no_old_keys(resp.get_json())
        named = client.get(f"{BASE}/{sub_id}", headers=auth_headers).get_json()["data"]["subscription"][
            "allowed_actions"
        ]["skip"]["date"]
        for action in ("skip-next-delivery", "pause", "resume"):
            # Each body as its client sends it: the date the skip names, the state the pause's screen showed.
            body = {
                "skip-next-delivery": lambda: {"expected_date": named},
                "pause": lambda: {"expected_state": _state(client, sub_id, auth_headers)},
                "resume": dict,
            }[action]()
            resp = client.post(f"{BASE}/{sub_id}/{action}", headers=auth_headers, json=body)
            assert resp.status_code == 200, (action, resp.get_json())
            _assert_no_old_keys(resp.get_json())
        resp = client.put(
            f"{BASE}/{sub_id}",
            headers=auth_headers,
            json={
                "frequency": "monthly",
                "delivery_day_of_month": 15,
                "expected_state": _state(client, sub_id, auth_headers),
            },
        )
        assert resp.status_code == 200, resp.get_json()
        _assert_no_old_keys(resp.get_json())

    def test_the_preview_takes_one_frequency_and_publishes_no_old_key(
        self, client, auth_headers, sample_product
    ):
        resp = client.post(
            f"{BASE}/preview",
            headers=auth_headers,
            json={"frequency": "weekly", "items": [{"product_id": sample_product.id, "quantity": 1}]},
        )

        assert resp.status_code == 200, resp.get_json()
        _assert_no_old_keys(resp.get_json())

    def test_no_admin_payload_carries_them(
        self, client, db, admin_auth_headers, sample_user, user_address, sample_product, monday
    ):
        created = client.post(
            ADMIN_BASE, headers=admin_auth_headers, json=_admin_body(sample_user.id, user_address.id, sample_product.id)
        )
        sub_id = _admin_created_id(created)
        _assert_no_old_keys(created.get_json())

        for path in (ADMIN_BASE, f"{ADMIN_BASE}/{sub_id}"):
            resp = client.get(path, headers=admin_auth_headers)
            assert resp.status_code == 200, (path, resp.get_json())
            _assert_no_old_keys(resp.get_json())
        resp = client.put(f"{ADMIN_BASE}/{sub_id}", headers=admin_auth_headers, json={"name": "Renamed water"})
        assert resp.status_code == 200, resp.get_json()
        _assert_no_old_keys(resp.get_json())

    def test_a_body_naming_only_the_old_pair_is_refused(
        self, client, db, auth_headers, admin_auth_headers, sample_user, user_address, sample_product
    ):
        old_pair = {"billing_cycle": "weekly", "delivery_frequency": "weekly"}
        bot = {k: v for k, v in _bot_body(user_address.id, sample_product.id).items() if k != "frequency"}
        admin = {k: v for k, v in _admin_body(sample_user.id, user_address.id, sample_product.id).items()
                 if k != "frequency"}

        assert client.post(f"{BASE}/", headers=auth_headers, json={**bot, **old_pair}).status_code == 400
        assert client.post(ADMIN_BASE, headers=admin_auth_headers, json={**admin, **old_pair}).status_code == 400
        preview = {"items": [{"product_id": sample_product.id, "quantity": 1}], **old_pair}
        assert client.post(f"{BASE}/preview", headers=auth_headers, json=preview).status_code == 400
        assert Subscription.query.count() == 0

    def test_the_old_filter_name_is_not_a_filter(
        self, client, db, admin_auth_headers, sample_user, user_address, sample_product, monday
    ):
        for frequency in ("monthly", "weekly"):
            body = _admin_body(sample_user.id, user_address.id, sample_product.id, frequency=frequency)
            assert client.post(ADMIN_BASE, headers=admin_auth_headers, json=body).status_code == 201

        by_frequency = client.get(f"{ADMIN_BASE}?frequency=weekly", headers=admin_auth_headers).get_json()
        by_old_name = client.get(f"{ADMIN_BASE}?billing_cycle=weekly", headers=admin_auth_headers).get_json()

        assert [item["frequency"] for item in by_frequency["data"]["items"]] == ["weekly"]
        assert sorted(item["frequency"] for item in by_old_name["data"]["items"]) == ["monthly", "weekly"]
