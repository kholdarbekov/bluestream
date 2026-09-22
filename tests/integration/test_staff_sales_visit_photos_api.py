"""POST /api/v1/staff/sales/visits/<id>/photos -- the 📷 button, driven as the staff bot drives it.

Multipart, through HTTP, because that is the half a service test cannot see: the bot posts a
file part plus two form scalars, and a route that read them off `request.get_json()` -- or
that handed an already-consumed stream to storage -- is green in every service test ever
written and ships an empty JPEG to Tashkent.

`FileStorageService` is the ONE thing stubbed here (D17, and the spec's *Testing* section:
"mock only external I/O"). The stub carries the real `upload_image` signature on purpose --
a `lambda *a, **k: {...}` accepts any call, so it would keep passing after the service
started writing into the wrong folder, under the wrong user, or with the resize turned off
(`feedback_no_op_stubs_hide_payload_drift`).
"""
import hashlib
from io import BytesIO

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.sales import Outlet, SalesAgentProfile
from business_app.models.sales_visits import VisitPhoto
from business_app.models.staff import StaffActivityLog
from business_app.services.sales import visit_service as visit_service_module
from business_app.utils.exceptions import FileStorageError
from shared.staff_constants import STAFF_ACTIONS
from tests.integration.test_staff_sales_visits_api import VISITS, _approved_outlet, _start
from tests.unit.test_sales_agent_role import make_sales_agent_user

pytestmark = pytest.mark.integration

OUTLETS = "/api/v1/staff/sales/outlets"
# Deterministic bytes, so the digest under test is the digest of what was POSTED.
STOREFRONT_BYTES = b"\xff\xd8\xff\xe0BAHOR-STOREFRONT-2026"
SHELF_BYTES = b"\xff\xd8\xff\xe0BAHOR-SHELF-2026"
STOREFRONT_SHA = hashlib.sha256(STOREFRONT_BYTES).hexdigest()
PHOTO_KEYS = {
    "id", "visit_id", "kind", "file_path", "sha256", "telegram_file_unique_id",
    "received_at", "duplicate_of_photo_id", "is_duplicate",
}


def _multipart_headers(headers):
    """Authorization ONLY.

    The shared auth fixtures pin `Content-Type: application/json`; passing that through makes
    werkzeug parse a multipart body as JSON and hand the route an empty `request.files`, so
    the route answers SALES_PHOTO_INVALID for a photo that was really there.
    """
    return {"Authorization": headers["Authorization"]}


def _post_photo(client, headers, visit_id, body, *, kind="storefront", filename="shelf.jpg",
                unique_id="AgADBAADqAADq2"):
    data = {"kind": kind, "file": (BytesIO(body), filename)}
    if unique_id is not None:
        data["telegram_file_unique_id"] = unique_id
    return client.post(
        f"{VISITS}/{visit_id}/photos",
        data=data,
        headers=_multipart_headers(headers),
        content_type="multipart/form-data",
    )


@pytest.fixture
def fake_storage(monkeypatch):
    """The single external-I/O seam on this path."""

    class _Storage:
        def __init__(self):
            self.calls = []
            self.error = None

        def upload_image(self, file, filename, folder="images", user_id=None, resize=True,
                         max_width=1920, max_height=1080, quality=85):
            if self.error is not None:
                raise self.error
            body = file.read()
            self.calls.append({
                "filename": filename, "folder": folder, "user_id": user_id,
                "resize": resize, "bytes": body,
            })
            # A DIFFERENT path on every call, exactly as a real resize+rename produces: it
            # is what proves the sha comes from the POSTED bytes, not from the artefact.
            return {"file_path": f"{folder}/{user_id}/{len(self.calls)}_{filename}", "is_image": True}

    storage = _Storage()
    monkeypatch.setattr(visit_service_module, "get_file_storage_service", lambda: storage)
    return storage


