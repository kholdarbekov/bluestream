"""The website subscription builder and the customer pages that show a subscription (plan 2026-10-24
Task 5; spec 2026-10-23 §2.1, §2.9, §4.7, §9).

The builder sends ONE `frequency`, the day that frequency uses (a weekday where 0 = Monday, converted from
the browser's getDay() where 0 = Sunday, or a day of the month) and a time slot. The bodies are the ones
the builder's real handlers produce (`admin_ui/src/__tests__/web/subscriptionBuilderBody.test.js` drives
the shipped JS and pins the SAME file, `tests/fixtures/web_subscription_builder_bodies.json`); here they
are POSTed to the real endpoint and the rows read back. The pages the website renders are fetched as a
customer fetches them.
"""

import copy
import json
import re
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from business_app.models.delivery import DeliveryTimeSlot
from business_app.models.subscription import Subscription
from shared.enums import SubscriptionFrequency
from tests.integration.reorder_reminder_builders import customer_headers

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
FIXTURE = json.loads((REPO / "tests" / "fixtures" / "web_subscription_builder_bodies.json").read_text("utf-8"))
JS = REPO / "business_app" / "static" / "js"


@pytest.fixture(autouse=True)
def _no_external_io(monkeypatch):
    """Celery publish and customer notifications are external I/O."""
    from unittest.mock import patch

    with patch("business_app.tasks.subscription_tasks.create_subscription_delivery_task.delay"), patch(
        "business_app.api.subscriptions.get_notification_service"
    ):
        yield


@pytest.fixture
def every_day_slot(db):
    slot = DeliveryTimeSlot(
        name="Morning", start_time="10:00", end_time="12:00", is_active=True, available_days=[0, 1, 2, 3, 4, 5, 6]
    )
    db.session.add(slot)
    db.session.commit()
    return slot


def _body(case, *, address, slot, product):
    """The case's body with the placeholder ids swapped for the real rows."""
    ids = FIXTURE["ids"]
    body = copy.deepcopy(case["body"])
    body["delivery_address_id"] = address.id
    body["delivery_time_slot_id"] = slot.id
    for item in body["items"]:
        assert item["product_id"] == ids["product"]
        item["product_id"] = product.id
    return body


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda c: c["name"])
def test_the_builder_body_creates_a_subscription_on_that_frequency_day_and_slot(
    client, db, auth_headers, user_address, sample_product, every_day_slot, case
):
    body = _body(case, address=user_address, slot=every_day_slot, product=sample_product)

    response = client.post("/api/v1/subscriptions/", headers=auth_headers, json=body)

    assert response.status_code == 201, response.get_json()
    published = response.get_json()["data"]["subscription"]
    row = db.session.get(Subscription, published["id"])
    assert row.frequency == SubscriptionFrequency(case["frequency"])
    assert row.delivery_time_slot_id == every_day_slot.id
    # The weekday the customer clicked is the weekday that is stored (0 = Monday), not the JS number.
    assert row.delivery_day_of_week == case.get("expected_weekday")
    assert row.delivery_day_of_month == case.get("day_of_month")
    assert published["frequency"] == case["frequency"]
    # What the website shows is the backend's answer, on the weekday that was chosen
    assert published["upcoming_delivery"]["date"] == row.next_delivery_date.isoformat()
    if "expected_weekday" in case:
        assert row.next_delivery_date.weekday() == case["expected_weekday"]
    if "day_of_month" in case:
        assert row.next_delivery_date.day == case["day_of_month"]
    assert published["upcoming_delivery"]["window_start"] == "10:00"
    assert published["upcoming_delivery"]["window_end"] == "12:00"


def test_the_slot_chooser_endpoint_the_builder_calls_offers_the_weekday_it_converted(
    client, db, every_day_slot
):
    sunday_only = DeliveryTimeSlot(
        name="Sunday", start_time="14:00", end_time="16:00", is_active=True, available_days=[6]
    )
    db.session.add(sunday_only)
    db.session.commit()

    # JS getDay 0 (Sunday) becomes weekday 6, and only then is the Sunday-only slot offered.
    sunday = client.get("/api/v1/delivery/time-slots?weekday=6").get_json()["time_slots"]
    monday = client.get("/api/v1/delivery/time-slots?weekday=0").get_json()["time_slots"]
    anyday = client.get("/api/v1/delivery/time-slots?weekday=any").get_json()["time_slots"]

    assert {s["name"] for s in sunday} == {"Morning", "Sunday"}
    assert {s["name"] for s in monday} == {"Morning"}
    assert {s["name"] for s in anyday} == {"Morning", "Sunday"}
    assert all("total_fee" not in s and "delivery_fee" not in s for s in sunday)


@pytest.mark.parametrize(
    ("frequency", "per_month"),
    [("daily", 365 / 12), ("weekly", 52 / 12), ("biweekly", 26 / 12), ("monthly", 1)],
)
def test_the_review_estimate_is_the_backends_deliveries_per_month(
    client, auth_headers, sample_product, frequency, per_month
):
    """The review step posts exactly this body and shows exactly this figure (no local multiplier)."""
    response = client.post(
        "/api/v1/subscriptions/preview",
        headers=auth_headers,
        json={"frequency": frequency, "items": [{"product_id": sample_product.id, "quantity": 2}]},
    )

    assert response.status_code == 200, response.get_json()
    preview = response.get_json()["data"]["preview"]
    assert preview["deliveries_per_month"] == pytest.approx(per_month, abs=0.005)
    assert preview["estimated_monthly_cost"] == pytest.approx(preview["total_amount"] * per_month, abs=1)


