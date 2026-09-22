"""add visits, visit_stock_checks, order_confirmation_requests and the sales_agent order source

Revision ID: d4e7f9a2b6c1
Revises: c3d9e5f1a7b2
Create Date: 2026-09-08 12:00:00.000000

Phase 2a of the Sales Agent design (docs/superpowers/specs/2026-09-07-sales-agent-role-design.md).
Hand-written so every constraint is named. Column adds on the two hot legacy
tables (orders, products) go through batch_alter_table, following
e7b1c2d3f4a5. The order-source CHECK swap is Postgres-only and dialect-guarded:
SQLite test databases are built from the models by db.create_all() and never
carry that constraint.

Tables added:
  * visits                        — the agent's visit state machine
  * visit_stock_checks            — one row per product counted at a visit
  * order_confirmation_requests   — the store owner's Confirm/Decline request

Columns added:
  * orders.visit_id               — FK + partial unique (at most one order per visit)
  * products.in_sales_stock_check — D22, the admin-chosen stock-check list

Constraint widened:
  * ck_orders_staff_creator_for_staff_source (originally a6c1b9d0e201) now reads
    order_source NOT IN ('phone', 'admin', 'sales_agent') OR created_by_staff_id IS NOT NULL

Rollback strategy:
    downgrade() restores the two-source CHECK first — narrowing it is always
    safe, because a 'sales_agent' row simply stops being required to carry a
    creator. It then drops orders.visit_id (partial unique index and FK first,
    so the visits table can be dropped afterwards), products.in_sales_stock_check,
    and finally order_confirmation_requests, visit_stock_checks and visits in FK
    order. Visit rows, stock checks, confirmation requests and the order<->visit
    link are LOST on downgrade — acceptable before go-live and recorded in the
    deploy runbook. Orders and products themselves are untouched.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "d4e7f9a2b6c1"
down_revision = "c3d9e5f1a7b2"
branch_labels = None
depends_on = None

VISIT_STATUSES = ("in_progress", "completed", "abandoned")
VISIT_STEPS = ("checkin", "stock", "order", "close")
VISIT_OUTCOMES = ("order_placed", "no_order", "closed", "owner_absent", "refused")
NO_ORDER_REASONS = ("sufficient_stock", "cash_issue", "price", "competitor", "other")
RATE_SOURCES = ("stock_checks", "orders", "none")
CONFIRMATION_STATUSES = ("pending", "confirmed", "declined", "expired")

# Mirrors business_app/utils/state_validators.py::STAFF_ORDER_SOURCES.
STAFF_ORDER_SOURCES = ("phone", "admin", "sales_agent")
STAFF_ORDER_SOURCES_BEFORE = ("phone", "admin")


def _quoted(values):
    return ", ".join(f"'{v}'" for v in values)


def _staff_creator_check(sources):
    return f"order_source NOT IN ({_quoted(sources)}) OR created_by_staff_id IS NOT NULL"


def upgrade():
    op.create_table(
        "visits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("outlet_id", sa.Integer(), sa.ForeignKey("outlets.id", name="fk_visits_outlet_id"), nullable=False),
        sa.Column(
            "agent_user_id", sa.Integer(), sa.ForeignKey("users.id", name="fk_visits_agent_user_id"), nullable=False
        ),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="in_progress"),
        sa.Column("planned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("current_step", sa.String(length=20), nullable=False, server_default="checkin"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("checkin_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checkin_latitude", sa.Float(), nullable=True),
        sa.Column("checkin_longitude", sa.Float(), nullable=True),
        sa.Column("checkin_accuracy_m", sa.Float(), nullable=True),
        sa.Column("distance_m", sa.Float(), nullable=True),
        sa.Column("in_radius", sa.Boolean(), nullable=True),
        sa.Column("checkin_skipped", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(length=30), nullable=True),
        sa.Column("no_order_reason", sa.String(length=30), nullable=True),
        sa.Column("dm_present", sa.Boolean(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("next_visit_at", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"status IN ({_quoted(VISIT_STATUSES)})", name="ck_visits_status"),
        sa.CheckConstraint(f"current_step IN ({_quoted(VISIT_STEPS)})", name="ck_visits_current_step"),
        sa.CheckConstraint(
            f"outcome IS NULL OR outcome IN ({_quoted(VISIT_OUTCOMES)})",
            name="ck_visits_outcome",
        ),
        sa.CheckConstraint(
            f"no_order_reason IS NULL OR no_order_reason IN ({_quoted(NO_ORDER_REASONS)})",
            name="ck_visits_no_order_reason",
        ),
        sa.CheckConstraint(
            "(checkin_latitude IS NULL) = (checkin_longitude IS NULL)",
            name="ck_visits_checkin_coords_pair",
        ),
    )
    op.create_index("ix_visits_outlet_id", "visits", ["outlet_id"])
    op.create_index("ix_visits_agent_user_id", "visits", ["agent_user_id"])
    op.create_index("ix_visits_started_at", "visits", ["started_at"])
    # At most one OPEN visit per agent. Partial so completed/abandoned visits
    # never block the next one. sqlite_where mirrors postgresql_where because
    # SQLite would otherwise drop the WHERE clause and turn this into a FULL
    # unique on agent_user_id (the driver_bottle_sessions precedent).
    op.create_index(
        "uq_visits_one_open_per_agent",
        "visits",
        ["agent_user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'in_progress'"),
        sqlite_where=sa.text("status = 'in_progress'"),
    )

    op.create_table(
        "visit_stock_checks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "visit_id", sa.Integer(), sa.ForeignKey("visits.id", name="fk_visit_stock_checks_visit_id"), nullable=False
        ),
        sa.Column(
            "product_id",
            sa.Integer(),
            sa.ForeignKey("products.id", name="fk_visit_stock_checks_product_id"),
            nullable=False,
        ),
        sa.Column("on_hand_qty", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("empties_qty", sa.Integer(), nullable=True),
        sa.Column("is_sold_out", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_low", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("suggested_qty", sa.Integer(), nullable=True),
        sa.Column("accepted_qty", sa.Integer(), nullable=True),
        sa.Column("rate_per_day", sa.Numeric(precision=8, scale=3), nullable=True),
        sa.Column("rate_source", sa.String(length=20), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("visit_id", "product_id", name="uq_visit_stock_checks_visit_product"),
        sa.CheckConstraint(
            f"rate_source IS NULL OR rate_source IN ({_quoted(RATE_SOURCES)})",
            name="ck_visit_stock_checks_rate_source",
        ),
    )
    op.create_index("ix_visit_stock_checks_visit_id", "visit_stock_checks", ["visit_id"])
    op.create_index("ix_visit_stock_checks_product_id", "visit_stock_checks", ["product_id"])

    op.create_table(
        "order_confirmation_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "order_id",
            sa.Integer(),
            sa.ForeignKey("orders.id", name="fk_order_confirmation_requests_order_id"),
            nullable=False,
        ),
        sa.Column(
            "outlet_id",
            sa.Integer(),
            sa.ForeignKey("outlets.id", name="fk_order_confirmation_requests_outlet_id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response_channel", sa.String(length=20), nullable=True),
        sa.Column("decline_reason", sa.Text(), nullable=True),
        sa.Column("request_id", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("request_id", name="uq_order_confirmation_requests_request_id"),
        sa.CheckConstraint(
            f"status IN ({_quoted(CONFIRMATION_STATUSES)})",
            name="ck_order_confirmation_requests_status",
        ),
    )
    op.create_index("ix_order_confirmation_requests_order_id", "order_confirmation_requests", ["order_id"])
    op.create_index("ix_order_confirmation_requests_status", "order_confirmation_requests", ["status"])
    op.create_index("ix_order_confirmation_requests_expires_at", "order_confirmation_requests", ["expires_at"])

    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.add_column(sa.Column("visit_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key("fk_orders_visit_id", "visits", ["visit_id"], ["id"])
    op.create_index(
        "uq_orders_visit_id",
        "orders",
        ["visit_id"],
        unique=True,
        postgresql_where=sa.text("visit_id IS NOT NULL"),
        sqlite_where=sa.text("visit_id IS NOT NULL"),
    )

    with op.batch_alter_table("products", schema=None) as batch_op:
        batch_op.add_column(sa.Column("in_sales_stock_check", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.create_index(batch_op.f("ix_products_in_sales_stock_check"), ["in_sales_stock_check"], unique=False)

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.drop_constraint("ck_orders_staff_creator_for_staff_source", "orders", type_="check")
        op.create_check_constraint(
            "ck_orders_staff_creator_for_staff_source",
            "orders",
            _staff_creator_check(STAFF_ORDER_SOURCES),
        )


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.drop_constraint("ck_orders_staff_creator_for_staff_source", "orders", type_="check")
        op.create_check_constraint(
            "ck_orders_staff_creator_for_staff_source",
            "orders",
            _staff_creator_check(STAFF_ORDER_SOURCES_BEFORE),
        )

    with op.batch_alter_table("products", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_products_in_sales_stock_check"))
        batch_op.drop_column("in_sales_stock_check")

    op.drop_index(
        "uq_orders_visit_id",
        table_name="orders",
        postgresql_where=sa.text("visit_id IS NOT NULL"),
        sqlite_where=sa.text("visit_id IS NOT NULL"),
    )
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.drop_constraint("fk_orders_visit_id", type_="foreignkey")
        batch_op.drop_column("visit_id")

    for name in (
        "ix_order_confirmation_requests_expires_at",
        "ix_order_confirmation_requests_status",
        "ix_order_confirmation_requests_order_id",
    ):
        op.drop_index(name, table_name="order_confirmation_requests")
    op.drop_table("order_confirmation_requests")

    op.drop_index("ix_visit_stock_checks_product_id", table_name="visit_stock_checks")
    op.drop_index("ix_visit_stock_checks_visit_id", table_name="visit_stock_checks")
    op.drop_table("visit_stock_checks")

    op.drop_index(
        "uq_visits_one_open_per_agent",
        table_name="visits",
        postgresql_where=sa.text("status = 'in_progress'"),
        sqlite_where=sa.text("status = 'in_progress'"),
    )
    for name in ("ix_visits_started_at", "ix_visits_agent_user_id", "ix_visits_outlet_id"):
        op.drop_index(name, table_name="visits")
    op.drop_table("visits")
