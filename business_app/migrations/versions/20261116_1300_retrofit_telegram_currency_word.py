"""customer-chat Telegram templates: say "so'm" instead of a typed "UZS" (en)

Revision ID: 7b3f1c9e4a62
Revises: 5d2e8a7c3b19
Create Date: 2026-11-16 13:00:00.000000

Owner decision 4 of the price-change transition: every amount the customer
reads in the customer-bot chat says "so'm" (en, uz) / "сум" (ru), never a typed
"UZS". DEFAULT_TEMPLATES now ships the en Telegram copy that way, but it only
seeds MISSING NotificationTemplate rows, so the rows production already holds
keep " UZS". Replace the token " UZS" with " so'm" in the en translation rows
of the ``payment_confirmation`` and ``payment_late_debit_credited`` Telegram
templates (the only Telegram defaults whose en text changed). uz and ru
content, other channels and every other word of an admin-edited row are left
alone. Idempotent; rows without " UZS" are not touched.

Rollback strategy:
    downgrade() reverses exactly (" so'm" -> " UZS") on the same rows. Lossy
    only if an admin typed "so'm" into one of these en templates after the
    upgrade: that edit is turned back into "UZS" too.
"""

from alembic import op
from sqlalchemy import bindparam, text

# revision identifiers, used by Alembic.
revision = "7b3f1c9e4a62"
down_revision = "5d2e8a7c3b19"
branch_labels = None
depends_on = None

# Frozen literals (never import app code into a migration).
_NOTIFICATION_TYPES = ("payment_confirmation", "payment_late_debit_credited")
_CHANNEL = "telegram"
_LANGUAGE = "en"
_OLD = " UZS"
_NEW = " so'm"


def upgrade():
    _apply(op.get_bind(), _OLD, _NEW)


def downgrade():
    _apply(op.get_bind(), _NEW, _OLD)


def _apply(bind, old, new):
    """Swap ``old`` for ``new`` in the targeted en translation rows; split out for tests."""
    ids_stmt = text(
        "SELECT id FROM notification_templates WHERE channel = :channel AND notification_type IN :types"
    ).bindparams(bindparam("types", expanding=True))
    template_ids = [row[0] for row in bind.execute(ids_stmt, {"channel": _CHANNEL, "types": list(_NOTIFICATION_TYPES)})]
    if not template_ids:
        return
    keys = [f"NotificationTemplate.content.{tid}" for tid in template_ids]
    select_stmt = text("SELECT id, value FROM translations WHERE language = :lang AND key IN :keys").bindparams(
        bindparam("keys", expanding=True)
    )
    for tr_id, value in bind.execute(select_stmt, {"lang": _LANGUAGE, "keys": keys}).fetchall():
        if not value or old not in value:
            continue
        bind.execute(
            text("UPDATE translations SET value = :v, updated_at = CURRENT_TIMESTAMP WHERE id = :id"),
            {"v": value.replace(old, new), "id": tr_id},
        )
