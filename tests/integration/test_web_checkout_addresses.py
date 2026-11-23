"""My addresses (/addresses) and the address wizard after the redesign (website redesign step 3, group "checkout",
unit "addresses", 2026-11-14).

The page is fetched the way a signed-in customer's browser fetches it (the storefront's httpOnly cookie), in uz, ru
and en, with the rows the group seed ships (scripts/seed_web_ui_checkout_translations.py). The API calls are the ones
static/js/address-wizard.js and static/js/pages/addresses.js send, with the bodies the wizard builds
(tests/fixtures/web_addresses/*_payload.json; admin_ui/src/__tests__/web/addressWizard.test.js asserts the script
sends exactly those bodies over the shell captured here).

Owner decision 4 (2026-11-13): the district list is populated, Edit does not overwrite the saved address with
geocoder output, the list refreshes after a save. Found on the way and fixed: "Make main" and the wizard's
"main address" / "business address" ticks on edit changed nothing (PUT /auth/addresses/<id> dropped both flags);
deleting an address that orders use answered in hard-coded English.
"""

import json
import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup
from flask_jwt_extended import create_access_token
from sqlalchemy.exc import IntegrityError

from business_app.models.translation import Translation
from business_app.models.user import UserAddress
from scripts.seed_web_ui_checkout_translations import CATEGORY, TRANSLATIONS as CHECKOUT
from tests.integration.public_page_builders import seed_copy
from tests.integration.test_web_password_reset_and_change import WEB_HEADERS, _logged_in_browser

pytestmark = pytest.mark.integration

LANGS = ("uz", "ru", "en")
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "web_addresses"
NEW = json.loads((FIXTURES / "new_payload.json").read_text(encoding="utf-8"))
EDIT = json.loads((FIXTURES / "edit_payload.json").read_text(encoding="utf-8"))
SHELL = FIXTURES / "shell.html"
INSIDE = (41.2995, 69.2401)  # Tashkent centre, inside the delivery zone
OUTSIDE = (40.5, 70.9)


@pytest.fixture
def checkout_copy(app, db):
    """The storefront rows the backend seed ships (landing.*, cabinet.*) plus the checkout group's own rows."""
    seed_copy(db, ("landing.", "web.display_labels", "cabinet.", "api.addresses.error.pin_"))
    for language, rows in CHECKOUT.items():
        for key, value in rows.items():
            db.session.add(Translation(key=key, language=language, value=value, category=CATEGORY, is_active=True))
    db.session.commit()
    app.jinja_env.cache.clear()
    yield
    app.jinja_env.cache.clear()


def _browser(app, user):
    """The customer's browser after the storefront login: cookies + the CSRF value base.js apiRequest sends."""
    with app.app_context():
        token = create_access_token(identity=str(user.id))
    return _logged_in_browser(app, token)


def _api(app, user, method, path, body=None):
    browser, csrf = _browser(app, user)
    kwargs = {"headers": {**WEB_HEADERS, "X-CSRF-TOKEN": csrf}}
    if body is not None:
        kwargs["data"] = json.dumps(body)
    return browser.open(f"/api/v1{path}", method=method, **kwargs)


def _address(db, user, **fields):
    values = {
        "title": "Uy",
        "full_address": "Toshkent, Shayxontohur tumani, drive probe",
        "district": "shaykhontohur",
        "latitude": INSIDE[0],
        "longitude": INSIDE[1],
        "is_default": False,
    }
    values.update(fields)
    address = UserAddress(user_id=user.id, **values)
    db.session.add(address)
    db.session.commit()
    return address


def _page(app, db, user, lang):
    user.preferred_language = lang
    db.session.commit()
    browser, _csrf = _browser(app, user)
    response = browser.get(f"/addresses?lang={lang}", follow_redirects=True)
    html = response.get_data(as_text=True)
    assert response.status_code == 200, html[:500]
    return BeautifulSoup(html, "html.parser"), html


