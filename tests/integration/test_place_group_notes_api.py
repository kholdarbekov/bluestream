"""Place-group admin note, one label normaliser, and the group label on search hits (HTTP)."""
from datetime import UTC, datetime

import pytest

from business_app import db as _db
from business_app.models.customer_link import CustomerLinkEvent
from business_app.models.user import User, UserAddress
from business_app.utils.password_security import hash_password
from shared.enums import UserRole, UserStatus, UserType

BASE = "/api/v1/admin"


def _customer(n):
    u = User(
        email=f"pn{n}@example.com", phone=f"+9987911{n:04d}", password_hash=hash_password("TestPassword123!"),
        first_name="Note", last_name=f"Cust{n}", user_type=UserType.INDIVIDUAL, role=UserRole.CUSTOMER,
        status=UserStatus.ACTIVE, is_verified=True, created_at=datetime.now(UTC),
    )
    _db.session.add(u)
    _db.session.commit()
    return u


def _address(user):
    a = UserAddress(user_id=user.id, title="work", full_address="Office 12 street",
                    latitude=41.31, longitude=69.28)
    _db.session.add(a)
    _db.session.commit()
    return a


def _make(client, headers, base_n, **extra):
    a, b = _address(_customer(base_n)), _address(_customer(base_n + 1))
    body = {"addressIds": [a.id, b.id], "reason": "r", **extra}
    r = client.post(f"{BASE}/place-groups", json=body, headers=headers)
    return r, a, b


def _detail(client, headers, gid):
    r = client.get(f"{BASE}/place-groups/{gid}", headers=headers)
    assert r.status_code == 200, r.get_json()
    return r.get_json()["data"]


def _rename_events(gid):
    return CustomerLinkEvent.query.filter(
        CustomerLinkEvent.event_type == "rename_place_group",
        CustomerLinkEvent.reason.like(f"[group {gid}]%"),
    ).all()


@pytest.mark.integration
def test_create_with_notes_is_returned_by_detail(client, db, admin_claim_headers):
    r, _, _ = _make(client, admin_claim_headers, 10, label=" Office 12 ", notes="Gate code 12")
    assert r.status_code == 201, r.get_json()
    d = _detail(client, admin_claim_headers, r.get_json()["data"]["place_group_id"])
    assert d["label"] == "Office 12"
    assert d["notes"] == "Gate code 12"


@pytest.mark.integration
def test_create_blank_label_becomes_none_and_long_label_is_refused(client, db, admin_claim_headers):
    r, _, _ = _make(client, admin_claim_headers, 20, label="   ")
    assert r.status_code == 201, r.get_json()
    d = _detail(client, admin_claim_headers, r.get_json()["data"]["place_group_id"])
    assert d["label"] is None
    assert d["notes"] is None
    bad, _, _ = _make(client, admin_claim_headers, 30, label="x" * 101)
    assert bad.status_code == 400
    assert bad.get_json()["data"]["error_code"] == "PLACE_GROUP_LABEL_INVALID"


@pytest.mark.integration
def test_rename_notes_only_on_unlabelled_place(client, db, admin_claim_headers):
    r, _, _ = _make(client, admin_claim_headers, 40)
    gid = r.get_json()["data"]["place_group_id"]
    p = client.patch(f"{BASE}/place-groups/{gid}", json={"notes": "Back door", "reason": "r"},
                     headers=admin_claim_headers)
    assert p.status_code == 200, p.get_json()
    assert p.get_json()["data"] == {"place_group_id": gid, "label": None, "notes": "Back door"}
    d = _detail(client, admin_claim_headers, gid)
    assert d["label"] is None and d["notes"] == "Back door"
    events = _rename_events(gid)
    assert len(events) == 1
    assert events[0].event_metadata["notes_changed"] is True
    assert events[0].reason.endswith(" | note updated")
    assert "→" not in events[0].reason


@pytest.mark.integration
def test_label_only_rename_keeps_notes(client, db, admin_claim_headers):
    r, _, _ = _make(client, admin_claim_headers, 50, label="A", notes="Gate 12")
    gid = r.get_json()["data"]["place_group_id"]
    p = client.patch(f"{BASE}/place-groups/{gid}", json={"label": "B", "reason": "r"},
                     headers=admin_claim_headers)
    assert p.status_code == 200, p.get_json()
    d = _detail(client, admin_claim_headers, gid)
    assert d["label"] == "B" and d["notes"] == "Gate 12"
    ev = _rename_events(gid)
    assert len(ev) == 1 and ev[0].event_metadata["notes_changed"] is False
    assert "note updated" not in ev[0].reason


