"""place lifecycle: structured audit metadata on customer_link_events

Revision ID: d2b8f4a6c9e1
Revises: a3e7d1f9c204
Create Date: 2026-07-29 09:00:00.000000

Spec §7.2. Joining a place RE-SCOPES the joiner's prior bottle history onto the
group, and the audit event has to name exactly which ledger entries moved so the
action is reviewable and reversible. `customer_link_events.reason` is a
String(500) that already carries the "[group N] " scope prefix, so it cannot
carry a list of ids.

Nullable with no backfill: every pre-existing row predates the join-time
re-scoping and genuinely has no metadata. The Python-side `default=dict` on the
model gives new rows `{}` without needing a server default here.

downgrade() drops the column — structurally real and lossy only for the audit
payload itself. tests/integration/test_migrations_roundtrip.py walks
upgrade(head) -> downgrade(base) -> upgrade(head) against a real ephemeral
Postgres, so it must not raise.
"""

import sqlalchemy as sa
from alembic import op

revision = "d2b8f4a6c9e1"
down_revision = "a3e7d1f9c204"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("customer_link_events", sa.Column("event_metadata", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("customer_link_events", "event_metadata")
