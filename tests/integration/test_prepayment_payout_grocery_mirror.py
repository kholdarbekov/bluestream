"""Return prepayment for a grocery store on a money (AMOUNT-mode) contract: the contract mirror
(spec 2026-11-10 §4.2, Task 7).

A grocery store's collections are mirrored onto its contract, so the surplus the store paid shows
twice: as the store's prepaid credit and as contract credit (a negative ``outstanding_amount``).
A return must move both, or neither: it posts +amount on the contract the drawn credit was
mirrored on, and its undo posts the inverse. ``lifetime_collected`` never moves, because the money
was collected and then handed back.

Every return, preview and undo goes through the admin routes the modal calls, with the body it
sends. Credit is booked the way production books it: the admin "Record collection" route (which
mirrors through ``settle_order_collection``), and the real writers for the shapes that route cannot
make (the electronic payment mirror, a cancelled card order re-booked as credit, a units top-up).
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import text

from business_app.models.corporate import (
    CorporateContractStatus,
    CorporatePrepaymentAccount,
    CorporatePrepaymentEventType,
    CorporatePrepaymentLedger,
)
from business_app.models.payment import CashCollectionEvent, CustomerCreditPayout, Payment
from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.corporate_contract_service import CorporateContractService
from shared.enums import OrderStatus, PaymentMethod, PaymentStatus
from tests.unit._scope_money_helpers import delivered_cod_order
from tests.unit.test_corporate_contract_service import _setup_units_grocery_reserved_order
from tests.unit.test_order_cash_edit_service import _make_grocery_amount_contract

pytestmark = pytest.mark.integration

BASE = "/api/v1/admin/staff/cash-reconciliation"
PREVIEW = BASE + "/customers/{}/prepayment-payouts/preview"
APPLY = BASE + "/customers/{}/prepayment-payouts"
UNDO = BASE + "/prepayment-payouts/{}/undo"
COLLECTIONS = BASE + "/collections"
ADJUSTMENTS = "/api/v1/admin/corporate/contracts/{}/adjustments"


# --------------------------------------------------------------------------- helpers


def _collect(client, headers, customer_id, amount, *, order_id=None, notes="Paid at the door"):
    """The admin "Record collection" modal (no collector: the office took it)."""
    body = {"customer_id": customer_id, "amount": amount, "source": "standalone_meeting", "notes": notes}
    if order_id is not None:
        body["order_id"] = order_id
    response = client.post(COLLECTIONS, json=body, headers=headers)
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]["cash_collection_event"]["id"]


def _preview(client, headers, customer_id, **body):
    response = client.post(PREVIEW.format(customer_id), json={"paid_by": "office", **body}, headers=headers)
    assert response.status_code == 200, response.get_json()
    return response.get_json()["data"]


def _apply(client, headers, customer_id, key="g-1", **body):
    payload = {"paid_by": "office", "reason": "Store closed for good", "idempotency_key": key, **body}
    response = client.post(APPLY.format(customer_id), json=payload, headers=headers)
    return response.status_code, response.get_json()


def _undo(client, headers, payout_id):
    response = client.post(UNDO.format(payout_id), json={"reason": "Recorded by mistake"}, headers=headers)
    return response.status_code, response.get_json()


def _account(db, contract_id):
    db.session.expire_all()
    return CorporatePrepaymentAccount.query.filter_by(contract_id=contract_id).first()


def _money(account):
    return (Decimal(str(account.outstanding_amount)), Decimal(str(account.lifetime_collected)))


def _rows(db, prefix):
    db.session.expire_all()
    return (
        CorporatePrepaymentLedger.query.filter(CorporatePrepaymentLedger.idempotency_key.like(f"{prefix}%"))
        .order_by(CorporatePrepaymentLedger.id)
        .all()
    )


def _effect(contract, amount):
    """One preview contract_effect row: the id, the readable label (number and name) and the amount."""
    return {
        "contract_id": contract.id,
        "contract_number": contract.contract_number,
        "name": contract.name,
        "amount": amount,
    }


def _balance(customer_id):
    return CashCollectionService().get_customer_prepaid_balance(customer_id)


def _grocery_door_surplus(client, headers, db, store):
    """The store's money contract; a delivered COD order of 100,000 charged on delivery; 130,000 paid at the door.

    The door cash pays the order and leaves 30,000 of credit, mirrored on the contract: outstanding
    -30,000 (contract credit), lifetime collected 130,000. Returns (contract, door event id).
    """
    contract, _ = _make_grocery_amount_contract(db, store)
    order, _payment = delivered_cod_order(db, store, total=Decimal("100000.00"))
    CorporateContractService().charge_on_delivery(order)
    db.session.commit()
    event_id = _collect(client, headers, store.id, 130000, order_id=order.id)
    assert _money(_account(db, contract.id)) == (Decimal("-30000.00"), Decimal("130000.00"))
    assert _balance(store.id) == Decimal("30000.00")
    return contract, event_id


def _assert_nothing_written(db, contract_id, money_before):
    assert CustomerCreditPayout.query.count() == 0
    assert _rows(db, "payout:") == []
    assert _money(_account(db, contract_id)) == money_before


# --------------------------------------------------------------------------- the mirror entry


def test_grocery_return_posts_the_matching_contract_entry(client, db, admin_user, admin_claim_headers, sample_user):
    contract, _ = _grocery_door_surplus(client, admin_claim_headers, db, sample_user)
    account_id = _account(db, contract.id).id

    preview = _preview(client, admin_claim_headers, sample_user.id, amount=30000)
    assert preview["contract_effect"] == [_effect(contract, 30000.0)]
    assert isinstance(preview["contract_effect"][0]["amount"], float)
    assert (preview["blocking_reasons"], preview["is_applicable"]) == ([], True)

    status, body = _apply(client, admin_claim_headers, sample_user.id, amount=30000)

    assert status == 201, body
    payout_id = body["data"]["payout"]["id"]
    assert body["data"]["available_prepayment_balance"] == 0.0
    account = _account(db, contract.id)
    assert _money(account) == (Decimal("0.00"), Decimal("130000.00"))  # outstanding moves; lifetime collected does not
    assert Decimal(str(account.lifetime_charged)) == Decimal("100000.00")
    [row] = _rows(db, "payout:")
    assert row.idempotency_key == f"payout:{payout_id}:contract:{contract.id}"
    assert (row.event_type, row.contract_id, row.account_id) == (
        CorporatePrepaymentEventType.ADJUSTMENT,
        contract.id,
        account_id,
    )
    assert Decimal(str(row.amount)) == Decimal("30000.00")
    assert (row.actor_user_id, row.notes) == (admin_user.id, f"Prepayment returned to the customer (payout {payout_id})")
    assert row.entry_metadata == {"kind": "prepayment_payout", "payout_id": payout_id, "outstanding_after": 0.0}


def test_undo_posts_the_inverse(client, db, admin_user, admin_claim_headers, sample_user):
    contract, _ = _grocery_door_surplus(client, admin_claim_headers, db, sample_user)
    status, body = _apply(client, admin_claim_headers, sample_user.id, amount=30000)
    assert status == 201, body
    payout_id = body["data"]["payout"]["id"]
    [forward] = _rows(db, "payout:")

    status, body = _undo(client, admin_claim_headers, payout_id)

    assert status == 200, body
    assert body["data"]["available_prepayment_balance"] == 30000.0
    assert _money(_account(db, contract.id)) == (Decimal("-30000.00"), Decimal("130000.00"))
    assert [r.id for r in _rows(db, "payout:")] == [forward.id]  # the forward entry stays: the ledger is append-only
    [reverse] = _rows(db, "reverse:payout:")
    assert reverse.idempotency_key == f"reverse:payout:{payout_id}:contract:{contract.id}"
    assert (reverse.event_type, reverse.contract_id) == (CorporatePrepaymentEventType.ADJUSTMENT, contract.id)
    assert Decimal(str(reverse.amount)) == Decimal("-30000.00")
    assert (reverse.actor_user_id, reverse.notes) == (admin_user.id, f"Prepayment return undone (payout {payout_id})")
    assert reverse.entry_metadata == {
        "kind": "prepayment_payout_reversal",
        "payout_id": payout_id,
        "reversal_of_entry_id": forward.id,
        "outstanding_after": -30000.0,
    }


def _former_contract_then_current(client, headers, db, store):
    """The store's FIRST money contract took the door surplus (30,000) and ended; its CURRENT contract took a later
    office prepayment (20,000). Returns (former, current, former event id, current event id)."""
    former, door_event_id = _grocery_door_surplus(client, headers, db, store)
    former.status = CorporateContractStatus.TERMINATED
    former.is_active = False
    former.end_date = datetime.now(UTC) - timedelta(hours=1)
    db.session.commit()
    current, _ = _make_grocery_amount_contract(db, store)
    office_event_id = _collect(client, headers, store.id, 20000, notes="Prepaid at the office")
    assert [(row.contract_id, Decimal(str(row.amount))) for row in _rows(db, "collect:cash_event:")] == [
        (former.id, Decimal("130000.00")),
        (current.id, Decimal("20000.00")),
    ]
    return former, current, door_event_id, office_event_id


def test_a_return_across_a_closed_former_contract_and_the_current_one_posts_on_each(
    client, db, admin_user, admin_claim_headers, sample_user
):
    """Each drawn event posts on the contract ITS cash was mirrored on, never on the store's current one: two
    entries, two keys. The undo reverses both."""
    former, current, door_event_id, office_event_id = _former_contract_then_current(
        client, admin_claim_headers, db, sample_user
    )
    assert _money(_account(db, former.id)) == (Decimal("-30000.00"), Decimal("130000.00"))
    assert _money(_account(db, current.id)) == (Decimal("-20000.00"), Decimal("20000.00"))

    preview = _preview(client, admin_claim_headers, sample_user.id, amount=45000)
    assert [(credit["event_id"], credit["amount"]) for credit in preview["drawn"]] == [
        (door_event_id, 30000.0),
        (office_event_id, 15000.0),
    ]
    assert preview["contract_effect"] == sorted(
        [_effect(former, 30000.0), _effect(current, 15000.0)],
        key=lambda effect: effect["contract_id"],
    )

    status, body = _apply(client, admin_claim_headers, sample_user.id, amount=45000)

    assert status == 201, body
    payout_id = body["data"]["payout"]["id"]
    forward = _rows(db, "payout:")
    assert [(row.idempotency_key, row.contract_id, Decimal(str(row.amount))) for row in forward] == sorted(
        [
            (f"payout:{payout_id}:contract:{former.id}", former.id, Decimal("30000.00")),
            (f"payout:{payout_id}:contract:{current.id}", current.id, Decimal("15000.00")),
        ],
        key=lambda entry: entry[1],
    )
    assert _money(_account(db, former.id)) == (Decimal("0.00"), Decimal("130000.00"))
    assert _money(_account(db, current.id)) == (Decimal("-5000.00"), Decimal("20000.00"))

    status, body = _undo(client, admin_claim_headers, payout_id)

    assert status == 200, body
    reverse = _rows(db, "reverse:payout:")
    assert sorted((row.idempotency_key, row.contract_id, Decimal(str(row.amount))) for row in reverse) == sorted(
        [
            (f"reverse:payout:{payout_id}:contract:{former.id}", former.id, Decimal("-30000.00")),
            (f"reverse:payout:{payout_id}:contract:{current.id}", current.id, Decimal("-15000.00")),
        ]
    )
    assert {row.entry_metadata["reversal_of_entry_id"] for row in reverse} == {row.id for row in forward}
    assert _money(_account(db, former.id)) == (Decimal("-30000.00"), Decimal("130000.00"))
    assert _money(_account(db, current.id)) == (Decimal("-20000.00"), Decimal("20000.00"))
    assert _balance(sample_user.id) == Decimal("50000.00")


def _mirror_lookup_statements(db, run):
    """The ledger SELECTs `run()` issues against corporate_prepayment_ledger, by kind."""
    seen = []

    def record(conn, cursor, statement, parameters, context, executemany):
        sql = statement.lower()
        if sql.lstrip().startswith("select") and "from corporate_prepayment_ledger" in sql:
            if "reverse:collect" in " ".join(str(p) for p in parameters):
                seen.append("reversal")
            elif any(str(p).startswith("topup:") for p in parameters):
                seen.append("topup")
            elif any(str(p).startswith("collect:") for p in parameters):
                seen.append("collect")
            else:
                seen.append("other")

    sa_event.listen(db.engine, "before_cursor_execute", record)
    try:
        run()
    finally:
        sa_event.remove(db.engine, "before_cursor_execute", record)
    return seen


def test_the_mirror_lookups_are_batched_over_every_drawn_event(
    client, db, admin_user, admin_claim_headers, sample_user
):
    """One COLLECT query, one reversal query and one TOPUP query however many events a return draws."""
    contract, _ = _grocery_door_surplus(client, admin_claim_headers, db, sample_user)
    for amount in (20000, 10000, 5000):
        _collect(client, admin_claim_headers, sample_user.id, amount, notes="Prepaid at the office")

    seen = _mirror_lookup_statements(
        db, lambda: _preview(client, admin_claim_headers, sample_user.id, amount=60000)
    )

    assert sorted(seen) == ["collect", "reversal", "topup"]
    assert _preview(client, admin_claim_headers, sample_user.id, amount=60000)["contract_effect"] == [
        _effect(contract, 60000.0)
    ]


def test_two_drawn_events_on_one_contract_post_one_entry(client, db, admin_claim_headers, sample_user):
    """The door surplus (30,000) and an office prepayment (20,000), both mirrored on the store's contract.
    A return of 40,000 draws both, and only part of the second: the entry is what was drawn, summed per contract."""
    contract, _ = _grocery_door_surplus(client, admin_claim_headers, db, sample_user)
    _collect(client, admin_claim_headers, sample_user.id, 20000, notes="Prepaid at the office")
    assert _money(_account(db, contract.id)) == (Decimal("-50000.00"), Decimal("150000.00"))

    preview = _preview(client, admin_claim_headers, sample_user.id, amount=40000)
    assert [credit["amount"] for credit in preview["drawn"]] == [30000.0, 10000.0]
    assert preview["contract_effect"] == [_effect(contract, 40000.0)]

    status, body = _apply(client, admin_claim_headers, sample_user.id, amount=40000)

    assert status == 201, body
    [row] = _rows(db, "payout:")
    assert Decimal(str(row.amount)) == Decimal("40000.00")
    assert _money(_account(db, contract.id)) == (Decimal("-10000.00"), Decimal("150000.00"))
    assert _balance(sample_user.id) == Decimal("10000.00")


def test_cancelled_card_credit_mirrors_on_the_payment_collect(client, db, admin_user, admin_claim_headers, sample_user):
    """A card-paid order is mirrored by the payment's own COLLECT. Cancelled, it is re-booked as credit with the
    event mirror suppressed, so the contract credit lives on that payment's COLLECT, and the return posts there."""
    contract, _ = _make_grocery_amount_contract(db, sample_user)
    order, payment = delivered_cod_order(
        db,
        sample_user,
        total=Decimal("8000.00"),
        outstanding=Decimal("0.00"),
        status=OrderStatus.CANCELLED,
        payment_method=PaymentMethod.CARD,
        payment_status=PaymentStatus.COMPLETED,
    )
    contracts = CorporateContractService()
    # The electronic payment mirror, exactly as PaymentService posts it on completion.
    contracts.settle_order_collection(
        user=sample_user,
        order_id=order.id,
        collected_amount=Decimal("8000.00"),
        source="card",
        payment_id=payment.id,
        notes="Electronic payment settled against grocery contract",
    )
    event = CashCollectionService().credit_customer_for_dead_order_prepayment(
        payment, reason="Store cancelled the order", actor_user_id=admin_user.id
    )
    db.session.commit()
    assert (event.proof_data["flow"], event.proof_data["payment_id"]) == ("order_cancel_prepaid_credit", payment.id)
    assert _rows(db, f"collect:cash_event:{event.id}:") == []  # the event mirror is suppressed for this flow
    assert _money(_account(db, contract.id)) == (Decimal("-8000.00"), Decimal("8000.00"))

    preview = _preview(client, admin_claim_headers, sample_user.id)
    assert preview["contract_effect"] == [_effect(contract, 8000.0)]
    status, body = _apply(client, admin_claim_headers, sample_user.id, amount=8000)

    assert status == 201, body
    payout_id = body["data"]["payout"]["id"]
    [row] = _rows(db, "payout:")
    assert (row.idempotency_key, Decimal(str(row.amount))) == (
        f"payout:{payout_id}:contract:{contract.id}",
        Decimal("8000.00"),
    )
    assert _money(_account(db, contract.id)) == (Decimal("0.00"), Decimal("8000.00"))


