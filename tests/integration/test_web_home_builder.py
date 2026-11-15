"""The subscription builder on / after the redesign (website redesign step 3, group "home", unit "constructor").

The page is fetched as the browser fetches it, in uz, ru and en, with the rows the seeds ship
(scripts/seed_web_ui_home_translations.py and the backend seed's landing.* / web.display_labels.* rows). What the
script (static/js/subscription-constructor.js) gets from the page is its data island; these tests read the island the
page really renders and replay the requests the script makes with it:
- sign-in from the builder goes to the island's `sign_in_url`, and /login sends the customer back to the builder
  (the script used to send `?redirect=`, which /login ignores, so the customer landed on /my-account);
- the ways to pay are the payments SSOT's customer-selectable methods, each named in the page language, so step 4 and
  the review never print the API's English display name;
- "Add new address" opens My addresses.
"""

import json
import re

import pytest
from bs4 import BeautifulSoup
from flask_jwt_extended import create_access_token

from business_app.models.translation import Translation
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, _category_for
from scripts.seed_web_ui_home_translations import CATEGORY, TRANSLATIONS as HOME
from shared.payment_methods import CUSTOMER_SELECTABLE_METHODS
from tests.integration.public_page_builders import flat, get_page, seed_copy

pytestmark = pytest.mark.integration

LANGS = ("uz", "ru", "en")
BUILDER_URL = "/#subscription-constructor"
SIGN_IN_URL = "/login?next=/%23subscription-constructor"
STEPS = ("products", "schedule", "address", "payment", "review")
# The builder's English-literal storefront keys (the rest are landing.* rows).
LITERAL_KEYS = ("Day {day} of the month", "Shown once you are signed in", "Please select a day of the month")
RAW_KEY = re.compile(r"^(web|landing)\.[a-z0-9_.]+$")


@pytest.fixture
def home_copy(app, db):
    """The rows the seeds ship for the builder: landing.* and web.display_labels.* from the backend seed, its three
    English-literal keys, and this page group's web.home.* rows."""
    seed_copy(db, ("landing.", "web.display_labels"))
    for key in LITERAL_KEYS:
        for language in LANGS:
            db.session.add(Translation(key=key, language=language, value=BACKEND_TRANSLATIONS[key][language],
                                       category=_category_for(key), is_active=True))
    for language, rows in HOME.items():
        for key, value in rows.items():
            db.session.add(Translation(key=key, language=language, value=value, category=CATEGORY, is_active=True))
    db.session.commit()
    app.jinja_env.cache.clear()


def _h(lang, key, **values):
    return HOME[lang][key].format(**values) if values else HOME[lang][key]


def _island(soup):
    return json.loads(soup.select_one("script#subscription-constructor-data").string)


def _strings(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, str):
        yield value


def _signed_in(app, user):
    with app.app_context():
        token = create_access_token(identity=str(user.id))
    browser = app.test_client()
    browser.set_cookie("access_token_cookie", token)
    # The first visit settles the page language in the session as a browser's does: the default language, whose
    # URLs carry no `?lang=` (any other language is added to a bare URL with one redirect).
    assert browser.get("/?lang=uz", follow_redirects=True).status_code == 200
    return browser


@pytest.mark.parametrize("lang", LANGS)
def test_the_builder_reads_in_the_visitors_language_with_every_hook_its_script_uses(app, db, home_copy, lang):
    soup = get_page(app, "/", lang)
    section = soup.select_one("#subscription-constructor")
    island = _island(soup)

    assert "web.home." not in str(section), "a web.home key reached the builder untranslated"
    assert not [s for s in _strings(island) if RAW_KEY.match(s)], "a raw key reached the builder's script"
    assert flat(section.select_one("#home-builder-title").get_text()) == _h(lang, "web.home.builder.title")
    # The section head is left-aligned like every other section's (the centred variant is for marketing intros).
    assert "ae-section-head--center" not in section.select_one(".ae-section-head")["class"]

    steps = section.select(".constructor-step")
    assert [s["data-step"] for s in steps] == ["1", "2", "3", "4", "5"]
    assert [flat(s.select_one(".step-label").get_text()) for s in steps] == [
        _h(lang, f"web.home.builder.step_{name}") for name in STEPS
    ]
    assert steps[0].get("aria-current") == "step"
    assert flat(section.select_one(".sc-compact").get_text()) == (
        f"{_h(lang, 'web.home.builder.step_of', current=1, total=5)}: {_h(lang, 'web.home.builder.step_products')}"
    )
    titles = [flat(section.select_one(f"#step-{n} .sc-panel-title").get_text()) for n in range(1, 6)]
    assert titles == [_h(lang, f"web.home.builder.{name}_title") for name in STEPS]

    # The script's hooks: panels, lists it renders into, the selects, Back / Next / Create, the inline message.
    for selector in ("#products-grid", "#addresses-list", "#payment-methods-grid", "#review-container",
                     "#time-slots-select", "#month-day-select", "#loading-overlay"):
        assert section.select_one(selector) is not None, selector
    assert flat(section.select_one(".nav-btn-next").get_text()) == _h(lang, "web.home.builder.next")
    assert flat(section.select_one(".nav-btn-prev").get_text()) == _h(lang, "web.home.builder.back")
    assert flat(section.select_one(".nav-btn-submit").get_text()) == _h(lang, "web.home.builder.create")
    error = section.select_one("#sc-error")
    assert error["role"] == "alert" and error.has_attr("hidden") and error.select_one(".sc-error__text") is not None

    # The words the script prints itself: the stepper line, the quantity control and a failed water list's retry.
    assert island["ui"] == {name: _h(lang, f"web.home.builder.{name}")
                            for name in ("step_of", "min_quantity", "quantity", "fewer", "more", "catalog_failed",
                                         "retry")}

    # The weekday buttons are named with the island's weekday names (0 = Monday), the ones the review prints, so a
    # day reads the same on its button and in the review; each button carries the browser's getDay() number.
    days = {int(b["data-day"]): flat(b.get_text()) for b in section.select("#day-selector .day-btn")}
    assert [days[(index + 1) % 7] for index in range(7)] == island["weekday_names"]
    assert {"{current}", "{total}"} <= set(re.findall(r"\{\w+\}", island["ui"]["step_of"]))
    assert "{count}" in island["ui"]["min_quantity"]


