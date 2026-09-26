"""The payment-confirmation template retrofit brings legacy DB rows up to the shipped defaults."""

import importlib.util
from pathlib import Path

import pytest

from business_app.services.notification_service import DEFAULT_TEMPLATES

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "business_app"
    / "migrations"
    / "versions"
    / "20260926_1200_retrofit_payment_confirmation_placeholders.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("_payment_confirmation_retrofit", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = _load_migration()
retrofit = migration._retrofit_payment_confirmation

# Verbatim copies of the legacy rows still live in production.
LEGACY_TELEGRAM = {
    "uz": (
        "✅ <b>To'lov tasdiqlandi!</b>\n\nBuyurtma: #{order_number}\nSumma: {payment_amount} so'm\n"
        "Usul: {payment_method}\n\n{payment_follow_up_message}\n\nXaridingiz uchun rahmat!"
    ),
    "en": (
        "✅ <b>Payment Confirmed!</b>\n\nOrder: #{order_number}\nAmount: {payment_amount} UZS\n"
        "Method: {payment_method}\n\n{payment_follow_up_message}\n\nThank you for your purchase!"
    ),
    "ru": (
        "✅ <b>Оплата подтверждена!</b>\n\nЗаказ: #{order_number}\nСумма: {payment_amount} сум\n"
        "Способ: {payment_method}\n\n{payment_follow_up_message}\n\nСпасибо за покупку!"
    ),
}
LEGACY_EMAIL = {
    "uz": (
        "<h2>To'lov qabul qilindi</h2>\n"
        "<p>#{order_number} raqamli buyurtmangiz uchun to'lovni muvaffaqiyatli qabul qildik.</p>\n"
        "<p><strong>To'lov tafsilotlari:</strong></p>\n<ul>\n"
        "    <li>Summa: {payment_amount} so'm</li>\n    <li>Usul: {payment_method}</li>\n"
        "    <li>Havola: {payment_reference}</li>\n</ul>\n<p>{payment_follow_up_message}</p>"
    ),
    "en": (
        "<h2>Payment Received</h2>\n<p>We have successfully received your payment for order #{order_number}.</p>\n"
        "<p><strong>Payment Details:</strong></p>\n<ul>\n"
        "    <li>Amount: {payment_amount} UZS</li>\n    <li>Method: {payment_method}</li>\n"
        "    <li>Reference: {payment_reference}</li>\n</ul>\n<p>{payment_follow_up_message}</p>"
    ),
    "ru": (
        "<h2>Оплата получена</h2>\n<p>Мы успешно получили вашу оплату за заказ #{order_number}.</p>\n"
        "<p><strong>Детали оплаты:</strong></p>\n<ul>\n"
        "    <li>Сумма: {payment_amount} сум</li>\n    <li>Способ: {payment_method}</li>\n"
        "    <li>Ссылка: {payment_reference}</li>\n</ul>\n<p>{payment_follow_up_message}</p>"
    ),
}


def _default(channel, language):
    return DEFAULT_TEMPLATES[("payment_confirmation", channel)]["translations"][language]["content"]


@pytest.mark.parametrize("language", ["uz", "en", "ru"])
def test_legacy_telegram_row_becomes_the_shipped_default(language):
    assert retrofit(LEGACY_TELEGRAM[language], "telegram") == _default("telegram", language)


@pytest.mark.parametrize("language", ["uz", "en", "ru"])
def test_legacy_email_row_becomes_the_shipped_default(language):
    assert retrofit(LEGACY_EMAIL[language], "email") == _default("email", language)


@pytest.mark.parametrize(
    ("content", "channel"),
    [(LEGACY_TELEGRAM["uz"], "telegram"), (LEGACY_EMAIL["ru"], "email")],
)
def test_retrofit_is_idempotent(content, channel):
    once = retrofit(content, channel)
    assert retrofit(once, channel) == once


@pytest.mark.parametrize("content", ["Rahmat!", "", None])
def test_content_without_anchors_is_left_alone(content):
    assert retrofit(content, "telegram") == content


def test_apply_retrofits_column_and_translation_rows(app, db):
    from business_app.models.notification import NotificationTemplate
    from business_app.models.translation import Translation

    telegram = NotificationTemplate(
        name="payment_confirmation_telegram",
        notification_type="payment_confirmation",
        channel="telegram",
        content=LEGACY_TELEGRAM["uz"],
        is_active=True,
    )
    email = NotificationTemplate(
        name="payment_confirmation_email",
        notification_type="payment_confirmation",
        channel="email",
        content=LEGACY_EMAIL["uz"],
        is_active=True,
    )
    db.session.add_all([telegram, email])
    db.session.flush()
    db.session.add_all(
        [
            Translation(
                key=f"NotificationTemplate.content.{telegram.id}",
                language="ru",
                value=LEGACY_TELEGRAM["ru"],
                category="entity_notificationtemplate",
                is_active=True,
            ),
            Translation(
                key=f"NotificationTemplate.content.{email.id}",
                language="en",
                value=LEGACY_EMAIL["en"],
                category="entity_notificationtemplate",
                is_active=True,
            ),
        ]
    )
    db.session.commit()

    migration._apply(db.session.connection())
    db.session.commit()
    migration._apply(db.session.connection())
    db.session.commit()

    db.session.refresh(telegram)
    db.session.refresh(email)
    assert telegram.content == _default("telegram", "uz")
    assert email.content == _default("email", "uz")
    ru_row = Translation.query.filter_by(key=f"NotificationTemplate.content.{telegram.id}", language="ru").one()
    en_row = Translation.query.filter_by(key=f"NotificationTemplate.content.{email.id}", language="en").one()
    assert ru_row.value == _default("telegram", "ru")
    assert en_row.value == _default("email", "en")
