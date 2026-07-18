"""Retrofit the ``{payment_follow_up_message}`` placeholder into legacy
payment-confirmation notification templates.

Background
----------
Payment-confirmation copy used to hardcode the follow-up sentence
("Your order is now being processed…" / "Buyurtmangiz qayta ishlanmoqda…").
It was later made *contextual* (different copy for a still-processing order vs
one already delivered) by injecting it through the ``{payment_follow_up_message}``
placeholder that ``NotificationService`` fills at send time. The in-repo
templates (file-based email templates + bundled ``DEFAULT_TEMPLATES``) were
migrated to the placeholder, but ``NotificationTemplate`` rows live in the DB
and could not be edited from the repo, so a runtime string-rewrite shim was
bolted on instead.

That shim ran on *every* send and used substring ``.replace()`` with legacy
phrases, one of which ("Buyurtmangiz qayta ishlanmoqda.", "Ваш заказ
обрабатывается.") is a strict prefix of the new copy. Once the placeholder had
already produced the new copy, the shim matched that prefix and re-appended the
tail — duplicating "Keyingi holat bo'yicha sizni xabardor qilamiz." in
production.

The shim has been removed. This one-time, idempotent migration does the shim's
legitimate job exactly once: for any ``payment_confirmation`` template still
holding a legacy hardcoded follow-up sentence, it swaps that sentence for the
``{payment_follow_up_message}`` placeholder so the contextual copy keeps
working. Rows that already use the placeholder (or hold unrelated custom copy)
are left untouched.

Revision ID: b8f4d2a1c9e6
Revises: 3d7a1e9f6c52
Create Date: 2026-07-18 10:40:00.000000

"""

from alembic import op
from sqlalchemy import bindparam, text

# revision identifiers, used by Alembic.
revision = "b8f4d2a1c9e6"
down_revision = "3d7a1e9f6c52"
branch_labels = None
depends_on = None


PLACEHOLDER = "{payment_follow_up_message}"

# Legacy hardcoded follow-up sentences that predate the placeholder, in every
# supported language. A shorter phrase can be a strict prefix of a longer one
# (e.g. "Buyurtmangiz qayta ishlanmoqda." ⊂ "Buyurtmangiz qayta ishlanmoqda.
# Yetkazib berishga tayyor bo'lganda xabar beramiz."), so we always try the
# LONGEST match first — replacing the short prefix first would leave a dangling
# tail. ``_retrofit_follow_up_placeholder`` enforces the ordering with sorted().
_LEGACY_FOLLOW_UP_PHRASES = (
    "Buyurtmangiz hozir qayta ishlanmoqda va tez orada yetkazib beriladi. "
    "Yuqoridagi tugma orqali buyurtma holatini kuzatishingiz mumkin.",
    "Buyurtmangiz qayta ishlanmoqda. Yetkazib berishga tayyor bo'lganda xabar beramiz.",
    "Buyurtmangiz qayta ishlanmoqda.",
    "Ваш заказ обрабатывается и скоро будет доставлен. " "Вы можете отслеживать статус заказа по кнопке выше.",
    "Ваш заказ обрабатывается. Мы уведомим вас, когда он будет готов к доставке.",
    "Ваш заказ обрабатывается.",
    "Your order is now being processed and will be delivered soon. "
    "You can track your order status using the button above.",
    "Your order is now being processed. We'll notify you when it's ready for delivery.",
    "Your order is now being processed.",
)


# The CURRENT (corrected) contextual follow-up copy, verbatim, in every language
# and stage — mirrors NotificationService.PAYMENT_FOLLOW_UP_MESSAGES. A row that
# already holds one of these needs no retrofit. This guard is load-bearing: the
# short legacy phrase ("Buyurtmangiz qayta ishlanmoqda." / "Ваш заказ
# обрабатывается.") is a STRICT PREFIX of the new "processing" copy for uz/ru, so
# without it a row hardcoding the new copy (e.g. an operator's incident hotfix)
# would have its prefix swapped for the placeholder and leave a dangling tail —
# re-introducing the very duplication this migration removes.
_CURRENT_FOLLOW_UP_COPY = (
    # uz
    "Buyurtmangiz qayta ishlanmoqda. Keyingi holat bo'yicha sizni xabardor qilamiz.",
    "Buyurtmangiz allaqachon yetkazib berilgan. Ushbu xabar to'lovingiz qabul qilinganini tasdiqlaydi.",
    # en
    "Your order is being processed. We'll notify you about the next status update.",
    "Your order has already been delivered. This message confirms that we have received your payment.",
    # ru
    "Ваш заказ обрабатывается. Мы сообщим вам о следующем обновлении статуса.",
    "Ваш заказ уже доставлен. Это сообщение подтверждает, что ваша оплата получена.",
)


def _retrofit_follow_up_placeholder(content):
    """Return ``content`` with the first legacy follow-up sentence replaced by
    the ``{payment_follow_up_message}`` placeholder.

    Idempotent: content that already contains the placeholder, already holds the
    current corrected copy, or has no legacy phrase at all is returned unchanged.
    Longest phrase wins, so a shorter prefix never leaves a dangling tail.
    """
    if not content or PLACEHOLDER in content:
        return content
    # Already-corrected copy (verbatim, not via the placeholder) is up to date —
    # never treat its leading sentence as a legacy phrase to replace.
    if any(current in content for current in _CURRENT_FOLLOW_UP_COPY):
        return content
    for phrase in sorted(_LEGACY_FOLLOW_UP_PHRASES, key=len, reverse=True):
        if phrase in content:
            return content.replace(phrase, PLACEHOLDER, 1)
    return content


def upgrade():
    _apply(op.get_bind())


def _apply(bind):
    """Run the retrofit against ``bind`` (a Connection).

    Split out from ``upgrade`` so it can be exercised directly against a real
    database in tests, without an Alembic migration context.
    """
    # 1. Canonical (Uzbek default) content stored on the column itself.
    rows = bind.execute(
        text("SELECT id, content FROM notification_templates " "WHERE notification_type = 'payment_confirmation'")
    ).fetchall()

    template_ids = []
    for row_id, content in rows:
        template_ids.append(row_id)
        new_content = _retrofit_follow_up_placeholder(content)
        if new_content != content:
            bind.execute(
                text("UPDATE notification_templates SET content = :c WHERE id = :id"),
                {"c": new_content, "id": row_id},
            )

    # 2. Per-language (en/ru/uz) overrides live in the unified translations
    #    table under key "NotificationTemplate.content.<id>".
    if not template_ids:
        return

    keys = [f"NotificationTemplate.content.{tid}" for tid in template_ids]
    select_stmt = text("SELECT id, value FROM translations WHERE key IN :keys").bindparams(
        bindparam("keys", expanding=True)
    )
    translation_rows = bind.execute(select_stmt, {"keys": keys}).fetchall()

    for tr_id, value in translation_rows:
        new_value = _retrofit_follow_up_placeholder(value)
        if new_value != value:
            bind.execute(
                text("UPDATE translations SET value = :v, updated_at = CURRENT_TIMESTAMP " "WHERE id = :id"),
                {"v": new_value, "id": tr_id},
            )


def downgrade():
    # Data-only retrofit. We cannot reconstruct which specific legacy sentence a
    # given template originally held (and the contextual copy is strictly better
    # copy anyway), so the downgrade is intentionally a no-op. Leaving the
    # placeholder in place is harmless: NotificationService fills it on send.
    pass
