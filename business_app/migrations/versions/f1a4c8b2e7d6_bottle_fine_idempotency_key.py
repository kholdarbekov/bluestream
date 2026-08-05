"""bottle fines: client-supplied idempotency key

Revision ID: f1a4c8b2e7d6
Revises: d2b8f4a6c9e1
Create Date: 2026-08-03 12:00:00.000000

`issue_fine` mints a `BottleFine` and a FINE_ISSUED ledger row with no
idempotency of any kind, so a POST the backend committed and then failed to
acknowledge can be delivered twice and the fine — real money — is issued twice.
Keying only the LEDGER row would be strictly worse than doing nothing: the
ledger entry would dedupe while the `bottle_fines` row duplicated, leaving the
money and its audit trail disagreeing. The fine's own money lives in this table
(the FINE_ISSUED ledger entry carries quantity=0), so the guard has to live here.

Nullable with no backfill: every existing fine predates client tokens and
genuinely has none, and the column stays NULL for every server-initiated and
legacy-client fine. Same shape as `bottle_ledger.idempotency_key`: String(255),
nullable, plain UNIQUE on the key alone.

Plain, NOT partial, and NOT `nulls_not_distinct`: Postgres and SQLite both treat
NULLs as distinct under a plain UNIQUE, so a `WHERE idempotency_key IS NOT NULL`
predicate would add nothing. Every partial index in this repo carries a *status*
predicate, never a NULL one.

downgrade() drops the constraint then the column, in exact reverse order.
tests/integration/test_migrations_roundtrip.py walks
upgrade(head) -> downgrade(base) -> upgrade(head) against a real ephemeral
Postgres, so it must not raise. Never `op.drop_constraint(None, ...)`
(migrations/CONVENTIONS.md §5).
"""

import sqlalchemy as sa
from alembic import op

revision = "f1a4c8b2e7d6"
down_revision = "d2b8f4a6c9e1"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "bottle_fines",
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
    )
    op.create_unique_constraint("uq_bottle_fines_idempotency_key", "bottle_fines", ["idempotency_key"])


def downgrade():
    op.drop_constraint("uq_bottle_fines_idempotency_key", "bottle_fines", type_="unique")
    op.drop_column("bottle_fines", "idempotency_key")
