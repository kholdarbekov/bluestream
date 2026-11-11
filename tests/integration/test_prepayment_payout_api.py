"""Return prepayment: the admin preview and apply routes, end to end (spec 2026-11-10, Task 2).

Every test drives the HTTP routes the admin modal calls, with the body it sends, and reads the
ledger back from the database. Credit is seeded the way production books it: the admin "Record
collection" route, or `CashCollectionService.post_collection` where a test needs a fixed
`occurred_at` (the draw order) or a flow marker a real writer stamps.
"""

import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy.orm import Session

from business_app.models.delivery import DeliveryPerson
from business_app.models.payment import (
    CashCollectionAllocation,
    CashCollectionEvent,
    CustomerCreditPayout,
    CustomerCreditPayoutDebit,
    DriverCashSession,
)
from business_app.models.audit import AuditLog
from business_app.services.cash_collection_service import CLICK_RAIL_RESTORE_REASON_PREFIX, CashCollectionService
from business_app.services.click_payment_provider_service import ClickPaymentProviderService
from business_app.services.prepayment_payout_service import PrepaymentPayoutService
from business_app.utils.audit_logger import AuditEventType, AuditLogger, AuditSeverity, audit_logger
from business_app.utils.timezone_utils import ensure_utc
from shared.enums import OrderStatus
from tests.unit._scope_money_helpers import delivered_cod_order, link_users, make_user

pytestmark = pytest.mark.integration

BASE = "/api/v1/admin/staff/cash-reconciliation/customers/{}"
PREVIEW = BASE + "/prepayment-payouts/preview"
APPLY = BASE + "/prepayment-payouts"
HISTORY = BASE + "/prepayment-history"
COLLECTIONS = "/api/v1/admin/staff/cash-reconciliation/collections"

SUMMARY_KEYS = {
    "customer_id",
    "pool_user_ids",
    "free_balance",
    "amount",
    "target_balance",
    "paid_by",
    "driver_user_id",
    "paid_at",
    "drawn",
    "card_click_amount",
    "revenue_counted_amount",
    "warnings",
    "session_effect",
    "contract_effect",
    "blocking_reasons",
    "is_applicable",
}
DAY = timedelta(days=1)


# --------------------------------------------------------------------------- helpers


