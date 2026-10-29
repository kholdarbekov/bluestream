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
import os
import re
from decimal import Decimal
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from business_app.models.delivery import DeliveryTimeSlot
from business_app.models.subscription import Subscription
from shared.enums import SubscriptionFrequency
from tests.integration.reorder_reminder_builders import customer_headers
from tests.integration.test_subscription_page_data import discounted  # noqa: F401  (fixture)

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
    if "delivery_time_slot_id" in body:  # an "Any time" body names no slot
        body["delivery_time_slot_id"] = slot.id
    for item in body["items"]:
        assert item["product_id"] == ids["product"]
        item["product_id"] = product.id
    return body


def _seed_any_time_word(db):
    from business_app.models.translation import Translation
    from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, _category_for

    key = "api.subscriptions.time.any_time"
    for language, value in BACKEND_TRANSLATIONS[key].items():
        db.session.add(Translation(key=key, language=language, value=value, category=_category_for(key)))
    db.session.commit()


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=lambda c: c["name"])
def test_the_builder_body_creates_a_subscription_on_that_frequency_day_and_slot(
    client, db, auth_headers, user_address, sample_product, every_day_slot, case
):
    body = _body(case, address=user_address, slot=every_day_slot, product=sample_product)
    if case.get("any_time"):
        _seed_any_time_word(db)

    response = client.post("/api/v1/subscriptions/", headers=auth_headers, json=body)

    assert response.status_code == 201, response.get_json()
    published = response.get_json()["data"]["subscription"]
    row = db.session.get(Subscription, published["id"])
    assert row.frequency == SubscriptionFrequency(case["frequency"])
    # "Any time" is no slot at all (spec 2026-10-27 §6.1); every other body names the slot it chose.
    assert row.delivery_time_slot_id == (None if case.get("any_time") else every_day_slot.id)
    # The builder sends no description (spec 2026-10-27 §11): the row keeps its empty default.
    assert not row.description
    assert "description" not in case["body"]
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
    if case.get("any_time"):
        assert published["upcoming_delivery"]["time_label"] == "Any time"
        assert published["upcoming_delivery"]["window_start"] is None
    else:
        assert published["upcoming_delivery"]["window_start"] == "10:00"
        assert published["upcoming_delivery"]["window_end"] == "12:00"


@pytest.fixture
def weekday_only_slot(db):
    """Offered Monday to Friday: a monthly delivery, which lands on a different weekday each month, may not use it."""
    slot = DeliveryTimeSlot(
        name="Office hours", start_time="09:00", end_time="18:00", is_active=True, available_days=[0, 1, 2, 3, 4]
    )
    db.session.add(slot)
    db.session.commit()
    return slot


def _case(name):
    return next(case for case in FIXTURE["cases"] if case["name"] == name)


SLOT_NOT_EVERY_DAY = "api.subscriptions.schedule.slot_not_every_day"


def test_create_refuses_the_builders_monthly_body_with_a_slot_not_offered_every_day(
    client, db, auth_headers, user_address, sample_product, weekday_only_slot, every_day_slot
):
    """Spec 2026-10-26 §8.2 on create, as on the schedule PUT: the same guard, the same translated refusal.
    Before, the subscription was saved and its orders on the slot's missing weekdays had no time. With the
    every-day slot active every weekday is a delivery day (spec 2026-10-27 §6.4), which the weekday slot misses."""
    from business_app.models.translation import Translation
    from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, _category_for

    for language, value in BACKEND_TRANSLATIONS[SLOT_NOT_EVERY_DAY].items():
        db.session.add(Translation(key=SLOT_NOT_EVERY_DAY, language=language, value=value,
                                   category=_category_for(SLOT_NOT_EVERY_DAY)))
    db.session.commit()
    body = _body(_case("monthly on the 15th"), address=user_address, slot=weekday_only_slot, product=sample_product)

    response = client.post("/api/v1/subscriptions/", headers=auth_headers, json=body)

    assert response.status_code == 400, response.get_json()
    assert response.get_json()["message"] in set(BACKEND_TRANSLATIONS[SLOT_NOT_EVERY_DAY].values())
    assert Subscription.query.count() == 0


