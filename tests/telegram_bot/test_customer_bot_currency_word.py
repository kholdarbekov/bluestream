"""Every customer-bot amount says "so'm" / "сум", never a typed "UZS" (Task 47a, owner decision 4).

After the price-change lines landed, a cart screen would print the new lines in "so'm" beside existing
lines in "UZS". The sweep changes ONLY the currency word: no sentence is reworded, no placeholder is added
or removed, no number is computed. Out of scope: the staff bot, the admin UI, the website, ISO codes in data
(`telegram_bot/config.py` `supported_currencies`, the backend's payment payload field).
"""

from __future__ import annotations

import ast
import hashlib
import hmac
import importlib
import importlib.util
import json
from pathlib import Path
from string import Formatter

import pytest
from aiohttp import test_utils

import bot as bot_module  # noqa: F401  (claims the bot's bare module names first)
from keyboards import ProductKeyboards
from utils import format_money

from scripts.seed_backend_translations import BACKEND_TRANSLATIONS
from scripts.seed_prepayment_translations import PREPAYMENT_TRANSLATIONS

from tests.telegram_bot.ptb_harness import DEFAULT_USER_ID, build_bot_harness
from tests.telegram_bot.test_checkout_journey_dispatcher import (
    BOTTLE_19L,
    Shop,
    add_address,
    fill_cart,
    reach_confirmation,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

LANGUAGES = ("en", "uz", "ru")
WORD = {"en": "so'm", "uz": "so'm", "ru": "сум"}

_REPO = Path(__file__).resolve().parents[2]

# key -> (the languages that carried "UZS" on cf842da, the placeholders every one of them carries on HEAD).
# Written by hand from HEAD: the sweep must not add, remove or rename a placeholder.
_ALL = ("en", "uz", "ru")
_EN = ("en",)
SWEPT = {
    "telegram.payment.pay_message": (_ALL, {"amount", "order_number"}),
    "telegram.order.total": (_ALL, {"0"}),
    "telegram.orders.delivery_fee": (_ALL, {"amount"}),
    "telegram.orders.grand_total": (_ALL, {"amount"}),
    "telegram.orders.estimate_discount_line": (_ALL, {"amount"}),
    "telegram.orders.estimate_reward_line": (_ALL, {"amount"}),
    "telegram.orders.estimate_tier_line": (_ALL, {"percentage", "amount", "tier_name"}),
    "telegram.orders.order_tier_discount_line": (_ALL, {"amount"}),
    "telegram.orders.detail_paid_line": (_ALL, {"amount"}),
    "telegram.orders.detail_to_pay_line": (_ALL, {"amount"}),
    "telegram.orders.estimate_prepaid_line": (_ALL, {"amount"}),
    "telegram.orders.estimate_prepaid_balance_after": (_ALL, {"amount"}),
    "telegram.orders.estimate_payable": (_ALL, {"icon", "amount"}),
    "telegram.orders.estimate_neutral_total": (_ALL, {"amount"}),
    "telegram.orders.estimate_cod_savings": (_ALL, {"amount"}),
    "telegram.cart_min_order_warning": (_ALL, {"min_amount", "remaining"}),
    "telegram.products.unit_price_label": (_ALL, {"price"}),
    "telegram.cart.cod_prepaid_balance": (_EN, {"available_balance"}),
    "telegram.orders.cod_prepaid_balance": (_EN, {"available_balance"}),
    "telegram.cart.cod_prepaid_auto_applied_next": (_EN, {"potential_applied"}),
    "telegram.cart.cod_estimated_payable": (_EN, {"payable_after"}),
    "telegram.loyalty.lock_add_order": (_EN, {"amount"}),
    "telegram.agent_order.proposed": (
        _EN, {"items", "order_number", "outlet_name", "address", "agent_name", "delivery_date", "total"}),
    "telegram.customer_notice.order_created": (_EN, {"items", "payment", "delivery", "order_number", "total"}),
    "telegram.customer_notice.pay_cash": (_EN, {"amount"}),
    "telegram.customer_notice.pay_cash_prepaid": (_EN, {"amount"}),
    "telegram.customer_notice.prepayment_returned": (_EN, {"balance", "amount"}),
    "telegram.orders.cod_restricted_person": (_EN, {"threshold", "net_debt_total"}),
    "telegram.orders.cod_prepayment_applied": (_EN, {"potential_applied", "payable_after"}),
    "telegram.orders.cod_prepaid_auto_applied": (_EN, {"potential_applied"}),
    "telegram.orders.cod_estimated_payable": (_EN, {"payable_after"}),
}
SWEPT_ROWS = [(key, language) for key, (languages, _) in SWEPT.items() for language in languages]

SEEDED = {**BACKEND_TRANSLATIONS, **PREPAYMENT_TRANSLATIONS}
TRANSLATIONS = {
    (language, key): row[language]
    for key, row in SEEDED.items()
    if key.startswith("telegram.")
    for language in LANGUAGES
    if row.get(language)
}


def _placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(text) if name}


