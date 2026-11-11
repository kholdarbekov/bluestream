"""Financial summary shows prepayments handed back as money (spec 2026-11-10 P9, Task 9).

Credit is booked with `CashCollectionService.post_collection` (the flow marker a real writer stamps),
the return goes through the real payout apply/undo routes, and the figures are read from the route the
admin reports screen calls: `POST /api/v1/admin/reports/generate`.
"""

import csv
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace

import pytest
import redis

from business_app.models.payment import CustomerCreditPayout
from business_app.services.admin_report_service import AdminReportService
from business_app.services.cash_collection_service import CashCollectionService
from business_app.utils import decorators

pytestmark = pytest.mark.integration

BASE = "/api/v1/admin/staff/cash-reconciliation/customers/{}"
APPLY = BASE + "/prepayment-payouts"
UNDO = "/api/v1/admin/staff/cash-reconciliation/prepayment-payouts/{}/undo"
GENERATE = "/api/v1/admin/reports/generate"


class _UnlimitedRedis:
    """What the rate limiter asks Redis: no counter yet, so every request is the first of its window."""

    def get(self, key):
        return None

    def setex(self, key, seconds, value):
        return True


@pytest.fixture(autouse=True)
def _report_rate_limit_off(monkeypatch):
    """The route allows 5 reports per 30 minutes per user, counted in Redis. These tests generate many reports as
    one admin, so the limiter's Redis is swapped for one that never counts: nothing is written to, or deleted from,
    the Redis other (possibly concurrent) test runs share."""
    monkeypatch.setattr(
        decorators, "redis", SimpleNamespace(from_url=lambda url: _UnlimitedRedis(), RedisError=redis.RedisError)
    )


def _credit(user, amount, admin, *, when, proof_data=None):
    return (
        CashCollectionService()
        .post_collection(
            customer_id=user.id,
            amount=Decimal(amount),
            source="admin_adjustment",
            recorded_by_user_id=admin.id,
            notes="Prepaid",
            occurred_at=when,
            proof_data=proof_data,
        )
        .id
    )


def _return(client, headers, customer_id, amount, key="p9-1"):
    response = client.post(
        APPLY.format(customer_id),
        json={"paid_by": "office", "reason": "Customer moved away", "idempotency_key": key, "amount": amount},
        headers=headers,
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]["payout"]["id"]


def _generate(client, headers, **body):
    now = datetime.now(UTC)
    response = client.post(
        GENERATE,
        json={
            "report_type": "financial_summary",
            "date_range": {
                "start_date": (now - timedelta(days=1)).isoformat(),
                "end_date": (now + timedelta(days=1)).isoformat(),
            },
            **body,
        },
        headers=headers,
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]


def _summary(client, headers):
    return _generate(client, headers)["report"]["summary"]


def _seed_mixed_credit(user, admin):
    """8,000 of cancelled-order credit (revenue-counted) then 12,000 of office cash (never revenue)."""
    now = datetime.now(UTC)
    _credit(
        user,
        "8000",
        admin,
        when=now - timedelta(hours=2),
        proof_data={"flow": "order_cancel_prepaid_credit", "payment_method": "click"},
    )
    _credit(user, "12000", admin, when=now - timedelta(hours=1))


def test_revenue_counted_return_is_subtracted_once(client, db, admin_user, admin_claim_headers, sample_user):
    _seed_mixed_credit(sample_user, admin_user)
    before = _summary(client, admin_claim_headers)
    assert before["prepayment_returns_revenue_counted"] == 0.0
    assert before["prepayments_returned_to_customers"] == 0.0

    _return(client, admin_claim_headers, sample_user.id, 20000)
    after = _summary(client, admin_claim_headers)

    assert after["prepayment_returns_revenue_counted"] == 8000.0
    assert after["prepayments_returned_to_customers"] == 12000.0
    assert after["net_revenue"] == before["net_revenue"] - 8000.0
    assert after["total_revenue"] == before["total_revenue"]
    assert after["total_cash_collected"] == before["total_cash_collected"]


def test_the_csv_export_header_carries_both_prepayment_return_columns(
    client, db, admin_user, admin_claim_headers, sample_user
):
    _seed_mixed_credit(sample_user, admin_user)
    _return(client, admin_claim_headers, sample_user.id, 20000)

    data = _generate(client, admin_claim_headers, format="csv")

    assert data["format"] == "csv"
    rows = list(csv.DictReader(StringIO(data["report"])))
    assert len(rows) == 1
    assert {"prepayment_returns_revenue_counted", "prepayments_returned_to_customers"} <= set(rows[0])
    assert (rows[0]["prepayment_returns_revenue_counted"], rows[0]["prepayments_returned_to_customers"]) == (
        "8000.0",
        "12000.0",
    )


def test_undone_return_counts_nowhere(client, db, admin_user, admin_claim_headers, sample_user):
    _seed_mixed_credit(sample_user, admin_user)
    before = _summary(client, admin_claim_headers)
    payout_id = _return(client, admin_claim_headers, sample_user.id, 20000)
    response = client.post(UNDO.format(payout_id), json={"reason": "Recorded by mistake"}, headers=admin_claim_headers)
    assert response.status_code == 200, response.get_json()

    after = _summary(client, admin_claim_headers)

    assert after["prepayment_returns_revenue_counted"] == 0.0
    assert after["prepayments_returned_to_customers"] == 0.0
    assert after["net_revenue"] == before["net_revenue"]


def test_backdated_return_counts_in_its_recording_period(client, db, admin_user, admin_claim_headers, sample_user):
    _seed_mixed_credit(sample_user, admin_user)
    paid_at = (datetime.now(UTC) - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    response = client.post(
        APPLY.format(sample_user.id),
        json={
            "paid_by": "office",
            "reason": "Customer moved away",
            "idempotency_key": "p9-backdated",
            "amount": 20000,
            "paid_at": paid_at,
        },
        headers=admin_claim_headers,
    )
    assert response.status_code == 201, response.get_json()
    payout = db.session.get(CustomerCreditPayout, response.get_json()["data"]["payout"]["id"])
    assert payout.paid_at < datetime.now(UTC).replace(tzinfo=payout.paid_at.tzinfo) - timedelta(days=39)

    today = _summary(client, admin_claim_headers)
    month_ago = AdminReportService.generate(
        "financial_summary", datetime.now(UTC) - timedelta(days=60), datetime.now(UTC) - timedelta(days=30), {}
    )["summary"]

    assert today["prepayment_returns_revenue_counted"] == 8000.0
    assert today["prepayments_returned_to_customers"] == 12000.0
    assert month_ago["prepayment_returns_revenue_counted"] == 0.0
    assert month_ago["prepayments_returned_to_customers"] == 0.0
