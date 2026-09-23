"""The supervisor's view of visit photos (D27/D28), driven the way Visits.js and Outlets.js call it.

Only Telegram is faked (`requests.get` in the shared proxy). The routes, the auth decorator, the
services and the database are real.
"""
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
import requests

from business_app.models.sales import Outlet, SalesAgentProfile
from business_app.models.sales_visits import VisitPhoto
from tests.integration.test_admin_sales_visits_api import _visit
from tests.unit.test_sales_agent_role import make_sales_agent_user

pytestmark = pytest.mark.integration

VISITS = "/api/v1/admin/sales/visits"
OUTLETS = "/api/v1/admin/sales/outlets"
PHOTOS = "/api/v1/admin/sales/visit-photos"
DAY = datetime(2026, 9, 14, 6, 0, tzinfo=UTC)  # 11:00 local
PERIOD = {"start_date": "2026-09-14", "end_date": "2026-09-14"}
STAFF_TOKEN = "staff-token-123"


@pytest.fixture
def agent(db):
    user = make_sales_agent_user(db, staff_roles=["sales_agent"])
    db.session.add(SalesAgentProfile(user_id=user.id, districts=["chilanzar"]))
    db.session.commit()
    return user


def _outlet(db, agent, *, name, outlet_type):
    row = Outlet(name=name, outlet_type=outlet_type, stage="active", district="chilanzar",
                 assigned_agent_user_id=agent.id)
    db.session.add(row)
    db.session.commit()
    return row


def _photo(db, visit, *, file_id, received_at, kind="storefront", duplicate_of=None):
    photo = VisitPhoto(visit_id=visit.id, kind=kind, telegram_file_id=file_id, sha256="a" * 64,
                       received_at=received_at, duplicate_of_photo_id=duplicate_of)
    db.session.add(photo)
    db.session.commit()
    return photo


@pytest.fixture
def estate(db, agent):
    """A shop visit WITH a photo, a shop visit WITHOUT one, and a home visit without one."""
    shop = _outlet(db, agent, name="Bahor market", outlet_type="grocery_store")
    office = _outlet(db, agent, name="Yashnobod ofis", outlet_type="workplace")
    home = _outlet(db, agent, name="Anvar aka uyi", outlet_type="individual")
    shot = _visit(db, agent=agent, outlet=shop, started_at=DAY, outcome="no_order", planned=True)
    bare = _visit(db, agent=agent, outlet=office, started_at=DAY + timedelta(hours=1), outcome="no_order", planned=True)
    homely = _visit(db, agent=agent, outlet=home, started_at=DAY + timedelta(hours=2), outcome="no_order", planned=False)
    first = _photo(db, shot, file_id="AgAC-first", received_at=DAY + timedelta(minutes=5))
    second = _photo(db, shot, file_id="AgAC-second", received_at=DAY + timedelta(minutes=9), kind="shelf",
                    duplicate_of=first.id)
    return {"shop": shop, "shot": shot, "bare": bare, "homely": homely, "first": first, "second": second}


def _fake_telegram(monkeypatch, *, get_file_ok=True, payload=b"JPEGBYTES"):
    calls = []

    def fake_get(url, *a, **kw):
        calls.append((url, kw.get("params")))
        resp = MagicMock()
        resp.status_code = 200
        if "/getFile" in url:
            resp.json.return_value = (
                {"ok": True, "result": {"file_path": "photos/file_7.jpg"}}
                if get_file_ok
                else {"ok": False, "description": "wrong file identifier"}
            )
        else:
            resp.iter_content = lambda chunk_size=8192: iter([payload])
            resp.headers = {"Content-Length": str(len(payload))}
        return resp

    monkeypatch.setattr("business_app.services.telegram_file_proxy.requests.get", fake_get)
    monkeypatch.setattr("business_app.services.telegram_file_proxy.redis_client.get", MagicMock(return_value=None))
    setex = MagicMock()
    monkeypatch.setattr("business_app.services.telegram_file_proxy.redis_client.setex", setex)
    return calls, setex