# ---- the page --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_the_addresses_page_renders_the_list_frame_and_the_wizard_in_each_language(
    app, db, sample_user, checkout_copy, lang
):
    soup, html = _page(app, db, sample_user, lang)

    assert "web.checkout." not in html
    assert soup.select_one("#addressesList") is not None
    assert soup.select_one("#noAddressesMessage") is not None
    assert len(soup.select('[data-action="show-add-address"]')) == 2  # page head + empty state
    assert soup.select_one("#addressWizardModal.ae-modal") is not None
    assert soup.select_one("#addressDeleteModal") is not None
    # The district list is a native select: script.js's nice-select skips `.ignore`, so the options the wizard
    # loads from geo-config are the ones the customer sees.
    assert "ignore" in soup.select_one("#districtSelect")["class"]
    # No Building/Entrance inputs: UserAddress has no column for them (the bot keeps the entrance in the note).
    assert soup.select_one("#buildingNumber") is None and soup.select_one("#entranceNumber") is None
    for field in (
        "addressTitle",
        "fullAddress",
        "streetAddress",
        "floorNumber",
        "apartmentNumber",
        "landmark",
        "deliveryInstructions",
        "isDefault",
        "isBusiness",
        "wizardPrevBtn",
        "wizardNextBtn",
        "wizardSaveBtn",
    ):
        assert soup.select_one(f"#{field}") is not None, field

    wizard = json.loads(soup.select_one("#address-wizard-data").string)
    rows = CHECKOUT[lang]
    assert wizard["i18n"]["step_labels"] == [
        rows["web.checkout.wizard.step_map"],
        rows["web.checkout.wizard.step_details"],
        rows["web.checkout.wizard.step_confirm"],
    ]
    assert wizard["i18n"]["step_count"] == rows["web.checkout.wizard.step_count"]
    # Leaflet comes from the vendored copy, on first open, never from a CDN.
    assert wizard["leaflet"]["js"].endswith("/static/vendor/leaflet/leaflet.js")
    assert "unpkg.com" not in html
    page_data = json.loads(soup.select_one("#page-data").string)
    assert page_data["i18n"]["delete_text"] == rows["web.checkout.addr.delete_text"]
    assert all(value and not value.startswith("web.") for value in page_data["i18n"].values())
    # The page's own stylesheet and the wizard's, both versioned.
    assert re.search(r'href="/static/css/pages/checkout\.css\?v=\d+"', html)
    assert re.search(r'href="/static/css/address-wizard\.css\?v=\d+"', html)
    # The old English-key copy is gone.
    for retired in ("Add New Address", "Select Location", "Address Summary", "Apartment/Floor"):
        assert retired not in html

    if lang == "ru":
        _check_shell(soup)


def _check_shell(soup):
    """The markup + data islands the jsdom test runs the shipped scripts over. UPDATE_WEB_FIXTURES=1 rewrites it."""
    parts = [
        str(soup.select_one(selector))
        for selector in (
            "#addressesPage",
            "#addressWizardModal",
            "#addressDeleteModal",
            "#address-wizard-data",
            "#page-data",
        )
    ]
    shell = re.sub(r"\?v=\d+", "", "\n".join(parts)) + "\n"
    if os.environ.get("UPDATE_WEB_FIXTURES"):
        SHELL.write_text(shell, encoding="utf-8")
    assert SHELL.read_text(encoding="utf-8") == shell, "stale shell: rerun with UPDATE_WEB_FIXTURES=1"


def test_the_district_list_the_wizard_fills_comes_from_geo_config(client):
    """GET /addresses/geo-config is what populates #districtSelect: keys + names in the page's language."""
    response = client.get("/api/v1/addresses/geo-config?lang=ru")
    data = response.get_json()["data"]
    assert response.status_code == 200
    assert len(data["districts"]) >= 12
    names = {d["key"]: d["name"] for d in data["districts"]}
    assert names["chilanzar"] and names["chilanzar"] != "chilanzar"
    assert data["polygon"], "the wizard draws and checks the delivery zone from this polygon"


# ---- saving (the wizard's exact bodies) ------------------------------------------------------------------------


def test_the_wizard_saves_a_new_address_with_every_field_it_collects(app, db, sample_user):
    response = _api(app, sample_user, "POST", "/auth/addresses", NEW)

    assert response.status_code == 201, response.get_json()
    saved = response.get_json()["data"]["address"]
    row = db.session.get(UserAddress, saved["id"])
    for field in (
        "title",
        "full_address",
        "street_address",
        "district",
        "floor_number",
        "apartment_number",
        "delivery_instructions",
        "landmark",
        "is_default",
        "is_business",
    ):
        assert getattr(row, field) == NEW[field], field
    assert (row.latitude, row.longitude) == (NEW["latitude"], NEW["longitude"])
    # The list the page reloads after the 'ae:address-saved' event shows it.
    listed = _api(app, sample_user, "GET", "/auth/addresses").get_json()["data"]["addresses"]
    assert [a["id"] for a in listed] == [saved["id"]]