def test_unmirrored_event_posts_nothing(client, db, admin_claim_headers, sample_user):
    """Credit booked before the store became a grocery store was never mirrored: nothing to post."""
    _collect(client, admin_claim_headers, sample_user.id, 25000, notes="Prepaid at the office")
    contract, _ = _make_grocery_amount_contract(db, sample_user)
    assert _rows(db, "collect:") == []

    preview = _preview(client, admin_claim_headers, sample_user.id)
    assert (preview["contract_effect"], preview["blocking_reasons"]) == ([], [])
    status, body = _apply(client, admin_claim_headers, sample_user.id, amount=25000)

    assert status == 201, body
    assert _rows(db, "payout:") == []
    assert _money(_account(db, contract.id)) == (Decimal("0.00"), Decimal("0.00"))
    assert _balance(sample_user.id) == Decimal("0.00")


def test_reversed_collect_row_is_not_live(client, db, admin_claim_headers, sample_user):
    """A COLLECT row with a ``reverse:collect:{id}`` row was already taken back off the contract: it mirrors nothing."""
    contract, _ = _make_grocery_amount_contract(db, sample_user)
    event_id = _collect(client, admin_claim_headers, sample_user.id, 20000, notes="Prepaid at the office")
    CorporateContractService().reverse_order_collection(cash_event_id=event_id, reason="Mirror taken back")
    db.session.commit()
    assert _money(_account(db, contract.id)) == (Decimal("0.00"), Decimal("0.00"))

    preview = _preview(client, admin_claim_headers, sample_user.id)
    assert (preview["contract_effect"], preview["blocking_reasons"]) == ([], [])
    status, body = _apply(client, admin_claim_headers, sample_user.id, amount=20000)

    assert status == 201, body
    assert _rows(db, "payout:") == []
    assert _money(_account(db, contract.id)) == (Decimal("0.00"), Decimal("0.00"))


