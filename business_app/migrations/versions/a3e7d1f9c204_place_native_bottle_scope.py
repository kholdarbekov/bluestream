"""place-native bottle scope: balances and ledger keyed by place, not person

Revision ID: a3e7d1f9c204
Revises: f7c3b9e1d5a2
Create Date: 2026-07-28 10:00:00.000000

Step order is load-bearing (spec section 12): every constraint is added only
after the data already satisfies it, and bottle_fines is backfilled while
bottle_balances.address_id is still populated on every row.

downgrade() is STRUCTURALLY real but LOSSY on data. It must not raise:
tests/integration/test_migrations_roundtrip.py:96 walks the whole chain down to
base on a real ephemeral Postgres database, and exists precisely to catch a
downgrade that no-ops or raises. The per-person split of a merged place balance
is genuinely unrecoverable, so the data rollback path is the dump of
bottle_balances / bottle_ledger / bottle_fines that the deploy runbook takes
immediately before `db upgrade` — not this function.
"""

import sqlalchemy as sa
from alembic import op

revision = "a3e7d1f9c204"
down_revision = "f7c3b9e1d5a2"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()

    # -- 1. nullable columns only; no constraints yet -----------------------
    op.add_column("bottle_balances", sa.Column("address_group_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_bottle_balances_address_group",
        "bottle_balances",
        "address_groups",
        ["address_group_id"],
        ["id"],
    )
    op.add_column("bottle_ledger", sa.Column("address_group_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_bottle_ledger_address_group",
        "bottle_ledger",
        "address_groups",
        ["address_group_id"],
        ["id"],
    )
    # Both ledger indexes are declared on the model (`index=True` on the column
    # plus the composite in __table_args__), so db.create_all() builds them in
    # the SQLite suite. Creating them here keeps the migrated Postgres schema
    # identical — and downgrade() drops the composite by name, which would raise
    # "index does not exist" if upgrade() never created it.
    op.create_index("ix_bottle_ledger_address_group_id", "bottle_ledger", ["address_group_id"])
    op.create_index("idx_bottle_ledger_group_occurred", "bottle_ledger", ["address_group_id", "occurred_at"])
    op.add_column("bottle_fines", sa.Column("address_id", sa.Integer(), nullable=True))
    op.add_column("bottle_fines", sa.Column("address_group_id", sa.Integer(), nullable=True))

    # -- 2. fines backfilled WHILE balances still carry address_id ----------
    conn.execute(
        sa.text(
            """
        UPDATE bottle_fines f
           SET address_id = b.address_id,
               address_group_id = a.address_group_id
          FROM bottle_balances b
          JOIN addresses a ON a.id = b.address_id
         WHERE f.bottle_balance_id = b.id
    """
        )
    )
    orphans = conn.execute(sa.text("SELECT COUNT(*) FROM bottle_fines WHERE address_id IS NULL")).scalar()
    if orphans:
        raise RuntimeError(f"{orphans} bottle_fines rows have no resolvable address; resolve before migrating")
    op.alter_column("bottle_fines", "address_id", nullable=False)
    op.create_foreign_key("fk_bottle_fines_address", "bottle_fines", "addresses", ["address_id"], ["id"])
    op.create_foreign_key(
        "fk_bottle_fines_address_group", "bottle_fines", "address_groups", ["address_group_id"], ["id"]
    )
    op.drop_index("idx_bottle_fines_balance", table_name="bottle_fines")
    op.drop_column("bottle_fines", "bottle_balance_id")
    op.create_index("idx_bottle_fines_address", "bottle_fines", ["address_id"])

    # -- 3. collapse duplicate (user_id, address_id) rows per address -------
    # Reachable today: record_standalone_collection takes customer_id and
    # address_id straight from request JSON with no ownership check
    # (api/staff.py:1008-1019), so two users can hold rows on one address.
    conn.execute(
        sa.text(
            """
        WITH keep AS (SELECT address_id, MIN(id) AS keep_id FROM bottle_balances GROUP BY address_id)
        UPDATE bottle_balances b
           SET balance = agg.total
          FROM (SELECT address_id, SUM(balance) AS total FROM bottle_balances GROUP BY address_id) agg,
               keep
         WHERE b.id = keep.keep_id AND keep.address_id = agg.address_id
    """
        )
    )
    conn.execute(
        sa.text(
            """
        DELETE FROM bottle_balances
         WHERE id NOT IN (SELECT MIN(id) FROM bottle_balances GROUP BY address_id)
    """
        )
    )

    # -- 4. drop the old key, add the new constraints ----------------------
    op.drop_constraint("uq_bottle_balance_user_address", "bottle_balances", type_="unique")
    op.drop_index("idx_bottle_balances_user", table_name="bottle_balances")
    op.drop_column("bottle_balances", "user_id")
    op.alter_column("bottle_balances", "address_id", nullable=True)
    op.create_check_constraint(
        "ck_bottle_balance_scope",
        "bottle_balances",
        "(address_group_id IS NULL) <> (address_id IS NULL)",
    )
    op.create_unique_constraint("uq_bottle_balance_group", "bottle_balances", ["address_group_id"])
    op.create_unique_constraint("uq_bottle_balance_addr", "bottle_balances", ["address_id"])
    op.create_index("idx_bottle_balances_group", "bottle_balances", ["address_group_id"])

    # -- 5. stamp the ledger scope, then merge grouped balance rows --------
    conn.execute(
        sa.text(
            """
        UPDATE bottle_ledger l
           SET address_group_id = a.address_group_id
          FROM addresses a
         WHERE a.id = l.address_id AND a.address_group_id IS NOT NULL
    """
        )
    )
    # one row per group, summing the member addresses' rows
    conn.execute(
        sa.text(
            """
        INSERT INTO bottle_balances (address_group_id, address_id, balance,
                                     last_delivery_at, last_return_at, created_at, updated_at)
        SELECT a.address_group_id, NULL, COALESCE(SUM(b.balance), 0),
               MAX(b.last_delivery_at), MAX(b.last_return_at), NOW(), NOW()
          FROM bottle_balances b
          JOIN addresses a ON a.id = b.address_id
         WHERE a.address_group_id IS NOT NULL
         GROUP BY a.address_group_id
    """
        )
    )
    conn.execute(
        sa.text(
            """
        DELETE FROM bottle_balances
         WHERE address_id IN (SELECT id FROM addresses WHERE address_group_id IS NOT NULL)
    """
        )
    )

    # -- 6. recompute balance_after per scope, deterministically -----------
    # (occurred_at, id): occurred_at alone is unstable because paired entries
    # written in one transaction share a timestamp (FINE_ISSUED qty 0 next to
    # FINE_PAID), which would make reruns non-deterministic.
    conn.execute(
        sa.text(
            """
        WITH running AS (
            SELECT id,
                   SUM(quantity) OVER (
                       PARTITION BY COALESCE(address_group_id, -address_id)
                       ORDER BY occurred_at, id
                       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                   ) AS bal
              FROM bottle_ledger
        )
        UPDATE bottle_ledger l SET balance_after = running.bal
          FROM running WHERE running.id = l.id
    """
        )
    )


def downgrade():
    """Structural rollback. LOSSY: a place's whole balance is restored onto its
    lowest-numbered member address and attributed to that address's owner,
    because the per-person split it was merged from no longer exists anywhere.

    Also LOSSY on fines: `bottle_fines.bottle_balance_id` is NOT NULL, so any
    fine left with no restorable balance row — its address has neither an
    address-keyed row nor a group whose restored row can adopt it (e.g. every
    member address of its group was deleted) — is DROPPED rather than blocking
    the rollback.

    Use the pre-upgrade dump for a real data rollback.
    """
    conn = op.get_bind()

    # -- balances: explode place rows back onto a member address -----------
    op.add_column("bottle_balances", sa.Column("user_id", sa.Integer(), nullable=True))
    op.drop_constraint("uq_bottle_balance_addr", "bottle_balances", type_="unique")
    op.drop_constraint("uq_bottle_balance_group", "bottle_balances", type_="unique")
    op.drop_constraint("ck_bottle_balance_scope", "bottle_balances", type_="check")
    op.drop_index("idx_bottle_balances_group", table_name="bottle_balances")

    conn.execute(
        sa.text(
            """
        UPDATE bottle_balances b
           SET address_id = (SELECT MIN(a.id) FROM addresses a
                              WHERE a.address_group_id = b.address_group_id)
         WHERE b.address_group_id IS NOT NULL
    """
        )
    )
    # a group whose member addresses are all gone has nothing to attach to
    conn.execute(sa.text("DELETE FROM bottle_balances WHERE address_id IS NULL"))
    conn.execute(
        sa.text(
            """
        UPDATE bottle_balances b SET user_id = a.user_id
          FROM addresses a WHERE a.id = b.address_id
    """
        )
    )
    conn.execute(sa.text("DELETE FROM bottle_balances WHERE user_id IS NULL"))

    op.alter_column("bottle_balances", "user_id", nullable=False)
    op.alter_column("bottle_balances", "address_id", nullable=False)
    op.create_foreign_key("bottle_balances_user_id_fkey", "bottle_balances", "users", ["user_id"], ["id"])
    # A grouped address may still carry its own address-keyed row once the
    # service layer is place-native (post Tasks 3-9), so exploding the group row
    # onto MIN(member address) can collide on (user_id, address_id). The
    # roundtrip test runs on an empty DB and cannot reach this; without the
    # dedup the unique below would raise, and "downgrade must not raise" is a
    # hard constraint.
    conn.execute(
        sa.text(
            """
        DELETE FROM bottle_balances a USING bottle_balances b
         WHERE a.user_id=b.user_id AND a.address_id=b.address_id AND a.id>b.id
    """
        )
    )
    op.create_unique_constraint("uq_bottle_balance_user_address", "bottle_balances", ["user_id", "address_id"])
    op.create_index("idx_bottle_balances_user", "bottle_balances", ["user_id"])
    op.drop_constraint("fk_bottle_balances_address_group", "bottle_balances", type_="foreignkey")
    op.drop_column("bottle_balances", "address_group_id")

    # -- ledger -------------------------------------------------------------
    op.drop_index("idx_bottle_ledger_group_occurred", table_name="bottle_ledger")
    op.drop_index("ix_bottle_ledger_address_group_id", table_name="bottle_ledger")
    op.drop_constraint("fk_bottle_ledger_address_group", "bottle_ledger", type_="foreignkey")
    op.drop_column("bottle_ledger", "address_group_id")

    # -- fines: re-point at the restored balance rows -----------------------
    # COALESCE arm 2 is load-bearing: the balances step above collapsed each
    # group onto MIN(member address), so a fine on any OTHER member address has
    # no address-keyed row to match and would be silently deleted below. Fall
    # back to the group's restored row, found through `addresses` because
    # bottle_balances.address_group_id has already been dropped by this point
    # (bottle_fines.address_group_id survives until the end of this function).
    op.add_column("bottle_fines", sa.Column("bottle_balance_id", sa.Integer(), nullable=True))
    conn.execute(
        sa.text(
            """
        UPDATE bottle_fines f SET bottle_balance_id = COALESCE(
            (SELECT b.id FROM bottle_balances b WHERE b.address_id = f.address_id LIMIT 1),
            (SELECT b.id FROM bottle_balances b JOIN addresses a ON a.id = b.address_id
              WHERE a.address_group_id = f.address_group_id LIMIT 1))
    """
        )
    )
    conn.execute(sa.text("DELETE FROM bottle_fines WHERE bottle_balance_id IS NULL"))
    op.alter_column("bottle_fines", "bottle_balance_id", nullable=False)
    op.create_foreign_key(
        "bottle_fines_bottle_balance_id_fkey",
        "bottle_fines",
        "bottle_balances",
        ["bottle_balance_id"],
        ["id"],
    )
    op.create_index("idx_bottle_fines_balance", "bottle_fines", ["bottle_balance_id"])
    op.drop_index("idx_bottle_fines_address", table_name="bottle_fines")
    op.drop_constraint("fk_bottle_fines_address_group", "bottle_fines", type_="foreignkey")
    op.drop_constraint("fk_bottle_fines_address", "bottle_fines", type_="foreignkey")
    op.drop_column("bottle_fines", "address_group_id")
    op.drop_column("bottle_fines", "address_id")
