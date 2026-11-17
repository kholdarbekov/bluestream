"""The customer bot against the REAL backend: a courtesy order, and a confirm drawn before the switch (Task 51).

The screen tests before this one prove what the bot draws from a recorded backend; this file proves the CONTRACT.
The real customer bot runs through the real dispatcher, its own `_make_request` (signing included) is answered by
the real Flask app, and the price moves through the real switch (`apply_due_price_changes`). Only Telegram, the
bot's own SQL and its token Redis are faked.

Every amount on a customer screen goes through the bot's own `format_money` (so'm / сум, Task 47a); no customer
screen prints "UZS".
"""

import html
import re
from decimal import Decimal

import pytest

import bot as bot_module  # noqa: F401  (resolves the bot's own modules first)

# Bare bot-module names: already in sys.modules after `import bot`.
import api_client as api_client_module
import utils as utils_module
from token_manager import TokenManager
from utils import format_date, format_money

# AFTER the bot import, for the sys.path reason test_agent_order_callbacks.py documents.
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS  # noqa: E402

from business_app.models.order import Order
from business_app.services.price_change.switch import apply_due_price_changes
from business_app.utils.timezone_utils import get_utc_now
from tests.bot_dispatcher_harness import FlaskBackend
from tests.integration.price_courtesy_builders import announced_change, delivered_before_notice, entry_of, switch
from tests.telegram_bot.ptb_harness import DEFAULT_USER_ID, FakeDatabase, build_bot_harness
from tests.telegram_bot.test_token_manager import _FakeRedis

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

# Every seeded `telegram.*` row, in every language: the screens read the real copy.
TRANSLATIONS = {
    (language, key): row[language]
    for key, row in BACKEND_TRANSLATIONS.items()
    if key.startswith("telegram.")
    for language in ("uz", "ru", "en")
    if row.get(language)
}

NEW_PRICE = Decimal("17000.00")
ORDERS = "/api/v1/orders"


def en(key: str, **fmt) -> str:
    return BACKEND_TRANSLATIONS[key]["en"].format(**fmt)


async def _always_allowed(*_args, **_kwargs):
    return True


def _connected(db, user):
    """`user` as a customer connected to the bot, with the verified phone `POST /api/v1/orders/` requires."""
    user.telegram_id = str(DEFAULT_USER_ID)
    user.phone_verified_at = get_utc_now()
    db.session.commit()
    return user


async def _customer_bot_on_the_real_backend(app, monkeypatch, user):
    """The customer bot with the production TokenManager, its own request funnel answered by the real Flask app."""
    database = FakeDatabase(user={
        "id": user.id,
        "telegram_id": str(DEFAULT_USER_ID),
        "first_name": user.first_name,
        "phone": user.phone,
        "preferred_language": "en",
        "role": "customer",
        "status": "active",
        "bot_state": "{}",
        "user_type": "individual",
    })
    bot = await build_bot_harness(monkeypatch, translations=TRANSLATIONS, database=database, strict_entities=True)
    monkeypatch.setattr(utils_module.rate_limiter, "allow_request", _always_allowed)
    tokens = TokenManager("redis://unused")
    tokens.redis, tokens._connected = _FakeRedis(), True
    bot.application.bot_data["token_manager"] = tokens
    client = api_client_module.api_client
    monkeypatch.setattr(client, "token_manager", tokens)
    monkeypatch.delattr(client, "_make_request")  # the bot's own funnel, not the harness double
    backend = FlaskBackend(app)
    monkeypatch.setattr(client, "_client", backend)
    return bot, backend, tokens


async def _reach_confirmation(bot, tapper, product_id, quantity, address_id):
    """Product -> quantity -> Checkout -> cart -> checkout -> address -> cash: the confirm screen is on screen."""
    for callback_data in (
        f"product_{product_id}",
        f"qty_set_{product_id}_{quantity}",
        f"buy_{product_id}_{quantity}",
        "cart_view",
        "cart_checkout",
        f"address_{address_id}",
        "payment_cash",
    ):
        await bot.send(tapper.tap(callback_data))
    return bot.telegram.last_shown()


