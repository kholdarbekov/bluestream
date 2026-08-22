"""What "in stock" is allowed to mean on a customer-facing product screen.

WHY THIS FILE EXISTS
--------------------
Wave 1 made ``ProductHandlers._purchase_bounds`` the ONE expression of "how
many of this product may this customer hold" — including the part that keeps
biting: zero stock is a REAL ceiling of zero, not "unknown". Two surfaces that
answer the very same question in words rather than in numbers never moved:

* ``_format_products_list`` painted its badge from ``stock_quantity > 0``,
* ``_format_product_details`` picked ``in_stock`` / ``out_of_stock`` the same way.

With ``stock_quantity = 1`` and ``min_order_quantity = 2`` both said IN STOCK
while nothing at all was orderable: the customer taps the product, taps "Add to
cart", and is answered with a bare "out of stock" toast on a screen that told
them the opposite two taps ago.

So these tests drive the REAL dispatcher and assert on the badge/word the
customer actually sees, and then pin the structural claim: exactly one function
in the whole customer bot reads ``stock_quantity``.
"""

import ast
import pathlib

import pytest

from handlers.products import ProductHandlers
from tests.telegram_bot.ptb_harness import build_bot_harness

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


CATEGORY_ID = 1

# One bottle on the shelf, two per order minimum: the exact shape of the lie.
SCARCE_ID = 7
SCARCE_NAME = "Aqua Element 18.9 l"
SCARCE_FRAGMENT = "Element"

# Comfortably orderable — the positive control, so a fix that simply paints
# everything "out of stock" cannot pass this file.
PLENTIFUL_ID = 8
PLENTIFUL_NAME = "Aqua Sport 0.5 l"
PLENTIFUL_FRAGMENT = "Sport"


def build_catalogue(scarce_stock: int = 1, scarce_min_qty: int = 2) -> dict:
    return {
        SCARCE_ID: {
            "id": SCARCE_ID,
            "name": SCARCE_NAME,
            "current_price": 15000.0,
            "category": {"id": CATEGORY_ID, "name": "Suv"},
            "inventory": {
                "min_order_quantity": scarce_min_qty,
                "stock_quantity": scarce_stock,
            },
            "specifications": {"volume": 18.9, "volume_unit": "l"},
        },
        PLENTIFUL_ID: {
            "id": PLENTIFUL_ID,
            "name": PLENTIFUL_NAME,
            "current_price": 9000.0,
            "category": {"id": CATEGORY_ID, "name": "Suv"},
            "inventory": {"min_order_quantity": 1, "stock_quantity": 40},
            "specifications": {"volume": 0.5, "volume_unit": "l"},
        },
    }


# Real, distinct copy: an unseeded key renders as `humanised_missing_key`, so a
# test asserting on a string it forgot to seed reads like a pass.
TRANSLATIONS = {
    ("uz", "telegram.products.in_stock"): "Mavjud",
    ("uz", "telegram.products.out_of_stock"): "Tugagan",
    ("uz", "telegram.products.stock_label"): "Holat",
    ("uz", "telegram.products.min_order_quantity_label"): "Eng kam miqdor: {min_qty}",
    ("uz", "telegram.price"): "Narx",
    ("uz", "telegram.products.volume_label"): "Hajm",
    ("uz", "telegram.products.category_label"): "Turkum",
    ("uz", "telegram.back"): "Orqaga",
    ("uz", "telegram.product.add_to_cart"): "Savatga qo'shish",
}


def t(key: str, language: str = "uz", **fmt) -> str:
    return TRANSLATIONS[(language, key)].format(**fmt)


def install_catalogue_backend(backend, catalogue: dict) -> None:
    """Wire the real api_client endpoint paths onto ``catalogue``."""
    backend.route(
        "GET",
        "/api/v1/products",
        lambda call: {"data": {"items": list(catalogue.values())}, "meta": {"pages": 1}},
    )
    for pid in catalogue:
        backend.route(
            "GET",
            f"/api/v1/products/{pid}",
            lambda call, pid=pid: {"data": {"product": catalogue[pid]}},
        )


