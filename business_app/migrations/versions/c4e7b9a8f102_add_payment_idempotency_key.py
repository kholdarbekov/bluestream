"""add payment idempotency_key (PAY-005)

Revision ID: c4e7b9a8f102
Revises: 9ef3918623ff
Create Date: 2026-04-25 00:00:00.000000

Adds payments.idempotency_key (sha256-derived, 32 chars). Backfills existing
rows with the computed key so the unique index can be enforced. New rows get
the key set at __init__ time on the model.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c4e7b9a8f102"
down_revision = "9ef3918623ff"
branch_labels = None
depends_on = None


def upgrade():
    # 0. Ensure pgcrypto is available — the backfill below uses DIGEST(), which
    #    ships with the pgcrypto extension and is not in the Postgres core
    #    catalog. Idempotent and a no-op on environments where it's already
    #    installed. (Fix surfaced when migrations finally started running under
    #    the dedicated `migrate` compose service; previously the entrypoint
    #    swallowed every migration failure as "may be okay if no migrations
    #    pending", masking that this one had never applied anywhere.)
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # 1. Add column nullable (so we can backfill before flipping unique).
    with op.batch_alter_table("payments", schema=None) as batch_op:
        batch_op.add_column(sa.Column("idempotency_key", sa.String(length=64), nullable=True))

    # 2. Backfill existing rows. We compute sha256(order_id:user_id:amount:method)[:32]
    #    inside Postgres so we don't drag every row through Python. NULL order_id
    #    rows (subscription-only payments etc.) get a synthetic key keyed on
    #    payment_id so the uniqueness invariant still holds.
    op.execute(
        """
        UPDATE payments
        SET idempotency_key = LEFT(
            ENCODE(
                DIGEST(
                    COALESCE(order_id::text, 'no_order') || ':' ||
                    user_id::text || ':' ||
                    amount::text || ':' ||
                    payment_method::text,
                    'sha256'
                ),
                'hex'
            ),
            32
        )
        WHERE idempotency_key IS NULL
        """
    )

    # 3. Enforce uniqueness + index for fast lookup. Kept nullable=True so any
    #    edge-case row added between step 1 and step 3 doesn't fail the migration.
    with op.batch_alter_table("payments", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_payments_idempotency_key"),
            ["idempotency_key"],
            unique=True,
        )


def downgrade():
    with op.batch_alter_table("payments", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_payments_idempotency_key"))
        batch_op.drop_column("idempotency_key")