def test_every_slot_the_builder_offers_for_monthly_is_one_create_accepts(
    client, db, auth_headers, user_address, sample_product, every_day_slot, weekday_only_slot
):
    """The builder asks `weekday=any&frequency=monthly` (subscriptionBuilderBody.test.js pins the URL): the read
    list and the create guard are one rule."""
    offered = client.get("/api/v1/delivery/time-slots?weekday=any&frequency=monthly").get_json()["time_slots"]
    assert [slot["id"] for slot in offered] == [every_day_slot.id]

    for slot, status in ((every_day_slot, 201), (weekday_only_slot, 400)):
        body = _body(_case("monthly on the 15th"), address=user_address, slot=slot, product=sample_product)
        response = client.post("/api/v1/subscriptions/", headers=auth_headers, json=body)
        assert response.status_code == status, (slot.name, response.get_json())


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


def test_the_builder_preview_counts_the_chosen_slots_days(
    client, db, auth_headers, sample_product, every_day_slot, weekday_only_slot
):
    """Spec 2026-10-27 §6.4: a daily subscription delivers on its slot's days, so the review's per-month figure is
    the one the saved subscription then publishes (`test_the_daily_estimate_counts_only_delivery_days`). An
    inactive or unknown slot counts as none: every delivery day."""

    def preview(**fields):
        body = {"frequency": "daily", "items": [{"product_id": sample_product.id, "quantity": 2}], **fields}
        response = client.post("/api/v1/subscriptions/preview", headers=auth_headers, json=body)
        assert response.status_code == 200, response.get_json()
        return response.get_json()["data"]["preview"]

    weekdays = preview(delivery_time_slot_id=weekday_only_slot.id)
    assert weekdays["deliveries_per_month"] == float(Decimal(1825) / Decimal(84))
    assert weekdays["estimated_monthly_cost"] == pytest.approx(weekdays["total_amount"] * 1825 / 84, abs=0.01)

    every_day = float(Decimal(365) / Decimal(12))
    assert preview()["deliveries_per_month"] == every_day
    assert preview(delivery_time_slot_id=999999)["deliveries_per_month"] == every_day
    weekday_only_slot.is_active = False
    db.session.commit()
    assert preview(delivery_time_slot_id=weekday_only_slot.id)["deliveries_per_month"] == every_day

    refused = client.post("/api/v1/subscriptions/preview", headers=auth_headers, json={
        "frequency": "daily", "items": [{"product_id": sample_product.id, "quantity": 2}], "delivery_time_slot_id": 0,
    })
    assert refused.status_code == 400, refused.get_json()


# Re-review G6 (spec 2026-10-26 B10): the builder shows only the money the backend publishes. Its product cards
# read the subscription catalog (`GET /subscriptions/products`, asked anonymously: the builder lists products
# before sign-in), and its review step reads the create preview's labels; it multiplies nothing itself. This
# fixture is the two real answers for a product on sale (base 8 000, discount 6 000: the subscription price is
# 6 000), which admin_ui/src/__tests__/web/subscriptionBuilderBody.test.js renders through the shipped script.
QUOTES = REPO / "tests" / "fixtures" / "web_subscription_builder_quotes.json"
UPDATE = os.environ.get("UPDATE_WEB_SUBSCRIPTION_FIXTURES") == "1"
_HINT_KEY = "landing.custom_subscription.review.per_month_hint."
NBSP = " "


def _seed_labels(db):
    from business_app.models.translation import Translation
    from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, _category_for

    for key, values in BACKEND_TRANSLATIONS.items():
        if key.startswith(("web.display_labels.", _HINT_KEY)):
            for language, value in values.items():
                db.session.add(Translation(key=key, language=language, value=value, category=_category_for(key)))
    db.session.commit()


