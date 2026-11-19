"""The admin Driver expenses page shows a claim's receipt photo (spec 2026-11-17 §6.8).

Drives `GET /api/v1/admin/driver-expenses/<id>/receipt`, the route the page's photo calls, with the role-claim tokens
the admin UI holds. A claim is recorded through the staff bot's Send (`claim`) on a trip the admin records cash on
(`collect_on_trip`). Only Telegram is faked: the shared file-proxy seam the visit photo tests use. The route, the
guard, the service and the database are real.
"""

import logging

import pytest

from tests.integration._driver_expense_helpers import ADMIN, claim
from tests.integration._prepayment_payout_helpers import collect_on_trip, driver_headers
from tests.integration.test_admin_sales_visit_photos_api import _fake_telegram, _fake_telegram_raising
from tests.integration.test_driver_expense_admin_read_api import operator_claim_headers  # noqa: F401 (fixture)

pytestmark = pytest.mark.integration

RECEIPT = ADMIN + "/{}/receipt"
STAFF_TOKEN = "STAFF:TOKEN"


def _claim(client, app, driver, **body):
    status, answer = claim(client, app, driver, 150000, **body)
    assert status == 201, answer
    return answer["data"]["expense"]["id"]


@pytest.fixture
def trip_with_receipt(client, app, admin_claim_headers, sample_user, second_delivery_driver, monkeypatch):
    """A driver with trip cash who posted one fuel claim carrying a receipt photo."""
    monkeypatch.setitem(app.config, "STAFF_BOT_TOKEN", STAFF_TOKEN)
    collect_on_trip(client, admin_claim_headers, sample_user.id, second_delivery_driver.id, 420000)
    return _claim(client, app, second_delivery_driver, receipt_file_id="AgAC-receipt")


def test_a_receipt_streams_through_the_staff_bot(client, admin_claim_headers, trip_with_receipt, monkeypatch):
    calls, _ = _fake_telegram(monkeypatch)

    served = client.get(RECEIPT.format(trip_with_receipt), headers=admin_claim_headers)

    assert (served.status_code, served.data, served.mimetype) == (200, b"JPEGBYTES", "image/jpeg"), (
        served.get_data(as_text=True)
    )
    assert f"expense_{trip_with_receipt}_receipt.jpg" in served.headers["Content-Disposition"]
    # The claim's own file id, through the STAFF bot's token: the download and the lookup both use it.
    get_file = [(url, params) for url, params in calls if "/getFile" in url]
    assert get_file == [("https://api.telegram.org/botSTAFF:TOKEN/getFile", {"file_id": "AgAC-receipt"})]
    assert any(url.startswith(f"https://api.telegram.org/file/bot{STAFF_TOKEN}/") for url, _ in calls)


def test_a_claim_without_a_receipt_is_404_and_calls_no_one(
    client, app, admin_claim_headers, sample_user, second_delivery_driver, monkeypatch
):
    collect_on_trip(client, admin_claim_headers, sample_user.id, second_delivery_driver.id, 420000)
    expense_id = _claim(client, app, second_delivery_driver)
    calls, _ = _fake_telegram(monkeypatch)

    missing = client.get(RECEIPT.format(expense_id), headers=admin_claim_headers)

    assert (missing.status_code, missing.get_json()["error_code"]) == (404, "DRIVER_EXPENSE_NO_RECEIPT")
    assert calls == []


def test_an_unknown_claim_is_404_and_calls_no_one(client, admin_claim_headers, monkeypatch):
    calls, _ = _fake_telegram(monkeypatch)

    missing = client.get(RECEIPT.format(999999), headers=admin_claim_headers)

    assert (missing.status_code, missing.get_json()["error_code"]) == (404, "DRIVER_EXPENSE_NOT_FOUND")
    assert calls == []


def test_a_receipt_telegram_no_longer_serves_is_unavailable_not_a_500(
    client, admin_claim_headers, trip_with_receipt, monkeypatch, caplog, app
):
    _fake_telegram(monkeypatch, get_file_ok=False)

    app.logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="business_app"):
            gone = client.get(RECEIPT.format(trip_with_receipt), headers=admin_claim_headers)
    finally:
        app.logger.removeHandler(caplog.handler)

    assert (gone.status_code, gone.get_json()["error_code"]) == (404, "DRIVER_EXPENSE_RECEIPT_UNAVAILABLE")
    assert any("unavailable" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


def test_a_connection_error_during_the_receipt_fetch_never_leaks_the_staff_token(
    client, admin_claim_headers, trip_with_receipt, monkeypatch, caplog, app
):
    _fake_telegram_raising(monkeypatch, fail_on="getFile")

    app.logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="business_app"):
            gone = client.get(RECEIPT.format(trip_with_receipt), headers=admin_claim_headers)
    finally:
        app.logger.removeHandler(caplog.handler)

    assert (gone.status_code, gone.get_json()["error_code"]) == (404, "DRIVER_EXPENSE_RECEIPT_UNAVAILABLE")
    assert STAFF_TOKEN not in caplog.text


@pytest.mark.parametrize("who,status", [("admin", 200), ("manager", 200), ("operator", 403), ("driver", 403)])
def test_only_admins_and_managers_stream_receipts(
    request, client, app, trip_with_receipt, second_delivery_driver, monkeypatch, who, status
):
    _fake_telegram(monkeypatch)
    headers = driver_headers(app, second_delivery_driver) if who == "driver" else request.getfixturevalue(
        f"{who}_claim_headers"
    )

    served = app.test_client().get(RECEIPT.format(trip_with_receipt), headers=headers)

    assert served.status_code == status, served.get_data(as_text=True)