def test_a_photo_is_stored_hashed_and_answered_as_its_own_row(
    client, db, fake_storage, sales_agent_auth_headers, operator_auth_headers, sales_agent_user
):
    outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers)
    visit_id = _start(client, sales_agent_auth_headers, outlet["id"])["id"]

    created = _post_photo(client, sales_agent_auth_headers, visit_id, STOREFRONT_BYTES)

    assert created.status_code == 201, created.get_data(as_text=True)
    photo = created.get_json()["data"]["photo"]
    assert set(photo) == PHOTO_KEYS
    assert photo["visit_id"] == visit_id and photo["kind"] == "storefront"
    assert photo["sha256"] == STOREFRONT_SHA
    assert photo["is_duplicate"] is False and photo["duplicate_of_photo_id"] is None
    assert photo["telegram_file_unique_id"] == "AgADBAADqAADq2"
    assert photo["received_at"].endswith("+00:00")
    # The path is the storage service's answer, echoed -- never one the route invented.
    assert photo["file_path"] == f"sales_visits/{sales_agent_user.id}/1_shelf.jpg"

    # The payload the service handed storage, field by field (R8).
    assert len(fake_storage.calls) == 1
    call = fake_storage.calls[0]
    assert call["folder"] == "sales_visits"
    assert call["user_id"] == sales_agent_user.id
    assert call["resize"] is True
    assert call["filename"] == "shelf.jpg"
    # The WHOLE file reached storage: the sha is taken by reading the stream, so a missing
    # rewind stores a zero-byte JPEG and every test that only checks the row still passes.
    assert call["bytes"] == STOREFRONT_BYTES

    row = VisitPhoto.query.filter_by(visit_id=visit_id).one()
    assert (row.sha256, row.kind, row.duplicate_of_photo_id) == (STOREFRONT_SHA, "storefront", None)

    # The audit row, asserted field by field. `STAFF_ACTIONS["VISIT_PHOTO_ADDED"]` has exactly
    # one writer in the whole repo -- this call -- so a dropped `_log_activity` leaves the
    # constant dead and nothing else in the suite notices: the photo is stored, the 201 is
    # identical, and the supervisor's trail is simply absent. `is_duplicate` is carried in the
    # metadata because that is the fact phase 3's exception feed reads.
    logged = StaffActivityLog.query.filter_by(action=STAFF_ACTIONS["VISIT_PHOTO_ADDED"]).all()
    assert len(logged) == 1
    assert logged[0].user_id == sales_agent_user.id
    assert (logged[0].entity_type, logged[0].entity_id) == ("visit", visit_id)
    assert logged[0].metadata_ == {
        "outlet_id": outlet["id"], "photo_id": photo["id"], "kind": "storefront", "is_duplicate": False,
    }

    # The photo is a side note, not a step: the visit is still where it was.
    current = client.get(f"{VISITS}/current", headers=sales_agent_auth_headers).get_json()["data"]
    assert current["visit"]["current_step"] == "checkin"


def test_the_same_bytes_from_the_same_agent_are_flagged_across_visits(
    client, db, fake_storage, sales_agent_auth_headers, operator_auth_headers
):
    """D17: the dedupe is over the BYTES and over the AGENT, not over the visit.

    The photo a rep re-sends from their gallery at the next shop is the whole reason the
    hash is stored, and it arrives on a different visit id every time.
    """
    outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers)
    first_visit = _start(client, sales_agent_auth_headers, outlet["id"])["id"]
    first = _post_photo(client, sales_agent_auth_headers, first_visit, STOREFRONT_BYTES)
    assert first.status_code == 201, first.get_data(as_text=True)
    original_id = first.get_json()["data"]["photo"]["id"]
    assert client.post(f"{VISITS}/{first_visit}/abandon", headers=sales_agent_auth_headers).status_code == 200

    second_visit = _start(client, sales_agent_auth_headers, outlet["id"])["id"]
    repeat = _post_photo(client, sales_agent_auth_headers, second_visit, STOREFRONT_BYTES, kind="shelf")
    fresh = _post_photo(client, sales_agent_auth_headers, second_visit, SHELF_BYTES, kind="shelf")

    assert repeat.status_code == 201, repeat.get_data(as_text=True)
    flagged = repeat.get_json()["data"]["photo"]
    assert flagged["is_duplicate"] is True and flagged["duplicate_of_photo_id"] == original_id
    assert flagged["sha256"] == STOREFRONT_SHA
    # Flagged, never refused: the agent's visit carries on, the exception feed gets the row.
    assert flagged["visit_id"] == second_visit
    assert fresh.status_code == 201, fresh.get_data(as_text=True)
    assert fresh.get_json()["data"]["photo"]["is_duplicate"] is False

    # A THIRD copy points at the ORIGINAL, not at the second copy: a chain would make one
    # re-sent photo look like two distinct duplicates in the feed.
    third = _post_photo(client, sales_agent_auth_headers, second_visit, STOREFRONT_BYTES, kind="other")
    assert third.status_code == 201, third.get_data(as_text=True)
    assert third.get_json()["data"]["photo"]["duplicate_of_photo_id"] == original_id


