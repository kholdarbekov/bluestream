"""add eta_model_fits and delivery_promises.promised_date: the weekly fit's one home and the day a promise names

Revision ID: 533fdcecfeff
Revises: 9e7056f6124a
Create Date: 2026-11-04 12:00:00.000000

Spec: docs/superpowers/specs/2026-11-03-honest-capacity-aware-eta-design.md §4.10.
Plan: docs/superpowers/plans/2026-11-04-honest-eta-phase2.md, Task 4 (R11, R13).

Added:
  * eta_model_fits                    one INSERT-ONLY row per weekly fit: the stop curve, the depot line and the window
                                      buckets the shadow engine plans with, the rows each was fitted from, and which
                                      components the fit kept from the previous one. `data_to` is unique: a re-run for
                                      the same window of plan days inserts nothing. The engine reads the latest row
                                      with `fitted_at <= now`, through ix_eta_model_fits_fitted_at.
  * delivery_promises.promised_date   the local calendar day a promise names (R11). Nullable with no default: a Phase 1
                                      row has none, and so does an unconfirmed engine row.

The table has no foreign key and nothing references it. The `params` JSON shape is documented on
business_app/models/eta_model_fit.py.

Rollback strategy:
    downgrade() drops the column first, then the table. LOSSY: every stored fit and every recorded promised day is
    discarded; both can be derived again (the next weekly fit re-reads 28 plan days of history, and a promised day is
    the local date of `expected_at`, or the order's own date for a row without a time). Until the first fit is written
    the engine plans with the seeds.

    Order matters, and the code goes back LAST:
      1. Stop the app services (business_app, celery_worker, celery_beat).
      2. Dump eta_model_fits (and delivery_promises) if the data is wanted.
      3. Downgrade to 9e7056f6124a with the NEW checkout still in place.
      4. Revert the checkout to the previous release.
      5. Start the services.
    Why: the shared DeliveryPromise model maps `promised_date`, so nothing may read delivery_promises while the column
    is gone; and the old checkout has no 533fdcecfeff revision file, so the `migrate` one-shot that every `up` runs
    would fail on the unknown revision. The kill switch ETA_SHADOW_ENGINE_ENABLED=false (then recreate celery_worker
    and celery_beat) needs no rollback at all.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "533fdcecfeff"
down_revision = "9e7056f6124a"
branch_labels = None
depends_on = None

TABLE = "eta_model_fits"


def upgrade():
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("fitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data_to", sa.DateTime(timezone=True), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("rows_used", sa.JSON(), nullable=False),
        sa.Column("kept_previous", sa.JSON(), nullable=False),
        sa.UniqueConstraint("data_to", name=f"uq_{TABLE}_data_to"),
    )
    op.create_index(f"ix_{TABLE}_fitted_at", TABLE, ["fitted_at"])
    op.add_column("delivery_promises", sa.Column("promised_date", sa.Date(), nullable=True))


def downgrade():
    op.drop_column("delivery_promises", "promised_date")
    op.drop_table(TABLE)
