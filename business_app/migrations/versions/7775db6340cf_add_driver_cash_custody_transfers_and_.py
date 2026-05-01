"""add driver cash custody transfers and session fields

Revision ID: 7775db6340cf
Revises: 348515beb00c
Create Date: 2026-03-08 21:47:32.639474

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "7775db6340cf"
down_revision = "348515beb00c"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "driver_cash_transfers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("transfer_id", sa.String(length=100), nullable=False),
        sa.Column("driver_cash_session_id", sa.Integer(), nullable=False),
        sa.Column("driver_user_id", sa.Integer(), nullable=False),
        sa.Column("declared_transfer_cash", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"),
        sa.Column("counted_transfer_cash", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("transfer_variance", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0"),
        sa.Column("transfer_status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("transfer_metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("driver_confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("driver_confirmed_by_user_id", sa.Integer(), nullable=False),
        sa.Column("checkpoint_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checkpoint_confirmed_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["checkpoint_confirmed_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["driver_cash_session_id"], ["driver_cash_sessions.id"]),
        sa.ForeignKeyConstraint(["driver_confirmed_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["driver_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("driver_cash_transfers", schema=None) as batch_op:
        batch_op.create_index(
            "idx_driver_cash_transfers_session_created", ["driver_cash_session_id", "created_at"], unique=False
        )
        batch_op.create_index(
            "idx_driver_cash_transfers_status_created", ["transfer_status", "created_at"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_driver_cash_transfers_checkpoint_confirmed_by_user_id"),
            ["checkpoint_confirmed_by_user_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_driver_cash_transfers_driver_cash_session_id"), ["driver_cash_session_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_driver_cash_transfers_driver_confirmed_by_user_id"),
            ["driver_confirmed_by_user_id"],
            unique=False,
        )
        batch_op.create_index(batch_op.f("ix_driver_cash_transfers_driver_user_id"), ["driver_user_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_driver_cash_transfers_transfer_id"), ["transfer_id"], unique=True)
        batch_op.create_index(batch_op.f("ix_driver_cash_transfers_transfer_status"), ["transfer_status"], unique=False)

    with op.batch_alter_table("driver_cash_sessions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("gross_cash_collected", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("transferred_cash_total", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("expected_cash_on_hand", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("submission_due_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("last_reminder_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("reminder_stage", sa.String(length=32), nullable=False, server_default="none"))
        batch_op.add_column(sa.Column("verification_reason_code", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("resolution_reason_code", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("risk_flags", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")))

        batch_op.alter_column("gross_cash_collected", server_default=None)
        batch_op.alter_column("transferred_cash_total", server_default=None)
        batch_op.alter_column("expected_cash_on_hand", server_default=None)
        batch_op.alter_column("reminder_stage", server_default=None)
        batch_op.alter_column("risk_flags", server_default=None)

    with op.batch_alter_table("driver_cash_transfers", schema=None) as batch_op:
        batch_op.alter_column("declared_transfer_cash", server_default=None)
        batch_op.alter_column("transfer_variance", server_default=None)
        batch_op.alter_column("transfer_status", server_default=None)
        batch_op.alter_column("transfer_metadata", server_default=None)


def downgrade():
    with op.batch_alter_table("driver_cash_sessions", schema=None) as batch_op:
        batch_op.drop_column("risk_flags")
        batch_op.drop_column("resolution_reason_code")
        batch_op.drop_column("verification_reason_code")
        batch_op.drop_column("reminder_stage")
        batch_op.drop_column("last_reminder_at")
        batch_op.drop_column("submission_due_at")
        batch_op.drop_column("expected_cash_on_hand")
        batch_op.drop_column("transferred_cash_total")
        batch_op.drop_column("gross_cash_collected")

    with op.batch_alter_table("driver_cash_transfers", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_driver_cash_transfers_transfer_status"))
        batch_op.drop_index(batch_op.f("ix_driver_cash_transfers_transfer_id"))
        batch_op.drop_index(batch_op.f("ix_driver_cash_transfers_driver_user_id"))
        batch_op.drop_index(batch_op.f("ix_driver_cash_transfers_driver_confirmed_by_user_id"))
        batch_op.drop_index(batch_op.f("ix_driver_cash_transfers_driver_cash_session_id"))
        batch_op.drop_index(batch_op.f("ix_driver_cash_transfers_checkpoint_confirmed_by_user_id"))
        batch_op.drop_index("idx_driver_cash_transfers_status_created")
        batch_op.drop_index("idx_driver_cash_transfers_session_created")

    op.drop_table("driver_cash_transfers")
