"""address_groups: forwarding pointer for a DISSOLVED place

Revision ID: d6b3f8a2c5e9
Revises: f1a4c8b2e7d6
Create Date: 2026-08-05 09:00:00.000000

Spec §7.3 dissolves a place onto its LAST MEMBER and keeps the (now memberless)
`address_groups` row for ever, because `bottle_ledger.address_group_id` is a
foreign key that every DEPARTED member's rows still carry. §7.1/§7.3 also
deliberately never re-stamp a departed member's frozen references — NULLing them
would drop the place's history into a departed address's own scope and mint
bottles onto someone who left with nothing.

The consequence, open until now: a `bottle_fines` row and a `delivery:{order}`
`bottle_ledger` row belonging to a departed member keep naming a group that has
since dissolved. Settling or waiving that fine, and correcting that order, were
REFUSED BY NAME (`BOTTLE_SCOPE_UNREACHABLE`, `BOTTLE_CORRECTION_SCOPE_NOT_LIVE`)
because booking to the frozen scope would re-INSERT exactly the orphaned
`bottle_balances` row §7.3's dissolve exists to delete. A visible refusal beat
silent corruption, but it left the admin with no way forward at all.

`dissolved_onto_address_id` records WHICH address the dissolve released the
place's history onto, so those two operations can follow the history instead of
refusing. It names an ADDRESS, not a group: the address's LIVE scope is
re-resolved at read time, so a survivor that has since joined a new place
forwards to that place and the pointer never chains and is never rewritten. A
dissolved group can never be re-populated (`PLACE_GROUP_DISSOLVED`), so the value
is write-once by construction.

ondelete SET NULL, deliberately. An UNGROUPED address is deletable (only a
grouped one is fenced by `assert_address_not_in_place_group`), and losing the
survivor means losing the destination. The two operations then fall back to the
refusal they had before, which is why those refusals stay in the code.

BACKFILL. `CustomerLinkEvent` already recorded the survivor, in
`event_metadata['dissolved_onto_address_id']`, together with the group id in the
`reason` prefix `"[group N] "` that `remove_address_from_group` writes. That is an
audit blob nothing could resolve through — this migration resolves it once, so
places that dissolved BEFORE this column existed are unstuck too rather than
staying refused for ever. Guarded on all four sides: the group must still be
memberless, its pointer must still be NULL, the recorded address must still
exist, and only the LATEST such event per group is used. A group whose audit row
is missing, truncated or unparseable simply keeps a NULL pointer and keeps the
old refusal — the backfill can leave the column under-populated, never wrong.

Rollback strategy:
    downgrade() drops the FK then the column, in exact reverse order. Fully
    reversible: no other table references it and no data outside this column is
    touched. The pointer is re-derivable from `customer_link_events` by re-running
    upgrade(), so even the backfilled values are not lost by a rollback.
    tests/integration/test_migrations_roundtrip.py walks
    upgrade(head) -> downgrade(base) -> upgrade(head) against a real ephemeral
    Postgres, so neither direction may raise. Never `op.drop_constraint(None, ...)`
    (migrations/CONVENTIONS.md §5).
"""

import sqlalchemy as sa
from alembic import op

revision = "d6b3f8a2c5e9"
down_revision = "f1a4c8b2e7d6"
branch_labels = None
depends_on = None


# `event_metadata` is JSON (not JSONB), so `->>` is used rather than the
# jsonb-only `?` containment operator — it simply yields NULL when the key is
# absent, which the outer WHERE already filters on.
_BACKFILL = """
UPDATE address_groups g
   SET dissolved_onto_address_id = src.address_id
  FROM (
        SELECT DISTINCT ON (x.group_id) x.group_id, x.address_id
          FROM (
                SELECT (substring(e.reason from '^\\[group ([0-9]+)\\]'))::bigint AS group_id,
                       (e.event_metadata ->> 'dissolved_onto_address_id')::bigint AS address_id,
                       e.id AS event_id
                  FROM customer_link_events e
                 WHERE e.event_type = 'remove_from_place_group'
                   AND e.reason ~ '^\\[group [0-9]+\\]'
                   AND (e.event_metadata ->> 'dissolved_onto_address_id') ~ '^[0-9]+$'
               ) x
         ORDER BY x.group_id, x.event_id DESC
       ) src
 WHERE g.id = src.group_id
   AND g.dissolved_onto_address_id IS NULL
   AND EXISTS (SELECT 1 FROM addresses a WHERE a.id = src.address_id)
   AND NOT EXISTS (SELECT 1 FROM addresses m WHERE m.address_group_id = g.id)
"""


def upgrade():
    op.add_column(
        "address_groups",
        sa.Column("dissolved_onto_address_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_address_groups_dissolved_onto_address",
        "address_groups",
        "addresses",
        ["dissolved_onto_address_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Postgres-only: the fast suite builds its schema with db.create_all() and
    # never runs migrations, and this statement uses regex + DISTINCT ON.
    if op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text(_BACKFILL))


def downgrade():
    op.drop_constraint("fk_address_groups_dissolved_onto_address", "address_groups", type_="foreignkey")
    op.drop_column("address_groups", "dissolved_onto_address_id")
