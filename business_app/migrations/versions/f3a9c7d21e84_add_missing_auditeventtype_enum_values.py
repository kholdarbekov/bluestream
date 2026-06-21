"""add missing auditeventtype enum values

Revision ID: f3a9c7d21e84
Revises: e1a2b3c4d5f6
Create Date: 2026-06-21 14:00:00.000000

The Python ``AuditEventType`` enum (business_app/models/audit.py) grew three
members that were never added to the Postgres ``auditeventtype`` type:

  - order_edited                       (admin order edit)
  - session_reopened                   (driver cash / bottle session reopen)
  - payment_verification_code_verified (card token verification)

SQLAlchemy sends the enum *value* string to Postgres, so audit inserts for
those events failed with ``InvalidTextRepresentation`` and the audit rows were
silently dropped. This migration backfills the three labels.

``ALTER TYPE ... ADD VALUE`` is wrapped in an autocommit block so it never
collides with the migration transaction (version-agnostic; the new values are
not used inside this migration).
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "f3a9c7d21e84"
down_revision = "e1a2b3c4d5f6"
branch_labels = None
depends_on = None


_MISSING_VALUES = (
    "order_edited",
    "session_reopened",
    "payment_verification_code_verified",
)


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        for value in _MISSING_VALUES:
            op.execute(f"ALTER TYPE auditeventtype ADD VALUE IF NOT EXISTS '{value}'")


def downgrade():
    # PostgreSQL cannot drop individual enum labels without recreating the type,
    # which is unsafe with live referencing data. Leaving the additive labels in
    # place on downgrade is harmless and matches project precedent.
    pass
