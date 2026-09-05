"""Seed the telegram bottle delivery-summary, concern-capture (support), and
ledger-history i18n keys (category='telegram', trilingual uz/ru/en).

These are the keys from the 2026-07-11 bottle-delivery-summary design (§3.7):
the delivered-order Telegram message, the "Report an issue" support flow, and
the paginated per-address ledger-history view. The eight
``telegram.bottles.event.*`` labels map one-to-one to ``BottleLedgerEventType``
values — see tests/unit/test_bottle_translation_keys.py, which fails if this set
ever drifts from the enum.

Run inside the business_app container (scripts/ is NOT volume-mounted, so pipe
the file in over stdin), then hot-reload the bot's cache:
    docker compose exec -T business_app python - < scripts/seed_bottle_ledger_translations.py
    # then POST /internal/reload-translations on the bot webhook server
"""

from business_app import create_app
from business_app.models.translation import Translation

CATEGORY = "telegram"

# {key: {language: value}} — natural per-key trilingual shape (mirrors
# BACKEND_TRANSLATIONS in scripts/seed_backend_translations.py). Double-quoted
# values throughout so Uzbek/English apostrophes need no escaping.
KEYS = {
    # --- Delivered-order summary message (§3.3 mockup) ---
    "telegram.delivery_summary.title": {
        "en": "✅ Order #{order_number} delivered!",
        "uz": "✅ #{order_number} raqamli buyurtma yetkazib berildi!",
        "ru": "✅ Заказ №{order_number} доставлен!",
    },
    "telegram.delivery_summary.bottles_delivered": {
        "en": "🍶 Bottles delivered: {count}",
        "uz": "🍶 Yetkazilgan idishlar: {count}",
        "ru": "🍶 Доставлено бутылей: {count}",
    },
    "telegram.delivery_summary.bottles_collected": {
        "en": "♻️ Empty bottles collected: {count}",
        "uz": "♻️ Qaytarib olingan bo'sh idishlar: {count}",
        "ru": "♻️ Пустых бутылей забрано: {count}",
    },
    # PLACE-scoped, not address-scoped: `get_order_bottle_summary` reports the
    # balance of the place the delivery address belongs to (the address group
    # when one exists), so at a shared workplace this number can include empties
    # standing at a coworker's DIFFERENT address in the same group. The copy must
    # therefore say "place", and it must never say WHOSE (spec §7) — the count is
    # the only thing allowed across that boundary. `{count}` is pinned by
    # PARAM_KEYS in tests/unit/test_bottle_translation_keys.py.
    "telegram.delivery_summary.balance": {
        "en": "📊 Bottles at this place: {count}",
        "uz": "📊 Ushbu joydagi idishlar: {count}",
        "ru": "📊 Бутылей в этом месте: {count}",
    },
    "telegram.delivery_summary.report_button": {
        "en": "⚠️ Report an issue",
        "uz": "⚠️ Muammo haqida xabar berish",
        "ru": "⚠️ Сообщить о проблеме",
    },
    # --- Concern-capture support flow (§3.4) ---
    "telegram.support.describe_issue_prompt": {
        "en": "Please describe the issue with order #{order_number}. Send your message and we'll pass it to our support team.",
        "uz": "Iltimos, #{order_number}-buyurtma bo'yicha muammoni yozib yuboring. Xabaringizni yuboring, biz uni qo'llab-quvvatlash guruhiga yetkazamiz.",
        "ru": "Пожалуйста, опишите проблему с заказом №{order_number}. Отправьте сообщение, и мы передадим его в службу поддержки.",
    },
    "telegram.support.cancel_button": {
        "en": "Cancel",
        "uz": "Bekor qilish",
        "ru": "Отмена",
    },
    "telegram.support.ack": {
        "en": "✅ Thank you! Your message has been sent to our support team. We'll get back to you soon.",
        "uz": "✅ Rahmat! Xabaringiz qo'llab-quvvatlash guruhiga yuborildi. Tez orada siz bilan bog'lanamiz.",
        "ru": "✅ Спасибо! Ваше сообщение отправлено в службу поддержки. Мы скоро свяжемся с вами.",
    },
    "telegram.support.send_failed": {
        "en": "Sorry, we couldn't send your message. Please try again.",
        "uz": "Kechirasiz, xabaringizni yuborib bo'lmadi. Iltimos, qaytadan urinib ko'ring.",
        "ru": "К сожалению, не удалось отправить сообщение. Пожалуйста, попробуйте ещё раз.",
    },
    "telegram.support.cancelled": {
        "en": "Report cancelled.",
        "uz": "Bekor qilindi.",
        "ru": "Отправка отменена.",
    },
    # --- Ledger-history view (§3.6) ---
    "telegram.bottles.history_button": {
        "en": "📜 History",
        "uz": "📜 Tarix",
        "ru": "📜 История",
    },
    "telegram.bottles.history_title": {
        "en": "📜 Bottle history",
        "uz": "📜 Idishlar harakatlari",
        "ru": "📜 История бутылей",
    },
    "telegram.bottles.history_empty": {
        "en": "No bottle movements recorded yet.",
        "uz": "Hozircha idishlar harakatlari qayd etilmagan.",
        "ru": "Пока нет записей о движении бутылей.",
    },
    # --- Ledger event labels: one per BottleLedgerEventType value (all 8) ---
    "telegram.bottles.event.delivery": {
        "en": "Delivered",
        "uz": "Yetkazildi",
        "ru": "Доставлено",
    },
    "telegram.bottles.event.return_on_delivery": {
        "en": "Collected",
        "uz": "Olindi",
        "ru": "Забрано",
    },
    "telegram.bottles.event.standalone_collection": {
        "en": "Bottles collected",
        "uz": "Idishlar olindi",
        "ru": "Бутыли забраны",
    },
    "telegram.bottles.event.admin_adjustment": {
        "en": "Adjustment",
        "uz": "Tuzatish",
        "ru": "Корректировка",
    },
    "telegram.bottles.event.fine_issued": {
        "en": "Fine issued",
        "uz": "Jarima belgilandi",
        "ru": "Начислен штраф",
    },
    "telegram.bottles.event.fine_reversed": {
        "en": "Fine reversed",
        "uz": "Jarima bekor qilindi",
        "ru": "Штраф отменён",
    },
    "telegram.bottles.event.fine_paid": {
        "en": "Fine paid",
        "uz": "Jarima to'landi",
        "ru": "Штраф оплачен",
    },
    "telegram.bottles.event.initial_balance": {
        "en": "Opening balance",
        "uz": "Boshlang'ich qoldiq",
        "ru": "Начальный остаток",
    },
}


def _to_language_keyed(keys: dict) -> dict:
    """Transform {key: {lang: value}} -> {lang: {key: value}} for the model's
    bulk_create_or_update signature."""
    by_language: dict = {"en": {}, "uz": {}, "ru": {}}
    for key, translations in keys.items():
        for language, value in translations.items():
            by_language[language][key] = value
    return by_language


def seed_bottle_ledger_translations(user_id: int | None = None) -> None:
    """Idempotent upsert of the bottle delivery-summary / support / ledger-history
    telegram translation keys (category='telegram')."""
    Translation.bulk_create_or_update(
        _to_language_keyed(KEYS), category=CATEGORY, user_id=user_id
    )


def main() -> None:
    app = create_app()
    with app.app_context():
        seed_bottle_ledger_translations()
        print(
            f"Seeded {len(KEYS)} telegram bottle/support translation keys "
            f"({len(KEYS) * 3} rows). Now POST /internal/reload-translations."
        )


if __name__ == "__main__":
    main()
