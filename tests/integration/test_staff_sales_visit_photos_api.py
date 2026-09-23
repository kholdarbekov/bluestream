"""POST /api/v1/staff/sales/visits/<id>/photos -- the photo the staff bot files, driven over HTTP.

JSON since D27 (docs/superpowers/specs/2026-09-23-outlet-editing-and-visit-photos-design.md): the
photo stays on Telegram. The bot posts the staff bot's `file_id` and the SHA-256 of the bytes it
downloaded into memory; nothing is stored on our side, so there is no storage seam to stub -- the
route, the service and the database are all real.
"""
import hashlib

import pytest
from flask_jwt_extended import create_access_token

from business_app.models.sales import Outlet, SalesAgentProfile
from business_app.models.sales_visits import VisitPhoto
from business_app.models.staff import StaffActivityLog
from shared.staff_constants import STAFF_ACTIONS
from tests.integration.test_staff_sales_visits_api import VISITS, _approved_outlet, _start
from tests.unit.test_sales_agent_role import make_sales_agent_user

pytestmark = pytest.mark.integration

STOREFRONT_SHA = hashlib.sha256(b"BAHOR-STOREFRONT-2026").hexdigest()
SHELF_SHA = hashlib.sha256(b"BAHOR-SHELF-2026").hexdigest()
STOREFRONT_FILE_ID = "AgACAgIAAxkBAAIBstorefront"
# No `telegram_file_id`: the admin stream route resolves it from the row, so publishing it would
# only invite a client to go to Telegram with it.
PHOTO_KEYS = {
    "id", "visit_id", "kind", "sha256", "telegram_file_unique_id",
    "received_at", "duplicate_of_photo_id", "is_duplicate",
}


def _post_photo(client, headers, visit_id, sha, *, kind="storefront", file_id=STOREFRONT_FILE_ID,
                unique_id="AgADBAADqAADq2"):
    body = {"kind": kind, "telegram_file_id": file_id, "sha256": sha}
    if unique_id is not None:
        body["telegram_file_unique_id"] = unique_id
    return client.post(f"{VISITS}/{visit_id}/photos", json=body, headers=headers)


def _headers_for(app, user):
    with app.app_context():
        token = create_access_token(identity=str(user.id))
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def test_a_photo_is_recorded_by_its_file_id_and_answered_as_its_own_row(
    client, db, sales_agent_auth_headers, operator_auth_headers, sales_agent_user
):
    outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers)
    visit_id = _start(client, sales_agent_auth_headers, outlet["id"])["id"]

    created = _post_photo(client, sales_agent_auth_headers, visit_id, STOREFRONT_SHA)

    assert created.status_code == 201, created.get_data(as_text=True)
    photo = created.get_json()["data"]["photo"]
    assert set(photo) == PHOTO_KEYS
    assert photo["visit_id"] == visit_id and photo["kind"] == "storefront"
    assert photo["sha256"] == STOREFRONT_SHA
    assert photo["is_duplicate"] is False and photo["duplicate_of_photo_id"] is None
    assert photo["telegram_file_unique_id"] == "AgADBAADqAADq2"
    assert photo["received_at"].endswith("+00:00")

    row = VisitPhoto.query.filter_by(visit_id=visit_id).one()
    assert (row.telegram_file_id, row.sha256, row.kind) == (STOREFRONT_FILE_ID, STOREFRONT_SHA, "storefront")

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


def test_the_same_digest_from_the_same_agent_is_flagged_across_visits(
    client, db, sales_agent_auth_headers, operator_auth_headers
):
    """The dedupe is over the PICTURE and the AGENT. Telegram mints a new file id for every upload,
    so a re-sent gallery photo arrives with a new id and the same digest -- on a new visit."""
    outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers)
    first_visit = _start(client, sales_agent_auth_headers, outlet["id"])["id"]
    first = _post_photo(client, sales_agent_auth_headers, first_visit, STOREFRONT_SHA)
    assert first.status_code == 201, first.get_data(as_text=True)
    original_id = first.get_json()["data"]["photo"]["id"]
    assert client.post(f"{VISITS}/{first_visit}/abandon", headers=sales_agent_auth_headers).status_code == 200

    second_visit = _start(client, sales_agent_auth_headers, outlet["id"])["id"]
    repeat = _post_photo(client, sales_agent_auth_headers, second_visit, STOREFRONT_SHA, kind="shelf",
                         file_id="AgAC-reupload", unique_id="AgAD-reupload")
    fresh = _post_photo(client, sales_agent_auth_headers, second_visit, SHELF_SHA, kind="shelf",
                        file_id="AgAC-shelf", unique_id="AgAD-shelf")

    assert repeat.status_code == 201, repeat.get_data(as_text=True)
    flagged = repeat.get_json()["data"]["photo"]
    assert flagged["is_duplicate"] is True and flagged["duplicate_of_photo_id"] == original_id
    assert flagged["visit_id"] == second_visit
    assert fresh.status_code == 201, fresh.get_data(as_text=True)
    assert fresh.get_json()["data"]["photo"]["is_duplicate"] is False

    # A THIRD copy points at the ORIGINAL, not at the second copy.
    third = _post_photo(client, sales_agent_auth_headers, second_visit, STOREFRONT_SHA, kind="other",
                        file_id="AgAC-third", unique_id=None)
    assert third.status_code == 201, third.get_data(as_text=True)
    assert third.get_json()["data"]["photo"]["duplicate_of_photo_id"] == original_id


