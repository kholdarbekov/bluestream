"""add tax committee api token table and product expire_days

Revision ID: 80105c879c6f
Revises: f66d4fcce111
Create Date: 2026-04-06 12:00:00.000000

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "80105c879c6f"
down_revision = "f66d4fcce111"
branch_labels = None
depends_on = None


def upgrade():
    # Tax Committee API token storage
    op.create_table(
        "tax_committee_api_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tax_committee_api_tokens_is_active", "tax_committee_api_tokens", ["is_active"])

    # Product expire_days for marking code utilisation
    op.add_column("products", sa.Column("expire_days", sa.Integer(), nullable=True))

    # Add 'utilised' to marking code ledger event type enum
    op.execute("ALTER TYPE marking_code_ledger_event_type ADD VALUE IF NOT EXISTS 'utilised'")


def downgrade():
    op.execute(
        "DELETE FROM pg_enum WHERE enumlabel = 'utilised' "
        "AND enumtypid = (SELECT oid FROM pg_type WHERE typname = 'marking_code_ledger_event_type')"
    )
    op.drop_column("products", "expire_days")
    op.drop_index("ix_tax_committee_api_tokens_is_active", table_name="tax_committee_api_tokens")
    op.drop_table("tax_committee_api_tokens")
