"""The operator's staff bot against the REAL backend: a courtesy phone order, and a summary drawn before the
switch (Task 52).

The dispatcher tests before this one prove what the operator's screens draw from a recorded backend; this file
proves the CONTRACT. The real staff bot runs through the real dispatcher, signs in through the real
`/api/v1/staff/auth/login`, and its own `_make_request` (signing included) is answered by the real Flask app, so
the cart and the summary read the real order-estimate, the order is placed through the real
`POST /api/v1/staff/operator/orders`, and the price moves through the real switch (`apply_due_price_changes`).
Only Telegram, the bot's own SQL and its token Redis are faked.

The staff bot keeps "UZS" (`staff.currency.uzs`) deliberately: the so'm rule binds the customer surfaces only.
"""

from decimal import Decimal

import pytest

from business_app.models.order import Order
from business_app.services.price_change.switch import apply_due_price_changes
from staff_bot import api_client as staff_api_module
from staff_bot.handlers.operator.create_order import CONFIRM_ORDER
from staff_bot.utils.formatters import format_local_date

from tests.bot_dispatcher_harness import FlaskBackend
from tests.integration.price_courtesy_builders import announced_change, delivered_before_notice, entry_of, switch
from tests.integration.test_operator_order_price_parity import client_address, operator  # noqa: F401  (fixtures)
from tests.staff_bot.ptb_harness import DEFAULT_DRIVER_TELEGRAM_ID
from tests.staff_bot.test_staff_bot_signs_in_again_after_revocation import _token_manager
from tests.staff_bot.test_staff_operator_journey_dispatcher import (
    _curated,
    build_staff,
    capture_errors,
    menu_label,
    sign_in,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

NEW_PRICE = Decimal("17000.00")
CREATE_ORDER = "/api/v1/staff/operator/orders"
STATE = "staff_create_order"


def _copy(key: str, **values) -> str:
    return _curated(key, "en").format(**values)


async def _operator_bot_on_the_real_backend(app, db, monkeypatch, operator_user):
    """The operator's staff bot, its own request funnel answered by the real Flask app.

    The login finds the staff member by `telegram_id`, so the operator carries the bot's Telegram id first."""
    operator_user.telegram_id = str(DEFAULT_DRIVER_TELEGRAM_ID)
    operator_user.preferred_language = "en"
    db.session.commit()
    bot = await build_staff(monkeypatch, roles=["operator"], language="en")
    tokens = _token_manager()
    bot.application.bot_data["token_manager"] = tokens
    client = staff_api_module.api_client
    monkeypatch.setattr(client, "token_manager", tokens)
    monkeypatch.delattr(client, "_make_request")  # the bot's own funnel, not the harness double
    backend = FlaskBackend(app)
    monkeypatch.setattr(client, "_client", backend)
    return bot, backend


async def _tap(bot, ops, callback_data):
    """Tap a button the bot itself drew on its last screen."""
    shown = bot.telegram.last_shown()
    assert callback_data in shown.callback_data(), (callback_data, shown.text)
    await bot.send(ops.tap(callback_data))


async def _to_the_cart(bot, customer, address, product, quantity):
    """Sign in -> Create order -> the caller's phone -> their card -> the address -> the product -> the quantity."""
    ops, labels = await sign_in(bot)
    await bot.send(ops.text(menu_label(labels, "staff.menu.create_order")))
    await bot.send(ops.text(customer.phone))
    await _tap(bot, ops, f"staff_op_order_{customer.id}")
    await _tap(bot, ops, f"staff_op_addr_{address.id}")
    await _tap(bot, ops, f"staff_op_product_{product.id}")
    await _tap(bot, ops, f"staff_op_qty_{product.id}_{quantity}")
    return ops, bot.telegram.last_shown()


async def _to_the_summary(bot, ops):
    """Done -> cash -> no note: the confirm summary is on screen."""
    for callback_data in ("staff_op_products_done", "staff_op_pay_cash", "staff_op_skip_notes"):
        await _tap(bot, ops, callback_data)
    return bot.telegram.last_shown()


async def test_an_operator_places_a_courtesy_phone_order_on_the_real_routes(
    app, db, sample_user, client_address, sample_product, admin_user, operator, monkeypatch  # noqa: F811
):
    delivered = delivered_before_notice(db, sample_user, client_address)  # 40 days ago: before the announcement
    change = announced_change(db, sample_product, admin_user, new_price=NEW_PRICE)
    switch(db)
    bot, backend = await _operator_bot_on_the_real_backend(app, db, monkeypatch, operator)
    errors = capture_errors(bot)

    ops, cart = await _to_the_cart(bot, sample_user, client_address, sample_product, 4)

    expected = [
        "  • Pure Water 19L x4 — 64,000 UZS",
        "    " + _copy(
            "staff.price_change.courtesy_split",
            courtesy_qty=2, courtesy_price="15,000 UZS", regular_qty=2, regular_price="17,000 UZS",
        ),
        "",
        f"💰 {_curated('staff.operator.subtotal', 'en')}: 64,000 UZS",
        "🏷 " + _copy("staff.price_change.status_available", date=format_local_date(change.courtesy_last_day)),
        "   • Pure Water 19L: up to 2",
    ]
    lines = cart.text.split("\n")
    start = lines.index(expected[0])
    assert lines[start:start + len(expected)] == expected, cart.text

    await _to_the_summary(bot, ops)
    await _tap(bot, ops, "staff_op_confirm_order")

    assert ("POST", CREATE_ORDER, 201) in [step[:3] for step in backend.seen]
    db.session.expire_all()
    [order] = Order.query.filter(Order.user_id == sample_user.id, Order.id != delivered.id).all()
    [item] = order.order_items
    assert (item.courtesy_quantity, item.total_price) == (2, Decimal("64000.00"))
    assert entry_of(db, sample_user).order_id == order.id
    assert not errors, errors


async def test_an_operator_summary_drawn_before_the_switch_is_refused_redrawn_and_then_charged_the_new_price(
    app, db, sample_user, client_address, sample_product, admin_user, operator, monkeypatch  # noqa: F811
):
    # No order before the announcement: not on the courtesy list. The change is due today, not applied.
    change = announced_change(db, sample_product, admin_user, new_price=NEW_PRICE)
    bot, backend = await _operator_bot_on_the_real_backend(app, db, monkeypatch, operator)
    errors = capture_errors(bot)

    ops, _cart = await _to_the_cart(bot, sample_user, client_address, sample_product, 3)
    summary = (await _to_the_summary(bot, ops)).text

    lines = summary.split("\n")
    at = lines.index("  • Pure Water 19L x3 — 45,000 UZS")
    dated = "📅 " + _copy(
        "staff.price_change.upcoming_line", date=format_local_date(change.change_on), price="17,000 UZS",
    )
    assert lines[at + 1] == "    " + dated, summary
    assert f"💰 {_curated('staff.operator.subtotal', 'en')}: 45,000 UZS" in lines

    apply_due_price_changes()
    await _tap(bot, ops, "staff_op_confirm_order")

    redrawn = bot.telegram.last_shown().text
    assert bot.conversation_state(STATE) == CONFIRM_ORDER
    assert redrawn.split("\n")[0] == _curated("staff.price_change.price_changed_note", "en")
    assert "  • Pure Water 19L x3 — 51,000 UZS" in redrawn.split("\n"), redrawn
    assert f"💰 {_curated('staff.operator.subtotal', 'en')}: 51,000 UZS" in redrawn.split("\n")
    db.session.expire_all()
    assert Order.query.filter(Order.user_id == sample_user.id).count() == 0, "a refused confirm places nothing"

    await _tap(bot, ops, "staff_op_confirm_order")

    assert [step[2] for step in backend.seen if step[:2] == ("POST", CREATE_ORDER)] == [409, 201]
    db.session.expire_all()
    [order] = Order.query.filter(Order.user_id == sample_user.id).all()
    assert order.total_amount == Decimal("51000.00")
    assert not errors, errors