def test_a_signed_out_visitor_signs_in_and_comes_back_to_the_builder(app, db, home_copy, sample_user):
    soup = get_page(app, "/", "ru")
    link = soup.select_one("#subscription-constructor .sc-signin a")
    assert flat(soup.select_one("#subscription-constructor .sc-signin").get_text(" ")).startswith(
        _h("ru", "web.home.builder.sign_in_note")
    )
    # The note says "Sign in" and its button says the same verb (en "Sign in", not the header's "Login").
    assert flat(link.get_text()) == _h("ru", "web.home.builder.sign_in")
    assert flat(get_page(app, "/", "en").select_one("#subscription-constructor .sc-signin a").get_text()) == "Sign in"
    # The note's link and the script's redirect (Next on step 2, Create while signed out) are one URL.
    assert link["href"] == _island(soup)["sign_in_url"] == SIGN_IN_URL

    # The login page that URL opens sends the customer back to the builder once signed in ...
    login = BeautifulSoup(app.test_client().get(SIGN_IN_URL).get_data(as_text=True), "html.parser")
    data = [json.loads(s.string) for s in login.select('script[type="application/json"]') if s.string]
    assert [d["after_login_url"] for d in data if "after_login_url" in d] == [BUILDER_URL]
    # ... and a customer who is already signed in is sent straight there.
    response = _signed_in(app, sample_user).get(SIGN_IN_URL)
    assert response.status_code == 302
    assert response.headers["Location"].endswith(BUILDER_URL)

    # What the builder sent before: /login reads `next`, so `?redirect=` left the customer on My account.
    old = BeautifulSoup(app.test_client().get("/login?redirect=/").get_data(as_text=True), "html.parser")
    old_data = [json.loads(s.string) for s in old.select('script[type="application/json"]') if s.string]
    assert [d["after_login_url"] for d in old_data if "after_login_url" in d] == ["/my-account"]


def test_a_signed_in_customer_gets_no_sign_in_note(app, db, home_copy, sample_user):
    response = _signed_in(app, sample_user).get("/?lang=ru", follow_redirects=True)
    soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    assert response.status_code == 200
    assert soup.select_one("#subscription-constructor .constructor-container") is not None
    assert soup.select_one("#subscription-constructor .sc-signin") is None


@pytest.mark.parametrize("lang", ("ru", "uz"))
def test_every_way_to_pay_the_builder_is_offered_is_named_in_the_page_language(
    app, db, home_copy, client, auth_headers, lang
):
    """Step 4 and the review name a method with the island's label (the API's display_name is English)."""
    island = _island(get_page(app, "/", lang))
    selectable = {method.value for method in CUSTOMER_SELECTABLE_METHODS}
    assert set(island["method_labels"]) == set(island["method_notes"]) == selectable

    # The menu the script asks for (GET /payments/methods?context=subscription), as a customer gets it.
    response = client.get("/api/v1/payments/methods?context=subscription", headers=auth_headers)
    assert response.status_code == 200, response.get_json()
    offered = response.get_json()["data"]["available_methods"]
    assert offered, "the subscription menu offered no method"
    for method in offered:
        label = island["method_labels"][method["method"]]
        assert label == BACKEND_TRANSLATIONS[f"web.display_labels.payment_method.{method['method']}"][lang]
        assert island["method_notes"][method["method"]] == _h(lang, f"web.home.builder.method_note.{method['method']}")
        if method["method"] != "click":  # Click is a brand name in every language
            assert label != method["display_name"]


def test_add_new_address_opens_my_addresses(app, db, home_copy, sample_user):
    island = _island(get_page(app, "/", "en"))
    assert island["addresses_url"] == "/addresses"
    assert _signed_in(app, sample_user).get(island["addresses_url"]).status_code == 200


def test_the_manage_link_sits_under_the_builder_in_the_page_language(app, db, home_copy):
    soup = get_page(app, "/", "uz")
    [link] = soup.select("#subscription-constructor .more-btn a.ae-btn")
    assert link["href"] == "/my-subscriptions"
    assert flat(link.get_text()) == _h("uz", "web.home.builder.manage")
