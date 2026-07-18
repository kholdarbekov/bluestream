"""Unit tests for the payment-follow-up placeholder retrofit migration
(``b8f4d2a1c9e6``).

The migration replaces a legacy hardcoded follow-up sentence with the
``{payment_follow_up_message}`` placeholder so the contextual copy keeps
working after the runtime rewrite shim was removed. The transform lives in the
migration module as ``_retrofit_follow_up_placeholder`` — these tests load that
module by path and exercise the pure transform directly.
"""

import importlib.util
from pathlib import Path

import pytest

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "business_app"
    / "migrations"
    / "versions"
    / "b8f4d2a1c9e6_retrofit_payment_follow_up_placeholder.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("_retrofit_migration", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = _load_migration()
retrofit = migration._retrofit_follow_up_placeholder
PLACEHOLDER = migration.PLACEHOLDER


@pytest.mark.parametrize(
    "legacy",
    [
        "Buyurtmangiz qayta ishlanmoqda.",
        "Buyurtmangiz qayta ishlanmoqda. Yetkazib berishga tayyor bo'lganda xabar beramiz.",
        "Buyurtmangiz hozir qayta ishlanmoqda va tez orada yetkazib beriladi. "
        "Yuqoridagi tugma orqali buyurtma holatini kuzatishingiz mumkin.",
        "Ваш заказ обрабатывается.",
        "Ваш заказ обрабатывается. Мы уведомим вас, когда он будет готов к доставке.",
        "Your order is now being processed.",
        "Your order is now being processed. We'll notify you when it's ready for delivery.",
    ],
)
def test_legacy_phrase_is_replaced_by_the_placeholder(legacy):
    assert retrofit(legacy) == PLACEHOLDER


def test_longest_phrase_wins_so_no_dangling_tail():
    """The short "Buyurtmangiz qayta ishlanmoqda." is a strict prefix of the
    longer sentence. Matching the short one first would leave the tail
    "Yetkazib berishga tayyor bo'lganda xabar beramiz." dangling — the exact
    class of bug (mismatched substring surgery) that caused the incident."""
    legacy = "Buyurtmangiz qayta ishlanmoqda. Yetkazib berishga tayyor bo'lganda xabar beramiz."
    result = retrofit(legacy)
    assert result == PLACEHOLDER
    assert "Yetkazib berishga tayyor" not in result


def test_placeholder_is_preserved_inside_a_full_template():
    template = (
        "✅ <b>To'lov tasdiqlandi!</b>\n\n"
        "Buyurtma: #{order_number}\n\n"
        "Buyurtmangiz qayta ishlanmoqda.\n\n"
        "Xaridingiz uchun rahmat!"
    )
    result = retrofit(template)
    assert result == (
        "✅ <b>To'lov tasdiqlandi!</b>\n\n"
        "Buyurtma: #{order_number}\n\n"
        f"{PLACEHOLDER}\n\n"
        "Xaridingiz uchun rahmat!"
    )


def test_content_already_using_placeholder_is_unchanged():
    content = f"✅ To'lov tasdiqlandi!\n\n{PLACEHOLDER}\n\nRahmat!"
    assert retrofit(content) == content


@pytest.mark.parametrize(
    "current_copy",
    [
        # uz "processing" — the short legacy phrase "Buyurtmangiz qayta
        # ishlanmoqda." is a STRICT PREFIX of this sentence.
        "Buyurtmangiz qayta ishlanmoqda. Keyingi holat bo'yicha sizni xabardor qilamiz.",
        # ru "processing" — legacy "Ваш заказ обрабатывается." is a strict prefix.
        "Ваш заказ обрабатывается. Мы сообщим вам о следующем обновлении статуса.",
        # en "processing" — control (legacy keeps the dropped word "now", so safe).
        "Your order is being processed. We'll notify you about the next status update.",
        # delivered copy (no legacy prefix overlap) — must also be left alone.
        "Buyurtmangiz allaqachon yetkazib berilgan. Ushbu xabar to'lovingiz qabul qilinganini tasdiqlaydi.",
    ],
)
def test_content_already_holding_current_copy_is_left_unchanged(current_copy):
    """If a template already holds the CURRENT (corrected) follow-up copy without
    the placeholder — e.g. an operator hand-edited the DB template to stop the
    visible duplication during the incident — the retrofit must NOT touch it.

    Regression guard: the short legacy phrase is a strict prefix of the uz/ru
    current copy, so a naive prefix replacement would swap the prefix for the
    placeholder and leave a dangling tail, re-duplicating the sentence at send —
    the very bug this whole change exists to eliminate."""
    assert retrofit(current_copy) == current_copy


def test_current_copy_embedded_in_full_template_is_left_unchanged():
    template = (
        "✅ <b>To'lov tasdiqlandi!</b>\n\n"
        "Buyurtma: #{order_number}\n\n"
        "Buyurtmangiz qayta ishlanmoqda. Keyingi holat bo'yicha sizni xabardor qilamiz.\n\n"
        "Xaridingiz uchun rahmat!"
    )
    assert retrofit(template) == template


def test_unrelated_content_is_unchanged():
    content = "✅ To'lov tasdiqlandi! Rahmat!"
    assert retrofit(content) == content


@pytest.mark.parametrize("empty", [None, ""])
def test_empty_content_is_unchanged(empty):
    assert retrofit(empty) == empty


def test_retrofit_is_idempotent():
    legacy = "Buyurtmangiz qayta ishlanmoqda. Yetkazib berishga tayyor bo'lganda xabar beramiz."
    once = retrofit(legacy)
    assert retrofit(once) == once


def test_apply_retrofits_column_and_translation_rows(app, db):
    """End-to-end: the migration's real SQL retrofits both the uz default on the
    ``notification_templates.content`` column and the en override in the unified
    ``translations`` table, and is idempotent."""
    from business_app.models.notification import NotificationTemplate
    from business_app.models.translation import Translation

    template = NotificationTemplate(
        name="payment_confirmation_telegram",
        notification_type="payment_confirmation",
        channel="telegram",
        content=(
            "✅ To'lov tasdiqlandi!\n\n"
            "Buyurtmangiz qayta ishlanmoqda. Yetkazib berishga tayyor bo'lganda xabar beramiz.\n\n"
            "Rahmat!"
        ),
        is_active=True,
    )
    db.session.add(template)
    db.session.flush()

    en_key = f"NotificationTemplate.content.{template.id}"
    db.session.add(
        Translation(
            key=en_key,
            language="en",
            value=(
                "✅ Payment Confirmed!\n\n"
                "Your order is now being processed. We'll notify you when it's ready for delivery.\n\n"
                "Thank you!"
            ),
            category="notification",
            is_active=True,
        )
    )
    db.session.commit()

    migration._apply(db.session.connection())
    db.session.commit()

    db.session.refresh(template)
    en_row = Translation.query.filter_by(key=en_key, language="en").first()

    # uz column: legacy sentence swapped for the placeholder, no dangling tail.
    assert PLACEHOLDER in template.content
    assert "Buyurtmangiz qayta ishlanmoqda." not in template.content
    assert "Yetkazib berishga tayyor" not in template.content
    # en translation override: same treatment.
    assert PLACEHOLDER in en_row.value
    assert "now being processed" not in en_row.value

    # Idempotent: a second run is a no-op.
    content_after_first = template.content
    en_after_first = en_row.value
    migration._apply(db.session.connection())
    db.session.commit()
    db.session.refresh(template)
    en_row_again = Translation.query.filter_by(key=en_key, language="en").first()
    assert template.content == content_after_first
    assert en_row_again.value == en_after_first
