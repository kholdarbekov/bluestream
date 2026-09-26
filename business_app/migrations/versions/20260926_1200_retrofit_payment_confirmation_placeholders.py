"""payment-confirmation templates: add the status-header and payment-details placeholders

Revision ID: a4c7e9b2d5f8
Revises: d7e8f9a0b1c2
Create Date: 2026-09-26 12:00:00.000000

The DB template rows win over DEFAULT_TEMPLATES, and the live rows predate
`{payment_status_header}` and `{payment_details}`, so customers always saw a
hardcoded "confirmed" header and never the collection breakdown. Swap the
legacy header for its placeholder and insert the details placeholder, on the
column (uz) and on every translation row. Idempotent; rows without the
expected anchors are left alone.

Rollback strategy:
    downgrade() is a no-op: the placeholders render correctly on the previous
    code as well, and the exact legacy text is not worth reconstructing.
"""

from alembic import op
from sqlalchemy import bindparam, text

# revision identifiers, used by Alembic.
revision = "a4c7e9b2d5f8"
down_revision = "d7e8f9a0b1c2"
branch_labels = None
depends_on = None


STATUS_HEADER = "{payment_status_header}"
DETAILS = "{payment_details}"

_LEGACY_TELEGRAM_HEADERS = (
    "✅ <b>To'lov tasdiqlandi!</b>",
    "✅ <b>Payment Confirmed!</b>",
    "✅ <b>Оплата подтверждена!</b>",
)
_TELEGRAM_DETAILS_ANCHOR = "{payment_method}"
_EMAIL_DETAILS_ANCHOR = "<p>{payment_follow_up_message}</p>"


def _retrofit_payment_confirmation(content, channel):
    """Return ``content`` with the missing placeholders added for ``channel``."""
    if not content:
        return content
    if channel == "telegram":
        if STATUS_HEADER not in content:
            for header in _LEGACY_TELEGRAM_HEADERS:
                if header in content:
                    content = content.replace(header, STATUS_HEADER, 1)
                    break
        if DETAILS not in content and _TELEGRAM_DETAILS_ANCHOR in content:
            content = content.replace(_TELEGRAM_DETAILS_ANCHOR, _TELEGRAM_DETAILS_ANCHOR + DETAILS, 1)
    elif channel == "email":
        if DETAILS not in content and _EMAIL_DETAILS_ANCHOR in content:
            content = content.replace(_EMAIL_DETAILS_ANCHOR, f"<p>{DETAILS}</p>\n{_EMAIL_DETAILS_ANCHOR}", 1)
    return content


def upgrade():
    _apply(op.get_bind())


def _apply(bind):
    """Run the retrofit against ``bind`` (a Connection); split out for tests."""
    rows = bind.execute(
        text(
            "SELECT id, channel, content FROM notification_templates "
            "WHERE notification_type = 'payment_confirmation'"
        )
    ).fetchall()
    if not rows:
        return

    channel_by_key = {}
    for row_id, channel, content in rows:
        channel_by_key[f"NotificationTemplate.content.{row_id}"] = channel
        new_content = _retrofit_payment_confirmation(content, channel)
        if new_content != content:
            bind.execute(
                text("UPDATE notification_templates SET content = :c WHERE id = :id"),
                {"c": new_content, "id": row_id},
            )

    select_stmt = text("SELECT id, key, value FROM translations WHERE key IN :keys").bindparams(
        bindparam("keys", expanding=True)
    )
    for tr_id, key, value in bind.execute(select_stmt, {"keys": list(channel_by_key)}).fetchall():
        new_value = _retrofit_payment_confirmation(value, channel_by_key[key])
        if new_value != value:
            bind.execute(
                text("UPDATE translations SET value = :v, updated_at = CURRENT_TIMESTAMP WHERE id = :id"),
                {"v": new_value, "id": tr_id},
            )


def downgrade():
    pass