def test_the_builder_quote_fixture_is_what_the_endpoints_answer(app, db, auth_headers, discounted):
    _seed_labels(db)
    # As the builder asks: no cookies (an expired session cookie must not turn the list into a 401) and the
    # page's own language.
    catalog = app.test_client().get("/api/v1/subscriptions/products", headers={"Accept-Language": "en"})
    body = {"frequency": "weekly", "items": [{"product_id": discounted.id, "quantity": 2}]}
    preview = app.test_client().post("/api/v1/subscriptions/preview", headers=auth_headers, json=body)

    assert catalog.status_code == 200, catalog.get_json()
    assert preview.status_code == 200, preview.get_json()
    generated = {
        "_comment": "Written by tests/integration/test_web_subscription_builder_contract.py (re-review G6): the "
        "anonymous subscription catalog and the create preview for a product on sale, as the endpoints answer. "
        "Regenerate with UPDATE_WEB_SUBSCRIPTION_FIXTURES=1.",
        "catalog": {"path": "/api/v1/subscriptions/products", "response": catalog.get_json()},
        "preview": {"path": "/api/v1/subscriptions/preview", "body": body, "response": preview.get_json()},
    }
    if UPDATE:
        QUOTES.write_text(json.dumps(generated, ensure_ascii=False, indent=1, sort_keys=True) + "\n", "utf-8")
    assert QUOTES.exists(), f"{QUOTES} is missing: run with UPDATE_WEB_SUBSCRIPTION_FIXTURES=1"
    assert json.loads(QUOTES.read_text("utf-8")) == json.loads(json.dumps(generated)), (
        f"{QUOTES.name} is stale: regenerate it with UPDATE_WEB_SUBSCRIPTION_FIXTURES=1 and re-run the builder Vitest."
    )
    # The published figures are the subscription price (the discount price), never the base price.
    [row] = [r for r in catalog.get_json()["data"]["items"] if r["id"] == discounted.id]
    assert row["unit_price_label"] == f"6{NBSP}000{NBSP}so'm"
    quote = preview.get_json()["data"]["preview"]
    assert [(line["unit_price_label"], line["line_total_label"]) for line in quote["items"]] == [
        (f"6{NBSP}000{NBSP}so'm", f"12{NBSP}000{NBSP}so'm")
    ]
    assert quote["total_amount_label"] == f"12{NBSP}000{NBSP}so'm"
    assert quote["estimated_monthly_cost_label"] == f"52{NBSP}000{NBSP}so'm"


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


@pytest.mark.parametrize("lang", ["en", "uz", "ru"])
def test_my_account_shows_the_frequency_of_an_active_subscription(
    app, client, db, auth_headers, user_address, sample_product, every_day_slot, sample_user, lang
):
    """The frequency is named in the reader's language (`cabinet.subs.frequency.*`), never the raw enum."""
    from business_app.models.translation import Translation
    from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

    body = _body(FIXTURE["cases"][0], address=user_address, slot=every_day_slot, product=sample_product)
    assert client.post("/api/v1/subscriptions/", headers=auth_headers, json=body).status_code == 201
    key = "cabinet.subs.frequency." + FIXTURE["cases"][0]["frequency"]
    for language in ("en", "uz", "ru"):
        db.session.add(Translation(key=key, language=language, value=BACKEND_TRANSLATIONS[key][language],
                                   category="cabinet"))
    sample_user.preferred_language = lang  # a uz reader's `?lang=uz` (the default) is dropped from the URL
    db.session.commit()
    client.application.jinja_env.cache.clear()

    response = app.test_client().get(
        f"/my-account?lang={lang}", headers=customer_headers(app, sample_user), follow_redirects=True
    )

    assert response.status_code == 200
    text = BeautifulSoup(response.get_data(as_text=True), "html.parser").get_text(" ", strip=True)
    assert re.search(r"UZS\s*/\s*" + re.escape(BACKEND_TRANSLATIONS[key][lang]), text), text[:2000]
    assert not re.search(r"UZS\s*/\s*weekly", text)


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
    """The page shows the backend's schedule sentence and upcoming delivery (spec 2026-10-26 §12); it never
    reads the retired billing pair or words the frequency itself."""
    script = (JS / "pages" / "my-subscriptions.js").read_text("utf-8")
    template = (REPO / "business_app" / "templates" / "frontend" / "my_subscriptions.html").read_text("utf-8")
    for source in (script, template):
        assert "next_billing" not in source
        assert "delivery_frequency" not in source
    assert "sub.schedule_sentence" in script
    assert "sub.upcoming_delivery" in script


def test_the_builder_source_sends_no_billing_pair_and_converts_the_weekday_in_one_place():
    source = (JS / "subscription-constructor.js").read_text("utf-8")
    assert "billing_cycle" not in source and "delivery_frequency" not in source
    assert source.count("(jsDay + 6) % 7") == 1
    # The definition and its callers: selectDay, and syncScheduleControls (which day button shows the weekday held).
    assert source.count("weekdayFromJsDay(") == 3
    assert "getDefaultTimeSlots" not in source  # no invented slot ids


