"""ten litre trip units: a product setting, per-trip and per-transfer unit lines, transfer Rejected

Revision ID: 9c4e2a7d1b58
Revises: 3d8f5a1c7e92
Create Date: 2026-11-19 12:00:00.000000

Spec: docs/superpowers/specs/2026-11-18-ten-litre-bottle-trip-design.md.

Added:
  * products.counted_on_bottle_trip       the admin setting (default false; no backfill, nothing changes until set)
  * driver_bottle_session_units           one row per (trip, one-way unit product): the trip tallies
  * driver_bottle_transfer_units          one row per (transfer, one-way unit product)
  * driver_bottle_transfers.rejected_at   when the receiver refused the hand-over
  * 'rejected' in the native enum driver_bottle_transfer_status

Rollback strategy:
    downgrade() drops the two tables and the two columns. It leaves the enum value 'rejected': Postgres cannot
    drop a value from a native enum, and a value nothing uses is harmless. It refuses, writing nothing, while any
    transfer is in status 'rejected' (the code this rolls back to cannot load one: the admin Bottle Transfers tab
    would answer 500) or any trip or transfer unit line exists (dropping them would lose every 10 L tally without
    a word). Export the two unit tables and delete their rows, and move every rejected transfer off that status by
    hand, before rolling back. Before the office ticks the setting, a rollback finds no unit line, only the
    rejected transfers of 18.9 L hand-overs.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "9c4e2a7d1b58"
down_revision = "3d8f5a1c7e92"
branch_labels = None
depends_on = None


def upgrade():
    # Postgres cannot use a new enum value in the transaction that adds it: commit it first.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE driver_bottle_transfer_status ADD VALUE IF NOT EXISTS 'rejected'")

    with op.batch_alter_table("products", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("counted_on_bottle_trip", sa.Boolean(), server_default=sa.false(), nullable=False)
        )

    op.create_table(
        "driver_bottle_session_units",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("loaded", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("delivered", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("transferred_in", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("transferred_out", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("returned", sa.Integer(), nullable=True),
        sa.Column("discrepancy", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "loaded >= 0 AND delivered >= 0 AND transferred_in >= 0 AND transferred_out >= 0 "
            "AND (returned IS NULL OR returned >= 0)",
            name="ck_dbsu_counts_non_negative",
        ),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.ForeignKeyConstraint(["session_id"], ["driver_bottle_sessions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "product_id", name="uq_dbsu_session_product"),
    )
    with op.batch_alter_table("driver_bottle_session_units", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_driver_bottle_session_units_product_id"), ["product_id"])
        batch_op.create_index(batch_op.f("ix_driver_bottle_session_units_session_id"), ["session_id"])

    op.create_table(
        "driver_bottle_transfer_units",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("transfer_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("declared", sa.Integer(), nullable=False),
        sa.Column("confirmed", sa.Integer(), nullable=True),
        sa.Column("resolved", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "declared >= 0 AND (confirmed IS NULL OR confirmed >= 0) AND (resolved IS NULL OR resolved >= 0)",
            name="ck_dbtu_counts_non_negative",
        ),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.ForeignKeyConstraint(["transfer_id"], ["driver_bottle_transfers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("transfer_id", "product_id", name="uq_dbtu_transfer_product"),
    )
    with op.batch_alter_table("driver_bottle_transfer_units", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_driver_bottle_transfer_units_product_id"), ["product_id"])
        batch_op.create_index(batch_op.f("ix_driver_bottle_transfer_units_transfer_id"), ["transfer_id"])

    with op.batch_alter_table("driver_bottle_transfers", schema=None) as batch_op:
        batch_op.add_column(sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    conn = op.get_bind()
    rejected = conn.execute(
        sa.text("SELECT count(*) FROM driver_bottle_transfers WHERE status::text = 'rejected'")
    ).scalar()
    trip_lines = conn.execute(sa.text("SELECT count(*) FROM driver_bottle_session_units")).scalar()
    transfer_lines = conn.execute(sa.text("SELECT count(*) FROM driver_bottle_transfer_units")).scalar()
    if rejected or trip_lines or transfer_lines:
        raise RuntimeError(
            f"ten litre trip units downgrade: {rejected} rejected transfers, {trip_lines} trip unit lines and "
            f"{transfer_lines} transfer unit lines exist; export and delete driver_bottle_transfer_units and "
            "driver_bottle_session_units, and move every rejected transfer off that status by hand, first (the "
            "previous code cannot load a rejected transfer; see this migration's rollback strategy)"
        )

    # The enum value 'rejected' stays: Postgres cannot drop a value from a native enum, and it is harmless.
    with op.batch_alter_table("driver_bottle_transfers", schema=None) as batch_op:
        batch_op.drop_column("rejected_at")

    with op.batch_alter_table("driver_bottle_transfer_units", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_driver_bottle_transfer_units_transfer_id"))
        batch_op.drop_index(batch_op.f("ix_driver_bottle_transfer_units_product_id"))
    op.drop_table("driver_bottle_transfer_units")

    with op.batch_alter_table("driver_bottle_session_units", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_driver_bottle_session_units_session_id"))
        batch_op.drop_index(batch_op.f("ix_driver_bottle_session_units_product_id"))
    op.drop_table("driver_bottle_session_units")

    with op.batch_alter_table("products", schema=None) as batch_op:
        batch_op.drop_column("counted_on_bottle_trip")
