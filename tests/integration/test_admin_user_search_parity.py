"""Admin user-search parity with the staff bot's customer search.

The admin UI create-order picker (``admin_ui/src/pages/Orders.js``) drives
``GET /admin/users`` with a ``search`` term, so these tests hit that exact
endpoint with the payloads the picker actually sends. The staff bot has long
supported Latin<->Cyrillic names, formatted phones and multi-word queries via
:mod:`shared.user_search`; this endpoint must behave the same way.

Regression halves (email / company_name) are pinned here too: the same
``search`` param backs the Users page, Subscriptions, SupportInbox and
LinkedAccountsPanel, so widening name matching must not drop those columns.
"""

from datetime import UTC, datetime

import pytest

from business_app import db as _db
from business_app.models.user import User
from business_app.utils.password_security import hash_password
from shared.enums import UserRole, UserStatus, UserType


def _user(email, phone, first, last, role=UserRole.CUSTOMER, company=None):
    u = User(
        email=email,
        phone=phone,
        password_hash=hash_password("TestPassword123!"),
        first_name=first,
        last_name=last,
        company_name=company,
        user_type=UserType.INDIVIDUAL,
        role=role,
        status=UserStatus.ACTIVE,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    _db.session.add(u)
    _db.session.commit()
    return u


def _search(client, headers, term, **params):
    """Call the endpoint exactly as the create-order picker does."""
    resp = client.get(
        "/api/v1/admin/users",
        query_string={"search": term, "per_page": 50, **params},
        headers=headers,
    )
    assert resp.status_code == 200, resp.get_json()
    return [item["id"] for item in resp.get_json()["data"]["items"]]


@pytest.mark.integration
def test_latin_query_finds_cyrillic_stored_customer(client, db, admin_auth_headers):
    """Typing 'Aziz' must find a customer stored as 'Азиз'."""
    u = _user("cyr@example.com", "+998935100001", "Азиз", "Каримов")

    assert u.id in _search(client, admin_auth_headers, "Aziz")


@pytest.mark.integration
def test_cyrillic_query_finds_latin_stored_customer(client, db, admin_auth_headers):
    """Typing 'Азиз' must find a customer stored as 'Aziz'."""
    u = _user("lat@example.com", "+998935100002", "Aziz", "Karimov")

    assert u.id in _search(client, admin_auth_headers, "Азиз")


@pytest.mark.integration
def test_multi_word_query_matches_across_first_and_last_name(client, db, admin_auth_headers):
    """'Umar Xoldarbekov' spans two columns; neither column holds the whole string."""
    u = _user("mw@example.com", "+998935100003", "Umar", "Xoldarbekov")

    assert u.id in _search(client, admin_auth_headers, "Umar Xoldarbekov")


@pytest.mark.integration
def test_reversed_word_order_matches(client, db, admin_auth_headers):
    """Staff type surname-first as often as not."""
    u = _user("rw@example.com", "+998935100004", "Umar", "Xoldarbekov")

    assert u.id in _search(client, admin_auth_headers, "Xoldarbekov Umar")


@pytest.mark.integration
def test_partial_tokens_still_match(client, db, admin_auth_headers):
    """Prefix typing must narrow, not zero out, the picker."""
    u = _user("pt@example.com", "+998935100005", "Umar", "Xoldarbekov")

    assert u.id in _search(client, admin_auth_headers, "Uma Xol")


@pytest.mark.integration
def test_formatted_phone_query_matches_canonical_phone(client, db, admin_auth_headers):
    """Spaces and dashes must not defeat the match against '+998935101234'."""
    u = _user("ph@example.com", "+998935101234", "Phone", "Person")

    assert u.id in _search(client, admin_auth_headers, "93 510-12-34")


@pytest.mark.integration
def test_plus_prefixed_phone_query_matches(client, db, admin_auth_headers):
    u = _user("ph2@example.com", "+998935101235", "Phone", "Two")

    assert u.id in _search(client, admin_auth_headers, "+998 93 510 12 35")


@pytest.mark.integration
def test_email_search_still_works(client, db, admin_auth_headers):
    """Regression: the Users page searches by email through this same param."""
    u = _user("findme@example.com", "+998935100006", "Email", "Person")

    assert u.id in _search(client, admin_auth_headers, "findme@example.com")


@pytest.mark.integration
def test_company_name_search_still_works(client, db, admin_auth_headers):
    """Regression: corporate lookups search company_name through this same param."""
    u = _user("co@example.com", "+998935100007", "Corp", "Contact", company="Aqua Element LLC")

    assert u.id in _search(client, admin_auth_headers, "Aqua Element")


@pytest.mark.integration
def test_role_filter_restricts_picker_to_customers(client, db, admin_auth_headers):
    """The create-order picker passes role=customer; staff must not be selectable."""
    customer = _user("c@example.com", "+998935100008", "Aziz", "Customer")
    staff = _user("s@example.com", "+998935100009", "Aziz", "Operator", role=UserRole.OPERATOR)

    found = _search(client, admin_auth_headers, "Aziz", role="customer")

    assert customer.id in found
    assert staff.id not in found


@pytest.mark.integration
def test_unrelated_customer_is_not_returned(client, db, admin_auth_headers):
    """Widening must not turn the picker into a list-everyone control."""
    target = _user("t@example.com", "+998935100010", "Umar", "Xoldarbekov")
    other = _user("o@example.com", "+998935100011", "Bekzod", "Rahimov")

    found = _search(client, admin_auth_headers, "Umar Xoldarbekov")

    assert target.id in found
    assert other.id not in found