def test_another_agents_identical_photo_is_not_a_duplicate(
    client, app, db, sales_agent_auth_headers, operator_auth_headers
):
    outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers)
    mine = _start(client, sales_agent_auth_headers, outlet["id"])["id"]
    assert _post_photo(client, sales_agent_auth_headers, mine, STOREFRONT_SHA).status_code == 201

    other = make_sales_agent_user(db, phone="+998901234581", staff_roles=["sales_agent"])
    db.session.add(SalesAgentProfile(user_id=other.id, districts=["yunusabad"]))
    other_outlet = Outlet(
        name="Yunusobod do'kon", outlet_type="grocery_store", stage="active",
        district="yunusabad", assigned_agent_user_id=other.id,
    )
    db.session.add(other_outlet)
    db.session.commit()
    other_headers = _headers_for(app, other)
    theirs = _start(client, other_headers, other_outlet.id)["id"]

    posted = _post_photo(client, other_headers, theirs, STOREFRONT_SHA, file_id="AgAC-theirs")

    assert posted.status_code == 201, posted.get_data(as_text=True)
    assert posted.get_json()["data"]["photo"]["is_duplicate"] is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": "film"},
        {"telegram_file_id": ""},
        {"telegram_file_id": None},
        {"telegram_file_id": "x" * 256},
        {"sha256": "not-a-digest"},
        {"sha256": STOREFRONT_SHA.upper()},
        {"sha256": None},
    ],
    ids=["bad-kind", "empty-file-id", "missing-file-id", "long-file-id", "junk-sha", "uppercase-sha", "missing-sha"],
)
def test_an_unusable_photo_payload_is_one_code_and_writes_nothing(
    client, db, sales_agent_auth_headers, operator_auth_headers, overrides
):
    """Every refusal is SALES_PHOTO_INVALID -- the one code the bot branches on."""
    outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers)
    visit_id = _start(client, sales_agent_auth_headers, outlet["id"])["id"]
    body = {"kind": "storefront", "telegram_file_id": STOREFRONT_FILE_ID, "sha256": STOREFRONT_SHA, **overrides}

    response = client.post(f"{VISITS}/{visit_id}/photos", json=body, headers=sales_agent_auth_headers)

    assert response.status_code == 400, response.get_data(as_text=True)
    assert response.get_json()["error_code"] == "SALES_PHOTO_INVALID"
    assert VisitPhoto.query.count() == 0


def test_a_photo_needs_the_callers_own_open_visit(
    client, app, db, sales_agent_auth_headers, operator_auth_headers, driver_auth_headers
):
    outlet = _approved_outlet(client, sales_agent_auth_headers, operator_auth_headers)
    visit_id = _start(client, sales_agent_auth_headers, outlet["id"])["id"]

    other = make_sales_agent_user(db, phone="+998901234582", staff_roles=["sales_agent"])
    db.session.add(SalesAgentProfile(user_id=other.id, districts=["yunusabad"]))
    db.session.commit()
    poached = _post_photo(client, _headers_for(app, other), visit_id, STOREFRONT_SHA)
    assert poached.status_code == 403, poached.get_data(as_text=True)
    assert poached.get_json()["error_code"] == "SALES_VISIT_NOT_OWNED"

    assert _post_photo(client, driver_auth_headers, visit_id, STOREFRONT_SHA).status_code == 403

    assert client.post(f"{VISITS}/{visit_id}/abandon", headers=sales_agent_auth_headers).status_code == 200
    late = _post_photo(client, sales_agent_auth_headers, visit_id, STOREFRONT_SHA)
    assert late.status_code == 409, late.get_data(as_text=True)
    assert late.get_json()["error_code"] == "SALES_VISIT_NOT_OPEN"
    assert VisitPhoto.query.count() == 0
