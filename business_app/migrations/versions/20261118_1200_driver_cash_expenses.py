"""add driver_cash_expenses (a driver's claim for cash spent on a trip)

Revision ID: 3d8f5a1c7e92
Revises: 7b3f1c9e4a62
Create Date: 2026-11-18 12:00:00.000000

Spec: docs/superpowers/specs/2026-11-17-driver-cash-expenses-design.md.

Added:
  * driver_cash_expenses  one row per claim. pending -> approved | rejected | withdrawn; an approved claim
                          can be undone back to pending. The CHECKs pin which columns each status needs.

The three tuples below are FROZEN copies of shared.staff_constants.DRIVER_EXPENSE_CATEGORIES,
DRIVER_EXPENSE_NOTE_REQUIRED_CATEGORIES and DRIVER_EXPENSE_STATUSES (CONVENTIONS.md: never import a live tuple
into a migration); tests/integration/test_driver_cash_expense_pg_constraints.py pins them equal.

Rollback strategy:
    downgrade() refuses (RuntimeError) while driver_cash_expenses holds any row: an approved claim lowers the
    cash on hand of its trip, and the code this rolls back to knows no claims, so deleting them would turn that
    money back into cash the driver still holds. Export the table and settle or reverse every approved claim in
    the admin UI first, then delete the rows. With none left, downgrade() drops the table.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "3d8f5a1c7e92"
down_revision = "7b3f1c9e4a62"
branch_labels = None
depends_on = None

T = "driver_cash_expenses"

# Frozen copies (CONVENTIONS.md): never import the live tuples into a migration.
_CATEGORIES = ("fuel", "new_bottles", "caps", "labels", "other_supplies", "vehicle", "parking_wash", "other")
_NOTE_REQUIRED = ("other",)
_STATUSES = ("pending", "approved", "rejected", "withdrawn")


def _quoted(values):
    return ", ".join(f"'{v}'" for v in values)


def _user_fk(column, *, nullable):
    return sa.Column(column, sa.Integer(), sa.ForeignKey("users.id", name=f"fk_{T}_{column}"), nullable=nullable)


def upgrade():
    op.create_table(
        T,
        sa.Column("id", sa.Integer(), primary_key=True),
        _user_fk("driver_user_id", nullable=False),
        sa.Column(
            "driver_cash_session_id",
            sa.Integer(),
            sa.ForeignKey("driver_cash_sessions.id", name=f"fk_{T}_driver_cash_session_id"),
            nullable=False,
        ),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column("receipt_file_id", sa.String(255), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("approved_amount", sa.Numeric(12, 2), nullable=True),
        _user_fk("decided_by_user_id", nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_reason", sa.String(500), nullable=True),
        sa.Column("self_decided", sa.Boolean(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount > 0", name=f"ck_{T}_amount_positive"),
        sa.CheckConstraint(f"category IN ({_quoted(_CATEGORIES)})", name=f"ck_{T}_category"),
        sa.CheckConstraint(
            f"category NOT IN ({_quoted(_NOTE_REQUIRED)}) OR (note IS NOT NULL AND length(trim(note)) > 0)",
            name=f"ck_{T}_note_required",
        ),
        sa.CheckConstraint(f"status IN ({_quoted(_STATUSES)})", name=f"ck_{T}_status"),
        sa.CheckConstraint(
            "(status = 'approved' AND approved_amount IS NOT NULL AND approved_amount > 0 "
            "AND approved_amount <= amount) "
            "OR (status <> 'approved' AND approved_amount IS NULL)",
            name=f"ck_{T}_approved_amount",
        ),
        sa.CheckConstraint(
            "(status IN ('approved', 'rejected') AND decided_by_user_id IS NOT NULL AND decided_at IS NOT NULL) "
            "OR (status NOT IN ('approved', 'rejected') AND decided_by_user_id IS NULL AND decided_at IS NULL)",
            name=f"ck_{T}_decided",
        ),
        sa.CheckConstraint(
            "status <> 'rejected' OR (decision_reason IS NOT NULL AND length(trim(decision_reason)) > 0)",
            name=f"ck_{T}_rejected_reason",
        ),
        sa.CheckConstraint(
            "(status = 'withdrawn' AND withdrawn_at IS NOT NULL) "
            "OR (status <> 'withdrawn' AND withdrawn_at IS NULL)",
            name=f"ck_{T}_withdrawn",
        ),
        sa.CheckConstraint(
            "status <> 'pending' OR (approved_amount IS NULL AND decided_by_user_id IS NULL "
            "AND decided_at IS NULL AND decision_reason IS NULL AND withdrawn_at IS NULL "
            "AND NOT self_decided)",
            name=f"ck_{T}_pending_clean",
        ),
        sa.UniqueConstraint("driver_user_id", "idempotency_key", name=f"uq_{T}_driver_idempotency_key"),
    )
    op.create_index(f"ix_{T}_status_submitted", T, ["status", "submitted_at"])
    op.create_index(f"ix_{T}_session_status", T, ["driver_cash_session_id", "status"])
    op.create_index(f"ix_{T}_driver_status", T, ["driver_user_id", "status"])


def downgrade():
    rows = op.get_bind().execute(sa.text(f"SELECT count(*) FROM {T}")).scalar()
    if rows:
        raise RuntimeError(
            f"driver cash expenses downgrade: {rows} claim rows exist; export and delete {T} first "
            "(see this migration's rollback strategy)"
        )
    op.drop_table(T)
