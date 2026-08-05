"""place groups + allocation scope columns

Revision ID: f7c3b9e1d5a2
Revises: e5f9a3c7b1d4
Create Date: 2026-07-24 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f7c3b9e1d5a2"
down_revision = "e5f9a3c7b1d4"
branch_labels = None
depends_on = None

# scope_snapshot is JSONB on Postgres, plain JSON everywhere else (SQLite in the
# test suite). Postgres `json` has NO equality operator, so DISTINCT / GROUP BY /
# `=` over the column raise 42883 there while passing silently on SQLite. Nothing
# reads it that way today, but the type is free to fix while this migration is
# unshipped and a table rewrite once it is not. AllocationScope.to_snapshot()
# emits a plain dict and from_event() only calls .get(), so no code changes.
SCOPE_SNAPSHOT_TYPE = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")

# Every FK below points at users.id and was created NO ACTION, which makes
# CrossPlatformSyncService._transfer_user_references' terminal
# `db.session.delete(secondary_user)` abort on Postgres. Per column:
#   * customer_distinct_pairs.user_id_low/high (NOT NULL) — the row asserts
#     "these two accounts are different people". Once a participant no longer
#     exists the assertion has no subject and cannot be made NULL, so the row
#     must go with it: CASCADE.
#   * every *_admin_id / primary_user_id (all nullable) — these are audit or
#     display stamps. Deleting the audit row to preserve a stamp would be
#     backwards, so the stamp is what yields: SET NULL.
_USER_FK_ACTIONS = (
    # (constraint, table, local cols, remote table, remote cols, ondelete)
    ("fk_canonical_customers_primary_user", "canonical_customers", ["primary_user_id"], "users", ["id"], "SET NULL"),
    (
        "fk_canonical_customers_created_by_admin",
        "canonical_customers",
        ["created_by_admin_id"],
        "users",
        ["id"],
        "SET NULL",
    ),
    ("fk_customer_link_events_admin", "customer_link_events", ["acting_admin_id"], "users", ["id"], "SET NULL"),
    ("fk_customer_distinct_pairs_low", "customer_distinct_pairs", ["user_id_low"], "users", ["id"], "CASCADE"),
    ("fk_customer_distinct_pairs_high", "customer_distinct_pairs", ["user_id_high"], "users", ["id"], "CASCADE"),
    (
        "fk_customer_distinct_pairs_admin",
        "customer_distinct_pairs",
        ["dismissed_by_admin_id"],
        "users",
        ["id"],
        "SET NULL",
    ),
)


def upgrade():
    # 1. Place groups become ownerless (may span customers): the canonical FK is
    #    deprecated — nullable. Column retained only so this migration stays
    #    reversible.
    op.alter_column("address_groups", "canonical_customer_id", existing_type=sa.Integer(), nullable=True)
    # Existing rows deliberately keep their (now inert) canonical_customer_id:
    # nullable is all Phase 2 needs, the retained values keep downgrade() lossless,
    # and the retained values keep the pre-Phase-2 state recoverable. New place groups are always written NULL.

    # 2. Scope-aware allocation engine (Plan 2b consumes): the resolved scope is
    #    frozen ON the event. Existing events backfill to 'personal'/NULL via the
    #    server_default — their behavior is unchanged.
    op.add_column(
        "cash_collection_events",
        sa.Column("scope_type", sa.String(length=16), nullable=False, server_default="personal"),
    )
    op.add_column("cash_collection_events", sa.Column("scope_snapshot", SCOPE_SNAPSHOT_TYPE, nullable=True))

    # 3. Dual audit stamps. NO backfill by design: payment.user_id is mutable
    #    (ensure_cod_payment_for_order reassigns it), so stamping present-day
    #    values onto historical allocations would fabricate history. NULL is honest.
    op.add_column("cash_collection_allocations", sa.Column("source_customer_id", sa.Integer(), nullable=True))
    op.add_column("cash_collection_allocations", sa.Column("beneficiary_user_id", sa.Integer(), nullable=True))

    # 4. Place-suggestion dismiss registry (the suggestion ENGINE lands in Plan 2c).
    #    ondelete on all three FKs: "these two addresses are not the same place"
    #    is meaningless once either address is gone (CASCADE), while the admin
    #    who said so is a stamp the assertion can outlive (SET NULL).
    op.create_table(
        "place_suggestion_dismissals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "address_id_low",
            sa.Integer(),
            sa.ForeignKey("addresses.id", name="fk_place_suggestion_dismissals_low", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "address_id_high",
            sa.Integer(),
            sa.ForeignKey("addresses.id", name="fk_place_suggestion_dismissals_high", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "dismissed_by_admin_id",
            sa.Integer(),
            sa.ForeignKey("users.id", name="fk_place_suggestion_dismissals_admin", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("signal_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("address_id_low", "address_id_high", name="uq_place_suggestion_dismissals"),
    )

    # 5. orders.delivery_address_id is the join key of every place query — the
    #    ring-1 candidate scan inside the FOR UPDATE window, the COD cap check on
    #    every cash order creation, the place debtor list and correction replay —
    #    and it was unindexed. Composite with `status` because every one of those
    #    queries also filters on status. Plain (transactional) create_index, NOT
    #    CONCURRENTLY: CONCURRENTLY cannot run inside a transaction and this
    #    repo's migrations are transactional.
    op.create_index("idx_orders_delivery_address_status", "orders", ["delivery_address_id", "status"])

    # 6. Retrofit ondelete onto the users FKs of the two preceding (also
    #    unshipped) migrations. Declared HERE rather than edited into
    #    c4d8e2f1a6b3/e5f9a3c7b1d4 so dev — already at this head with live
    #    canonical-customer rows — picks the actions up from a one-step
    #    downgrade/upgrade instead of a destructive rewind past table creation.
    for name, table, local_cols, remote, remote_cols, action in _USER_FK_ACTIONS:
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(name, table, remote, local_cols, remote_cols, ondelete=action)


def downgrade():
    for name, table, local_cols, remote, remote_cols, _action in _USER_FK_ACTIONS:
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(name, table, remote, local_cols, remote_cols)

    op.drop_index("idx_orders_delivery_address_status", table_name="orders")
    op.drop_table("place_suggestion_dismissals")
    op.drop_column("cash_collection_allocations", "beneficiary_user_id")
    op.drop_column("cash_collection_allocations", "source_customer_id")
    op.drop_column("cash_collection_events", "scope_snapshot")
    op.drop_column("cash_collection_events", "scope_type")
    # Ownerless (NULL-canonical) groups cannot survive the NOT NULL restore:
    # detach their member addresses, then delete them, then tighten the column.
    op.execute(
        "UPDATE addresses SET address_group_id = NULL WHERE address_group_id IN "
        "(SELECT id FROM address_groups WHERE canonical_customer_id IS NULL)"
    )
    op.execute("DELETE FROM address_groups WHERE canonical_customer_id IS NULL")
    op.alter_column("address_groups", "canonical_customer_id", existing_type=sa.Integer(), nullable=False)
