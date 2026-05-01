"""Add driver session membership and accepted_by_driver_id

Revision ID: d4753e242cd4
Revises: 119266894b43
Create Date: 2026-04-15 17:58:07.665326

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "d4753e242cd4"
down_revision = "119266894b43"
branch_labels = None
depends_on = None


def upgrade():
    # Create driver_session_memberships table
    op.create_table(
        "driver_session_memberships",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("session_owner_id", sa.Integer(), nullable=False),
        sa.Column("member_driver_id", sa.Integer(), nullable=False),
        sa.Column(
            "status", sa.Enum("active", "left", "revoked", name="driver_session_membership_status"), nullable=False
        ),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invited_by_user_id", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["invited_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["member_driver_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["session_id"], ["driver_bottle_sessions.id"]),
        sa.ForeignKeyConstraint(["session_owner_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("driver_session_memberships", schema=None) as batch_op:
        batch_op.create_index("idx_dsm_member_status", ["member_driver_id", "status"], unique=False)
        batch_op.create_index("idx_dsm_owner_status", ["session_owner_id", "status"], unique=False)
        batch_op.create_index("idx_dsm_session", ["session_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_driver_session_memberships_member_driver_id"),
            ["member_driver_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_driver_session_memberships_session_id"),
            ["session_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_driver_session_memberships_session_owner_id"),
            ["session_owner_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_driver_session_memberships_status"),
            ["status"],
            unique=False,
        )
        # Partial unique index: at most one ACTIVE membership per driver
        batch_op.create_index(
            "uq_dsm_member_active",
            ["member_driver_id"],
            unique=True,
            postgresql_where=sa.text("status = 'active'"),
        )

    # Add accepted_by_driver_id to driver_bottle_session_orders
    with op.batch_alter_table("driver_bottle_session_orders", schema=None) as batch_op:
        batch_op.add_column(sa.Column("accepted_by_driver_id", sa.Integer(), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_driver_bottle_session_orders_accepted_by_driver_id"),
            ["accepted_by_driver_id"],
            unique=False,
        )
        batch_op.create_foreign_key(
            "fk_dbso_accepted_by_driver",
            "users",
            ["accepted_by_driver_id"],
            ["id"],
        )


def downgrade():
    with op.batch_alter_table("driver_bottle_session_orders", schema=None) as batch_op:
        batch_op.drop_constraint("fk_dbso_accepted_by_driver", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_driver_bottle_session_orders_accepted_by_driver_id"))
        batch_op.drop_column("accepted_by_driver_id")

    with op.batch_alter_table("driver_session_memberships", schema=None) as batch_op:
        batch_op.drop_index("uq_dsm_member_active", postgresql_where=sa.text("status = 'active'"))
        batch_op.drop_index(batch_op.f("ix_driver_session_memberships_status"))
        batch_op.drop_index(batch_op.f("ix_driver_session_memberships_session_owner_id"))
        batch_op.drop_index(batch_op.f("ix_driver_session_memberships_session_id"))
        batch_op.drop_index(batch_op.f("ix_driver_session_memberships_member_driver_id"))
        batch_op.drop_index("idx_dsm_session")
        batch_op.drop_index("idx_dsm_owner_status")
        batch_op.drop_index("idx_dsm_member_status")

    op.drop_table("driver_session_memberships")
    op.execute("DROP TYPE IF EXISTS driver_session_membership_status")