def _record_collection(client, headers, customer_id, amount):
    """The admin "Record collection" modal: an office adjustment that parks `amount` as credit."""
    response = client.post(
        COLLECTIONS,
        json={
            "customer_id": customer_id,
            "amount": amount,
            "source": "admin_adjustment",
            "notes": "Prepaid in cash at the office",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]["cash_collection_event"]["id"]


def _credit(user, amount, admin, *, occurred_at, source="admin_adjustment", proof_data=None, notes=None):
    """`amount` of credit on `user`, collected at `occurred_at` (the draw order is oldest-first)."""
    event = CashCollectionService().post_collection(
        customer_id=user.id,
        amount=Decimal(amount),
        source=source,
        recorded_by_user_id=admin.id,
        notes=notes or "Prepaid in cash at the office",
        occurred_at=occurred_at,
        proof_data=proof_data,
    )
    return event.id


def _preview(client, headers, customer_id, **body):
    response = client.post(PREVIEW.format(customer_id), json=body, headers=headers)
    return response.status_code, response.get_json()


def _apply(client, headers, customer_id, **body):
    payload = {"paid_by": "office", "reason": "Customer moved away", "idempotency_key": "k-1"}
    payload.update(body)
    response = client.post(APPLY.format(customer_id), json=payload, headers=headers)
    return response.status_code, response.get_json()


def _unapplied(db, event_id):
    db.session.expire_all()
    return Decimal(str(db.session.get(CashCollectionEvent, event_id).unapplied_amount))


def _debits(db):
    db.session.expire_all()
    return [
        (d.cash_collection_event_id, Decimal(str(d.amount)), d.revenue_counted, d.card_click)
        for d in CustomerCreditPayoutDebit.query.order_by(CustomerCreditPayoutDebit.id).all()
    ]


def _balance(customer_id):
    return CashCollectionService().get_customer_prepaid_balance(customer_id)


# --------------------------------------------------------------------------- the draw


def test_full_office_return_drains_free_balance(client, db, admin_user, admin_claim_headers, sample_user):
    event_id = _record_collection(client, admin_claim_headers, sample_user.id, 50000)
    before = datetime.now(UTC)

    status, body = _preview(client, admin_claim_headers, sample_user.id, paid_by="office")

    assert status == 200, body
    summary = body["data"]
    assert set(summary) == SUMMARY_KEYS
    assert {k: v for k, v in summary.items() if k != "paid_at"} == {
        "customer_id": sample_user.id,
        "pool_user_ids": [sample_user.id],
        "free_balance": 50000.0,
        "amount": 50000.0,
        "target_balance": None,
        "paid_by": "office",
        "driver_user_id": None,
        "drawn": [
            {
                "event_id": event_id,
                "customer_id": sample_user.id,
                "amount": 50000.0,
                "revenue_counted": False,
                "card_click": False,
            }
        ],
        "card_click_amount": 0.0,
        "revenue_counted_amount": 0.0,
        "warnings": {"pending_orders": [], "live_subscriptions": [], "place_debts": []},
        "session_effect": None,
        "contract_effect": [],
        "blocking_reasons": [],
        "is_applicable": True,
    }

    status, body = _apply(client, admin_claim_headers, sample_user.id, amount=50000)
    after = datetime.now(UTC)

    assert status == 201, body
    data = body["data"]
    assert set(data) == {"payout", "available_prepayment_balance", "driver_cash_session", "next_session_id", "replayed"}
    assert data["available_prepayment_balance"] == 0.0
    assert data["driver_cash_session"] is None
    assert data["next_session_id"] is None
    assert data["replayed"] is False
    payout = data["payout"]
    payout_row = CustomerCreditPayout.query.one()
    assert {k: v for k, v in payout.items() if k not in ("paid_at", "created_at")} == {
        "id": payout_row.id,
        "customer_id": sample_user.id,
        "amount": 50000.0,
        "paid_by": "office",
        "driver_user_id": None,
        "driver_name": None,
        "driver_cash_session_id": None,
        "reason": "Customer moved away",
        "actor_user_id": admin_user.id,
        "actor_name": "Admin User",
        "voided_at": None,
        "voided_by_user_id": None,
        "void_reason": None,
        "card_click_amount": 0.0,
        "revenue_counted_amount": 0.0,
        "can_undo": True,
        "undo_block_code": None,
        "debits": [{"event_id": event_id, "amount": 50000.0}],
    }
    # The response renders date-times in the viewer's timezone (TimezoneMiddleware); the instant is what counts.
    assert datetime.fromisoformat(payout["paid_at"]) == ensure_utc(payout_row.paid_at)
    assert before <= ensure_utc(payout_row.paid_at) <= after
    assert payout["created_at"] is not None

    # The collection is never rewritten: only its unapplied credit drops.
    db.session.expire_all()
    event = db.session.get(CashCollectionEvent, event_id)
    assert Decimal(str(event.amount)) == Decimal("50000.00")
    assert Decimal(str(event.unapplied_amount)) == Decimal("0.00")
    assert event.voided_at is None
    assert CashCollectionEvent.query.filter(CashCollectionEvent.voided_at.isnot(None)).count() == 0
    assert _debits(db) == [(event_id, Decimal("50000.00"), False, False)]
    assert payout_row.idempotency_key == "k-1"
    assert _balance(sample_user.id) == Decimal("0.00")


def test_partial_return_draws_oldest_first(client, db, admin_user, admin_claim_headers, sample_user):
    now = datetime.now(UTC)
    # The newer event is created FIRST, so a draw ordered by id would take it first.
    newer = _credit(sample_user, "30000", admin_user, occurred_at=now - DAY)
    older = _credit(sample_user, "20000", admin_user, occurred_at=now - 2 * DAY)
    assert newer < older

    status, body = _apply(client, admin_claim_headers, sample_user.id, amount=35000)

    assert status == 201, body
    assert [(d["event_id"], d["amount"]) for d in body["data"]["payout"]["debits"]] == [
        (older, 20000.0),
        (newer, 15000.0),
    ]
    assert [(e, a) for e, a, _, _ in _debits(db)] == [(older, Decimal("20000.00")), (newer, Decimal("15000.00"))]
    assert (_unapplied(db, older), _unapplied(db, newer)) == (Decimal("0.00"), Decimal("15000.00"))
    assert body["data"]["available_prepayment_balance"] == 15000.0


def test_linked_cluster_draws_across_members(client, db, admin_user, admin_claim_headers):
    a, b = make_user(db), make_user(db)
    link_users(db, [a, b])
    now = datetime.now(UTC)
    b_event = _credit(b, "30000", admin_user, occurred_at=now - DAY)
    a_event = _credit(a, "20000", admin_user, occurred_at=now - 2 * DAY)

    status, body = _apply(client, admin_claim_headers, b.id, amount=35000)

    assert status == 201, body
    assert body["data"]["payout"]["customer_id"] == b.id
    assert [(e, a_) for e, a_, _, _ in _debits(db)] == [(a_event, Decimal("20000.00")), (b_event, Decimal("15000.00"))]
    assert body["data"]["available_prepayment_balance"] == 15000.0
    assert _balance(a.id) == _balance(b.id) == Decimal("15000.00")


def test_both_linked_anchors_plan_the_same_draw(client, db, admin_user, admin_claim_headers):
    a, b = make_user(db), make_user(db)
    link_users(db, [a, b])
    now = datetime.now(UTC)
    b_event = _credit(b, "30000", admin_user, occurred_at=now - DAY)
    a_event = _credit(a, "20000", admin_user, occurred_at=now - 2 * DAY)

    _, via_a = _preview(client, admin_claim_headers, a.id, paid_by="office", amount=35000)
    _, via_b = _preview(client, admin_claim_headers, b.id, paid_by="office", amount=35000)

    assert via_a["data"]["drawn"] == via_b["data"]["drawn"]
    assert [(d["event_id"], d["customer_id"], d["amount"]) for d in via_a["data"]["drawn"]] == [
        (a_event, a.id, 20000.0),
        (b_event, b.id, 15000.0),
    ]
    assert sorted(via_a["data"]["pool_user_ids"]) == sorted(via_b["data"]["pool_user_ids"]) == sorted([a.id, b.id])


def test_grocery_member_never_drawn_for_individual(client, db, admin_user, admin_claim_headers):
    grocery, sibling = make_user(db, grocery=True), make_user(db)
    link_users(db, [grocery, sibling])
    now = datetime.now(UTC)
    grocery_event = _credit(grocery, "40000", admin_user, occurred_at=now - 2 * DAY)
    sibling_event = _credit(sibling, "25000", admin_user, occurred_at=now - DAY)

    _, individual = _preview(client, admin_claim_headers, sibling.id, paid_by="office")
    _, shop = _preview(client, admin_claim_headers, grocery.id, paid_by="office")

    assert [d["event_id"] for d in individual["data"]["drawn"]] == [sibling_event]
    assert individual["data"]["free_balance"] == 25000.0
    assert individual["data"]["pool_user_ids"] == [sibling.id]
    assert [d["event_id"] for d in shop["data"]["drawn"]] == [grocery_event]
    assert shop["data"]["free_balance"] == 40000.0
    assert shop["data"]["pool_user_ids"] == [grocery.id]


def test_amount_above_free_balance_is_blocked(client, db, admin_user, admin_claim_headers, sample_user):
    event_id = _record_collection(client, admin_claim_headers, sample_user.id, 50000)

    status, body = _preview(client, admin_claim_headers, sample_user.id, paid_by="office", amount=60000)

    assert status == 200, body
    assert body["data"]["blocking_reasons"][0].startswith("amount_exceeds_free_balance:")
    assert body["data"]["is_applicable"] is False
    assert body["data"]["drawn"] == []

    status, body = _apply(client, admin_claim_headers, sample_user.id, amount=60000)

    assert status == 400, body
    assert body["data"]["error_code"] == "PAYOUT_BLOCKED"
    assert body["errors"][0].startswith("amount_exceeds_free_balance:")
    assert CustomerCreditPayout.query.count() == 0
    assert _unapplied(db, event_id) == Decimal("50000.00")


# --------------------------------------------------------------------------- classification (P7, P9)


def test_card_click_and_revenue_flags_are_stored(client, db, admin_user, admin_claim_headers, sample_user):
    service = CashCollectionService()
    now = datetime.now(UTC)

    # A personal card transfer of 25 000 against a 15 000 delivered order: 10 000 left over.
    card_order, _ = delivered_cod_order(db, sample_user, total=Decimal("15000.00"))
    pct = service.post_collection(
        customer_id=sample_user.id,
        amount=Decimal("25000.00"),
        source="personal_card_transfer",
        order_id=card_order.id,
        recorded_by_user_id=admin_user.id,
        notes="Card transfer to the owner",
        occurred_at=now - 4 * DAY,
    ).id
    # 15 000 handed over at the door for a 10 000 order: 5 000 surplus.
    door_order, _ = delivered_cod_order(db, sample_user, total=Decimal("10000.00"))
    door = service.post_collection(
        customer_id=sample_user.id,
        amount=Decimal("15000.00"),
        source="standalone_meeting",
        order_id=door_order.id,
        recorded_by_user_id=admin_user.id,
        notes="Paid at the door with a larger note",
        occurred_at=now - 3 * DAY,
    ).id
    # A cancelled Click order re-booked as credit (the B4a writer's marker).
    cancel = _credit(
        sample_user,
        "8000",
        admin_user,
        occurred_at=now - 2 * DAY,
        source="backfill",
        proof_data={"flow": "order_cancel_prepaid_credit", "payment_method": "click"},
        notes="Order cancelled after a click payment",
    )
    # A paid order edited down, the difference handed back as credit.
    edit = _credit(
        sample_user,
        "4000",
        admin_user,
        occurred_at=now - DAY,
        proof_data={"flow": "order_edit_refund"},
        notes="Order edit refund",
    )
    assert [_unapplied(db, e) for e in (pct, door, cancel, edit)] == [
        Decimal("10000.00"),
        Decimal("5000.00"),
        Decimal("8000.00"),
        Decimal("4000.00"),
    ]

    _, preview = _preview(client, admin_claim_headers, sample_user.id, paid_by="office", amount=27000)
    assert [(d["event_id"], d["amount"], d["revenue_counted"], d["card_click"]) for d in preview["data"]["drawn"]] == [
        (pct, 10000.0, False, True),
        (door, 5000.0, False, False),
        (cancel, 8000.0, True, True),
        (edit, 4000.0, True, False),
    ]
    assert (preview["data"]["card_click_amount"], preview["data"]["revenue_counted_amount"]) == (18000.0, 12000.0)

    status, body = _apply(client, admin_claim_headers, sample_user.id, amount=27000)

    assert status == 201, body
    assert (body["data"]["payout"]["card_click_amount"], body["data"]["payout"]["revenue_counted_amount"]) == (
        18000.0,
        12000.0,
    )
    assert _debits(db) == [
        (pct, Decimal("10000.00"), False, True),
        (door, Decimal("5000.00"), False, False),
        (cancel, Decimal("8000.00"), True, True),
        (edit, Decimal("4000.00"), True, False),
    ]


# The reason the Click rail restore writes, built from the constant the restore and the classifying clause share.
CLICK_RESTORE_REASON = (
    f"{CLICK_RAIL_RESTORE_REASON_PREFIX}77 settled order 5; its cash freed for the customer's debts, "
    "the rest prepaid balance"
)


@pytest.mark.parametrize(
    "pending_total,held_mode",
    [
        ("20000.00", "prepaid_reservation"),  # the credit covers part of the order: a reservation
        ("9000.00", "prepaid_credit"),  # it covers all of it: settled ahead of delivery
    ],
)
def test_click_residual_is_card_click_but_released_reservation_is_not(
    client, db, admin_user, admin_claim_headers, pending_total, held_mode
):
    service = CashCollectionService()
    now = datetime.now(UTC)

    # Door cash that settled an order a late Click debit then paid: the rail restore reverses it.
    residual_customer = make_user(db)
    order, _ = delivered_cod_order(db, residual_customer, total=Decimal("12000.00"))
    residual = service.post_collection(
        customer_id=residual_customer.id,
        amount=Decimal("12000.00"),
        source="standalone_meeting",
        order_id=order.id,
        recorded_by_user_id=admin_user.id,
        notes="Paid at the door",
        occurred_at=now - DAY,
    )
    allocation = CashCollectionAllocation.query.filter_by(cash_collection_event_id=residual.id).one()
    assert allocation.allocation_mode == "auto"
    service.reverse_allocation_to_payment(allocation.id, reversed_by_user_id=None, reason=CLICK_RESTORE_REASON)

    # Prior credit held on a pending order, released by the same restore: not Click money.
    released_customer = make_user(db)
    pending, _ = delivered_cod_order(db, released_customer, total=Decimal(pending_total), status=OrderStatus.CONFIRMED)
    released = _credit(released_customer, "9000", admin_user, occurred_at=now - DAY)
    held = CashCollectionAllocation.query.filter_by(cash_collection_event_id=released).one()
    assert held.allocation_mode == held_mode
    # The restore's own two releases, in its order.
    service.release_pre_delivery_prepaid_settlement_for_order(pending.id, reason=CLICK_RESTORE_REASON)
    service.release_reserved_prepayment_for_order(pending.id, reason=CLICK_RESTORE_REASON)
    db.session.commit()
    db.session.expire_all()
    held = db.session.get(CashCollectionAllocation, held.id)
    assert (held.reversed_at is not None, held.reversal_reason) == (True, CLICK_RESTORE_REASON)

    _, residual_plan = _preview(client, admin_claim_headers, residual_customer.id, paid_by="office")
    _, released_plan = _preview(client, admin_claim_headers, released_customer.id, paid_by="office")

    assert [(d["event_id"], d["amount"], d["card_click"]) for d in residual_plan["data"]["drawn"]] == [
        (residual.id, 12000.0, True)
    ]
    assert [(d["event_id"], d["amount"], d["card_click"]) for d in released_plan["data"]["drawn"]] == [
        (released, 9000.0, False)
    ]


def test_a_real_click_rail_restore_leaves_card_click_residual(
    app, client, db, admin_user, admin_claim_headers, no_fiscalization
):
    """No hand-written reason: the restore itself reverses the door cash and writes the reason the clause reads."""
    customer = make_user(db)
    order, payment = delivered_cod_order(db, customer, total=Decimal("12000.00"))
    door = CashCollectionService().post_collection(
        customer_id=customer.id,
        amount=Decimal("12000.00"),
        source="standalone_meeting",
        order_id=order.id,
        recorded_by_user_id=admin_user.id,
        notes="Paid at the door",
        occurred_at=datetime.now(UTC) - DAY,
    )
    payment.provider_data = {"click": {"click_trans_id": "940001", "click_paydoc_id": "5231141285"}}
    db.session.commit()
    allocation = CashCollectionAllocation.query.filter_by(cash_collection_event_id=door.id).one()
    assert allocation.reversed_at is None

    ClickPaymentProviderService()._restore_click_rail_after_offline_settlement(
        payment, {"click_trans_id": "940001", "click_paydoc_id": "5231141285", "amount": "12000"}, str(payment.id)
    )
    db.session.commit()
    db.session.expire_all()

    allocation = db.session.get(CashCollectionAllocation, allocation.id)
    assert allocation.reversal_reason.startswith(CLICK_RAIL_RESTORE_REASON_PREFIX)
    _, plan = _preview(client, admin_claim_headers, customer.id, paid_by="office")
    assert [(d["event_id"], d["amount"], d["card_click"]) for d in plan["data"]["drawn"]] == [(door.id, 12000.0, True)]


def test_click_late_debit_backfill_is_card_click(client, db, admin_user, admin_claim_headers, sample_user):
    late = _credit(
        sample_user,
        "7000",
        admin_user,
        occurred_at=datetime.now(UTC) - DAY,
        source="backfill",
        proof_data={"flow": "click_late_debit", "click_trans_id": "77"},
        notes="Late Click debit credited",
    )

    _, plan = _preview(client, admin_claim_headers, sample_user.id, paid_by="office")

    assert plan["data"]["drawn"] == [
        {
            "event_id": late,
            "customer_id": sample_user.id,
            "amount": 7000.0,
            "revenue_counted": False,
            "card_click": True,
        }
    ]


# --------------------------------------------------------------------------- who paid (P4)


def test_driver_settled_records_driver_only(
    client, db, admin_user, admin_claim_headers, sample_user, second_delivery_driver
):
    # The driver took 50 000 from the customer with nothing owed: it is credit, in the driver's trip cash.
    event = CashCollectionService().post_collection(
        customer_id=sample_user.id,
        amount=Decimal("50000.00"),
        source="standalone_meeting",
        collector_user_id=second_delivery_driver.id,
        recorded_by_user_id=admin_user.id,
        notes="Paid ahead to the driver",
    )
    session_id = event.driver_cash_session_id
    session = db.session.get(DriverCashSession, session_id)
    on_hand_before, gross_before, status_before = (
        Decimal(str(session.expected_cash_on_hand)),
        Decimal(str(session.expected_cash)),
        session.status,
    )
    assert on_hand_before == Decimal("50000.00")

    status, body = _apply(
        client,
        admin_claim_headers,
        sample_user.id,
        amount=50000,
        paid_by="driver_settled",
        driver_user_id=second_delivery_driver.id,
    )

    assert status == 201, body
    payout = body["data"]["payout"]
    assert (payout["paid_by"], payout["driver_user_id"], payout["driver_name"], payout["driver_cash_session_id"]) == (
        "driver_settled",
        second_delivery_driver.id,
        "Second Driver",
        None,
    )
    assert body["data"]["driver_cash_session"] is None
    db.session.expire_all()
    session = db.session.get(DriverCashSession, session_id)
    assert (
        Decimal(str(session.expected_cash_on_hand)),
        Decimal(str(session.expected_cash)),
        session.status,
    ) == (on_hand_before, gross_before, status_before)
    row = CustomerCreditPayout.query.one()
    assert (row.paid_by, row.driver_user_id, row.driver_cash_session_id) == (
        "driver_settled",
        second_delivery_driver.id,
        None,
    )


def test_delivery_person_id_is_not_a_driver_id(
    client, db, admin_user, sample_user, admin_claim_headers, second_delivery_driver
):
    _record_collection(client, admin_claim_headers, sample_user.id, 50000)
    dp = DeliveryPerson.query.filter_by(user_id=second_delivery_driver.id).one()
    assert dp.id != dp.user_id

    _, plan = _preview(client, admin_claim_headers, sample_user.id, paid_by="driver_settled", driver_user_id=dp.id)
    status, body = _apply(
        client, admin_claim_headers, sample_user.id, amount=50000, paid_by="driver_settled", driver_user_id=dp.id
    )

    assert plan["data"]["blocking_reasons"][0].startswith("driver_not_eligible:")
    assert status == 400, body
    assert body["data"]["error_code"] == "PAYOUT_BLOCKED"
    assert body["errors"][0].startswith("driver_not_eligible:")
    assert CustomerCreditPayout.query.count() == 0


def test_inactive_delivery_person_is_blocked(
    client, db, admin_user, admin_claim_headers, sample_user, second_delivery_driver
):
    _record_collection(client, admin_claim_headers, sample_user.id, 50000)
    dp = DeliveryPerson.query.filter_by(user_id=second_delivery_driver.id).one()
    dp.is_active = False
    db.session.commit()

    _, plan = _preview(
        client, admin_claim_headers, sample_user.id, paid_by="driver_settled", driver_user_id=second_delivery_driver.id
    )
    status, body = _apply(
        client,
        admin_claim_headers,
        sample_user.id,
        amount=50000,
        paid_by="driver_settled",
        driver_user_id=second_delivery_driver.id,
    )

    assert len(plan["data"]["blocking_reasons"]) == 1
    assert plan["data"]["blocking_reasons"][0].startswith("driver_not_eligible:")
    assert plan["data"]["is_applicable"] is False
    assert (status, body["data"]["error_code"]) == (400, "PAYOUT_BLOCKED")
    assert CustomerCreditPayout.query.count() == 0


# --------------------------------------------------------------------------- idempotency and inputs


def test_double_submit_with_one_key_pays_once(client, db, admin_user, admin_claim_headers, sample_user):
    event_id = _record_collection(client, admin_claim_headers, sample_user.id, 50000)

    first_status, first = _apply(client, admin_claim_headers, sample_user.id, amount=20000, idempotency_key="k-7")
    second_status, second = _apply(client, admin_claim_headers, sample_user.id, amount=20000, idempotency_key="k-7")

    assert (first_status, second_status) == (201, 200), (first, second)
    assert first["data"]["replayed"] is False
    assert second["data"]["replayed"] is True
    assert second["data"]["payout"] == first["data"]["payout"]
    assert second["data"]["available_prepayment_balance"] == 30000.0
    assert CustomerCreditPayout.query.count() == 1
    assert CustomerCreditPayoutDebit.query.count() == 1
    assert _unapplied(db, event_id) == Decimal("30000.00")


@pytest.mark.parametrize("amount", ["50 000", "20000.555", True, -5, 0, "abc"])
def test_amount_shapes(client, db, admin_user, admin_claim_headers, sample_user, amount):
    _record_collection(client, admin_claim_headers, sample_user.id, 50000)

    preview_status, preview = _preview(client, admin_claim_headers, sample_user.id, paid_by="office", amount=amount)
    apply_status, applied = _apply(client, admin_claim_headers, sample_user.id, amount=amount)

    assert (preview_status, preview["data"]["error_code"]) == (400, "PAYOUT_AMOUNT_INVALID")
    assert (apply_status, applied["data"]["error_code"]) == (400, "PAYOUT_AMOUNT_INVALID")
    assert CustomerCreditPayout.query.count() == 0


def test_whole_and_two_decimal_amounts_are_accepted(client, db, admin_user, admin_claim_headers, sample_user):
    event_id = _record_collection(client, admin_claim_headers, sample_user.id, 50000)

    first_status, first = _apply(client, admin_claim_headers, sample_user.id, amount=20000, idempotency_key="k-int")
    second_status, second = _apply(
        client, admin_claim_headers, sample_user.id, amount="20000.00", idempotency_key="k-str"
    )

    assert (first_status, second_status) == (201, 201), (first, second)
    assert (first["data"]["payout"]["amount"], second["data"]["payout"]["amount"]) == (20000.0, 20000.0)
    assert _unapplied(db, event_id) == Decimal("10000.00")


def test_apply_without_amount_or_target_is_refused(client, db, admin_user, admin_claim_headers, sample_user):
    _record_collection(client, admin_claim_headers, sample_user.id, 50000)

    status, body = _apply(client, admin_claim_headers, sample_user.id)

    assert (status, body["data"]["error_code"]) == (400, "PAYOUT_AMOUNT_INVALID")
    assert CustomerCreditPayout.query.count() == 0


@pytest.mark.parametrize(
    "body_patch,code",
    [
        ({"paid_by": "bank"}, "PAYOUT_PAID_BY_INVALID"),
        ({"paid_by": "driver_settled"}, "PAYOUT_DRIVER_REQUIRED"),
        ({"paid_by": "office", "driver_user_id": 5}, "PAYOUT_DRIVER_NOT_ALLOWED"),
        ({"paid_at": "2026-11-10T09:00:00"}, "PAYOUT_PAID_AT_INVALID"),  # naive
        ({"paid_at": "2999-01-01T00:00:00Z"}, "PAYOUT_PAID_AT_INVALID"),  # future
        ({"reason": "abc"}, "PAYOUT_REASON_REQUIRED"),
        ({"reason": "x" * 501}, "PAYOUT_REASON_TOO_LONG"),
        ({"idempotency_key": None}, "PAYOUT_IDEMPOTENCY_KEY_REQUIRED"),
        ({"idempotency_key": ""}, "PAYOUT_IDEMPOTENCY_KEY_REQUIRED"),
        ({"idempotency_key": "   "}, "PAYOUT_IDEMPOTENCY_KEY_REQUIRED"),
        ({"idempotency_key": 12345}, "PAYOUT_IDEMPOTENCY_KEY_REQUIRED"),
        ({"idempotency_key": "k" * 65}, "PAYOUT_IDEMPOTENCY_KEY_REQUIRED"),  # one over the column
    ],
)
def test_input_errors(client, db, admin_user, admin_claim_headers, sample_user, body_patch, code):
    _record_collection(client, admin_claim_headers, sample_user.id, 50000)

    status, body = _apply(client, admin_claim_headers, sample_user.id, amount=20000, **body_patch)

    assert (status, body["data"]["error_code"]) == (400, code), body
    assert CustomerCreditPayout.query.count() == 0


def test_a_key_of_exactly_the_column_length_is_accepted(client, db, admin_user, admin_claim_headers, sample_user):
    _record_collection(client, admin_claim_headers, sample_user.id, 50000)
    key = "k" * 64

    status, body = _apply(client, admin_claim_headers, sample_user.id, amount=20000, idempotency_key=key)

    assert status == 201, body
    assert CustomerCreditPayout.query.one().idempotency_key == key


def test_a_paid_at_beyond_the_calendar_is_refused_not_a_500(client, db, admin_user, admin_claim_headers, sample_user):
    """Year 1 at +05:00 parses, but moving it to UTC leaves the calendar (OverflowError)."""
    event_id = _record_collection(client, admin_claim_headers, sample_user.id, 50000)

    preview_status, preview = _preview(
        client, admin_claim_headers, sample_user.id, paid_by="office", paid_at="0001-01-01T00:00:00+05:00"
    )
    apply_status, applied = _apply(
        client, admin_claim_headers, sample_user.id, amount=20000, paid_at="0001-01-01T00:00:00+05:00"
    )

    assert (preview_status, preview["data"]["error_code"]) == (400, "PAYOUT_PAID_AT_INVALID"), preview
    assert (apply_status, applied["data"]["error_code"]) == (400, "PAYOUT_PAID_AT_INVALID"), applied
    assert CustomerCreditPayout.query.count() == 0
    assert _unapplied(db, event_id) == Decimal("50000.00")


# What a broken client could send instead of the JSON object the modal sends: an array, a bare string,
# a number, null, and text that is not JSON at all.
NOT_AN_OBJECT = ["[1, 2]", '"office"', "42", "null", "{paid_by: office"]


@pytest.mark.parametrize("raw", NOT_AN_OBJECT)
def test_a_body_that_is_not_a_json_object_is_refused_on_every_payout_route(
    client, db, admin_user, admin_claim_headers, sample_user, raw
):
    event_id = _record_collection(client, admin_claim_headers, sample_user.id, 50000)
    _, applied = _apply(client, admin_claim_headers, sample_user.id, amount=20000, idempotency_key="k-body")
    payout_id = applied["data"]["payout"]["id"]
    undo = f"/api/v1/admin/staff/cash-reconciliation/prepayment-payouts/{payout_id}/undo"

    answers = [
        client.post(url, data=raw, headers=admin_claim_headers)
        for url in (PREVIEW.format(sample_user.id), APPLY.format(sample_user.id), undo)
    ]

    assert [(r.status_code, r.get_json()["data"]["error_code"]) for r in answers] == [
        (400, "PAYOUT_BODY_INVALID")
    ] * 3, [r.get_json() for r in answers]
    assert [r.get_json()["errors"] for r in answers] == [["The request body must be a JSON object"]] * 3
    db.session.expire_all()
    assert [(p.id, p.voided_at) for p in CustomerCreditPayout.query.all()] == [(payout_id, None)]
    assert _unapplied(db, event_id) == Decimal("30000.00")


def test_an_empty_body_is_read_as_an_empty_object(client, db, admin_user, admin_claim_headers, sample_user):
    """No body at all is not a broken body: each route then names the first field it misses."""
    _record_collection(client, admin_claim_headers, sample_user.id, 50000)

    preview = client.post(PREVIEW.format(sample_user.id), headers=admin_claim_headers)
    apply = client.post(APPLY.format(sample_user.id), headers=admin_claim_headers)

    assert (preview.status_code, preview.get_json()["data"]["error_code"]) == (400, "PAYOUT_PAID_BY_INVALID")
    assert (apply.status_code, apply.get_json()["data"]["error_code"]) == (400, "PAYOUT_AMOUNT_INVALID")


# The first return with key "k-reuse": 20 000, office, for `sample_user` (50 000 free, 30 000 left).
REUSED_WITH = {
    "a-different-amount": {"amount": 15000},
    "a-target-instead-of-the-amount": {"amount": None, "target_balance": 30000},
    "a-different-payer": {"paid_by": "driver_settled", "driver_user_id": "driver"},
}


@pytest.mark.parametrize("change", sorted(REUSED_WITH))
def test_a_key_reused_for_a_different_return_is_refused(
    client, db, admin_user, admin_claim_headers, sample_user, second_delivery_driver, change
):
    event_id = _record_collection(client, admin_claim_headers, sample_user.id, 50000)
    first_status, first = _apply(client, admin_claim_headers, sample_user.id, amount=20000, idempotency_key="k-reuse")
    assert first_status == 201, first
    patch = {k: (second_delivery_driver.id if v == "driver" else v) for k, v in REUSED_WITH[change].items()}
    body = {"amount": 20000, "idempotency_key": "k-reuse", **patch}

    status, answer = _apply(client, admin_claim_headers, sample_user.id, **{k: v for k, v in body.items() if v is not None})

    assert (status, answer["data"]["error_code"]) == (400, "PAYOUT_IDEMPOTENCY_KEY_REUSED"), answer
    assert answer["errors"] == [
        f"This idempotency key already recorded return {first['data']['payout']['id']}, "
        "which differs from this request; start a new return"
    ]
    assert [(p.id, Decimal(str(p.amount))) for p in CustomerCreditPayout.query.all()] == [
        (first["data"]["payout"]["id"], Decimal("20000.00"))
    ]
    assert _unapplied(db, event_id) == Decimal("30000.00")


def test_a_key_reused_for_another_customer_is_refused(client, db, admin_user, admin_claim_headers, sample_user):
    other = make_user(db)
    _record_collection(client, admin_claim_headers, sample_user.id, 50000)
    other_event = _record_collection(client, admin_claim_headers, other.id, 40000)
    _, first = _apply(client, admin_claim_headers, sample_user.id, amount=20000, idempotency_key="k-other")

    status, answer = _apply(client, admin_claim_headers, other.id, amount=20000, idempotency_key="k-other")

    assert (status, answer["data"]["error_code"]) == (400, "PAYOUT_IDEMPOTENCY_KEY_REUSED"), answer
    assert CustomerCreditPayout.query.count() == 1
    assert CustomerCreditPayout.query.one().customer_id == sample_user.id
    assert _unapplied(db, other_event) == Decimal("40000.00")
    assert first["data"]["payout"]["customer_id"] == sample_user.id


def test_a_set_balance_key_replays_the_same_target_and_refuses_another(
    client, db, admin_user, admin_claim_headers, sample_user
):
    event_id = _record_collection(client, admin_claim_headers, sample_user.id, 50000)
    first_status, first = _apply(client, admin_claim_headers, sample_user.id, target_balance=15000, idempotency_key="k-t")

    replay_status, replay = _apply(
        client, admin_claim_headers, sample_user.id, target_balance=15000, idempotency_key="k-t"
    )
    other_target_status, other_target = _apply(
        client, admin_claim_headers, sample_user.id, target_balance=10000, idempotency_key="k-t"
    )
    same_amount_status, same_amount = _apply(
        client, admin_claim_headers, sample_user.id, amount=35000, idempotency_key="k-t"
    )

    assert (first_status, first["data"]["payout"]["amount"]) == (201, 35000.0), first
    assert (replay_status, replay["data"]["replayed"], replay["data"]["payout"]) == (
        200,
        True,
        first["data"]["payout"],
    )
    assert (other_target_status, other_target["data"]["error_code"]) == (400, "PAYOUT_IDEMPOTENCY_KEY_REUSED")
    assert (same_amount_status, same_amount["data"]["error_code"]) == (400, "PAYOUT_IDEMPOTENCY_KEY_REUSED")
    assert CustomerCreditPayout.query.count() == 1
    assert _unapplied(db, event_id) == Decimal("15000.00")


# --------------------------------------------------------------------------- set the balance (P12)


def test_set_balance_records_the_difference_as_a_return(client, db, admin_user, admin_claim_headers, sample_user):
    now = datetime.now(UTC)
    e2 = _credit(sample_user, "30000", admin_user, occurred_at=now - DAY)
    e1 = _credit(sample_user, "20000", admin_user, occurred_at=now - 2 * DAY)

    status, body = _preview(client, admin_claim_headers, sample_user.id, paid_by="office", target_balance=15000)

    assert status == 200, body
    plan = body["data"]
    assert (plan["free_balance"], plan["amount"], plan["target_balance"]) == (50000.0, 35000.0, 15000.0)
    assert [(d["event_id"], d["amount"]) for d in plan["drawn"]] == [(e1, 20000.0), (e2, 15000.0)]
    assert plan["blocking_reasons"] == []

    status, body = _apply(
        client,
        admin_claim_headers,
        sample_user.id,
        target_balance=15000,
        paid_at="2026-10-01T10:00:00+05:00",
        reason="Returned in cash in October",
    )

    assert status == 201, body
    payout = body["data"]["payout"]
    assert (payout["amount"], datetime.fromisoformat(payout["paid_at"]), payout["reason"]) == (
        35000.0,
        datetime(2026, 10, 1, 5, 0, tzinfo=UTC),
        "Returned in cash in October",
    )
    assert body["data"]["available_prepayment_balance"] == 15000.0
    assert Decimal(str(CustomerCreditPayout.query.one().amount)) == Decimal("35000.00")
    assert _balance(sample_user.id) == Decimal("15000.00")


def test_set_balance_to_zero_returns_everything_free(client, db, admin_user, admin_claim_headers, sample_user):
    # 30 000 of an 80 000 collection is held on a pending order (it covers it, so it is settled ahead of
    # delivery): only 50 000 is free (P6).
    delivered_cod_order(db, sample_user, total=Decimal("30000.00"), status=OrderStatus.CONFIRMED)
    event_id = _record_collection(client, admin_claim_headers, sample_user.id, 80000)
    held = CashCollectionAllocation.query.filter_by(cash_collection_event_id=event_id).one()
    held_before = (held.id, held.allocation_mode, Decimal(str(held.allocated_amount)), held.reversed_at)
    assert held_before[1:] == ("prepaid_credit", Decimal("30000.00"), None)
    assert held.allocation_metadata.get("settled_pre_delivery") is True

    _, plan = _preview(client, admin_claim_headers, sample_user.id, paid_by="office", target_balance=0)
    status, body = _apply(client, admin_claim_headers, sample_user.id, target_balance=0)

    assert (plan["data"]["free_balance"], plan["data"]["amount"], plan["data"]["target_balance"]) == (
        50000.0,
        50000.0,
        0.0,
    )
    assert status == 201, body
    assert body["data"]["payout"]["amount"] == 50000.0
    assert body["data"]["available_prepayment_balance"] == 0.0
    db.session.expire_all()
    held = db.session.get(CashCollectionAllocation, held_before[0])
    assert (held.id, held.allocation_mode, Decimal(str(held.allocated_amount)), held.reversed_at) == held_before
    assert _unapplied(db, event_id) == Decimal("0.00")


@pytest.mark.parametrize("target", [50000, 60000])
def test_target_not_below_free_balance_is_blocked(client, db, admin_user, admin_claim_headers, sample_user, target):
    event_id = _record_collection(client, admin_claim_headers, sample_user.id, 50000)

    _, plan = _preview(client, admin_claim_headers, sample_user.id, paid_by="office", target_balance=target)
    status, body = _apply(client, admin_claim_headers, sample_user.id, target_balance=target)

    assert plan["data"]["blocking_reasons"][0].startswith("target_not_below_free_balance:")
    assert plan["data"]["drawn"] == []
    assert (status, body["data"]["error_code"]) == (400, "PAYOUT_BLOCKED")
    assert body["errors"][0].startswith("target_not_below_free_balance:")
    assert CustomerCreditPayout.query.count() == 0
    assert _unapplied(db, event_id) == Decimal("50000.00")


def test_amount_and_target_together_are_refused(client, db, admin_user, admin_claim_headers, sample_user):
    _record_collection(client, admin_claim_headers, sample_user.id, 50000)

    preview_status, preview = _preview(
        client, admin_claim_headers, sample_user.id, paid_by="office", amount=10000, target_balance=40000
    )
    apply_status, applied = _apply(client, admin_claim_headers, sample_user.id, amount=10000, target_balance=40000)

    assert (preview_status, preview["data"]["error_code"]) == (400, "PAYOUT_AMOUNT_OR_TARGET")
    assert (apply_status, applied["data"]["error_code"]) == (400, "PAYOUT_AMOUNT_OR_TARGET")
    assert CustomerCreditPayout.query.count() == 0


@pytest.mark.parametrize("target", [0, 10000])
def test_set_balance_on_no_free_balance_reports_only_no_free_balance(
    client, db, admin_user, admin_claim_headers, sample_user, target
):
    """Controller ruling (Task 2): the free balance is the root cause, so a target on a zero free balance
    gives the one block, not `target_not_below_free_balance` beside it. The whole credit is held here."""
    delivered_cod_order(db, sample_user, total=Decimal("30000.00"), status=OrderStatus.CONFIRMED)
    event_id = _record_collection(client, admin_claim_headers, sample_user.id, 30000)

    status, plan = _preview(client, admin_claim_headers, sample_user.id, paid_by="office", target_balance=target)
    apply_status, applied = _apply(client, admin_claim_headers, sample_user.id, target_balance=target)

    only_block = ["no_free_balance: the customer has no free prepaid balance to return"]
    assert status == 200, plan
    assert (plan["data"]["free_balance"], plan["data"]["blocking_reasons"]) == (0.0, only_block)
    assert (apply_status, applied["data"]["error_code"], applied["errors"]) == (400, "PAYOUT_BLOCKED", only_block)
    assert CustomerCreditPayout.query.count() == 0
    assert _unapplied(db, event_id) == Decimal("0.00")


@pytest.mark.parametrize("target", ["abc", -1, True, "15 000"])
def test_target_shapes(client, db, admin_user, admin_claim_headers, sample_user, target):
    _record_collection(client, admin_claim_headers, sample_user.id, 50000)

    status, body = _preview(client, admin_claim_headers, sample_user.id, paid_by="office", target_balance=target)

    assert (status, body["data"]["error_code"]) == (400, "PAYOUT_AMOUNT_INVALID")


# --------------------------------------------------------------------------- when, and what preview leaves


def test_backdated_paid_at_is_stored_in_utc(client, db, admin_user, admin_claim_headers, sample_user):
    _record_collection(client, admin_claim_headers, sample_user.id, 50000)

    status, body = _apply(
        client, admin_claim_headers, sample_user.id, amount=10000, paid_at="2026-11-08T10:00:00+05:00"
    )

    assert status == 201, body
    assert datetime.fromisoformat(body["data"]["payout"]["paid_at"]) == datetime(2026, 11, 8, 5, 0, tzinfo=UTC)
    db.session.expire_all()
    assert ensure_utc(CustomerCreditPayout.query.one().paid_at) == datetime(2026, 11, 8, 5, 0, tzinfo=UTC)


def test_js_iso_paid_at_is_utc_not_local_time(client, db, admin_user, admin_claim_headers, sample_user):
    """What the modal sends (`toISOString()`). The routes read the body as sent (the raw bytes), so `Z` stays UTC
    and a naive value can still be refused, which the middleware's rewritten copy could not show."""
    _record_collection(client, admin_claim_headers, sample_user.id, 50000)

    status, plan = _preview(
        client, admin_claim_headers, sample_user.id, paid_by="office", paid_at="2026-11-08T05:00:00.000Z"
    )
    apply_status, body = _apply(
        client, admin_claim_headers, sample_user.id, amount=10000, paid_at="2026-11-08T05:00:00.000Z"
    )

    assert status == 200, plan
    assert datetime.fromisoformat(plan["data"]["paid_at"]) == datetime(2026, 11, 8, 5, 0, tzinfo=UTC)
    assert apply_status == 201, body
    db.session.expire_all()
    assert ensure_utc(CustomerCreditPayout.query.one().paid_at) == datetime(2026, 11, 8, 5, 0, tzinfo=UTC)


def test_the_planner_resolves_the_credit_pool_once_and_reads_warnings_only_on_preview(
    client, db, admin_user, admin_claim_headers, sample_user, monkeypatch
):
    """One planner answers both routes: it resolves the pool once (phase 1 and the warnings take the resolved
    ids), and the apply, running under the locks, never builds the preview-only warnings."""
    _record_collection(client, admin_claim_headers, sample_user.id, 50000)
    calls = {"pool": 0, "warnings": 0, "pool_in_plan": []}
    resolve_pool, build_warnings = CashCollectionService._credit_pool_for_anchor, PrepaymentPayoutService._warnings
    plan = PrepaymentPayoutService._plan

    def counting_pool(self, anchor_user_id):
        calls["pool"] += 1
        return resolve_pool(self, anchor_user_id)

    def counting_warnings(self, pool_user_ids):
        calls["warnings"] += 1
        return build_warnings(self, pool_user_ids)

    def counting_plan(self, **kwargs):
        before = calls["pool"]
        result = plan(self, **kwargs)
        calls["pool_in_plan"].append(calls["pool"] - before)
        return result

    monkeypatch.setattr(CashCollectionService, "_credit_pool_for_anchor", counting_pool)
    monkeypatch.setattr(PrepaymentPayoutService, "_warnings", counting_warnings)
    monkeypatch.setattr(PrepaymentPayoutService, "_plan", counting_plan)

    status, _ = _preview(client, admin_claim_headers, sample_user.id, paid_by="office", amount=20000)
    assert (status, calls["pool_in_plan"], calls["warnings"]) == (200, [1], 1)

    calls.update(warnings=0, pool_in_plan=[])
    status, body = _apply(client, admin_claim_headers, sample_user.id, amount=20000, idempotency_key="k-once")
    assert (status, calls["pool_in_plan"], calls["warnings"]) == (201, [1], 0), body


def test_preview_writes_nothing(client, db, admin_user, admin_claim_headers, sample_user, second_delivery_driver):
    event_id = _record_collection(client, admin_claim_headers, sample_user.id, 50000)
    sessions_before = DriverCashSession.query.count()

    for body in (
        {"paid_by": "office"},
        {"paid_by": "office", "amount": 20000},
        {"paid_by": "office", "target_balance": 10000},
        {"paid_by": "driver_settled", "driver_user_id": second_delivery_driver.id, "amount": 20000},
    ):
        status, response = _preview(client, admin_claim_headers, sample_user.id, **body)
        assert status == 200, response
        assert response["data"]["is_applicable"] is True

    assert _unapplied(db, event_id) == Decimal("50000.00")
    assert CustomerCreditPayout.query.count() == 0
    assert CustomerCreditPayoutDebit.query.count() == 0
    assert DriverCashSession.query.count() == sessions_before


# --------------------------------------------------------------------------- audit, access, history flag


def test_the_persisted_audit_row_keeps_the_client_request_id(client, db, admin_user, admin_claim_headers, sample_user):
    """The real audit logger, not a stub: its sanitizer redacts any key whose name contains "key" or "token",
    so a payload key called idempotency_key (or client_token) would be stored as "[REDACTED]" and the value lost."""
    event_id = _record_collection(client, admin_claim_headers, sample_user.id, 50000)

    status, body = _apply(client, admin_claim_headers, sample_user.id, amount=20000, idempotency_key="k-audit")

    assert status == 201, body
    row = AuditLog.query.filter_by(
        action="customer_credit_payout_recorded", resource_id=str(body["data"]["payout"]["id"])
    ).one()
    assert row.additional_data == {
        "customer_id": sample_user.id,
        "amount": "20000.00",
        "paid_by": "office",
        "driver_user_id": None,
        "paid_at": row.additional_data["paid_at"],
        "actor_user_id": admin_user.id,
        "reason": "Customer moved away",
        "client_request_id": "k-audit",
        "card_click_amount": "0.00",
        "revenue_counted_amount": "0.00",
        "debits": [{"event_id": event_id, "amount": "20000.00"}],
    }


def test_audit_after_commit_only(client, db, admin_user, admin_claim_headers, sample_user, monkeypatch):
    event_id = _record_collection(client, admin_claim_headers, sample_user.id, 50000)
    signature = inspect.signature(AuditLogger.log_event)
    calls = []
    seen = {"payout_flushed": False, "committed_after_payout": False}

    def _record(*args, **kwargs):
        bound = signature.bind(audit_logger, *args, **kwargs)  # TypeError on a call the real method refuses
        bound.apply_defaults()
        arguments = dict(bound.arguments)
        del arguments["self"]
        arguments["committed_before_this_call"] = seen["committed_after_payout"]
        calls.append(arguments)
        return "recorded-audit-event"

    def _after_flush(session, flush_context):
        if any(isinstance(obj, CustomerCreditPayout) for obj in session.new):
            seen["payout_flushed"] = True

    def _after_commit(session):
        if seen["payout_flushed"]:
            seen["committed_after_payout"] = True

    monkeypatch.setattr(audit_logger, "log_event", _record)
    sa_event.listen(Session, "after_flush", _after_flush)
    sa_event.listen(Session, "after_commit", _after_commit)
    try:
        blocked_status, _ = _apply(client, admin_claim_headers, sample_user.id, amount=60000, idempotency_key="k-a")
        status, body = _apply(
            client,
            admin_claim_headers,
            sample_user.id,
            amount=20000,
            idempotency_key="k-b",
            paid_at="2026-11-08T10:00:00+05:00",
        )
        replay_status, _ = _apply(client, admin_claim_headers, sample_user.id, amount=20000, idempotency_key="k-b")
    finally:
        sa_event.remove(Session, "after_flush", _after_flush)
        sa_event.remove(Session, "after_commit", _after_commit)

    assert (blocked_status, status, replay_status) == (400, 201, 200)
    payout_id = body["data"]["payout"]["id"]
    payout_calls = [c for c in calls if c["event_type"] == AuditEventType.PAYMENT_REFUNDED]
    assert payout_calls == [
        {
            "event_type": AuditEventType.PAYMENT_REFUNDED,
            "action": "customer_credit_payout_recorded",
            "severity": AuditSeverity.HIGH,
            "resource_type": "customer_credit_payout",
            "resource_id": str(payout_id),
            "description": f"Prepaid credit of 20000.00 returned to customer {sample_user.id} (office)",
            "old_values": None,
            "new_values": None,
            "success": True,
            "error_message": None,
            "duration_ms": None,
            "additional_data": {
                "customer_id": sample_user.id,
                "amount": "20000.00",
                "paid_by": "office",
                "driver_user_id": None,
                "paid_at": "2026-11-08T05:00:00+00:00",
                "actor_user_id": admin_user.id,
                "reason": "Customer moved away",
                "client_request_id": "k-b",
                "card_click_amount": "0.00",
                "revenue_counted_amount": "0.00",
                "debits": [{"event_id": event_id, "amount": "20000.00"}],
            },
            "committed_before_this_call": True,
        }
    ]


def test_unknown_customer_is_404(client, db, admin_user, admin_claim_headers):
    preview_status, _ = _preview(client, admin_claim_headers, 999_999, paid_by="office")
    apply_status, _ = _apply(client, admin_claim_headers, 999_999, amount=20000)

    assert (preview_status, apply_status) == (404, 404)


def test_manager_and_operator_get_403(
    client, db, admin_user, admin_claim_headers, sample_user, manager_claim_headers, operator_auth_headers
):
    event_id = _record_collection(client, admin_claim_headers, sample_user.id, 50000)

    for headers in (manager_claim_headers, operator_auth_headers):
        preview_status, _ = _preview(client, headers, sample_user.id, paid_by="office")
        apply_status, _ = _apply(client, headers, sample_user.id, amount=20000)
        assert (preview_status, apply_status) == (403, 403)

    assert CustomerCreditPayout.query.count() == 0
    assert _unapplied(db, event_id) == Decimal("50000.00")


@pytest.mark.parametrize("headers,expected", [("admin_claim_headers", True), ("manager_claim_headers", False)])
def test_history_publishes_can_return_prepayment(client, db, sample_user, request, headers, expected):
    response = client.get(HISTORY.format(sample_user.id), headers=request.getfixturevalue(headers))

    assert response.status_code == 200, response.get_json()
    assert response.get_json()["data"]["can_return_prepayment"] is expected


@pytest.mark.parametrize("headers,may_undo", [("admin_claim_headers", True), ("manager_claim_headers", False)])
def test_history_can_undo_follows_the_viewer(
    client, db, admin_user, admin_claim_headers, sample_user, request, headers, may_undo
):
    """P1: the Undo button and the ADMIN-only undo route decide from one source. A live, undoable return
    shows `can_undo` only to a viewer the undo route would let through; `undo_block_code` keeps the
    return's own reason (none here) for everyone."""
    _record_collection(client, admin_claim_headers, sample_user.id, 50000)
    status, applied = _apply(client, admin_claim_headers, sample_user.id, amount=20000)
    assert (status, applied["data"]["payout"]["can_undo"]) == (201, True), applied

    response = client.get(HISTORY.format(sample_user.id), headers=request.getfixturevalue(headers))

    assert response.status_code == 200, response.get_json()
    data = response.get_json()["data"]
    assert data["can_return_prepayment"] is may_undo
    assert [(row["id"], row["can_undo"], row["undo_block_code"]) for row in data["payouts"]] == [
        (applied["data"]["payout"]["id"], may_undo, None)
    ]
