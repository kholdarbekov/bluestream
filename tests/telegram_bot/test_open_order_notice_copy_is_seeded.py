"""The open-order notice (spec 2026-10-21 R9) ships with the owner-approved copy, exactly.

`shared.i18n_rendering.render_translation` never raises. Drop a language or rename `{status}` and
the customer reads a humanised key. Same precedent as test_typed_quantity_copy_is_seeded.py.
"""

from pathlib import Path

from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

KEY = "telegram.orders.open_order_notice"
APPROVED = {
    "uz": "📦 Sizda faol buyurtma bor: {order_number} — {status}.",
    "ru": "📦 У вас уже есть активный заказ: {order_number} — {status}.",
    "en": "📦 You already have an order in progress: {order_number} — {status}.",
}
_RENDERING_MODULE = Path(__file__).resolve().parents[2] / "telegram_bot" / "handlers" / "quick_order.py"


def test_the_notice_is_seeded_with_the_owner_approved_copy():
    assert BACKEND_TRANSLATIONS[KEY] == APPROVED


def test_the_quick_order_handler_renders_that_key():
    assert KEY in _RENDERING_MODULE.read_text(encoding="utf-8")
