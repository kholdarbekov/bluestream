"""The cabinet pages never print "None" for a missing name or email, and never show a sign-up
placeholder email (D23, Owner calls 14)."""

import pytest
from bs4 import BeautifulSoup

from business_app.models.user import User
from business_app.services.auth_service import AuthService
from business_app.utils.translations import get_translation
from tests.integration.reorder_reminder_builders import customer_headers

pytestmark = pytest.mark.integration

PHONE = "+998901112233"
PLACEHOLDER = "telegram_810000001@bluestream.local"


@pytest.fixture
def customer(db, sample_user):
    """A phone-only customer: first name only, no last name, no email."""
    sample_user.first_name = "Ali"
    sample_user.last_name = None
    sample_user.email = None
    sample_user.phone = PHONE
    db.session.commit()
    return sample_user


def _page(app, user, path):
    response = app.test_client().get(f"{path}?lang=en", headers=customer_headers(app, user), follow_redirects=True)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    return BeautifulSoup(html, "html.parser"), html


def _set(db, user, **fields):
    for key, value in fields.items():
        setattr(user, key, value)
    db.session.commit()


def test_the_sidebar_names_a_phone_only_customer_and_shows_the_phone(app, customer):
    soup, html = _page(app, customer, "/profile-settings")

    box = soup.select_one(".profile-info")
    assert box.select_one(".ae-cabinet-nav__name").get_text(strip=True) == "Ali"
    assert box.select_one("p.text-muted").get_text(strip=True) == PHONE
    assert "None" not in box.get_text()


def test_a_placeholder_email_is_never_shown(app, db, customer):
    _set(db, customer, email=PLACEHOLDER, phone=None)

    soup, html = _page(app, customer, "/profile-settings")

    assert PLACEHOLDER not in html
    assert soup.select_one(".profile-info p.text-muted") is None


def test_no_contact_line_without_email_or_phone(app, db, customer):
    _set(db, customer, phone=None)

    soup, _ = _page(app, customer, "/profile-settings")

    assert soup.select_one(".profile-info p.text-muted") is None
    assert soup.select_one(".profile-info .ae-cabinet-nav__name").get_text(strip=True) == "Ali"


def _email_row(soup):
    """The profile's read-only email row (website redesign 2026-11-14: contacts are rows, not a form)."""
    return soup.select_one('[data-contact="email"] dd')


def test_profile_settings_hides_the_email_field_without_a_real_email(app, db, customer):
    soup, html = _page(app, customer, "/profile-settings")
    assert soup.select_one(".ae-acx__email") is None
    assert "None" not in _email_row(soup).get_text() and 'value="None"' not in html

    _set(db, customer, email=PLACEHOLDER)
    soup, html = _page(app, customer, "/profile-settings")
    assert soup.select_one(".ae-acx__email") is None and PLACEHOLDER not in html


def test_profile_settings_keeps_a_real_email(app, db, customer):
    _set(db, customer, email="ali@example.com")

    soup, _ = _page(app, customer, "/profile-settings")

    assert _email_row(soup).select_one(".ae-acx__email").get_text(strip=True) == "ali@example.com"
    assert soup.select_one(".profile-info p.text-muted").get_text(strip=True) == "ali@example.com"


@pytest.mark.parametrize(
    "first, header, welcome",
    [("Ali", "Ali", "Welcome back, Ali!"), (None, "LABEL", "Welcome back!"), ("Telegram User", "LABEL", "Welcome back!")],
)
def test_the_header_and_welcome_line_use_the_greeting_name(app, db, customer, first, header, welcome):
    _set(db, customer, first_name=first)
    label = get_translation("landing.nav.my_account", "en")

    soup, _ = _page(app, customer, "/my-account")

    assert soup.select(".user-menu button.dropdown-toggle")[0].get_text(strip=True) == (
        label if header == "LABEL" else header
    )
    assert soup.select_one(".welcome-section .ae-page-head__title").get_text(strip=True) == welcome


def test_the_bot_profile_payload_publishes_the_finished_lines(app, db, customer):
    """Pins the key set tests/telegram_bot/test_bot_profile_never_prints_none.py stands in for."""
    _set(db, customer, email=PLACEHOLDER)

    payload = AuthService().get_user_profile_data(customer.id)

    assert payload["full_name"] == "Ali"
    assert payload["contact_email"] is None
    assert {"id", "email", "phone", "first_name", "last_name", "date_of_birth", "gender", "role", "status",
            "email_verified", "phone_verified", "created_at", "last_login", "preferred_language",
            "loyalty_eligible", "permissions"} <= set(payload)


def test_to_dict_full_name_drops_missing_parts():
    assert User(first_name="Ali", last_name=None).to_dict()["full_name"] == "Ali"
