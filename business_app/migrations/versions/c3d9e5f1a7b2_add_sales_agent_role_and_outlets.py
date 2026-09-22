"""add sales_agent role, sales_agent_profiles, outlets, outlet_contacts, outlet_stage_history

Revision ID: c3d9e5f1a7b2
Revises: a1b7c3d9e5f2
Create Date: 2026-09-08 09:00:00.000000

Phase 1 of the Sales Agent design (docs/superpowers/specs/2026-09-07-sales-agent-role-design.md).
Hand-written so every constraint is named. The enum ADD VALUE is dialect-guarded
(SQLite test databases have no enum type); it runs in the migration's own
transaction, which Postgres 12+ allows because the new value is not used here.

Rollback strategy:
    downgrade() drops outlet_stage_history, outlet_contacts, outlets and
    sales_agent_profiles in FK order (phase-1 rows are lost — acceptable before
    go-live and recorded in the deploy runbook), remaps users.role='sales_agent'
    to 'customer', strips 'sales_agent' from users.staff_roles, then recreates
    the user_role enum without the value by rename-swap. DROP TYPE fails loudly
    if another column still depends on the old type. users.role carries no
    server default (see 06f202673824), so DROP DEFAULT is a no-op safety.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "c3d9e5f1a7b2"
down_revision = "a1b7c3d9e5f2"
branch_labels = None
depends_on = None

OUTLET_TYPES = ("grocery_store", "workplace", "individual")
OUTLET_STAGES = ("prospect", "trial", "activation_requested", "active", "at_risk", "dormant", "lost")


def _quoted(values):
    return ", ".join(f"'{v}'" for v in values)


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'sales_agent'")

    op.create_table(
        "sales_agent_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id", name="fk_sales_agent_profiles_user_id"), nullable=False
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("districts", sa.JSON(), nullable=False),
        sa.Column("weekly_new_outlet_target", sa.Integer(), nullable=True),
        sa.Column("employment_type", sa.String(length=20), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", name="fk_sales_agent_profiles_created_by"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", name="uq_sales_agent_profiles_user_id"),
    )
    op.create_index("ix_sales_agent_profiles_is_active", "sales_agent_profiles", ["is_active"])

    op.create_table(
        "outlets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("outlet_type", sa.String(length=20), nullable=False),
        sa.Column("channel", sa.String(length=30), nullable=True),
        sa.Column("stage", sa.String(length=30), nullable=False, server_default="prospect"),
        sa.Column("class", sa.String(length=1), nullable=True),
        sa.Column("cadence_days_override", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", name="fk_outlets_user_id"), nullable=True),
        sa.Column(
            "address_id", sa.Integer(), sa.ForeignKey("addresses.id", name="fk_outlets_address_id"), nullable=True
        ),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("address_text", sa.Text(), nullable=True),
        sa.Column("district", sa.String(length=100), nullable=True),
        sa.Column(
            "assigned_agent_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", name="fk_outlets_assigned_agent_user_id"),
            nullable=True,
        ),
        sa.Column(
            "onboarded_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", name="fk_outlets_onboarded_by_user_id"),
            nullable=True,
        ),
        sa.Column("next_visit_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("agent_next_visit_at", sa.Date(), nullable=True),
        sa.Column("last_visit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_order_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opening_hours", sa.JSON(), nullable=True),
        sa.Column("preferred_visit_window", sa.String(length=50), nullable=True),
        sa.Column("delivery_window_start", sa.Time(), nullable=True),
        sa.Column("delivery_window_end", sa.Time(), nullable=True),
        sa.Column("payment_terms", sa.String(length=20), nullable=False, server_default="cash"),
        sa.Column("legal_form", sa.String(length=30), nullable=True),
        sa.Column("tax_id", sa.String(length=20), nullable=True),
        sa.Column("preferred_language", sa.String(length=5), nullable=False, server_default="uz"),
        sa.Column("storefront_photo_path", sa.String(length=500), nullable=True),
        sa.Column("competitor_note", sa.Text(), nullable=True),
        sa.Column("status_warning", sa.Text(), nullable=True),
        sa.Column("dedupe_candidates", sa.JSON(), nullable=False),
        sa.Column("activation_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "approved_by_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", name="fk_outlets_approved_by_user_id"),
            nullable=True,
        ),
        sa.Column("rejected_reason", sa.Text(), nullable=True),
        sa.Column("lost_reason", sa.String(length=30), nullable=True),
        sa.Column("lost_note", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", name="uq_outlets_user_id"),
        sa.UniqueConstraint("address_id", name="uq_outlets_address_id"),
        sa.CheckConstraint(f"outlet_type IN ({_quoted(OUTLET_TYPES)})", name="ck_outlets_outlet_type"),
        sa.CheckConstraint(f"stage IN ({_quoted(OUTLET_STAGES)})", name="ck_outlets_stage"),
        sa.CheckConstraint("\"class\" IS NULL OR \"class\" IN ('A', 'B', 'C')", name="ck_outlets_class"),
        sa.CheckConstraint("(latitude IS NULL) = (longitude IS NULL)", name="ck_outlets_coords_pair"),
    )
    op.create_index("ix_outlets_stage", "outlets", ["stage"])
    op.create_index("ix_outlets_assigned_agent_user_id", "outlets", ["assigned_agent_user_id"])
    op.create_index("ix_outlets_onboarded_by_user_id", "outlets", ["onboarded_by_user_id"])
    op.create_index("ix_outlets_next_visit_due_at", "outlets", ["next_visit_due_at"])

    op.create_table(
        "outlet_contacts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "outlet_id", sa.Integer(), sa.ForeignKey("outlets.id", name="fk_outlet_contacts_outlet_id"), nullable=False
        ),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("phone", sa.String(length=20), nullable=True),
        sa.Column("role", sa.String(length=20), nullable=False, server_default="owner"),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("presence_window", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_outlet_contacts_outlet_id", "outlet_contacts", ["outlet_id"])
    op.create_index("ix_outlet_contacts_phone", "outlet_contacts", ["phone"])

    op.create_table(
        "outlet_stage_history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "outlet_id",
            sa.Integer(),
            sa.ForeignKey("outlets.id", name="fk_outlet_stage_history_outlet_id"),
            nullable=False,
        ),
        sa.Column("from_stage", sa.String(length=30), nullable=True),
        sa.Column("to_stage", sa.String(length=30), nullable=False),
        sa.Column("reason_code", sa.String(length=30), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "actor_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", name="fk_outlet_stage_history_actor_user_id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_outlet_stage_history_outlet_id", "outlet_stage_history", ["outlet_id"])


def downgrade():
    op.drop_index("ix_outlet_stage_history_outlet_id", table_name="outlet_stage_history")
    op.drop_table("outlet_stage_history")
    op.drop_index("ix_outlet_contacts_phone", table_name="outlet_contacts")
    op.drop_index("ix_outlet_contacts_outlet_id", table_name="outlet_contacts")
    op.drop_table("outlet_contacts")
    for name in (
        "ix_outlets_next_visit_due_at",
        "ix_outlets_onboarded_by_user_id",
        "ix_outlets_assigned_agent_user_id",
        "ix_outlets_stage",
    ):
        op.drop_index(name, table_name="outlets")
    op.drop_table("outlets")
    op.drop_index("ix_sales_agent_profiles_is_active", table_name="sales_agent_profiles")
    op.drop_table("sales_agent_profiles")

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("UPDATE users SET role = 'customer' WHERE role = 'sales_agent'")
    op.execute(
        "UPDATE users SET staff_roles = COALESCE("
        "(SELECT json_agg(r) FROM json_array_elements_text(staff_roles) AS r WHERE r <> 'sales_agent'), '[]'::json) "
        "WHERE staff_roles::text LIKE '%sales_agent%'"
    )
    op.execute("ALTER TABLE users ALTER COLUMN role DROP DEFAULT")
    op.execute("ALTER TYPE user_role RENAME TO user_role_old")
    op.execute("CREATE TYPE user_role AS ENUM ('customer', 'admin', 'manager', 'delivery_driver', 'operator')")
    op.execute("ALTER TABLE users ALTER COLUMN role TYPE user_role USING role::text::user_role")
    op.execute("DROP TYPE user_role_old")
