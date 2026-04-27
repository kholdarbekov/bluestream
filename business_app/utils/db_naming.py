"""ARCH-013: SQLAlchemy MetaData naming convention (defined, NOT installed yet).

The pattern strings follow the SQLAlchemy docs' canonical recommendation
(`docs.sqlalchemy.org/en/20/core/constraints.html#configuring-constraint-naming-conventions`).

Why this is defined but not yet wired into ``db.metadata``:

Installing the convention retroactively rewrites the constraint names that
``op.create_table()`` produces — every ``UniqueConstraint('email')`` in an
existing migration suddenly compiles to ``uq_users_email`` instead of the
PostgreSQL auto-name ``users_email_key``. That breaks two things in the
already-shipped migration chain:

  * ``9d04c0e40a2d_.py`` upgrade drops ``users_email_key`` / ``users_phone_key``
    by their literal PG names; with the convention active those constraints
    don't exist under that name on a fresh DB.
  * ``9ef3918623ff_drop_driver_bottle_loads_table.py`` does the same for
    ``corporate_contracts_contract_number_key`` and two ``driver_bottle_*_ref_key``
    constraints.

Updating those references in-place would in turn break any production DB that
was stamped at HEAD before the change — its constraints are still under the
PG auto-names, so the next downgrade would fail. Closing this cleanly needs
either:

  1. A one-shot rename migration that ``ALTER TABLE ... RENAME CONSTRAINT``
     every PG auto-name to the convention name across all prod DBs, then the
     in-place update of the affected migration files, OR
  2. A ``MetaData(naming_convention=NAMING_CONVENTION)`` only for autogenerate
     (env.py target_metadata) — keeping ``db.metadata`` raw at runtime so
     ``op.create_table()`` keeps PG auto-names. Risk: behaviour drifts between
     autogenerate and runtime.

Both routes are tracked as a follow-on PR. In the meantime new migrations
must pass an explicit ``name=`` to every ``ForeignKeyConstraint`` /
``op.create_foreign_key`` / ``UniqueConstraint`` so the auto-generated
``drop_constraint(None, ...)`` pattern (the original ARCH-013 trigger) does
not reappear. CI guard via TST-004 catches the regression.
"""

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