def test_another_agents_identical_photo_is_not_a_duplicate(
    client, app, db, fake_storage, sales_agent_auth_headers, operator_auth_headers
):
    """R8: the scope is PER AGENT. Two reps photographing the same chain storefront on the
    same morning is ordinary work, and flagging it would bury the one case the feed is for."""
    outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers)
    mine = _start(client, sales_agent_auth_headers, outlet["id"])["id"]
    assert _post_photo(client, sales_agent_auth_headers, mine, STOREFRONT_BYTES).status_code == 201

    other = make_sales_agent_user(db, phone="+998901234581", staff_roles=["sales_agent"])
    db.session.add(SalesAgentProfile(user_id=other.id, districts=["yunusabad"]))
    other_outlet = Outlet(
        name="Yunusobod do'kon", outlet_type="grocery_store", stage="active",
        district="yunusabad", assigned_agent_user_id=other.id,
    )
    db.session.add(other_outlet)
    db.session.commit()
    with app.app_context():
        other_headers = {"Authorization": f"Bearer {create_access_token(identity=str(other.id))}"}
    theirs = _start(client, {**other_headers, "Content-Type": "application/json"}, other_outlet.id)["id"]

    posted = _post_photo(client, other_headers, theirs, STOREFRONT_BYTES)

    assert posted.status_code == 201, posted.get_data(as_text=True)
    photo = posted.get_json()["data"]["photo"]
    assert photo["sha256"] == STOREFRONT_SHA
    assert photo["is_duplicate"] is False and photo["duplicate_of_photo_id"] is None


def test_a_bad_kind_a_missing_file_and_a_storage_refusal_share_one_code(
    client, db, fake_storage, sales_agent_auth_headers, operator_auth_headers
):
    """Three ways a photo can be unusable, one code the bot branches on (R8).

    The size ceiling and the image sniffing belong to `FileStorageService`; this asserts the
    TRANSLATION of its refusal, never a second copy of its limits.
    """
    outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers)
    visit_id = _start(client, sales_agent_auth_headers, outlet["id"])["id"]

    bad_kind = _post_photo(client, sales_agent_auth_headers, visit_id, STOREFRONT_BYTES, kind="film")
    assert bad_kind.status_code == 400, bad_kind.get_data(as_text=True)
    assert bad_kind.get_json()["error_code"] == "SALES_PHOTO_INVALID"

    no_file = client.post(
        f"{VISITS}/{visit_id}/photos",
        data={"kind": "shelf"},
        headers=_multipart_headers(sales_agent_auth_headers),
        content_type="multipart/form-data",
    )
    assert no_file.status_code == 400, no_file.get_data(as_text=True)
    assert no_file.get_json()["error_code"] == "SALES_PHOTO_INVALID"

    fake_storage.error = FileStorageError("File too large. Service maximum size: 16.0MB")
    refused = _post_photo(client, sales_agent_auth_headers, visit_id, STOREFRONT_BYTES)
    assert refused.status_code == 400, refused.get_data(as_text=True)
    assert refused.get_json()["error_code"] == "SALES_PHOTO_INVALID"

    # Nothing was written by any of the three, so a refusal leaves no half-photo behind.
    assert VisitPhoto.query.count() == 0


def test_a_photo_needs_the_callers_own_open_visit(
    client, app, db, fake_storage, sales_agent_auth_headers, operator_auth_headers, driver_auth_headers
):
    """R8: `get_owned(..., must_be_open=True)`. A closed visit is a record, not work."""
    outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers)
    visit_id = _start(client, sales_agent_auth_headers, outlet["id"])["id"]

    other = make_sales_agent_user(db, phone="+998901234582", staff_roles=["sales_agent"])
    db.session.add(SalesAgentProfile(user_id=other.id, districts=["yunusabad"]))
    db.session.commit()
    with app.app_context():
        other_headers = {"Authorization": f"Bearer {create_access_token(identity=str(other.id))}"}
    poached = _post_photo(client, other_headers, visit_id, STOREFRONT_BYTES)
    assert poached.status_code == 403, poached.get_data(as_text=True)
    assert poached.get_json()["error_code"] == "SALES_VISIT_NOT_OWNED"

    # A driver has no business on the sales surface at all.
    assert _post_photo(client, driver_auth_headers, visit_id, STOREFRONT_BYTES).status_code == 403

    assert client.post(f"{VISITS}/{visit_id}/abandon", headers=sales_agent_auth_headers).status_code == 200
    late = _post_photo(client, sales_agent_auth_headers, visit_id, STOREFRONT_BYTES)
    assert late.status_code == 409, late.get_data(as_text=True)
    assert late.get_json()["error_code"] == "SALES_VISIT_NOT_OPEN"
    assert VisitPhoto.query.count() == 0