def test_editing_keeps_the_customer_text_and_applies_the_main_and_business_ticks(app, db, sample_user):
    current_main = _address(db, sample_user, title="Try-out", is_default=True)
    edited = _address(db, sample_user, title="Drive branch 3", floor_number="3", landmark="old")

    response = _api(app, sample_user, "PUT", f"/auth/addresses/{edited.id}", EDIT)

    assert response.status_code == 200, response.get_json()
    db.session.expire_all()
    row = db.session.get(UserAddress, edited.id)
    assert row.full_address == EDIT["full_address"]  # the customer's own text, not a geocoder's
    assert row.title == "Офис" and row.district == "shaykhontohur"
    assert row.floor_number is None and row.landmark is None  # cleared fields are cleared
    assert row.is_business is True
    assert row.is_default is True
    assert db.session.get(UserAddress, current_main.id).is_default is False


def test_unticking_main_on_the_main_address_leaves_it_main(app, db, sample_user):
    main = _address(db, sample_user, is_default=True)

    response = _api(app, sample_user, "PUT", f"/auth/addresses/{main.id}", {**EDIT, "is_default": False})

    assert response.status_code == 200
    db.session.expire_all()
    assert db.session.get(UserAddress, main.id).is_default is True


def test_make_main_on_a_card_moves_the_main_address(app, db, sample_user):
    old = _address(db, sample_user, is_default=True)
    new = _address(db, sample_user, title="Ish")

    response = _api(app, sample_user, "PATCH", f"/auth/addresses/{new.id}/set-default")

    assert response.status_code == 200
    db.session.expire_all()
    assert db.session.get(UserAddress, new.id).is_default is True
    assert db.session.get(UserAddress, old.id).is_default is False


def test_a_point_outside_the_delivery_zone_is_refused_on_save(app, db, sample_user):
    body = {**NEW, "latitude": OUTSIDE[0], "longitude": OUTSIDE[1]}

    response = _api(app, sample_user, "POST", "/auth/addresses", body)

    assert response.status_code == 400
    assert UserAddress.query.filter_by(user_id=sample_user.id).count() == 0


def test_deleting_an_address_orders_use_answers_in_the_customer_language(app, db, sample_user, checkout_copy):
    """Orders keep a foreign key to the address (Postgres refuses the delete; the SQLite test DB has FKs off, so the
    refusal is raised at the delete itself)."""
    address = _address(db, sample_user)
    sample_user.preferred_language = "ru"
    db.session.commit()

    from business_app.services import auth_service

    real = auth_service.db.session.delete

    def refuse(obj):
        if isinstance(obj, UserAddress):
            raise IntegrityError("DELETE FROM addresses", {}, Exception("orders_delivery_address_id_fkey"))
        return real(obj)

    with patch.object(auth_service.db.session, "delete", side_effect=refuse):
        response = _api(app, sample_user, "DELETE", f"/auth/addresses/{address.id}")

    assert response.status_code == 400
    assert response.get_json()["message"] == CHECKOUT["ru"]["web.checkout.addr.in_use"]
    assert db.session.get(UserAddress, address.id) is not None


def test_a_spare_address_is_deleted(app, db, sample_user):
    _address(db, sample_user, is_default=True)
    spare = _address(db, sample_user, title="Ish")

    response = _api(app, sample_user, "DELETE", f"/auth/addresses/{spare.id}")

    assert response.status_code == 200
    assert db.session.get(UserAddress, spare.id) is None


# ---- the pin: reverse geocoding publishes the district key -----------------------------------------------------


@pytest.mark.parametrize(
    "geocoder_district,expected_key",
    [("Chilonzor tumani", "chilanzar"), ("Mirzo Ulug‘bek Tumani", "mirzo_ulugbek"), ("Some street", None)],
)
def test_reverse_geocode_publishes_the_canonical_district_key(app, db, sample_user, geocoder_district, expected_key):
    """The wizard selects the district from `district_key` (district_text.canonical_district), never by matching
    names in JS. The geocoder is the external call, so it is the one thing mocked."""
    answer = {"formatted_address": "Toshkent, Chilonzor", "address_components": {"city_district": geocoder_district}}
    with patch("business_app.services.maps_service.MapsService.reverse_geocode", return_value=answer):
        response = _api(
            app, sample_user, "POST", "/addresses/reverse-geocode", {"latitude": INSIDE[0], "longitude": INSIDE[1]}
        )

    assert response.status_code == 200, response.get_json()
    data = response.get_json()["data"]
    assert data["formatted_address"] == "Toshkent, Chilonzor"
    assert data["district"] == geocoder_district
    assert data["district_key"] == expected_key
