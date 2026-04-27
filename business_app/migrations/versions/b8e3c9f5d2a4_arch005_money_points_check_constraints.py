"""arch005: CHECK (>= 0) constraints on money + points columns

Revision ID: b8e3c9f5d2a4
Revises: a6c1b9d0e201
Create Date: 2026-04-26 17:30:00.000000

Adds DB-level non-negativity guards on the money / points columns called out
in docs/audit/01-architecture-backend.md#arch-005. Service-layer validation
is the primary line of defence; these constraints are the backstop that
catches the bug class the audit highlighted (refund/discount maths producing
negative totals that silently persist).

Out of scope (intentionally not constrained):
  * ``loyalty_transactions.points`` — by design negative for redemptions.
  * ``payments.amount`` for refund **transactions** — handled at the
    ``transactions`` row, not the ``payments.amount`` summary, which
    represents the gross authorised charge and stays non-negative.
  * ``analytics_*`` rollup tables — derived figures recomputable from source.
  * Geo coords (``tryouts.latitude/longitude``).

Pre-flight: counts violations against each column. If any exist, the
migration aborts with a clear message before installing constraints — a
violation indicates a real data-integrity bug the operator needs to triage,
not something to clamp silently.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "b8e3c9f5d2a4"
down_revision = "a6c1b9d0e201"
branch_labels = None
depends_on = None


# (table, column, constraint_name) — non-negative guards.
# Constraint name follows the ``ck_<table>_<column>_nonneg`` pattern from the
# audit recommendation; matches the SQLAlchemy naming convention reserved in
# business_app/utils/db_naming.py.
_NONNEG_COLUMNS = (
    # Orders
    ("orders", "subtotal"),
    ("orders", "discount_amount"),
    ("orders", "delivery_fee"),
    ("orders", "loyalty_discount"),
    ("orders", "total_amount"),
    # Order items
    ("order_items", "unit_price"),
    ("order_items", "discount_amount"),
    ("order_items", "total_price"),
    # Payments (gross authorised charge)
    ("payments", "amount"),
    # Subscriptions
    ("subscriptions", "billing_amount"),
    ("subscriptions", "total_amount_billed"),
    # Loyalty totals (per-user aggregates — never negative).
    # Table is named ``loyalty_points`` (one row per user) — not ``loyalty_users``.
    ("loyalty_points", "current_balance"),
    ("loyalty_points", "total_earned"),
    ("loyalty_points", "total_redeemed"),
    ("loyalty_points", "total_expired"),
    # Loyalty programs (configurables — never negative)
    ("loyalty_programs", "signup_bonus"),
    ("loyalty_programs", "referral_bonus"),
    ("loyalty_programs", "birthday_bonus"),
    ("loyalty_programs", "min_redemption_points"),
    ("loyalty_programs", "uzs_per_point"),
    # Loyalty rewards (configurables)
    ("loyalty_rewards", "points_cost"),
)


def _constraint_name(table: str, column: str) -> str:
    return f"ck_{table}_{column}_nonneg"


def _abort_if_violations_exist():
    """Refuse to install constraints if rows violate any guard.

    A violation here is a data-integrity bug — silently clamping would mask
    the underlying logic error in refund / discount maths. Surface the count
    and the first 10 offending row ids per column so an operator can triage.
    """
    conn = op.get_bind()
    insp = sa.inspect(conn)
    existing_tables = set(insp.get_table_names())

    violations = {}
    for table, column in _NONNEG_COLUMNS:
        if table not in existing_tables:
            # Brand-new DB chains may not have reached the migration that
            # creates this table; nothing to violate yet.
            continue
        existing_cols = {c["name"] for c in insp.get_columns(table)}
        if column not in existing_cols:
            continue
        count = conn.execute(sa.text(f'SELECT COUNT(*) FROM "{table}" WHERE "{column}" < 0')).scalar()
        if count:
            sample = conn.execute(
                sa.text(f'SELECT id, "{column}" FROM "{table}" ' f'WHERE "{column}" < 0 ORDER BY id LIMIT 10')
            ).fetchall()
            violations[(table, column)] = (count, sample)

    if violations:
        for (table, column), (count, sample) in violations.items():
            print(f"ARCH-005 violation: {count} rows with {table}.{column} < 0")
            for row in sample:
                print(f"  {table}.id={row[0]} {column}={row[1]}")
        raise RuntimeError(
            "ARCH-005 migration aborted: existing rows violate the proposed "
            f"non-negative invariants ({list(violations)}). Investigate the "
            "service-layer bug that produced them, reconcile the rows "
            "manually, then re-run the migration. Clamping to 0 is NOT done "
            "automatically because that loses the evidence of the bug."
        )


def upgrade():
    _abort_if_violations_exist()

    for table, column in _NONNEG_COLUMNS:
        op.create_check_constraint(
            _constraint_name(table, column),
            table,
            f'"{column}" >= 0',
        )


def downgrade():
    for table, column in reversed(_NONNEG_COLUMNS):
        op.drop_constraint(
            _constraint_name(table, column),
            table,
            type_="check",
        )