async def test_an_eligible_customer_orders_four_and_is_charged_two_at_the_kept_price(
    app, db, sample_user, user_address, sample_product, admin_user, monkeypatch
):
    _connected(db, sample_user)
    delivered = delivered_before_notice(db, sample_user, user_address)  # 40 days ago: before the announcement
    announced_change(db, sample_product, admin_user, new_price=NEW_PRICE)
    switch(db)
    bot, backend, _tokens = await _customer_bot_on_the_real_backend(app, monkeypatch, sample_user)
    tapper = bot.updates()

    screen = await _reach_confirmation(bot, tapper, sample_product.id, 4, user_address.id)

    assert screen.refused is None
    split = en(
        "telegram.price_change.courtesy_split",
        courtesy_qty=2, courtesy_price=format_money(15000.0, "en"),
        regular_qty=2, regular_price=format_money(17000.0, "en"),
    )
    assert "\n   " + html.escape(split, quote=False) + "\n" in screen.text
    thanks = html.escape(en("telegram.price_change.courtesy_thanks"), quote=False)
    assert screen.text.count(thanks) == 1 and screen.text.endswith(thanks)
    assert "UZS" not in screen.text

    await bot.send(tapper.tap("confirm_order"))

    assert ("POST", ORDERS, 201) in [step[:3] for step in backend.seen]
    db.session.expire_all()
    [order] = Order.query.filter(Order.user_id == sample_user.id, Order.id != delivered.id).all()
    [item] = order.order_items
    assert (item.quantity, item.unit_price, item.courtesy_quantity, item.courtesy_unit_price, item.total_price) == (
        4, Decimal("17000.00"), 2, Decimal("15000.00"), Decimal("64000.00"),
    )
    assert entry_of(db, sample_user).order_id == order.id


async def test_a_confirm_drawn_before_the_switch_is_refused_redrawn_and_then_charged_the_new_price(
    app, db, sample_user, user_address, sample_product, admin_user, monkeypatch
):
    _connected(db, sample_user)  # no order before the announcement: not on the courtesy list
    change = announced_change(db, sample_product, admin_user, new_price=NEW_PRICE)  # due today, not applied
    bot, backend, tokens = await _customer_bot_on_the_real_backend(app, monkeypatch, sample_user)
    tapper = bot.updates()

    drawn = (await _reach_confirmation(bot, tapper, sample_product.id, 3, user_address.id)).text

    lines = drawn.split("\n")
    at = lines.index("• Pure Water 19L × 3 — " + format_money(45000.0, "en"))
    dated = "📅 " + en(
        "telegram.price_change.upcoming_line",
        date=format_date(change.change_on.isoformat()), price=format_money(17000.0, "en"),
    )
    assert lines[at + 1] == "   " + html.escape(dated, quote=False)
    assert "UZS" not in drawn

    apply_due_price_changes()
    await bot.send(tapper.tap("confirm_order"))

    redrawn = bot.telegram.last_shown()
    assert redrawn.refused is None
    assert html.escape(en("telegram.price_change.price_changed_note"), quote=False) in redrawn.text.split("\n")
    assert format_money(51000.0, "en") in redrawn.text
    assert "UZS" not in redrawn.text
    db.session.expire_all()
    assert Order.query.filter(Order.user_id == sample_user.id).count() == 0, "a refused confirm places nothing"
    to_pay = Decimal(re.search(r"To pay: ([\d,]+) ", redrawn.text).group(1).replace(",", ""))

    tokens.redis.locks.clear()  # a deliberate second tap, seconds later: the tap's 2 s dedup lock has expired
    await bot.send(tapper.tap("confirm_order"))

    assert [step[2] for step in backend.seen if step[:2] == ("POST", ORDERS)] == [409, 201]
    db.session.expire_all()
    [order] = Order.query.filter(Order.user_id == sample_user.id).all()
    assert order.total_amount == to_pay
