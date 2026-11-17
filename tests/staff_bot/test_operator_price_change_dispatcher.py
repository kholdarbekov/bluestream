"""The operator's price-change screens, driven through the real staff dispatcher.

During an announced price change the operator reads figures down the phone, so the
screens must state what the server quoted: the dated coming price while the notice
runs, the kept-price split of a courtesy line, and the customer's courtesy status.
Every quote below is a literal for its basket (never computed here), and the bot's
own output is compared with it.
"""

from __future__ import annotations

import pytest

from staff_bot.handlers.operator.create_order import CONFIRM_ORDER
from tests.staff_bot.ptb_harness import staff_backend_failure
from tests.staff_bot.test_staff_operator_journey_dispatcher import (  # noqa: F401  (operator is a fixture)
    BIG_BOTTLE_ID,
    CLIENT_ID,
    CREATE_ORDER_ENDPOINT,
    backend_calls,
    ESTIMATE_ENDPOINT,
    EXISTING_ADDRESS_ID,
    OPERATOR_ROOT,
    _curated,
    last_screen,
    operator,
    sign_in,
    menu_label,
    texts,
    CLIENT_PHONE,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

UZS = "UZS"
UPCOMING = {"new_price": 20000.0, "effective_date": "2027-02-01"}

# Four bottles: two at the kept 18,000, two at the regular 20,000, net 76,000.
COURTESY_LINE = {
    "product_id": BIG_BOTTLE_ID,
    "product_name": "Suv 19L",
    "quantity": 4,
    "unit_price": 20000.0,
    "discount_amount": 0.0,
    "courtesy_quantity": 2,
    "courtesy_unit_price": 18000.0,
    "regular_quantity": 2,
    "total_price": 76000.0,
    "is_contract_price": False,
    "upcoming_price": None,
}
STATUS_AVAILABLE = {
    "state": "available",
    "expires_on": "2027-02-14",
    "caps": [
        {"product_id": BIG_BOTTLE_ID, "product_name": "Suv 19L", "cap": 4},
        {"product_id": 12, "product_name": "Suv 5L", "cap": 6},
    ],
    "used_order_id": None,
    "used_order_number": None,
}
STATUS_USED = {
    "state": "used",
    "expires_on": "2027-02-14",
    "caps": [],
    "used_order_id": 91,
    "used_order_number": "BS-270203-0007",
}
STATUS_NONE = {"state": "none", "expires_on": None, "caps": [], "used_order_id": None, "used_order_number": None}


def _quote(*, courtesy_status, upcoming=None, courtesy=True):
    """The server's answer, literal per basket: quantity 1 for the catalogue, 4 for the cart."""

    def answer(call):
        items = (call.data or {}).get("items") or []
        quantity = items[0]["quantity"]
        if quantity == 4 and courtesy:
            line = dict(COURTESY_LINE)
        else:
            line = {
                **COURTESY_LINE,
                "quantity": quantity,
                "courtesy_quantity": None,
                "courtesy_unit_price": None,
                "regular_quantity": None,
                "total_price": 20000.0 * quantity,
                "upcoming_price": upcoming,
            }
        if quantity == 4 and not courtesy:
            line["upcoming_price"] = upcoming
        return {
            "items": [line],
            "subtotal": line["total_price"],
            "total_amount": line["total_price"],
            "price_courtesy": {"applied": True, "expires_on": "2027-02-14"} if line["courtesy_quantity"] else None,
            "courtesy_status": courtesy_status,
        }

    return answer


async def _to_the_cart(operator, quote, *, quantity=4):
    operator.backend.route("POST", ESTIMATE_ENDPOINT, quote)
    ops, labels = await sign_in(operator)
    await operator.send(ops.text(menu_label(labels, "staff.menu.create_order")))
    await operator.send(ops.text(CLIENT_PHONE))
    await operator.send(ops.tap(f"staff_op_order_{CLIENT_ID}"))
    await operator.send(ops.tap(f"staff_op_addr_{EXISTING_ADDRESS_ID}"))
    catalogue_screen = last_screen(operator)
    await operator.send(ops.tap(f"staff_op_product_{BIG_BOTTLE_ID}"))
    product_screen = last_screen(operator)
    await operator.send(ops.tap(f"staff_op_qty_{BIG_BOTTLE_ID}_{quantity}"))
    return ops, catalogue_screen, product_screen, last_screen(operator)


def _copy(key, **values):
    return _curated(key, "en").format(**values)


async def test_the_operator_cart_shows_the_kept_price_split_and_the_customers_status(operator):
    _ops, _c, _p, cart = await _to_the_cart(operator, _quote(courtesy_status=STATUS_AVAILABLE))

    expected = [
        f"  • Suv 19L x4 — 76,000 {UZS}",
        "    " + _copy(
            "staff.price_change.courtesy_split",
            courtesy_qty=2, courtesy_price=f"18,000 {UZS}", regular_qty=2, regular_price=f"20,000 {UZS}",
        ),
        "",
        f"💰 {_curated('staff.operator.subtotal', 'en')}: 76,000 {UZS}",
        "🏷 " + _copy("staff.price_change.status_available", date="14.02.2027"),
        "   • " + _copy("staff.price_change.status_cap_line", product="Suv 19L", cap=4),
        "   • " + _copy("staff.price_change.status_cap_line", product="Suv 5L", cap=6),
    ]
    lines = cart.text.split("\n")
    start = lines.index(expected[0])
    assert lines[start:start + len(expected)] == expected, cart.text


async def test_the_confirm_summary_reads_the_same_lines(operator):
    ops, _c, _p, cart = await _to_the_cart(operator, _quote(courtesy_status=STATUS_AVAILABLE))
    await operator.send(ops.tap("staff_op_products_done"))
    await operator.send(ops.tap("staff_op_pay_cash"))
    await operator.send(ops.text("-"))  # any note; the summary follows
    summary = last_screen(operator)

    split = _copy(
        "staff.price_change.courtesy_split",
        courtesy_qty=2, courtesy_price=f"18,000 {UZS}", regular_qty=2, regular_price=f"20,000 {UZS}",
    )
    assert f"  • Suv 19L x4 — 76,000 {UZS}\n    {split}\n" in summary.text, summary.text
    assert "🏷 " + _copy("staff.price_change.status_available", date="14.02.2027") in summary.text


async def test_a_courtesy_already_used_names_the_order_it_went_to(operator):
    _ops, _c, _p, cart = await _to_the_cart(operator, _quote(courtesy_status=STATUS_USED))

    assert "🏷 " + _copy("staff.price_change.status_used", order_number="BS-270203-0007") in cart.text
    assert _curated("staff.price_change.status_available", "en").split("{")[0] not in cart.text


async def test_a_customer_outside_the_courtesy_shows_no_status_line(operator):
    _ops, _c, _p, cart = await _to_the_cart(
        operator, _quote(courtesy_status=STATUS_NONE, courtesy=False), quantity=4,
    )

    assert "🏷" not in cart.text
    assert "(old price kept)" not in cart.text


async def test_during_the_notice_the_product_and_cart_state_the_coming_price(operator):
    _ops, _c, product, cart = await _to_the_cart(
        operator, _quote(courtesy_status=STATUS_NONE, upcoming=UPCOMING, courtesy=False),
    )
    coming = "📅 " + _copy("staff.price_change.upcoming_line", date="01.02.2027", price=f"20,000 {UZS}")

    assert f"💰 20,000 {UZS}\n{coming}" in product.text, product.text
    assert f"  • Suv 19L x4 — 80,000 {UZS}\n    {coming}\n" in cart.text, cart.text


# What a parallel order leaves behind: the courtesy is gone, four bottles cost 80,000.
NEW_QUOTE = {
    "items": [{**COURTESY_LINE, "courtesy_quantity": None, "courtesy_unit_price": None,
               "regular_quantity": None, "total_price": 80000.0}],
    "subtotal": 80000.0,
    "total_amount": 80000.0,
    "price_courtesy": None,
    "courtesy_status": STATUS_USED,
}


async def _to_the_summary(operator):
    ops, _c, _p, _cart = await _to_the_cart(operator, _quote(courtesy_status=STATUS_AVAILABLE))
    await operator.send(ops.tap("staff_op_products_done"))
    await operator.send(ops.tap("staff_op_pay_cash"))
    await operator.send(ops.text("Eshik oldiga qoldiring"))
    return ops


async def test_the_operator_confirm_sends_the_total_the_summary_showed(operator):
    ops = await _to_the_summary(operator)

    await operator.send(ops.tap("staff_op_confirm_order"))

    writes = backend_calls(operator, "POST", CREATE_ORDER_ENDPOINT)
    assert len(writes) == 1
    assert writes[0].data["expected_total"] == 76000.0


async def test_a_courtesy_taken_by_a_parallel_order_redraws_the_summary_with_the_new_total(operator):
    ops = await _to_the_summary(operator)
    answers = [
        staff_backend_failure(
            "api.common.state_changed", status_code=409, error_code="STATE_CHANGED",
            details={"reason": "price_changed", "quote": NEW_QUOTE}, error_type="CONFLICT",
        ),
        {"order_number": "BS-270203-0008"},
    ]
    operator.backend.route("POST", CREATE_ORDER_ENDPOINT, lambda call: answers.pop(0))

    await operator.send(ops.tap("staff_op_confirm_order"))

    redrawn = last_screen(operator)
    assert operator.conversation_state("staff_create_order") == CONFIRM_ORDER
    assert redrawn.text.split("\n")[0] == _curated("staff.price_change.price_changed_note", "en")
    assert f"  • Suv 19L x4 — 80,000 {UZS}" in redrawn.text
    assert f"💰 {_curated('staff.operator.subtotal', 'en')}: 80,000 {UZS}" in redrawn.text
    assert "🏷 " + _copy("staff.price_change.status_used", order_number="BS-270203-0007") in redrawn.text
    assert _curated("staff.error.api.conflict", "en") not in "\n".join(texts(operator))

    await operator.send(ops.tap("staff_op_confirm_order"))

    writes = backend_calls(operator, "POST", CREATE_ORDER_ENDPOINT)
    assert [w.data["expected_total"] for w in writes] == [76000.0, 80000.0]
    assert writes[1].data["items"] == writes[0].data["items"]
