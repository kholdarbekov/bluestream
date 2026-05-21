"""Add order_edit_history and session reopen fields

Revision ID: a8e2c1f3b5d6
Revises: 4d2b1e6a5f80
Create Date: 2026-05-19 12:00:00.000000

Adds:
  - order_edit_history table (audit trail of admin order edits)
  - reopen_at / reopened_by_user_id / reopened_reason / reopen_count to
    driver_cash_sessions and driver_bottle_sessions

These are the foundation rows for the "admin edits placed orders" feature.
No data backfill required: reopen_count defaults to 0; other columns nullable.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "a8e2c1f3b5d6"
down_revision = "4d2b1e6a5f80"
branch_labels = None
depends_on = None


def upgrade():
    # ---- order_edit_history ----------------------------------------------
    op.create_table(
        "order_edit_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("edited_by_user_id", sa.Integer(), nullable=False),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("diff", sa.JSON(), nullable=False),
        sa.Column(
            "is_post_delivery",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["edited_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("order_edit_history", schema=None) as batch_op:
        batch_op.create_index(
            "idx_order_edit_history_order_created",
            ["order_id", "created_at"],
            unique=False,
        )
        batch_op.create_index(
            "idx_order_edit_history_editor_created",
            ["edited_by_user_id", "created_at"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_order_edit_history_order_id"),
            ["order_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_order_edit_history_edited_by_user_id"),
            ["edited_by_user_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_order_edit_history_is_post_delivery"),
            ["is_post_delivery"],
            unique=False,
        )

    # ---- driver_cash_sessions: reopen fields -----------------------------
    with op.batch_alter_table("driver_cash_sessions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("reopened_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("reopened_by_user_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("reopened_reason", sa.String(length=255), nullable=True))
        batch_op.add_column(
            sa.Column(
                "reopen_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.create_foreign_key(
            "fk_driver_cash_sessions_reopened_by_user_id",
            "users",
            ["reopened_by_user_id"],
            ["id"],
        )
        batch_op.create_index(
            batch_op.f("ix_driver_cash_sessions_reopened_by_user_id"),
            ["reopened_by_user_id"],
            unique=False,
        )

    # ---- driver_bottle_sessions: reopen fields ---------------------------
    with op.batch_alter_table("driver_bottle_sessions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("reopened_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("reopened_by_user_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("reopened_reason", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "reopen_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.create_foreign_key(
            "fk_driver_bottle_sessions_reopened_by_user_id",
            "users",
            ["reopened_by_user_id"],
            ["id"],
        )


def downgrade():
    # ---- driver_bottle_sessions ------------------------------------------
    with op.batch_alter_table("driver_bottle_sessions", schema=None) as batch_op:
        batch_op.drop_constraint(
            "fk_driver_bottle_sessions_reopened_by_user_id",
            type_="foreignkey",
        )
        batch_op.drop_column("reopen_count")
        batch_op.drop_column("reopened_reason")
        batch_op.drop_column("reopened_by_user_id")
        batch_op.drop_column("reopened_at")

    # ---- driver_cash_sessions --------------------------------------------
    with op.batch_alter_table("driver_cash_sessions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_driver_cash_sessions_reopened_by_user_id"))
        batch_op.drop_constraint(
            "fk_driver_cash_sessions_reopened_by_user_id",
            type_="foreignkey",
        )
        batch_op.drop_column("reopen_count")
        batch_op.drop_column("reopened_reason")
        batch_op.drop_column("reopened_by_user_id")
        batch_op.drop_column("reopened_at")

    # ---- order_edit_history ----------------------------------------------
    with op.batch_alter_table("order_edit_history", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_order_edit_history_is_post_delivery"))
        batch_op.drop_index(batch_op.f("ix_order_edit_history_edited_by_user_id"))
        batch_op.drop_index(batch_op.f("ix_order_edit_history_order_id"))
        batch_op.drop_index("idx_order_edit_history_editor_created")
        batch_op.drop_index("idx_order_edit_history_order_created")
    op.drop_table("order_edit_history")