def test_no_customer_bot_copy_types_uzs():
    offenders = [
        (key, language)
        for table in (BACKEND_TRANSLATIONS, PREPAYMENT_TRANSLATIONS)
        for key, row in table.items()
        if key.startswith("telegram.")
        for language in LANGUAGES
        if "UZS" in (row.get(language) or "")
    ]
    assert offenders == [], f"customer-bot copy still types UZS: {offenders}"


@pytest.mark.parametrize("key, language", SWEPT_ROWS)
def test_each_swept_row_says_the_currency_word(key, language):
    text = SEEDED[key][language]

    assert WORD[language] in text
    assert _placeholders(text) == SWEPT[key][1]


def _docstring_nodes(tree: ast.AST) -> set[int]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                found.add(id(body[0].value))
    return found


def test_no_customer_bot_code_prints_uzs():
    offenders = []
    for path in sorted((_REPO / "telegram_bot").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docstrings = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and id(node) not in docstrings and " UZS" in node.value:
                offenders.append(f"{path.relative_to(_REPO)}:{node.lineno}")

    assert offenders == [], f"customer-bot code prints a typed UZS after an amount: {offenders}"


@pytest.mark.parametrize("language", LANGUAGES)
async def test_the_product_buttons_print_a_formatted_price_and_the_word(monkeypatch, language):
    await build_bot_harness(monkeypatch, translations=TRANSLATIONS)
    products = [{"id": 1, "name": "Suv 18.9 L", "pricing": {"current_price": 20000.0}}]
    expected = f"Suv 18.9 L - {format_money(20000.0, language)}"

    listing = ProductKeyboards.product_list(products, language=language)
    subscription = ProductKeyboards.product_list_for_subscription(products, language=language)

    assert listing.inline_keyboard[0][0].text == expected
    assert subscription.inline_keyboard[0][0].text == expected
    assert expected == f"Suv 18.9 L - 20,000 {WORD[language]}"


@pytest.fixture
async def bot(monkeypatch):
    return await build_bot_harness(monkeypatch, translations=TRANSLATIONS)


@pytest.fixture
def shop(bot, monkeypatch):
    return Shop(bot.backend, monkeypatch)


@pytest.fixture
def user(bot):
    return bot.updates()


@pytest.mark.parametrize("language", LANGUAGES)
async def test_the_cart_and_confirm_screens_say_the_word_in_each_language(bot, shop, user, language):
    bot.database.user["preferred_language"] = language
    add_address(bot, 900, "Uy", "Chilonzor 15")
    await fill_cart(bot, user, BOTTLE_19L, quantity=3)

    await bot.send(user.tap("cart_view"))
    cart = bot.telegram.last_shown().text

    await reach_confirmation(bot, user, 900, payment="cash")
    confirm = bot.telegram.last_shown().text

    for name, screen in (("cart", cart), ("confirm", confirm)):
        assert WORD[language] in screen, f"the {name} screen never says {WORD[language]!r}: {screen!r}"
        assert "UZS" not in screen, f"the {name} screen types UZS: {screen!r}"
        assert "Uzs" not in screen, f"the {name} screen shows the unseeded fallback: {screen!r}"


# -- the payment-success push (fixtures of test_webhook_payment_success.py) --------------------------------------

importlib.import_module("bot")
_WS_PATH = _REPO / "telegram_bot" / "webhook_server.py"
_spec = importlib.util.spec_from_file_location("telegram_webhook_server_currency_word_under_test", _WS_PATH)
ws_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ws_module)

SECRET = "test-webhook-secret"
PAYLOAD = {
    "user_id": 109,
    "telegram_id": DEFAULT_USER_ID,
    "order_id": 205,
    "order_number": "WB_000019_26",
    "amount": 30000.0,
    "currency": "UZS",
    "timestamp": "2026-11-02T12:23:40+00:00",
}


class _FakeRedis:
    def __init__(self):
        self.store = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, *keys):
        return sum(1 for key in keys if self.store.pop(key, None) is not None)


async def test_the_payment_message_prints_the_word_not_the_iso_code(bot, monkeypatch):
    bot.database.user["preferred_language"] = "ru"
    bot.application.bot_data["token_manager"].redis = _FakeRedis()
    monkeypatch.setattr(ws_module.config.security, "webhook_secret", SECRET)
    server = ws_module.WebhookServer()
    server.set_application(bot.application)
    await server.setup()
    raw = json.dumps(PAYLOAD).encode("utf-8")
    signature = hmac.new(SECRET.encode("utf-8"), raw, hashlib.sha256).hexdigest()

    async with test_utils.TestClient(test_utils.TestServer(server.app)) as client:
        response = await client.post(
            "/internal/payment-success",
            data=raw,
            headers={
                "Content-Type": "application/json",
                "X-Bot-Webhook-Signature": signature,
                "X-Request-ID": "payment-confirm:185",
            },
        )
        assert response.status == 200

    (sent,) = bot.telegram.of("sendMessage")
    assert sent.params["text"] == (
        "✅ Оплата прошла успешно!\n\nНомер заказа: #WB_000019_26\nСумма: 30,000 сум\n\nСпасибо за покупку!"
    )
    assert "UZS" not in sent.params["text"]