@pytest.fixture
def catalogue():
    return build_catalogue()


@pytest.fixture
async def bot(monkeypatch, catalogue):
    harness = await build_bot_harness(monkeypatch, translations=TRANSLATIONS)
    install_catalogue_backend(harness.backend, catalogue)
    return harness


@pytest.fixture
def user(bot):
    return bot.updates()


def shown_text(bot) -> str:
    """What the customer is looking at — caption or body, whichever was sent."""
    call = bot.telegram.last_shown()
    return call.params.get("text") or call.params.get("caption") or ""


def badge_line(text: str, name_fragment: str) -> str:
    """The one line of the product list that names ``name_fragment``.

    The list is MarkdownV2, so names arrive escaped; match on a fragment that
    escaping leaves alone. The fragment must also be UNIQUE across the
    catalogue — both products start "Aqua", and matching on that would have
    read the first product's badge for both and passed either way.
    """
    matches = [line for line in text.splitlines() if name_fragment in line]
    assert len(matches) == 1, (
        f"{name_fragment!r} matched {len(matches)} lines, expected exactly one:\n{text}"
    )
    return matches[0]


# ---------------------------------------------------------------------------
# D1 — the badge must mean what the button does
# ---------------------------------------------------------------------------


async def test_product_list_badge_says_out_of_stock_when_stock_is_below_the_minimum(
    bot, user
):
    """1 on the shelf, 2 per order: the list may not advertise it as available.

    ``_format_products_list`` used to paint ✅ from ``stock_quantity > 0``, which
    is a different question from the one the Add-to-cart button answers. The
    badge now comes from the same resolver the button does, so "✅" is a promise
    the next tap can keep.
    """
    await bot.send(user.tap(f"category_{CATEGORY_ID}"))

    text = shown_text(bot)
    assert badge_line(text, SCARCE_FRAGMENT).startswith("❌"), (
        "the list advertises a product no quantity of which can be ordered:\n" + text
    )
    # The positive control: a genuinely orderable product still reads available.
    assert badge_line(text, PLENTIFUL_FRAGMENT).startswith("✅"), (
        "the fix painted an orderable product as unavailable:\n" + text
    )


async def test_product_details_say_out_of_stock_when_stock_is_below_the_minimum(
    bot, user
):
    """Same lie one tap deeper: the details card printed "In stock".

    The customer reads the word, taps "Add to cart" and gets the out-of-stock
    toast that ``_purchase_bounds_or_refuse`` raises — two screens, two answers,
    one product.
    """
    await bot.send(user.tap(f"product_{SCARCE_ID}"))

    text = shown_text(bot)
    assert f"{t('telegram.products.stock_label')}: {t('telegram.products.out_of_stock')}" in text, (
        "the details card still calls an unorderable product available:\n" + text
    )
    assert t("telegram.products.in_stock") not in text


async def test_product_details_still_say_in_stock_when_the_minimum_is_reachable(
    bot, user
):
    """Positive control for the details card."""
    await bot.send(user.tap(f"product_{PLENTIFUL_ID}"))

    text = shown_text(bot)
    assert f"{t('telegram.products.stock_label')}: {t('telegram.products.in_stock')}" in text


