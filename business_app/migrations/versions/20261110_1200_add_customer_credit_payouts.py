"""add customer_credit_payouts and customer_credit_payout_debits (prepayment return)

Revision ID: 5e2b9c71d4a8
Revises: a7c41e9b2d05
Create Date: 2026-11-10 12:00:00.000000

Spec: docs/superpowers/specs/2026-11-10-prepayment-payout-design.md.

Added:
  * customer_credit_payouts        one row per return of a customer's free prepaid credit as money.
                                   Live iff voided_at IS NULL. A payer-shape CHECK pins which of
                                   driver_user_id / driver_cash_session_id each paid_by requires.
  * customer_credit_payout_debits  the part of each credit event a payout drew; carries the two
                                   classification booleans (revenue_counted, card_click).

The users FKs customer_id and driver_user_id are ON DELETE RESTRICT: users are hard-deleted only by
merges, which re-point these rows first, so a missed re-point fails loudly instead of erasing money
history. The paid_by tuple below is a FROZEN copy of CustomerCreditPayout.PAID_BY_VALUES;
tests/integration/test_customer_credit_payout_pg_constraints.py pins them equal.

Rollback strategy:
    downgrade() refuses (RuntimeError) while customer_credit_payouts holds any row, live or undone: the
    code it rolls back to knows no payouts, so a drawn credit event and a driver session lowered by a
    return would turn back into money (the event's credit re-offered by its next correction, the
    session's on hand back to gross). Before rolling back, undo every live return in the admin UI (that
    gives the credit back to its events and the on hand back to the driver's session); a return that
    can no longer be undone (its session is checked) means rolling forward instead. Then export both
    tables and delete their rows. With none left, downgrade() drops customer_credit_payout_debits then
    customer_credit_payouts; no other table references them.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "5e2b9c71d4a8"
down_revision = "a7c41e9b2d05"
branch_labels = None
depends_on = None

PAYOUTS = "customer_credit_payouts"
DEBITS = "customer_credit_payout_debits"

# Frozen copy (CONVENTIONS.md): never import the live tuple into a migration.
PAID_BY = ("office", "driver_current_session", "driver_settled")

PAYER_SHAPE = (
    "(paid_by = 'office' AND driver_user_id IS NULL AND driver_cash_session_id IS NULL) "
    "OR (paid_by = 'driver_settled' AND driver_user_id IS NOT NULL AND driver_cash_session_id IS NULL) "
    "OR (paid_by = 'driver_current_session' AND driver_user_id IS NOT NULL AND driver_cash_session_id IS NOT NULL)"
)
VOID_SHAPE = "(voided_at IS NULL AND void_reason IS NULL) OR (voided_at IS NOT NULL AND void_reason IS NOT NULL)"


def _quoted(values):
    return ", ".join(f"'{v}'" for v in values)


def _user_fk(column, *, ondelete, nullable):
    return sa.Column(
        column,
        sa.Integer(),
        sa.ForeignKey("users.id", name=f"fk_{PAYOUTS}_{column}", ondelete=ondelete),
        nullable=nullable,
    )


def upgrade():
    op.create_table(
        PAYOUTS,
        sa.Column("id", sa.Integer(), primary_key=True),
        _user_fk("customer_id", ondelete="RESTRICT", nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("paid_by", sa.String(32), nullable=False),
        _user_fk("driver_user_id", ondelete="RESTRICT", nullable=True),
        sa.Column(
            "driver_cash_session_id",
            sa.Integer(),
            sa.ForeignKey("driver_cash_sessions.id", name=f"fk_{PAYOUTS}_driver_cash_session_id"),
            nullable=True,
        ),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=False),
        _user_fk("actor_user_id", ondelete=None, nullable=False),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
        _user_fk("voided_by_user_id", ondelete=None, nullable=True),
        sa.Column("void_reason", sa.String(500), nullable=True),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("idempotency_key", name=f"uq_{PAYOUTS}_idempotency_key"),
        sa.CheckConstraint("amount > 0", name=f"ck_{PAYOUTS}_amount_positive"),
        sa.CheckConstraint(f"paid_by IN ({_quoted(PAID_BY)})", name=f"ck_{PAYOUTS}_paid_by"),
        sa.CheckConstraint(PAYER_SHAPE, name=f"ck_{PAYOUTS}_payer_shape"),
        sa.CheckConstraint(VOID_SHAPE, name=f"ck_{PAYOUTS}_void_shape"),
    )
    op.create_index(f"ix_{PAYOUTS}_customer_id", PAYOUTS, ["customer_id"])
    op.create_index(f"ix_{PAYOUTS}_driver_cash_session_id", PAYOUTS, ["driver_cash_session_id"])
    op.create_index(f"ix_{PAYOUTS}_created_at", PAYOUTS, ["created_at"])

    op.create_table(
        DEBITS,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "payout_id",
            sa.Integer(),
            sa.ForeignKey(f"{PAYOUTS}.id", name=f"fk_{DEBITS}_payout_id"),
            nullable=False,
        ),
        sa.Column(
            "cash_collection_event_id",
            sa.Integer(),
            sa.ForeignKey("cash_collection_events.id", name=f"fk_{DEBITS}_cash_collection_event_id"),
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("revenue_counted", sa.Boolean(), nullable=False),
        sa.Column("card_click", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("payout_id", "cash_collection_event_id", name=f"uq_{DEBITS}_payout_event"),
        sa.CheckConstraint("amount > 0", name=f"ck_{DEBITS}_amount_positive"),
    )
    op.create_index(f"ix_{DEBITS}_payout_id", DEBITS, ["payout_id"])
    op.create_index(f"ix_{DEBITS}_cash_collection_event_id", DEBITS, ["cash_collection_event_id"])


def downgrade():
    payouts = op.get_bind().execute(sa.text(f"SELECT count(*) FROM {PAYOUTS}")).scalar()
    if payouts:
        raise RuntimeError(
            f"customer credit payouts downgrade: {payouts} payout rows exist; export and delete {DEBITS} and "
            f"{PAYOUTS} first (undo every live return in the admin UI before that; see this migration's "
            "rollback strategy)"
        )
    op.drop_table(DEBITS)
    op.drop_table(PAYOUTS)
