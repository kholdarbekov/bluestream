"""add users.password_set_at (when the person last chose their website password)

Revision ID: 290c1500d908
Revises: e3b7c1d9a5f4
Create Date: 2026-11-14 12:00:00.000000

Owner decision 2026-11-14 ("bot customers: record users setting a website password"). Until now
auth_service.has_website_password guessed from the stored hash and the registration source, so a bot signup that
set a password through the emailed link was still treated as having none: /account-security never offered it the
change form, and an account merge dropped its real password. The rule now reads this column instead.

Added:
  * users.password_set_at   nullable TIMESTAMPTZ: stamped whenever a person chooses a password (register, phone
                            register, reset, change); NULL while the stored password is one we made up.

Backfill (one UPDATE: the users table is small, so no id batching): exactly the rows the old rule said have a
website password (a non-empty password_hash that is not the bot signup's placeholder literal, on an account whose
registration_source is not 'telegram'; a NULL source counted as not 'telegram'), so no customer's answer changes on
deploy. The value is created_at, the moment the account got the password it holds or an earlier one; updated_at
moves with every edit of the row (a sign-in, a profile save) and would date a password change that never happened.
Left NULL on purpose, as the old rule left them: a bot signup ('telegram') that set a password through the emailed
link before this ran. Its hash looks like the random-token hash of a telegram-login signup, so nothing stored tells
the two apart; it keeps the set-a-password panel until it sets one again, which stamps it.

Rollback strategy:
    downgrade() drops the column. LOSSY only in that the stamps are discarded; the code it rolls back to ignores the
    column and goes back to the old rule (a bot signup that has set a password again counts as having none).
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "290c1500d908"
down_revision = "e3b7c1d9a5f4"
branch_labels = None
depends_on = None

TABLE = "users"
COLUMN = "password_set_at"

# Frozen copy (CONVENTIONS.md): auth_service.TELEGRAM_PLACEHOLDER_PASSWORD_HASH as it stood on 2026-11-14.
TELEGRAM_PLACEHOLDER_PASSWORD_HASH = "telegram_user"


def _backfill_password_set_at(bind) -> int:
    """Stamp every row the old has_website_password rule answered True for; returns how many."""
    result = bind.execute(
        sa.text(
            f"UPDATE {TABLE} SET {COLUMN} = created_at "
            f"WHERE {COLUMN} IS NULL "
            "AND password_hash IS NOT NULL AND password_hash <> '' AND password_hash <> :placeholder "
            "AND registration_source IS DISTINCT FROM 'telegram'"
        ),
        {"placeholder": TELEGRAM_PLACEHOLDER_PASSWORD_HASH},
    )
    return result.rowcount


def upgrade():
    op.add_column(TABLE, sa.Column(COLUMN, sa.DateTime(timezone=True), nullable=True))
    _backfill_password_set_at(op.get_bind())


def downgrade():
    op.drop_column(TABLE, COLUMN)
