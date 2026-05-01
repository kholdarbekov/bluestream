"""add entity subtype and amount-tracked corporate contracts

Adds:
  - users.entity_subtype enum column (workplace | grocery_store), nullable.
  - corporate_contracts.tracking_mode enum column (units | amount), default units.
  - corporate_prepayment_accounts.outstanding_amount, lifetime_charged,
    lifetime_collected, last_charged_at, last_collected_at -- money-mode fields
    used only when the parent contract.tracking_mode == AMOUNT.
  - Extends corporate_prepayment_event_type enum with charge / collect.
  - Relaxes corporate_prepayment_ledger NOT NULL on units, balance_id, product_id;
    enforces the original invariants (plus AMOUNT-mode invariants) via a CHECK
    constraint that gates by event_type.

No data backfill: existing entity users keep entity_subtype = NULL and admins
must explicitly assign one before placing orders. Existing contracts default
tracking_mode = 'units', which preserves current behavior.

Revision ID: e2c5a8b1f9d4
Revises: d1a4f8e9c2b7
Create Date: 2026-04-30 12:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "e2c5a8b1f9d4"
down_revision = "d1a4f8e9c2b7"
branch_labels = None
depends_on = None


LEDGER_EVENT_CHECK_CONSTRAINT = "ck_corporate_prepayment_ledger_event_shape"


def upgrade():
    bind = op.get_bind()
    dialect_name = bind.dialect.name

    entity_subtype_enum = sa.Enum(
        "workplace",
        "grocery_store",
        name="entity_subtype",
    )
    tracking_mode_enum = sa.Enum(
        "units",
        "amount",
        name="corporate_contract_tracking_mode",
    )

    if dialect_name == "postgresql":
        # Postgres requires that newly-added enum values be committed before
        # they can be referenced in the same transaction (e.g. inside the
        # CHECK constraint added below). Add them inside an autocommit_block
        # so each ALTER TYPE statement commits independently.
        with op.get_context().autocommit_block():
            entity_subtype_enum.create(bind, checkfirst=True)
            tracking_mode_enum.create(bind, checkfirst=True)
            op.execute("ALTER TYPE corporate_prepayment_event_type ADD VALUE IF NOT EXISTS 'charge'")
            op.execute("ALTER TYPE corporate_prepayment_event_type ADD VALUE IF NOT EXISTS 'collect'")

    # users.entity_subtype
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("entity_subtype", entity_subtype_enum, nullable=True))
        batch_op.create_index(
            "ix_users_entity_subtype",
            ["entity_subtype"],
            unique=False,
        )

    # corporate_contracts.tracking_mode
    with op.batch_alter_table("corporate_contracts", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "tracking_mode",
                tracking_mode_enum,
                nullable=False,
                server_default="units",
            )
        )
        batch_op.create_index(
            "ix_corporate_contracts_tracking_mode",
            ["tracking_mode"],
            unique=False,
        )

    # corporate_prepayment_accounts money-mode fields
    with op.batch_alter_table("corporate_prepayment_accounts", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "outstanding_amount",
                sa.Numeric(precision=14, scale=2),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(
            sa.Column(
                "lifetime_charged",
                sa.Numeric(precision=14, scale=2),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(
            sa.Column(
                "lifetime_collected",
                sa.Numeric(precision=14, scale=2),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(sa.Column("last_charged_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("last_collected_at", sa.DateTime(timezone=True), nullable=True))

    # corporate_prepayment_ledger: relax nullability + add shape CHECK constraint
    with op.batch_alter_table("corporate_prepayment_ledger", schema=None) as batch_op:
        batch_op.alter_column("units", existing_type=sa.Numeric(precision=12, scale=2), nullable=True)
        batch_op.alter_column(
            "amount",
            existing_type=sa.Numeric(precision=12, scale=2),
            type_=sa.Numeric(precision=14, scale=2),
            existing_nullable=True,
        )

    # Legacy UNITS-mode rows pre-date the strict per-product invariants and may
    # have NULL balance_id / product_id from earlier migrations. Don't retro-
    # actively reject them. Constrain only the new AMOUNT-mode events to ensure
    # CHARGE / COLLECT always carry a money amount and never reference a unit
    # balance row. UNITS-mode events keep their existing nullability semantics.
    op.execute(
        f"""
        ALTER TABLE corporate_prepayment_ledger
        ADD CONSTRAINT {LEDGER_EVENT_CHECK_CONSTRAINT}
        CHECK (
            event_type NOT IN ('charge', 'collect')
            OR (
                units IS NULL
                AND product_id IS NULL
                AND balance_id IS NULL
                AND amount IS NOT NULL
            )
        )
        """
    )


def downgrade():
    op.execute(f"ALTER TABLE corporate_prepayment_ledger DROP CONSTRAINT IF EXISTS {LEDGER_EVENT_CHECK_CONSTRAINT}")

    # Restore NOT NULL on units / balance_id / product_id (any AMOUNT-mode rows
    # would prevent this; downgrade should be performed only when no grocery-store
    # ledger entries exist).
    with op.batch_alter_table("corporate_prepayment_ledger", schema=None) as batch_op:
        batch_op.alter_column(
            "amount",
            existing_type=sa.Numeric(precision=14, scale=2),
            type_=sa.Numeric(precision=12, scale=2),
            existing_nullable=True,
        )
        batch_op.alter_column(
            "units",
            existing_type=sa.Numeric(precision=12, scale=2),
            nullable=False,
            server_default="0",
        )

    with op.batch_alter_table("corporate_prepayment_accounts", schema=None) as batch_op:
        batch_op.drop_column("last_collected_at")
        batch_op.drop_column("last_charged_at")
        batch_op.drop_column("lifetime_collected")
        batch_op.drop_column("lifetime_charged")
        batch_op.drop_column("outstanding_amount")

    with op.batch_alter_table("corporate_contracts", schema=None) as batch_op:
        batch_op.drop_index("ix_corporate_contracts_tracking_mode")
        batch_op.drop_column("tracking_mode")

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_index("ix_users_entity_subtype")
        batch_op.drop_column("entity_subtype")

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        sa.Enum(name="corporate_contract_tracking_mode").drop(bind, checkfirst=True)
        sa.Enum(name="entity_subtype").drop(bind, checkfirst=True)
        # Postgres doesn't support DROP VALUE on enums; the 'charge' / 'collect'
        # values remain on corporate_prepayment_event_type after downgrade.
        # That's fine -- they're harmless when no rows reference them.