def test_the_builder_page_offers_daily_weekly_biweekly_monthly_a_day_of_month_and_every_weekday(app, db, client):
    response = client.get("/")
    assert response.status_code == 200
    soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")

    assert [o["data-frequency"] for o in soup.select("#step-2 .frequency-option")] == [
        "daily",
        "weekly",
        "biweekly",
        "monthly",
    ]
    assert [o["value"] for o in soup.select("#month-day-select option") if o["value"]] == [
        str(d) for d in range(1, 32)
    ]
    assert soup.select_one("#month-day-selector")["style"].replace(" ", "").find("display:none") >= 0
    # The day buttons carry the browser's getDay() number (Monday 1 ... Sunday 0); the script converts.
    assert [b["data-day"] for b in soup.select("#day-selector .day-btn")] == ["1", "2", "3", "4", "5", "6", "0"]
    assert soup.select_one("#time-slots-select") is not None


def test_my_account_shows_the_frequency_of_an_active_subscription(
    app, client, db, auth_headers, user_address, sample_product, every_day_slot, sample_user
):
    body = _body(FIXTURE["cases"][0], address=user_address, slot=every_day_slot, product=sample_product)
    assert client.post("/api/v1/subscriptions/", headers=auth_headers, json=body).status_code == 201

    response = app.test_client().get("/my-account?lang=en", headers=customer_headers(app, sample_user))

    assert response.status_code == 200
    text = BeautifulSoup(response.get_data(as_text=True), "html.parser").get_text(" ", strip=True)
    assert re.search(r"UZS\s*/\s*weekly", text), text[:2000]


def test_the_list_the_my_subscriptions_page_reads_carries_frequency_and_the_upcoming_delivery(
    client, db, auth_headers, user_address, sample_product, every_day_slot
):
    body = _body(FIXTURE["cases"][0], address=user_address, slot=every_day_slot, product=sample_product)
    assert client.post("/api/v1/subscriptions/", headers=auth_headers, json=body).status_code == 201

    response = client.get("/api/v1/subscriptions/?status=active", headers=auth_headers)

    assert response.status_code == 200
    (listed,) = response.get_json()["data"]["items"]
    assert listed["frequency"] == "weekly"
    assert listed["upcoming_delivery"]["window_start"] == "10:00"
    assert listed["upcoming_delivery"]["window_end"] == "12:00"
    assert listed["upcoming_delivery"]["date"] == listed["next_delivery_date"]


def test_the_my_subscriptions_script_reads_the_new_fields_and_no_billing_ones():
    script = (JS / "pages" / "my-subscriptions.js").read_text("utf-8")
    template = (REPO / "business_app" / "templates" / "frontend" / "my_subscriptions.html").read_text("utf-8")
    for source in (script, template):
        assert "next_billing" not in source
    assert "subscription.frequency" in script
    assert "upcoming_delivery" in script
    assert "subscription.delivery_frequency" not in script


def test_the_builder_source_sends_no_billing_pair_and_converts_the_weekday_in_one_place():
    source = (JS / "subscription-constructor.js").read_text("utf-8")
    assert "billing_cycle" not in source and "delivery_frequency" not in source
    assert source.count("(jsDay + 6) % 7") == 1
    assert source.count("weekdayFromJsDay(") == 2  # the definition and the one caller (selectDay)
    assert "getDefaultTimeSlots" not in source  # no invented slot ids


# The builder's own words and /my-subscriptions' "Time to be confirmed" (final review M7): English-literal
# keys seeded in every language and rendered through the page's own i18n, never hard-coded English.
BUILDER_WORDS = {
    "day_of_month": "Day {day} of the month",
    "preview_after_sign_in": "Shown once you are signed in",
    "select_day_of_month": "Please select a day of the month",
}


@pytest.mark.parametrize("lang", ["en", "uz", "ru"])
def test_the_builder_words_are_seeded_and_reach_the_script_in_the_visitors_language(app, db, client, lang):
    from business_app.models.translation import Translation
    from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, _category_for

    for key in [*BUILDER_WORDS.values(), "Time to be confirmed"]:
        row = BACKEND_TRANSLATIONS[key]
        assert all(row[language] for language in ("en", "uz", "ru")), key
        assert _category_for(key) == "general"
        db.session.add(Translation(key=key, language=lang, value=row[lang], category="general", is_active=True))
    db.session.commit()
    assert all("{day}" in BACKEND_TRANSLATIONS["Day {day} of the month"][language] for language in ("en", "uz", "ru"))
    client.application.jinja_env.cache.clear()

    html = client.application.test_client().get(f"/?lang={lang}", follow_redirects=True).get_data(as_text=True)

    island = BeautifulSoup(html, "html.parser").select_one("script#subscription-constructor-data")
    assert json.loads(island.string)["i18n"] == {
        name: BACKEND_TRANSLATIONS[key][lang] for name, key in BUILDER_WORDS.items()
    }


def test_the_builder_and_my_subscriptions_hard_code_none_of_those_words():
    source = (JS / "subscription-constructor.js").read_text("utf-8")
    for english in BUILDER_WORDS.values():
        assert english.replace("{day}", "") not in source.replace("${this.selectedDayOfMonth}", "")
    assert "of the month`" not in source
    for name in BUILDER_WORDS:
        assert f"CONSTRUCTOR_I18N.{name}" in source
    template = (REPO / "business_app" / "templates" / "frontend" / "my_subscriptions.html").read_text("utf-8")
    assert "'Time to be confirmed' | t" in template
    assert "PAGE_DATA.i18n.time_to_be_confirmed" in (JS / "pages" / "my-subscriptions.js").read_text("utf-8")