# --------------------------------------------------------------------------- blocks: never one side only


def test_units_topup_mirror_blocks(client, db, admin_claim_headers, sample_user):
    """A legacy UNITS contract mirrors money as prepaid bottles; a money return cannot mirror that."""
    service, contract, account, _product, order = _setup_units_grocery_reserved_order(db, sample_user)
    event_id = _collect(client, admin_claim_headers, sample_user.id, 28000, notes="Prepaid at the office")
    # The units mirror of that cash, as settle_order_collection posts it for an order on a UNITS contract.
    [topup] = service.topup_from_cash_collection(
        contract=contract, order_id=order.id, cash_event_id=event_id, collected_amount=Decimal("28000.00")
    )
    db.session.commit()
    assert topup.idempotency_key.startswith(f"topup:cash_event:{event_id}:")
    money_before = _money(_account(db, contract.id))

    preview = _preview(client, admin_claim_headers, sample_user.id)
    assert preview["contract_effect"] == []
    assert preview["blocking_reasons"] == [
        f"units_contract_mirror: credit event {event_id} funded prepaid bottles on a units contract, "
        "which a money return cannot mirror; adjust the contract instead"
    ]
    assert preview["is_applicable"] is False
    status, body = _apply(client, admin_claim_headers, sample_user.id, amount=28000)

    assert status == 400, body
    assert body["data"]["error_code"] == "PAYOUT_BLOCKED"
    _assert_nothing_written(db, contract.id, money_before)
    assert _balance(sample_user.id) == Decimal("28000.00")