# The builder's own words: seeded in every language and rendered through the page's own i18n island
# (`#subscription-constructor-data`), never hard-coded English in the script (final review M7; spec 2026-10-27 §6.2,
# §11 "The builder review card ... translated"). The island is `{i18n, frequency_names, weekday_names}`.
_CS = "landing.custom_subscription."
BUILDER_WORDS = {
    "day_of_month": "Day {day} of the month",
    "preview_after_sign_in": "Shown once you are signed in",
    "select_day_of_month": "Please select a day of the month",
    # The slot list's placeholder when slots are offered: the customer then chooses a time or Any time.
    "select_time_slot": _CS + "select_time_slot",
    "any_time": _CS + "any_time",
    "created": _CS + "created",
    **{f"error_{name}": f"{_CS}error.{name}" for name in (
        "products", "frequency", "day", "time", "address", "payment", "slots_failed", "methods_failed",
        "load_failed", "sign_in", "create_failed",
    )},
    **{f"address_{name}": f"{_CS}address.{name}" for name in (
        "none_title", "none_body", "add", "add_elsewhere", "apartment",
    )},
    **{f"review_{name}": f"{_CS}review.{name}" for name in (
        "name_title", "name_label", "name_placeholder", "name_hint", "products_title", "quantity", "schedule_title",
        "frequency", "day", "time", "address_title", "payment_title", "cost_title", "per_delivery",
        "per_delivery_hint", "per_month", "note",
    )},
    **{f"name_{name}": f"{_CS}name.{name}" for name in ("more", "to", "default")},
}
FREQUENCY_KEYS = {f: f"{_CS}frequency.{f}" for f in ("daily", "weekly", "biweekly", "monthly")}
WEEKDAY_KEYS = [f"web.display_labels.weekday.{d}" for d in range(7)]
ISLAND = REPO / "tests" / "fixtures" / "web_subscription_builder_island.json"


def _seed_builder_words(db, languages):
    from business_app.models.translation import Translation
    from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, _category_for

    for key in [*BUILDER_WORDS.values(), *FREQUENCY_KEYS.values(), *WEEKDAY_KEYS]:
        row = BACKEND_TRANSLATIONS[key]
        assert all(row[language] for language in ("en", "uz", "ru")), key
        if "." not in key:  # an English-literal storefront key
            assert _category_for(key) == "general"
        for language in languages:
            db.session.add(
                Translation(key=key, language=language, value=row[language], category=_category_for(key),
                            is_active=True)
            )
    db.session.commit()


def _island(client, lang):
    html = client.application.test_client().get(f"/?lang={lang}", follow_redirects=True).get_data(as_text=True)
    island = BeautifulSoup(html, "html.parser").select_one("script#subscription-constructor-data")
    return json.loads(island.string)


@pytest.mark.parametrize("lang", ["en", "uz", "ru"])
def test_the_builder_words_are_seeded_and_reach_the_script_in_the_visitors_language(app, db, client, lang):
    from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

    _seed_builder_words(db, [lang])
    assert all("{day}" in BACKEND_TRANSLATIONS["Day {day} of the month"][language] for language in ("en", "uz", "ru"))

    page = _island(client, lang)

    assert page["i18n"] == {name: BACKEND_TRANSLATIONS[key][lang] for name, key in BUILDER_WORDS.items()}
    assert page["frequency_names"] == {f: BACKEND_TRANSLATIONS[key][lang] for f, key in FREQUENCY_KEYS.items()}
    assert page["weekday_names"] == [BACKEND_TRANSLATIONS[key][lang] for key in WEEKDAY_KEYS]
    # Any time and Time to be confirmed carry D14's exact words (the builder only ever offers the first).
    assert BACKEND_TRANSLATIONS[_CS + "any_time"] == {"en": "Any time", "uz": "Istalgan vaqtda", "ru": "В любое время"}
    assert "time_to_be_confirmed" not in page["i18n"]


def test_the_page_follows_each_visitors_language_from_one_cached_template(app, db, client):
    """The `t` filter is not folded into the compiled template: the cache is never cleared between the
    languages, as in production, and every visit still gets its own words."""
    from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

    _seed_builder_words(db, ("en", "uz", "ru"))
    client.application.jinja_env.cache.clear()  # one cold start; the three renders below share the cache

    pages = {lang: _island(client, lang) for lang in ("ru", "en", "uz")}

    for lang, page in pages.items():
        assert page["i18n"] == {name: BACKEND_TRANSLATIONS[key][lang] for name, key in BUILDER_WORDS.items()}


