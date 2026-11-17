"""The Telegram currency-word retrofit turns a typed en "UZS" into "so'm" on existing rows."""

import importlib.util
from pathlib import Path

from business_app.services.notification_service import DEFAULT_TEMPLATES

_VERSIONS = Path(__file__).resolve().parents[2] / "business_app" / "migrations" / "versions"


def _load(filename, name):
    spec = importlib.util.spec_from_file_location(name, _VERSIONS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = _load("20261116_1300_retrofit_telegram_currency_word.py", "_telegram_currency_word")
placeholders = _load("20260926_1200_retrofit_payment_confirmation_placeholders.py", "_pc_placeholders_for_chain")

LEGACY_CONFIRMATION_EN = (
    "✅ <b>Payment Confirmed!</b>\n\nOrder: #{order_number}\nAmount: {payment_amount} UZS\n"
    "Method: {payment_method}\n\n{payment_follow_up_message}\n\nThank you for your purchase!"
)
LEGACY_LATE_EN = DEFAULT_TEMPLATES[("payment_late_debit_credited", "telegram")]["translations"]["en"][
    "content"
].replace(" so'm", " UZS")


def _default(notification_type, language):
    return DEFAULT_TEMPLATES[(notification_type, "telegram")]["translations"][language]["content"]


def _template(db, notification_type, channel="telegram"):
    from business_app.models.notification import NotificationTemplate

    row = NotificationTemplate(
        name=f"{notification_type}_{channel}",
        notification_type=notification_type,
        channel=channel,
        content="uz copy 100 UZS",
        is_active=True,
    )
    db.session.add(row)
    db.session.flush()
    return row


def _translation(db, template, language, value):
    from business_app.models.translation import Translation

    db.session.add(
        Translation(
            key=f"NotificationTemplate.content.{template.id}",
            language=language,
            value=value,
            category="entity_notificationtemplate",
            is_active=True,
        )
    )
    db.session.flush()


def _value(template, language):
    from business_app.models.translation import Translation

    return Translation.query.filter_by(key=f"NotificationTemplate.content.{template.id}", language=language).one().value


def _upgrade(db):
    migration._apply(db.session.connection(), migration._OLD, migration._NEW)
    db.session.commit()


def _downgrade(db):
    migration._apply(db.session.connection(), migration._NEW, migration._OLD)
    db.session.commit()


def test_old_en_rows_become_the_shipped_default(app, db):
    confirmation = _template(db, "payment_confirmation")
    late = _template(db, "payment_late_debit_credited")
    _translation(db, confirmation, "en", placeholders._retrofit_payment_confirmation(LEGACY_CONFIRMATION_EN, "telegram"))
    _translation(db, late, "en", LEGACY_LATE_EN)
    db.session.commit()

    _upgrade(db)

    assert _value(confirmation, "en") == _default("payment_confirmation", "en")
    assert _value(late, "en") == _default("payment_late_debit_credited", "en")


def test_uz_ru_other_channels_and_other_types_are_untouched(app, db):
    telegram = _template(db, "payment_confirmation")
    email = _template(db, "payment_confirmation", channel="email")
    other = _template(db, "order_confirmed")
    _translation(db, telegram, "uz", "Summa: 5 UZS")
    _translation(db, telegram, "ru", "Сумма: 5 UZS")
    _translation(db, email, "en", "Amount: 5 UZS")
    _translation(db, other, "en", "Amount: 5 UZS")
    db.session.commit()

    _upgrade(db)

    assert _value(telegram, "uz") == "Summa: 5 UZS"
    assert _value(telegram, "ru") == "Сумма: 5 UZS"
    assert _value(email, "en") == "Amount: 5 UZS"
    assert _value(other, "en") == "Amount: 5 UZS"


def test_admin_edited_en_row_keeps_its_edits_except_the_currency_token(app, db):
    row = _template(db, "payment_confirmation")
    _translation(db, row, "en", "Hi! We got {payment_amount} UZS for #{order_number}. UZSX stays. Cheers")
    db.session.commit()

    _upgrade(db)

    assert _value(row, "en") == "Hi! We got {payment_amount} so'm for #{order_number}. so'mX stays. Cheers"


def test_row_without_the_token_is_left_alone_and_upgrade_is_idempotent(app, db):
    clean = _template(db, "payment_late_debit_credited")
    old = _template(db, "payment_confirmation")
    _translation(db, clean, "en", "Custom copy {amount} so'm")
    _translation(db, old, "en", "Amount: {payment_amount} UZS")
    db.session.commit()

    _upgrade(db)
    once = (_value(clean, "en"), _value(old, "en"))
    _upgrade(db)

    assert once == ("Custom copy {amount} so'm", "Amount: {payment_amount} so'm")
    assert (_value(clean, "en"), _value(old, "en")) == once


def test_downgrade_restores_the_typed_currency_word(app, db):
    row = _template(db, "payment_late_debit_credited")
    _translation(db, row, "en", LEGACY_LATE_EN)
    db.session.commit()

    _upgrade(db)
    assert _value(row, "en") == _default("payment_late_debit_credited", "en")
    _downgrade(db)

    assert _value(row, "en") == LEGACY_LATE_EN


def test_no_templates_is_a_noop(app, db):
    _upgrade(db)
