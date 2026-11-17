"""add price_changes, price_change_items, price_courtesies, price_change_notices and the order-line courtesy columns

Revision ID: 5d2e8a7c3b19
Revises: 290c1500d908
Create Date: 2026-11-16 12:00:00.000000

Spec: docs/superpowers/specs/2026-11-16-price-change-transition-design.md.

Added:
  * price_changes          one planned change of product prices; at most one pending (scheduled or announced) at
                           a time, by a unique expression index. The status matrix is CHECKed.
  * price_change_items     one product's new price and courtesy cap inside a change. UNIQUE (change, product).
  * price_courtesies       one person's old-price entry per change. UNIQUE (change, user); order_id and used_at
                           are set together.
  * price_change_notices   one announcement or reminder owed to one person; channel NULL = queued.
  * order_items.courtesy_quantity / courtesy_unit_price   the part of a line sold at the old price; NULL in
                           both, or set together with 1 <= courtesy_quantity <= quantity.

Enum columns are VARCHAR(16) with a named CHECK (no Postgres TYPE). The vocabulary tuples below are FROZEN
copies of the model's (CONVENTIONS.md); tests/integration/test_price_change_pg_constraints.py pins them equal.

Rollback strategy:
    downgrade() drops the four tables and the two order_items columns. LOSSY: every price change, courtesy
    entry and notice record, and the courtesy split of any order line (the line's `discount_amount`/`total_price`
    stay correct). No Postgres TYPE is created.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "5d2e8a7c3b19"
down_revision = "290c1500d908"
branch_labels = None
depends_on = None

CHANGES = "price_changes"
ITEMS = "price_change_items"
COURTESIES = "price_courtesies"
NOTICES = "price_change_notices"

# Frozen copies (CONVENTIONS.md): never import the live tuples into a migration.
STATUSES = ("scheduled", "announced", "applied", "cancelled")
SOURCES = ("automatic", "manager")
KINDS = ("announcement", "reminder")
CHANNELS = ("telegram", "email", "none")

PENDING = "status IN ('scheduled', 'announced')"
STATUS_MATRIX = (
    "(status = 'applied') = (applied_at IS NOT NULL) "
    "AND (status = 'cancelled') = (cancelled_at IS NOT NULL) "
    "AND (status <> 'announced' OR announcement_started_at IS NOT NULL) "
    "AND (status <> 'scheduled' OR announcement_started_at IS NULL)"
)


def _quoted(values):
    return ", ".join(f"'{v}'" for v in values)


def _user_fk(table, column, ondelete, nullable):
    return sa.Column(
        column,
        sa.Integer(),
        sa.ForeignKey("users.id", name=f"fk_{table}_{column}", ondelete=ondelete),
        nullable=nullable,
    )


def _timestamps():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade():
    op.create_table(
        CHANGES,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("announce_on", sa.Date(), nullable=False),
        sa.Column("change_on", sa.Date(), nullable=False),
        sa.Column("reminder_lead_days", sa.Integer(), nullable=False),
        sa.Column("courtesy_days", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("announcement_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reminder_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        _user_fk(CHANGES, "cancelled_by_id", "SET NULL", True),
        _user_fk(CHANGES, "created_by_id", "RESTRICT", False),
        *_timestamps(),
        sa.CheckConstraint(f"status IN ({_quoted(STATUSES)})", name=f"ck_{CHANGES}_status"),
        sa.CheckConstraint(STATUS_MATRIX, name=f"ck_{CHANGES}_status_matrix"),
        sa.CheckConstraint("change_on > announce_on", name=f"ck_{CHANGES}_dates"),
        sa.CheckConstraint("reminder_lead_days >= 1", name=f"ck_{CHANGES}_reminder_lead_days"),
        sa.CheckConstraint("courtesy_days >= 1", name=f"ck_{CHANGES}_courtesy_days"),
    )
    pending = sa.text(PENDING)
    op.create_index(
        f"uq_{CHANGES}_one_pending",
        CHANGES,
        [sa.text(f"({PENDING})")],
        unique=True,
        postgresql_where=pending,
        sqlite_where=pending,
    )

    op.create_table(
        ITEMS,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "price_change_id",
            sa.Integer(),
            sa.ForeignKey(f"{CHANGES}.id", name=f"fk_{ITEMS}_price_change_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "product_id", sa.Integer(), sa.ForeignKey("products.id", name=f"fk_{ITEMS}_product_id"), nullable=False
        ),
        sa.Column("new_price", sa.Numeric(10, 2), nullable=False),
        sa.Column("courtesy_cap", sa.Integer(), nullable=False),
        sa.Column("old_price", sa.Numeric(10, 2), nullable=True),
        sa.UniqueConstraint("price_change_id", "product_id", name=f"uq_{ITEMS}_change_product"),
        sa.CheckConstraint("new_price > 0", name=f"ck_{ITEMS}_new_price"),
        sa.CheckConstraint("courtesy_cap >= 1", name=f"ck_{ITEMS}_courtesy_cap"),
    )
    op.create_index(f"ix_{ITEMS}_product_id", ITEMS, ["product_id"])

    op.create_table(
        COURTESIES,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "price_change_id",
            sa.Integer(),
            sa.ForeignKey(f"{CHANGES}.id", name=f"fk_{COURTESIES}_price_change_id"),
            nullable=False,
        ),
        _user_fk(COURTESIES, "user_id", "RESTRICT", False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        _user_fk(COURTESIES, "granted_by_id", "SET NULL", True),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "order_id", sa.Integer(), sa.ForeignKey("orders.id", name=f"fk_{COURTESIES}_order_id"), nullable=True
        ),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("price_change_id", "user_id", name=f"uq_{COURTESIES}_change_user"),
        sa.CheckConstraint(f"source IN ({_quoted(SOURCES)})", name=f"ck_{COURTESIES}_source"),
        sa.CheckConstraint("(order_id IS NULL) = (used_at IS NULL)", name=f"ck_{COURTESIES}_used"),
        sa.CheckConstraint("source <> 'manager' OR reason IS NOT NULL", name=f"ck_{COURTESIES}_manager_reason"),
    )
    op.create_index(f"ix_{COURTESIES}_user_id", COURTESIES, ["user_id"])
    op.create_index(f"ix_{COURTESIES}_order_id", COURTESIES, ["order_id"])

    op.create_table(
        NOTICES,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "price_change_id",
            sa.Integer(),
            sa.ForeignKey(f"{CHANGES}.id", name=f"fk_{NOTICES}_price_change_id"),
            nullable=False,
        ),
        _user_fk(NOTICES, "user_id", "RESTRICT", False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("channel", sa.String(16), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.String(255), nullable=True),
        sa.UniqueConstraint("price_change_id", "user_id", "kind", name=f"uq_{NOTICES}_change_user_kind"),
        sa.CheckConstraint(f"kind IN ({_quoted(KINDS)})", name=f"ck_{NOTICES}_kind"),
        sa.CheckConstraint(f"channel IS NULL OR channel IN ({_quoted(CHANNELS)})", name=f"ck_{NOTICES}_channel"),
        sa.CheckConstraint("channel <> 'none' OR sent_at IS NULL", name=f"ck_{NOTICES}_none_unsent"),
        sa.CheckConstraint("channel IS NOT NULL OR sent_at IS NULL", name=f"ck_{NOTICES}_queued_unsent"),
    )
    op.create_index(f"ix_{NOTICES}_user_id", NOTICES, ["user_id"])

    op.add_column("order_items", sa.Column("courtesy_quantity", sa.Integer(), nullable=True))
    op.add_column("order_items", sa.Column("courtesy_unit_price", sa.Numeric(10, 2), nullable=True))
    op.create_check_constraint(
        "ck_order_items_courtesy_pair", "order_items", "(courtesy_quantity IS NULL) = (courtesy_unit_price IS NULL)"
    )
    op.create_check_constraint(
        "ck_order_items_courtesy_quantity",
        "order_items",
        "courtesy_quantity IS NULL OR (courtesy_quantity >= 1 AND courtesy_quantity <= quantity)",
    )


def downgrade():
    op.drop_constraint("ck_order_items_courtesy_quantity", "order_items", type_="check")
    op.drop_constraint("ck_order_items_courtesy_pair", "order_items", type_="check")
    op.drop_column("order_items", "courtesy_unit_price")
    op.drop_column("order_items", "courtesy_quantity")
    op.drop_table(NOTICES)
    op.drop_table(COURTESIES)
    op.drop_table(ITEMS)
    op.drop_table(CHANGES)