def test_the_builder_island_fixture_is_what_the_page_renders(app, db, client):
    """The Vitest renders the shipped script in uz and ru against this file, so a word the page never sends in a
    language cannot hide behind the English the test would otherwise type."""
    _seed_builder_words(db, ("en", "uz", "ru"))
    generated = {
        "_comment": "Written by tests/integration/test_web_subscription_builder_contract.py: the data island "
        "`#subscription-constructor-data` the home page renders per language. Regenerate with "
        "UPDATE_WEB_SUBSCRIPTION_FIXTURES=1.",
        **{lang: _island(client, lang) for lang in ("en", "uz", "ru")},
    }
    if UPDATE:
        ISLAND.write_text(json.dumps(generated, ensure_ascii=False, indent=1, sort_keys=True) + "\n", "utf-8")
    assert ISLAND.exists(), f"{ISLAND} is missing: run with UPDATE_WEB_SUBSCRIPTION_FIXTURES=1"
    assert json.loads(ISLAND.read_text("utf-8")) == json.loads(json.dumps(generated)), (
        f"{ISLAND.name} is stale: regenerate it with UPDATE_WEB_SUBSCRIPTION_FIXTURES=1 and re-run the builder Vitest."
    )
    assert generated["uz"]["i18n"]["any_time"] == "Istalgan vaqtda"
    assert generated["ru"]["i18n"]["any_time"] == "В любое время"


def test_the_builder_and_my_subscriptions_hard_code_none_of_those_words():
    from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

    source = (JS / "subscription-constructor.js").read_text("utf-8")
    for key in BUILDER_WORDS.values():
        english = BACKEND_TRANSLATIONS[key]["en"]
        assert english.replace("{day}", "") not in source.replace("${this.selectedDayOfMonth}", ""), english
    assert "of the month`" not in source
    for name in BUILDER_WORDS:
        assert f"CONSTRUCTOR_I18N.{name}" in source, name
    for literal in ("Custom subscription with", "'Bi-Weekly'", "'Monday'", "showError('", "showSuccess('",
                    "Time to be confirmed"):
        assert literal not in source, literal
    # /my-subscriptions renders the backend's `time_label` (spec 2026-10-27 §6.3), never these words of its own.
    assert "Time to be confirmed" not in (JS / "pages" / "my-subscriptions.js").read_text("utf-8")


def test_the_builder_seed_drops_the_old_unconfirmed_time_word_and_adds_the_frequency_names():
    from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

    assert "Time to be confirmed" not in BACKEND_TRANSLATIONS  # the builder no longer shows it
    assert BACKEND_TRANSLATIONS[FREQUENCY_KEYS["biweekly"]] == {
        "en": "Bi-Weekly", "uz": "Ikki haftada bir marta", "ru": "Раз в две недели",
    }
    assert BACKEND_TRANSLATIONS[FREQUENCY_KEYS["monthly"]]["ru"] == "Ежемесячно"


@pytest.mark.parametrize(
    ("lang", "frequency", "sentence"),
    [
        ("ru", "monthly", "Примерно 1 доставка в месяц"),
        ("ru", "biweekly", "Примерно 2 доставки в месяц"),
        ("ru", "weekly", "Примерно 4 доставки в месяц"),
        ("ru", "daily", "Примерно 30 доставок в месяц"),
        ("en", "monthly", "About 1 delivery a month"),
        ("en", "weekly", "About 4 deliveries a month"),
        ("uz", "monthly", "Oyiga taxminan 1 ta yetkazib berish"),
        ("uz", "daily", "Oyiga taxminan 30 ta yetkazib berish"),
    ],
)
def test_the_preview_the_builder_calls_publishes_the_per_month_hint_in_the_page_language(
    app, db, auth_headers, sample_product, lang, frequency, sentence
):
    """The review step shows `deliveries_per_month_label` as it is: a whole count with the language's plural form
    (ru 1 доставка / 2 доставки / 30 доставок), never a decimal glued into one fixed word."""
    from business_app.models.translation import Translation
    from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, _category_for

    for key, values in BACKEND_TRANSLATIONS.items():
        if key.startswith(_HINT_KEY):
            db.session.add(Translation(key=key, language=lang, value=values[lang], category=_category_for(key),
                                       is_active=True))
    db.session.commit()

    response = app.test_client().post(
        f"/api/v1/subscriptions/preview?lang={lang}",
        headers=auth_headers,
        json={"frequency": frequency, "items": [{"product_id": sample_product.id, "quantity": 1}]},
    )

    assert response.status_code == 200, response.get_json()
    assert response.get_json()["data"]["preview"]["deliveries_per_month_label"] == sentence