def test_units_topup_of_a_cancelled_card_payment_blocks(client, db, admin_user, admin_claim_headers, sample_user):
    """On a UNITS contract a card payment is mirrored as prepaid bottles under the PAYMENT's key. Cancelled, the
    money is re-booked as credit while those bottles stay funded: a money return would pay out one side only."""
    service, contract, _account_row, _product, order = _setup_units_grocery_reserved_order(db, sample_user)
    payment = Payment(
        order_id=order.id,
        user_id=sample_user.id,
        payment_method=PaymentMethod.CARD,
        amount=order.total_amount,
        currency="UZS",
        status=PaymentStatus.COMPLETED,
        payment_id="pay-units-card",
        amount_collected=order.total_amount,
        outstanding_amount=Decimal("0.00"),
    )
    db.session.add(payment)
    db.session.commit()
    # The electronic payment mirror, exactly as PaymentService posts it on completion (UNITS branch: a top-up).
    service.settle_order_collection(
        user=sample_user,
        order_id=order.id,
        collected_amount=Decimal(str(order.total_amount)),
        source="card",
        payment_id=payment.id,
        notes="Electronic payment settled against grocery contract",
    )
    order.status = OrderStatus.CANCELLED
    event = CashCollectionService().credit_customer_for_dead_order_prepayment(
        payment, reason="Store cancelled the order", actor_user_id=admin_user.id
    )
    db.session.commit()
    assert [row.idempotency_key.split(":")[:3] for row in _rows(db, "topup:")] == [["topup", "payment", str(payment.id)]]
    money_before = _money(_account(db, contract.id))

    preview = _preview(client, admin_claim_headers, sample_user.id)
    assert preview["blocking_reasons"] == [
        f"units_contract_mirror: credit event {event.id} funded prepaid bottles on a units contract, "
        "which a money return cannot mirror; adjust the contract instead"
    ]
    status, body = _apply(client, admin_claim_headers, sample_user.id, amount=28000)

    assert status == 400, body
    assert body["data"]["error_code"] == "PAYOUT_BLOCKED"
    _assert_nothing_written(db, contract.id, money_before)