@pytest.mark.integration
def test_rename_with_nothing_changed_writes_no_event(client, db, admin_claim_headers):
    r, _, _ = _make(client, admin_claim_headers, 60, label="A", notes="N")
    gid = r.get_json()["data"]["place_group_id"]
    p = client.patch(f"{BASE}/place-groups/{gid}", json={"label": "A", "notes": " N ", "reason": "r"},
                     headers=admin_claim_headers)
    assert p.status_code == 200, p.get_json()
    assert p.get_json()["data"] == {"place_group_id": gid, "label": "A", "notes": "N"}
    assert _rename_events(gid) == []


@pytest.mark.integration
def test_rename_without_label_or_notes_is_refused(client, db, admin_claim_headers):
    r, _, _ = _make(client, admin_claim_headers, 70, label="A")
    gid = r.get_json()["data"]["place_group_id"]
    p = client.patch(f"{BASE}/place-groups/{gid}", json={"reason": "r"}, headers=admin_claim_headers)
    assert p.status_code == 400
    assert p.get_json()["data"]["error_code"] == "PLACE_GROUP_LABEL_INVALID"


@pytest.mark.integration
def test_notes_over_1000_are_refused(client, db, admin_claim_headers):
    bad, _, _ = _make(client, admin_claim_headers, 80, notes="n" * 1001)
    assert bad.status_code == 400
    assert bad.get_json()["data"]["error_code"] == "PLACE_GROUP_NOTES_INVALID"
    r, _, _ = _make(client, admin_claim_headers, 90, label="A", notes="n" * 1000)
    assert r.status_code == 201, r.get_json()
    gid = r.get_json()["data"]["place_group_id"]
    p = client.patch(f"{BASE}/place-groups/{gid}", json={"notes": "n" * 1001, "reason": "r"},
                     headers=admin_claim_headers)
    assert p.status_code == 400
    assert p.get_json()["data"]["error_code"] == "PLACE_GROUP_NOTES_INVALID"
    # Blank clears the note.
    c = client.patch(f"{BASE}/place-groups/{gid}", json={"notes": "  ", "reason": "r"},
                     headers=admin_claim_headers)
    assert c.status_code == 200 and c.get_json()["data"]["notes"] is None


@pytest.mark.integration
def test_non_string_notes_or_label_are_400_not_500(client, db, admin_claim_headers):
    """A number where the dialog sends text is refused with the field's own code, on create and on rename."""
    bad_create, _, _ = _make(client, admin_claim_headers, 120, notes=12)
    assert bad_create.status_code == 400, bad_create.get_json()
    assert bad_create.get_json()["data"]["error_code"] == "PLACE_GROUP_NOTES_INVALID"
    bad_label, _, _ = _make(client, admin_claim_headers, 130, label=12)
    assert bad_label.status_code == 400, bad_label.get_json()
    assert bad_label.get_json()["data"]["error_code"] == "PLACE_GROUP_LABEL_INVALID"

    r, _, _ = _make(client, admin_claim_headers, 140, label="A", notes="Gate 12")
    gid = r.get_json()["data"]["place_group_id"]
    for body, code in (
        ({"notes": 12, "reason": "r"}, "PLACE_GROUP_NOTES_INVALID"),
        ({"label": 12, "reason": "r"}, "PLACE_GROUP_LABEL_INVALID"),
        ({"label": ["A"], "notes": "x", "reason": "r"}, "PLACE_GROUP_LABEL_INVALID"),
    ):
        p = client.patch(f"{BASE}/place-groups/{gid}", json=body, headers=admin_claim_headers)
        assert p.status_code == 400, (body, p.get_json())
        assert p.get_json()["data"]["error_code"] == code
    d = _detail(client, admin_claim_headers, gid)
    assert (d["label"], d["notes"]) == ("A", "Gate 12")
    assert _rename_events(gid) == []


@pytest.mark.integration
def test_search_hit_carries_group_label(client, db, admin_claim_headers):
    r, a, b = _make(client, admin_claim_headers, 100, label="Office 12")
    assert r.status_code == 201, r.get_json()
    lone = _address(_customer(110))
    s = client.get(f"{BASE}/addresses/search?q=+99879110100&exclude_grouped=0", headers=admin_claim_headers)
    assert s.status_code == 200, s.get_json()
    rows = {h["address_id"]: h for h in s.get_json()["data"]["addresses"]}
    assert rows[a.id]["address_group_label"] == "Office 12"
    s2 = client.get(f"{BASE}/addresses/search?q=Cust110&exclude_grouped=0", headers=admin_claim_headers)
    rows2 = {h["address_id"]: h for h in s2.get_json()["data"]["addresses"]}
    assert rows2[lone.id]["address_group_label"] is None