def _fake_telegram_raising(monkeypatch, *, fail_on):
    """`requests.ConnectionError` stringifies to the full request URL -- which embeds the real
    staff-bot token -- so this fakes exactly the failure `TelegramFileProxy` has to scrub before
    the token can reach `AttachmentUnavailableError`'s message.

    `fail_on` is "getFile" (the file-path lookup itself fails) or "download" (the lookup
    succeeds and the byte download fails); both request URLs carry the token.
    """

    def fake_get(url, *a, **kw):
        is_get_file = "/getFile" in url
        if (fail_on == "getFile") == is_get_file:
            raise requests.ConnectionError(f"Max retries exceeded with url: {url}")
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"ok": True, "result": {"file_path": "photos/file_7.jpg"}}
        return resp

    monkeypatch.setattr("business_app.services.telegram_file_proxy.requests.get", fake_get)
    monkeypatch.setattr("business_app.services.telegram_file_proxy.redis_client.get", MagicMock(return_value=None))
    monkeypatch.setattr("business_app.services.telegram_file_proxy.redis_client.setex", MagicMock())


def test_visit_rows_carry_their_photos_and_whether_one_was_asked_for(client, admin_claim_headers, estate):
    listed = client.get(VISITS, headers=admin_claim_headers, query_string=PERIOD)

    assert listed.status_code == 200, listed.get_data(as_text=True)
    rows = {row["id"]: row for row in listed.get_json()["data"]["visits"]}
    shot = rows[estate["shot"].id]
    assert shot["photo_requested"] is True and shot["photo_count"] == 2
    assert [p["id"] for p in shot["photos"]] == [estate["first"].id, estate["second"].id]
    assert shot["photos"][1]["is_duplicate"] is True
    assert "telegram_file_id" not in shot["photos"][0]
    assert (rows[estate["bare"].id]["photo_requested"], rows[estate["bare"].id]["photo_count"]) == (True, 0)
    assert (rows[estate["homely"].id]["photo_requested"], rows[estate["homely"].id]["photo_count"]) == (False, 0)


def test_the_no_photo_filter_lists_only_business_visits_that_have_none(client, admin_claim_headers, estate):
    listed = client.get(VISITS, headers=admin_claim_headers, query_string={**PERIOD, "photo": "missing"})

    assert listed.status_code == 200, listed.get_data(as_text=True)
    # The shop visit has photos; the home visit was never asked for one (D28).
    assert [row["id"] for row in listed.get_json()["data"]["visits"]] == [estate["bare"].id]


def test_an_unknown_photo_filter_is_refused_not_ignored(client, admin_claim_headers, estate):
    refused = client.get(VISITS, headers=admin_claim_headers, query_string={**PERIOD, "photo": "all"})

    assert refused.status_code == 400, refused.get_data(as_text=True)
    assert refused.get_json()["error_code"] == "SALES_VISIT_PHOTO_FILTER_INVALID"


def test_the_outlet_photos_tab_lists_newest_first_with_who_took_them(client, admin_claim_headers, estate, agent):
    listed = client.get(f"{OUTLETS}/{estate['shop'].id}/photos", headers=admin_claim_headers)

    assert listed.status_code == 200, listed.get_data(as_text=True)
    data = listed.get_json()["data"]
    assert [p["id"] for p in data["photos"]] == [estate["second"].id, estate["first"].id]
    assert {p["agent_name"] for p in data["photos"]} == {agent.full_name}
    assert data["meta"]["total"] == 2

    other = client.get(f"{OUTLETS}/{estate['homely'].outlet_id}/photos", headers=admin_claim_headers)
    assert other.get_json()["data"]["photos"] == []
    missing = client.get(f"{OUTLETS}/999999/photos", headers=admin_claim_headers)
    assert missing.status_code == 404 and missing.get_json()["error_code"] == "SALES_OUTLET_NOT_FOUND"


def test_a_photo_streams_from_telegram_through_the_staff_bot(client, app, admin_claim_headers, estate, monkeypatch):
    monkeypatch.setitem(app.config, "STAFF_BOT_TOKEN", STAFF_TOKEN)
    calls, setex = _fake_telegram(monkeypatch)

    served = client.get(f"{PHOTOS}/{estate['first'].id}/file", headers=admin_claim_headers)

    assert served.status_code == 200, served.get_data(as_text=True)
    assert served.data == b"JPEGBYTES"
    assert served.headers["Content-Type"].startswith("image/jpeg")
    assert "inline" in served.headers["Content-Disposition"]
    # The row's file id, through the STAFF bot's token -- never the customer bot's.
    get_file = [(url, params) for url, params in calls if "/getFile" in url]
    assert get_file == [(f"https://api.telegram.org/bot{STAFF_TOKEN}/getFile", {"file_id": "AgAC-first"})]
    assert any(url.startswith(f"https://api.telegram.org/file/bot{STAFF_TOKEN}/") for url, _ in calls)
    assert setex.call_args[0][0] == "sales_photo:tg_file_path:AgAC-first"