def test_ambiguous_contract_mirror_blocks(client, db, admin_claim_headers, sample_user):
    """The door event's COLLECT rows sit on two contracts: there is no single contract to post the return on."""
    contract, event_id = _grocery_door_surplus(client, admin_claim_headers, db, sample_user)
    second, _ = _make_grocery_amount_contract(db, sample_user)
    CorporateContractService().record_money_collection(
        second, Decimal("5000.00"), source="standalone_meeting", cash_event_id=event_id
    )
    db.session.commit()
    money_before = (_money(_account(db, contract.id)), _money(_account(db, second.id)))

    preview = _preview(client, admin_claim_headers, sample_user.id)
    assert preview["contract_effect"] == []
    assert preview["blocking_reasons"] == [
        f"ambiguous_contract_mirror: credit event {event_id} is mirrored on contracts "
        f"{contract.id}, {second.id}, so there is no single contract to post the return on"
    ]
    status, body = _apply(client, admin_claim_headers, sample_user.id, amount=30000)

    assert status == 400, body
    assert body["data"]["error_code"] == "PAYOUT_BLOCKED"
    assert CustomerCreditPayout.query.count() == 0
    assert _rows(db, "payout:") == []
    assert (_money(_account(db, contract.id)), _money(_account(db, second.id))) == money_before


