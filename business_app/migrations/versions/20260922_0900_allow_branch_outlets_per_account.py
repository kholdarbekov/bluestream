"""allow several outlets per customer account (branch outlets)

Revision ID: e0f1a2b3c4d5
Revises: d8e9f0a1b2c3
Create Date: 2026-09-22 09:00:00.000000

D25 of the Sales Agent design (docs/superpowers/specs/2026-09-07-sales-agent-role-design.md):
an outlet is one PLACE (one address) and a customer ACCOUNT may own several of them. A
grocery chain is one account with several delivery addresses; `uq_outlets_user_id` made
every branch after the first impossible to insert, so *Import existing customers* created a
single outlet at the chain's default address, an agent standing in another branch checked in
"out of range", and approving a new outlet whose phone matched the chain was refused.

Changed:
  * uq_outlets_user_id     DROPPED — the account no longer caps the outlet count.
  * ix_outlets_user_id     ADDED   — that unique was also the ONLY index on
                           `outlets.user_id`; without a replacement "the other branches of
                           this account" (the card's branch count, the sibling dedupe, the
                           attach candidate) becomes a sequential scan.
  * uq_outlets_address_id  KEPT    — one outlet per ADDRESS is the invariant the whole
                           design rests on: two outlets on one address would share that
                           place's bottle balance and its per-branch order history.

No data change: dropping a unique cannot fail on existing rows, and nothing is back-filled
here. The branches themselves are created afterwards by *Import existing customers*.

Rollback strategy:
    downgrade() drops the index and RE-CREATES uq_outlets_user_id. That is safe on an empty
    database (the upgrade → downgrade → upgrade roundtrip) and on any estate where no account
    holds two outlets. It FAILS LOUDLY — `could not create unique index "uq_outlets_user_id"`
    — the moment one does, which is the correct behaviour: the extra rows are real shops with
    their own visits, stage history and orders, and a rollback must not guess which of them to
    delete. To roll back after branches exist, decide their fate first: re-point them (set
    `user_id` NULL) or delete them together with their visits, contacts and stage history,
    then downgrade. Marking them lost is NOT enough -- `mark_lost` changes only the stage and
    the lost reason, so `user_id` stays and the unique still refuses. The rows in the way:
        SELECT id, user_id, name FROM outlets WHERE user_id IN (
            SELECT user_id FROM outlets WHERE user_id IS NOT NULL
            GROUP BY user_id HAVING count(*) > 1);
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "e0f1a2b3c4d5"
down_revision = "d8e9f0a1b2c3"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("uq_outlets_user_id", "outlets", type_="unique")
    op.create_index("ix_outlets_user_id", "outlets", ["user_id"])


def downgrade():
    op.drop_index("ix_outlets_user_id", table_name="outlets")
    op.create_unique_constraint("uq_outlets_user_id", "outlets", ["user_id"])
