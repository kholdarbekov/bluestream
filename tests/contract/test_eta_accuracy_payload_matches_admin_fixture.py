"""The admin accuracy page's fixture against the report the backend builds (honest-ETA Phase 2, Tasks 9 and 10).

`tests/fixtures/admin_eta_accuracy/report.json` (`REPORT_FIXTURE`) is the report of the seeded scenario in
`tests/integration/test_admin_eta_accuracy.py`, which writes it; the admin UI's Vitest renders the page from it. Here
the report is built for a different, smaller day: one judged order and the weekly fit. Every key the fixture holds, at
any depth, must still be in what `EtaAccuracyReport.build` returns, so a renamed or dropped figure fails here before
the page reads a key the backend no longer sends.
"""

import json

import pytest

from business_app.services.eta_plan.report import EtaAccuracyReport
from shared.enums import DeliveryStatus
from tests.integration.eta_plan_builders import (
    REPORT_DAY,
    REPORT_FIT_MONDAY,
    REPORT_FIXTURE,
    REPORT_MONITORING,
    REPORT_NOW,
    add_fit,
    engine_promise,
    make_driver,
    order_at_km,
    report_day_at,
    tap,
)


def _missing(expected, actual, path="report"):
    """The key paths of `expected` that `actual` lacks. A list's items are held to the first item of `actual`'s."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            yield path
            return
        for key, value in expected.items():
            if key not in actual:
                yield f"{path}.{key}"
            else:
                yield from _missing(value, actual[key], f"{path}.{key}")
    elif isinstance(expected, list) and expected:
        if not (isinstance(actual, list) and actual):
            yield f"{path}[]"
            return
        for item in expected:
            yield from _missing(item, actual[0], f"{path}[]")


@pytest.mark.contract
def test_eta_accuracy_payload_matches_admin_fixture(db):
    at = report_day_at
    add_fit(db, REPORT_FIT_MONDAY, monitoring=REPORT_MONITORING)
    order = order_at_km(db, 2.0, created_at=at(8), driver=make_driver(db))
    engine_promise(db, order.delivery, at(9), expected_at=at(10, 30), window=(at(10), at(12)))
    tap(db, order.delivery, DeliveryStatus.DELIVERED, at(11))

    built = json.loads(json.dumps(EtaAccuracyReport.build(start_date=REPORT_DAY, end_date=REPORT_DAY, now=REPORT_NOW)))

    assert list(_missing(json.loads(REPORT_FIXTURE.read_text("utf-8")), built)) == []