def test_missing_account_blocks(client, db, admin_claim_headers, sample_user):
    """The contract lost its prepayment account (raw delete; the test DB runs without foreign keys).
    The return blocks, and no account is ever created for it."""
    contract, _ = _grocery_door_surplus(client, admin_claim_headers, db, sample_user)
    db.session.execute(
        text("DELETE FROM corporate_prepayment_accounts WHERE contract_id = :cid"), {"cid": contract.id}
    )
    db.session.commit()

    preview = _preview(client, admin_claim_headers, sample_user.id)
    assert preview["contract_effect"] == [_effect(contract, 30000.0)]
    assert preview["blocking_reasons"] == [
        f"contract_account_missing: contract {contract.id} has no prepayment account to post the return on"
    ]
    status, body = _apply(client, admin_claim_headers, sample_user.id, amount=30000)

    assert status == 400, body
    assert body["data"]["error_code"] == "PAYOUT_BLOCKED"
    assert CustomerCreditPayout.query.count() == 0
    assert _rows(db, "payout:") == []
    assert _account(db, contract.id) is None


# --------------------------------------------------------------------------- idempotency


def test_replayed_apply_posts_once(client, db, admin_claim_headers, sample_user):
    contract, _ = _grocery_door_surplus(client, admin_claim_headers, db, sample_user)
    status, first = _apply(client, admin_claim_headers, sample_user.id, key="g-replay", amount=30000)
    assert status == 201, first

    status, second = _apply(client, admin_claim_headers, sample_user.id, key="g-replay", amount=30000)

    assert status == 200, second
    assert second["data"]["replayed"] is True
    assert second["data"]["payout"]["id"] == first["data"]["payout"]["id"]
    assert len(_rows(db, "payout:")) == 1
    assert _money(_account(db, contract.id)) == (Decimal("0.00"), Decimal("130000.00"))