async def test_the_badge_and_the_quantity_write_agree_on_the_same_product(bot, user):
    """The screen and the write path must never disagree.

    Read against ``quantity_handler`` (the ± / preset row) rather than the first
    "Add to cart": that entry point deliberately declines to apply the ceiling
    because the SERVER owns the sold-out decision on a first add — there is no
    local snapshot old enough to trust against a live shelf. Every later
    quantity tap does go through ``_purchase_bounds_or_refuse``, so an "✅" that
    is refused, or an "❌" that would have been accepted, is the defect either
    way round.
    """
    await bot.send(user.tap(f"category_{CATEGORY_ID}"))
    text = shown_text(bot)

    for product_id, fragment in (
        (SCARCE_ID, SCARCE_FRAGMENT),
        (PLENTIFUL_ID, PLENTIFUL_FRAGMENT),
    ):
        bot.telegram.reset()
        await bot.send(user.tap(f"qty_inc_{product_id}_1"))
        refusals = [
            call
            for call in bot.telegram.of("answerCallbackQuery")
            if call.params.get("text") == t("telegram.products.out_of_stock")
        ]
        badge_promises_stock = badge_line(text, fragment).startswith("✅")
        assert badge_promises_stock is (not refusals), (
            f"{fragment}: the list badge and the quantity write disagree"
        )


@pytest.mark.parametrize(
    "stock, min_qty, orderable",
    [
        (0, 1, False),   # sold out
        (1, 1, True),    # the last one
        (1, 2, False),   # D1: stock below the product's own floor
        (2, 2, True),    # exactly enough
        (None, 2, True), # unknown stock is unknown, not zero
    ],
)
def test_the_resolver_decides_orderability(stock, min_qty, orderable):
    """The predicate itself, at its edges — including "unknown is not zero"."""
    product = {"inventory": {"min_order_quantity": min_qty, "stock_quantity": stock}}
    assert ProductHandlers._is_orderable(product) is orderable


# ---------------------------------------------------------------------------
# Exactly ONE place decides
# ---------------------------------------------------------------------------


BOT_ROOT = pathlib.Path(__file__).resolve().parents[2] / "telegram_bot"


def _stock_quantity_readers() -> set[tuple[str, str]]:
    """``(module, function)`` for every function in the bot that reads
    ``stock_quantity``.

    Matched on the exact string constant, so prose mentioning the field in a
    docstring (``keyboards.py`` explains why it no longer derives a ceiling
    from it) is not counted — only code that actually goes and looks.
    """
    found: set[tuple[str, str]] = set()
    for path in sorted(BOT_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        scope: list[str] = []

        def walk(node, scope=scope, rel=str(path.relative_to(BOT_ROOT))):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    scope.append(child.name)
                    walk(child)
                    scope.pop()
                    continue
                if isinstance(child, ast.Constant) and child.value == "stock_quantity":
                    found.add((rel, scope[-1] if scope else "<module>"))
                if isinstance(child, ast.Attribute) and child.attr == "stock_quantity":
                    found.add((rel, scope[-1] if scope else "<module>"))
                walk(child)

        walk(tree)
    return found


@pytest.mark.unit
def test_only_the_resolver_reads_stock_quantity():
    """One expression of "can this be ordered", or the screens drift again.

    Three surfaces used to spell the stock ceiling out for themselves and the
    third one — the badge — kept saying yes after the other two learned to say
    no. This fails the moment a fourth appears.
    """
    assert _stock_quantity_readers() == {("handlers/products.py", "_purchase_bounds")}


@pytest.mark.unit
def test_the_orderability_word_comes_from_the_resolver_not_from_the_field():
    """Both rendering surfaces must go through the predicate."""
    import inspect

    for renderer in (
        ProductHandlers._format_products_list,
        ProductHandlers._format_product_details,
    ):
        source = inspect.getsource(renderer)
        assert "_is_orderable" in source, (
            f"{renderer.__name__} decides availability without the resolver"
        )


@pytest.mark.unit
def test_no_hand_written_english_stock_copy_survives_in_utils():
    """``MessageBuilder.build_product_summary`` hard-coded "✅ In Stock" with no
    i18n lookup at all — a third copy of the predicate AND an English leak in a
    bot whose copy is entirely DB-backed. It had no callers, so it is gone."""
    source = (BOT_ROOT / "utils.py").read_text(encoding="utf-8")
    assert "In Stock" not in source
    assert "Out of Stock" not in source

    from utils import MessageBuilder

    assert not hasattr(MessageBuilder, "build_product_summary")
