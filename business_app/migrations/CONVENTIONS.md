# Migration Conventions (ARCH-012)

Reference: [docs/audit/01-architecture-backend.md#arch-012](../../docs/audit/01-architecture-backend.md#arch-012)

These rules apply to **every new migration** in `business_app/migrations/versions/`.
The contents of pre-existing migrations are not retroactively rewritten.

## 1. File naming

Run autogenerate with a descriptive `-m` message:

```bash
docker compose exec business_app flask db migrate -m "add cash_collected to deliveries"
```

Alembic produces `<revid>_add_cash_collected_to_deliveries.py`. Rename it to:

```
YYYYMMDD_HHMM_<verb>_<noun>.py     e.g. 20260427_1430_add_cash_collected_to_deliveries.py
```

Why: the audit flagged cryptic UUID-only names (e.g. `9d04c0e40a2d_.py`,
`cea8f329e11e_.py`) that were impossible to scan or sort. Date-prefixed names
sort chronologically in any directory listing and answer "when did this run?"
without opening the file.

When renaming, only change the *filename* — never the `revision` /
`down_revision` strings inside the file. Alembic identifies migrations by
those strings, not by filename.

## 2. Rollback strategy header

Every migration must include a `Rollback strategy:` paragraph in its module
docstring. The template at `business_app/migrations/script.py.mako` injects a
placeholder; fill it in before committing. Examples:

```python
"""Add subscriptions.next_billing_date column

Rollback strategy:
    downgrade() drops the column. Safe — no other migration references it.
"""
```

```python
"""Drop legacy driver_bottle_loads table

Rollback strategy:
    Forward-only. The table held transient driver state; recovery would
    require restoring from backup. Production stamped HEAD on 2026-03-15
    confirmed table empty before drop.
"""
```

```python
"""Convert subscriptions.billing_cycle VARCHAR -> ENUM

Rollback strategy:
    downgrade() re-adds the VARCHAR column and casts back via
    ``USING billing_cycle::text``. Reversible only while the ENUM still has
    its original member set.
"""
```

If the migration is destructive (drops tables/columns, alters types
non-reversibly), say so clearly. The audit team's bar: a future on-call
engineer reading just this header should know whether they can roll back.

## 3. Data migrations

For migrations that backfill or reshape data, follow the
`a6c1b9d0e201_arch006_state_invariant_checks.py` pattern:

* Use `op.execute(sa.text(...))` against `op.get_bind()` for raw SQL.
* For row-by-row backfill on large tables, **batch** with a `WHERE id BETWEEN
  :lo AND :hi` loop. Streaming a single `UPDATE` over millions of rows holds
  a long transaction and may bloat WAL.
* For invariant-tightening migrations (adding NOT NULL / CHECK after
  backfill), include a pre-flight that **counts violations and aborts** if
  any remain after backfill. Surface the offending row ids so an operator
  can triage. See `_abort_if_violations_exist()` in
  `a6c1b9d0e201_arch006_state_invariant_checks.py` and the same pattern in
  `b8e3c9f5d2a4_arch005_money_points_check_constraints.py`.
* Keep schema and data steps **in one Alembic transaction**. Postgres
  honours transactional DDL; running both inside one `def upgrade()` means
  partial failure leaves zero changes to roll back manually.

## 4. PostgreSQL ENUM cleanup

`op.drop_table()` does **not** drop the Postgres ENUM types declared inside
that table. If a migration creates an ENUM (via `sa.Enum(..., name='foo')`
or raw `CREATE TYPE`), its `downgrade()` must explicitly drop it:

```python
op.execute('DROP TYPE IF EXISTS foo')
```

Skipping this leaves orphan types in the schema — the `upgrade → downgrade
→ upgrade` roundtrip ([TST-004](../../tests/integration/test_migrations_roundtrip.py))
fails on the second `upgrade` with `DuplicateObject: type "foo" already exists`.
ARCH-013 fixed three pre-existing offenders; the convention going forward is
"if you create the type, drop it on the way back down."

## 5. Constraint naming

Until the pending `db.metadata` naming-convention install (see
`business_app/utils/db_naming.py` and ARCH-013), every new migration must
pass an **explicit `name=`** to:

* `sa.ForeignKeyConstraint(...)`
* `op.create_foreign_key(...)`
* `sa.UniqueConstraint(...)` / `op.create_unique_constraint(...)`
* `sa.CheckConstraint(...)` / `op.create_check_constraint(...)`

Naming patterns to use (matches the convention reserved in `db_naming.py`):

| Type | Pattern | Example |
|------|---------|---------|
| FK | `fk_<table>_<column>_<referred_table>` | `fk_orders_user_id_users` |
| UQ | `uq_<table>_<column>` | `uq_users_email` |
| CK | `ck_<table>_<constraint_name>` | `ck_orders_subtotal_nonneg` |
| IX | `ix_<table>_<column>` | `ix_orders_status` |
| PK | `pk_<table>` | `pk_orders` |

Never call `op.drop_constraint(None, ...)` — that line is the original
ARCH-013 trigger and CI will reject the migration via TST-004.

## 6. Verification

After authoring a migration, run the roundtrip test before committing:

```bash
scripts/precommit-backend-tests.sh   # full suite (the recommended path)
# or, just the migration test in isolation:
docker compose exec business_app pytest tests/integration/test_migrations_roundtrip.py -v --no-cov -o 'addopts='
```

The roundtrip applies every migration forward, rolls every one back to base,
and applies them all forward again on a transient database. A clean run is
the bar for merging.