def test_post_money_adjustment_key_is_idempotent(db, admin_user, sample_user):
    contract, _ = _make_grocery_amount_contract(db, sample_user)
    service = CorporateContractService()

    first = service.post_money_adjustment(
        contract, Decimal("12000.00"), admin_user.id, "Owner write-up", idempotency_key="k", metadata={"kind": "probe"}
    )
    db.session.commit()
    second = service.post_money_adjustment(
        contract, Decimal("12000.00"), admin_user.id, "Owner write-up", idempotency_key="k", metadata={"kind": "probe"}
    )
    db.session.commit()

    assert second.id == first.id
    assert [row.id for row in _rows(db, "k")] == [first.id]
    assert _money(_account(db, contract.id)) == (Decimal("12000.00"), Decimal("0.00"))
    assert first.entry_metadata == {"kind": "probe", "outstanding_after": 12000.0}


def test_manual_adjustment_route_is_unchanged(client, db, admin_claim_headers, sample_user):
    """The contract screen's manual adjustment (no key, no metadata) posts as it always did."""
    contract, _ = _make_grocery_amount_contract(db, sample_user)

    response = client.post(
        ADJUSTMENTS.format(contract.id), json={"amount": -7000, "reason": "Goodwill credit"}, headers=admin_claim_headers
    )

    assert response.status_code == 201, response.get_json()
    entry = response.get_json()["data"]["ledger_entry"]
    assert entry["idempotency_key"].startswith(f"adjustment:contract:{contract.id}:")
    assert entry["entry_metadata"] == {"outstanding_after": -7000.0}
    assert _money(_account(db, contract.id)) == (Decimal("-7000.00"), Decimal("0.00"))
    assert CashCollectionEvent.query.count() == 0