def test_an_unknown_photo_is_a_404_without_a_telegram_call(client, admin_claim_headers, estate, monkeypatch):
    calls, _ = _fake_telegram(monkeypatch)

    missing = client.get(f"{PHOTOS}/999999/file", headers=admin_claim_headers)

    assert missing.status_code == 404 and missing.get_json()["error_code"] == "SALES_PHOTO_NOT_FOUND"
    assert calls == []


def test_a_photo_telegram_no_longer_serves_is_unavailable_not_a_500(client, app, admin_claim_headers, estate, monkeypatch):
    monkeypatch.setitem(app.config, "STAFF_BOT_TOKEN", STAFF_TOKEN)
    _fake_telegram(monkeypatch, get_file_ok=False)

    gone = client.get(f"{PHOTOS}/{estate['first'].id}/file", headers=admin_claim_headers)

    assert gone.status_code == 404, gone.get_data(as_text=True)
    assert gone.get_json()["error_code"] == "SALES_PHOTO_UNAVAILABLE"


@pytest.mark.parametrize("fail_on", ["getFile", "download"])
def test_a_telegram_connection_error_never_leaks_the_staff_token_to_logs(
    client, app, admin_claim_headers, estate, monkeypatch, caplog, fail_on
):
    """Spec Testing section: "stream route -- ... token absent from logs."

    A raw `requests.ConnectionError` stringifies to the full request URL, which embeds the real
    bot token. `TelegramFileProxy.resolve_file_path`/`.stream` scrub it into
    `AttachmentUnavailableError`'s message before `VisitService.stream_photo` logs it -- but
    nothing pinned that: `stream_photo` re-raises as `NotFoundError(...) from exc`, which keeps
    the ORIGINAL, unscrubbed `requests.ConnectionError` reachable through `__context__`/
    `__cause__`. Today `log_exception` (error_handlers.py) happens to skip the stack trace for
    any `WaterBusinessException`, so the chain never reaches a log line -- but that is a property
    of `log_exception`'s branch, not of this route, and a change to either could ship the token
    to Loki with no red test.

    `current_app.logger` ("business_app") has `propagate=False`
    (business_app/utils/logging_config.py), so caplog's root-attached handler never sees a
    record here -- the `test_agent_confirmation_api.py::test_a_duplicate_answer_is_logged...`
    pattern of attaching `caplog.handler` directly to the logger applies.
    """
    monkeypatch.setitem(app.config, "STAFF_BOT_TOKEN", STAFF_TOKEN)
    _fake_telegram_raising(monkeypatch, fail_on=fail_on)

    app.logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="business_app"):
            gone = client.get(f"{PHOTOS}/{estate['first'].id}/file", headers=admin_claim_headers)
    finally:
        app.logger.removeHandler(caplog.handler)

    assert gone.status_code == 404, gone.get_data(as_text=True)
    assert gone.get_json()["error_code"] == "SALES_PHOTO_UNAVAILABLE"
    # Positive control (mandatory): the scrubbed warning must actually have been captured, so a
    # disabled or unwired logger cannot make the token-absence assertion below pass vacuously.
    warnings = [r for r in caplog.records if "unavailable" in r.getMessage()]
    assert len(warnings) == 1, [(r.name, r.levelname, r.getMessage()) for r in caplog.records]
    assert STAFF_TOKEN not in caplog.text


def test_no_staff_token_is_unavailable_and_calls_nobody(client, app, admin_claim_headers, estate, monkeypatch):
    for name in ("STAFF_BOT_TOKEN", "STAFF_TELEGRAM_BOT_TOKEN"):
        monkeypatch.delitem(app.config, name, raising=False)
        monkeypatch.delenv(name, raising=False)
    calls, _ = _fake_telegram(monkeypatch)

    gone = client.get(f"{PHOTOS}/{estate['first'].id}/file", headers=admin_claim_headers)

    assert gone.status_code == 404 and gone.get_json()["error_code"] == "SALES_PHOTO_UNAVAILABLE"
    assert calls == []


def test_the_photo_routes_are_for_managers_only(client, sales_agent_auth_headers, estate):
    assert client.get(f"{PHOTOS}/{estate['first'].id}/file", headers=sales_agent_auth_headers).status_code == 403
    assert client.get(f"{OUTLETS}/{estate['shop'].id}/photos", headers=sales_agent_auth_headers).status_code == 403
