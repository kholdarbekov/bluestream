"""sales-agent pay v5: commission tiers per product (D-BANDS); commission lines carry no amount

Revision ID: c5d8e2f1a7b3
Revises: b7e3c1a95d42
Create Date: 2026-10-05 12:00:00.000000

Spec: docs/superpowers/specs/2026-09-28-sales-agent-compensation-design.md §3.5b.
Hand-written so every constraint is named (CONVENTIONS.md §5).

sales_pay_plan_rates becomes one row per TIER: + from_unit (backfilled 1), product_id nullable
(NULL = the version's default schedule), - uq_..._version_product, + uq_..._version_product_from,
+ uq_..._version_default_from (partial unique index), + ck_..._from_unit. Each version's
default_rate_mode/value is copied into a product_id NULL, from_unit 1 row; then the two columns and
their CHECKs are dropped, so the rate has one home. Every v4 rate becomes a single-tier schedule.
sales_pay_ledger_lines: amount nullable; commission kinds store none (ck_..._amount_kind replaces
ck_..._amount_sign); ck_..._cause no longer allows 'plan_changed' and allows 'units_reduced'
and 'units_restored' (OQ-B3: units follow decreases).

Refuses to run while any commission ledger line or any statement exists: v5 changes what a
commission line means and translates nothing. Dev on 2026-10-05: 0 and 0. Prod runs b7e3c1a95d42 and
this revision in one step, so both are 0 there. Dev remediation: spec §11.2 step 0.

Rollback strategy:
    downgrade() is LOSSY: each multi-tier schedule keeps only its first tier (from_unit 1), which becomes
    the flat rate again. It refuses while any commission ledger line exists (v4 needs an amount on each)
    or any statement exists (v4 code cannot read calculator-version-2 inputs). With no statement, the
    commission lines are trial data the sync rebuilds: spec §11.5 deletes them first. Prefer a code
    rollback (§11.5).
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c5d8e2f1a7b3"
down_revision = "b7e3c1a95d42"
branch_labels = None
depends_on = None

# Frozen copies (migrations never import live tuples).
RATE_MODES = ("per_unit", "percent")
REVERSAL_CAUSES = ("not_delivered", "nothing_received")
V4_DIFFERENCE_CAUSES = ("received_reduced", "received_restored", "plan_changed")
V5_DIFFERENCE_CAUSES = ("received_reduced", "received_restored", "units_reduced", "units_restored")

R, V, L = "sales_pay_plan_rates", "sales_pay_plan_versions", "sales_pay_ledger_lines"
V4_AMOUNT_SIGN = (
    "(kind = 'commission_credit' AND amount >= 0) OR (kind = 'commission_reversal' AND amount <= 0) "
    "OR (kind = 'commission_difference' AND cause = 'received_reduced' AND amount < 0) "
    "OR (kind = 'commission_difference' AND cause = 'received_restored' AND amount > 0) "
    "OR (kind = 'commission_difference' AND cause = 'plan_changed' AND amount <> 0) "
    "OR (kind = 'new_outlet_bonus' AND amount >= 0)"
)
V5_AMOUNT_KIND = (
    "(kind = 'new_outlet_bonus' AND amount IS NOT NULL AND amount >= 0) "
    "OR (kind <> 'new_outlet_bonus' AND amount IS NULL)"
)
V4_DEFAULT_RATE_VALUE = (
    "default_rate_value >= 0 AND (default_rate_mode <> 'percent' OR default_rate_value <= 100) "
    "AND (default_rate_mode <> 'per_unit' OR default_rate_value = ROUND(default_rate_value, 0))"
)


def _quoted(values):
    return ", ".join(f"'{v}'" for v in values)


def _cause_rule(difference_causes):
    # `cause IS NOT NULL`, as in b7e3c1a95d42: `NULL IN (...)` is NULL, and a CHECK passes on NULL.
    return (
        "(kind = 'commission_reversal' AND cause IS NOT NULL "
        f"AND cause IN ({_quoted(REVERSAL_CAUSES)})) "
        "OR (kind = 'commission_difference' AND cause IS NOT NULL "
        f"AND cause IN ({_quoted(difference_causes)})) "
        "OR (kind IN ('commission_credit', 'new_outlet_bonus') AND cause IS NULL)"
    )


def _count(sql):
    return op.get_bind().execute(sa.text(sql)).scalar()


def _commission_lines():
    return _count(f"SELECT count(*) FROM {L} WHERE kind <> 'new_outlet_bonus'")


def upgrade():
    lines, statements = _commission_lines(), _count("SELECT count(*) FROM sales_pay_statements")
    if lines or statements:
        raise RuntimeError(
            f"pay v5: {lines} commission ledger lines and {statements} statements exist; see spec §11.2 step 0"
        )
    op.add_column(R, sa.Column("from_unit", sa.Integer(), nullable=True))
    op.execute(f"UPDATE {R} SET from_unit = 1")
    op.alter_column(R, "from_unit", nullable=False)
    op.alter_column(R, "product_id", nullable=True)
    op.drop_constraint("uq_sales_pay_plan_rates_version_product", R, type_="unique")
    op.create_unique_constraint(
        "uq_sales_pay_plan_rates_version_product_from", R, ["plan_version_id", "product_id", "from_unit"]
    )
    op.create_index(
        "uq_sales_pay_plan_rates_version_default_from",
        R,
        ["plan_version_id", "from_unit"],
        unique=True,
        postgresql_where=sa.text("product_id IS NULL"),
        sqlite_where=sa.text("product_id IS NULL"),
    )
    op.create_check_constraint("ck_sales_pay_plan_rates_from_unit", R, "from_unit >= 1")
    op.execute(
        f"INSERT INTO {R} (plan_version_id, product_id, from_unit, rate_mode, rate_value, created_at) "
        f"SELECT id, NULL, 1, default_rate_mode, default_rate_value, created_at FROM {V}"
    )
    op.drop_constraint("ck_sales_pay_plan_versions_default_rate_value", V, type_="check")
    op.drop_constraint("ck_sales_pay_plan_versions_default_rate_mode", V, type_="check")
    op.drop_column(V, "default_rate_value")
    op.drop_column(V, "default_rate_mode")

    op.drop_constraint("ck_sales_pay_ledger_lines_amount_sign", L, type_="check")
    op.drop_constraint("ck_sales_pay_ledger_lines_cause", L, type_="check")
    op.alter_column(L, "amount", nullable=True)
    op.create_check_constraint("ck_sales_pay_ledger_lines_amount_kind", L, V5_AMOUNT_KIND)
    op.create_check_constraint("ck_sales_pay_ledger_lines_cause", L, _cause_rule(V5_DIFFERENCE_CAUSES))


def downgrade():
    lines, statements = _commission_lines(), _count("SELECT count(*) FROM sales_pay_statements")
    if lines or statements:
        raise RuntimeError(
            f"pay v5 downgrade: {lines} commission ledger lines and {statements} statements exist; see spec §11.5"
        )
    op.drop_constraint("ck_sales_pay_ledger_lines_cause", L, type_="check")
    op.drop_constraint("ck_sales_pay_ledger_lines_amount_kind", L, type_="check")
    op.alter_column(L, "amount", nullable=False)
    op.create_check_constraint("ck_sales_pay_ledger_lines_cause", L, _cause_rule(V4_DIFFERENCE_CAUSES))
    op.create_check_constraint("ck_sales_pay_ledger_lines_amount_sign", L, V4_AMOUNT_SIGN)

    op.add_column(V, sa.Column("default_rate_mode", sa.String(10), nullable=True))
    op.add_column(V, sa.Column("default_rate_value", sa.Numeric(14, 2), nullable=True))
    op.execute(
        f"UPDATE {V} v SET default_rate_mode = r.rate_mode, default_rate_value = r.rate_value "
        f"FROM {R} r WHERE r.plan_version_id = v.id AND r.product_id IS NULL AND r.from_unit = 1"
    )
    op.alter_column(V, "default_rate_mode", nullable=False)
    op.alter_column(V, "default_rate_value", nullable=False)
    op.create_check_constraint(
        "ck_sales_pay_plan_versions_default_rate_mode", V, f"default_rate_mode IN ({_quoted(RATE_MODES)})"
    )
    op.create_check_constraint("ck_sales_pay_plan_versions_default_rate_value", V, V4_DEFAULT_RATE_VALUE)
    op.execute(f"DELETE FROM {R} WHERE product_id IS NULL OR from_unit > 1")  # LOSSY: first tier only
    op.drop_constraint("ck_sales_pay_plan_rates_from_unit", R, type_="check")
    op.drop_index("uq_sales_pay_plan_rates_version_default_from", table_name=R)
    op.drop_constraint("uq_sales_pay_plan_rates_version_product_from", R, type_="unique")
    op.create_unique_constraint("uq_sales_pay_plan_rates_version_product", R, ["plan_version_id", "product_id"])
    op.alter_column(R, "product_id", nullable=False)
    op.drop_column(R, "from_unit")
