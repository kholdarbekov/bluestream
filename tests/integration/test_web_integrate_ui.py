"""The shared-component integration of the website redesign (step 3, integrator "integrate-ui", 2026-11-14).

Driven the way a visitor uses the site:
- the referral link /my-loyalty shares (GET /api/v1/loyalty/referral -> `referral_link`, /register?ref=CODE) opens
  the sign-up page with the code already in its field, and the sign-up register.js then posts records the referral;
  anything in `ref` that is not a code is ignored;
- the offer block's "Prices as of" line shows people the date in their language and keeps the ISO date for machines
  (<time datetime>); without the date-format row the ISO date stands in;
- every page drops jQuery UI and the theme's dead plugins (site review SITE-05: no page used animate, owl, fancybox,
  flaticon, WOW, validation, appear or nice-select; the builder's selects are native).
"""

import json
from datetime import UTC, datetime
from urllib.parse import urlsplit

import pytest
from bs4 import BeautifulSoup

from business_app.models.translation import Translation
from business_app.models.user import User
from scripts.seed_web_ui_integration_translations import CATEGORY, TRANSLATIONS as INTEGRATION
from tests.integration.public_page_builders import LANGS, flat, page, shipped
from tests.integration.public_page_builders import offer_skus  # noqa: F401 -- fixture
from tests.integration.sales_pay_builders import freeze_local_now
from tests.integration.test_loyalty_api_integration import loyalty_account, loyalty_program  # noqa: F401 -- fixtures
from tests.integration.test_web_auth_forms import auth_copy  # noqa: F401 -- fixture
from tests.integration.test_web_password_reset_and_change import WEB_HEADERS

pytestmark = pytest.mark.integration


def _register_page(app, query, lang="en"):
    response = app.test_client().get(f"/register?{query}&lang={lang}", follow_redirects=True)
    assert response.status_code == 200
    return BeautifulSoup(response.get_data(as_text=True), "html.parser")


# ----------------------------------------------------------------------------------------------- referral link


def test_the_shared_referral_link_prefills_the_code_and_the_sign_up_records_it(
    app, db, auth_copy, auth_headers, sample_user, loyalty_account
):
    shared = app.test_client().get("/api/v1/loyalty/referral", headers=auth_headers)
    assert shared.status_code == 200, shared.get_json()
    link = urlsplit(shared.get_json()["data"]["referral_link"])
    code = shared.get_json()["data"]["referral_code"]
    assert link.path == "/register"

    for lang in LANGS:
        soup = _register_page(app, link.query, lang)
        field = soup.select_one("#registerForm input#referral_code")
        assert field["value"] == code
        assert field.find_parent("details").has_attr("open")  # the code is visible, not folded away

    # The body register.js posts for an email sign-up: the field's value goes as referral_code.
    answer = app.test_client().post(
        "/api/v1/auth/register",
        headers=WEB_HEADERS,
        data=json.dumps(
            {"first_name": "Dilnoza", "last_name": "", "password": "Abcdefg1", "referral_code": field["value"],
             "email": "referred-friend@example.com"}
        ),
    )
    assert answer.status_code == 201, answer.get_json()
    assert User.query.filter_by(email="referred-friend@example.com").one().referred_by_user_id == sample_user.id


@pytest.mark.parametrize(
    "ref",
    ["%3Cscript%3Ealert(1)%3C%2Fscript%3E", "REF%20ABC", "REF%22onfocus%3D1", "A" * 21, "РЕФ123", ""],
)
def test_a_ref_that_is_not_a_code_is_ignored(app, db, auth_copy, ref):
    soup = _register_page(app, f"ref={ref}")
    field = soup.select_one("#registerForm input#referral_code")
    assert not field.has_attr("value")
    assert not field.find_parent("details").has_attr("open")


def test_without_a_ref_the_referral_field_stays_folded(app, db, auth_copy):
    response = app.test_client().get("/register?lang=ru", follow_redirects=True)
    soup = BeautifulSoup(response.get_data(as_text=True), "html.parser")
    field = soup.select_one("#registerForm input#referral_code")
    assert not field.has_attr("value") and not field.find_parent("details").has_attr("open")


# ---------------------------------------------------------------------------------------------- offer "as of"


def _seed_date_format(db):
    for language, rows in INTEGRATION.items():
        for key, value in rows.items():
            db.session.add(Translation(key=key, language=language, value=value, category=CATEGORY, is_active=True))
    db.session.commit()


@pytest.mark.parametrize(
    "lang, shown", [("en", "13 November 2026"), ("ru", "13 ноября 2026"), ("uz", "2026-yil 13-noyabr")]
)
def test_the_as_of_line_shows_people_the_date_and_machines_the_iso_date(app, db, offer_skus, monkeypatch, lang, shown):
    freeze_local_now(monkeypatch, datetime(2026, 11, 12, 20, 30, tzinfo=UTC))  # 01:30 on the 13th in Tashkent
    _seed_date_format(db)

    line = page(app, db, "/", lang).select_one('[data-offer-facts] [data-offer-line="as-of"]')

    assert flat(line.get_text()) == flat(shipped("landing.offer.as_of", lang).format(date=shown))
    assert line.select_one("time")["datetime"] == "2026-11-13"
    assert line.select_one("time").get_text() == shown


def test_without_its_date_format_row_the_as_of_line_prints_the_iso_date(app, db, offer_skus, monkeypatch):
    freeze_local_now(monkeypatch, datetime(2026, 11, 12, 20, 30, tzinfo=UTC))

    line = page(app, db, "/pricing-policy", "en").select_one('[data-offer-line="as-of"]')

    assert flat(line.get_text()) == shipped("landing.offer.as_of", "en").format(date="2026-11-13")
    assert line.select_one("time")["datetime"] == "2026-11-13"
    assert "web.integration" not in line.get_text()


def test_llms_txt_keeps_the_plain_iso_date(app, db, offer_skus, monkeypatch):
    freeze_local_now(monkeypatch, datetime(2026, 11, 12, 20, 30, tzinfo=UTC))
    _seed_date_format(db)
    page(app, db, "/", "en")  # seeds the landing.* rows llms.txt reads

    text = app.test_client().get("/llms.txt").get_data(as_text=True)

    assert shipped("landing.offer.as_of", "en").format(date="2026-11-13") in text
    assert "<time" not in text


# ---------------------------------------------------------------------------------------------------- shell


DEAD_THEME_ASSETS = (
    "jquery-ui", "nice-select", "animate.css", "owl.css", "owl.js", "flaticon.css", "jquery.fancybox", "wow.js",
    "validation.js", "appear.js", "custom-base.css",
)


@pytest.mark.parametrize("path", ["/about", "/", "/shop"])
def test_pages_load_none_of_the_dead_theme_plugins(app, db, path):
    html = app.test_client().get(f"{path}?lang=en", follow_redirects=True).get_data(as_text=True)

    for asset in DEAD_THEME_ASSETS:
        assert asset not in html, f"{path} still loads {asset}"
    # What stays: jQuery and Bootstrap's modals (order, subscription and address-wizard modals) and the shell script.
    assert "js/jquery.js" in html and "js/bootstrap.min.js" in html and "js/script.js" in html
