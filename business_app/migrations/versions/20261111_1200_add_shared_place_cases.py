"""add shared_place_cases and address_groups.notes (shared-place detection)

Revision ID: e3b7c1d9a5f4
Revises: c8a4f2d6b1e3
Create Date: 2026-11-11 12:00:00.000000

Spec: docs/superpowers/specs/2026-11-10-shared-place-detection-design.md (section 9 binds).

Added:
  * shared_place_cases        one row per order whose first-order customer handed back empties the place's
                              balance does not explain. UNIQUE (order_id). The state matrix is CHECKed:
                              answered <-> driver_answer set; picked <-> picked_index set; decided <-> status
                              resolved/closed; a resolved case has a kind; a closed one has none.
  * address_groups.notes      nullable TEXT: the admin-only place note, pre-filled from the driver's note.

asked_driver_id and decided_by_user_id are ON DELETE SET NULL (NULL asked_driver_id means admin-entered); the
account merge re-points both anyway. The vocabulary tuples below are FROZEN copies of the model's
(CONVENTIONS.md); tests/integration/test_shared_place_case_pg_constraints.py pins them equal.

Rollback strategy:
    downgrade() drops shared_place_cases and address_groups.notes. LOSSY: every case (open or settled) and
    every place note is discarded; no other table references them and no bottle balance was written from them
    (a resolution's bottle movements are ordinary ledger rows and stay). The deploy rollback leaves this
    migration applied: the code it rolls back to ignores both objects.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "e3b7c1d9a5f4"
down_revision = "c8a4f2d6b1e3"
branch_labels = None
depends_on = None

TABLE = "shared_place_cases"

# Frozen copies (CONVENTIONS.md): never import the live tuples into a migration.
STATUSES = ("awaiting_driver", "awaiting_admin", "resolved", "closed")
DRIVER_ANSWERS = ("picked", "none", "skipped")
RESOLUTION_KINDS = ("shared_place", "same_home", "other_place", "own_bottles", "wrong_count")
EMPTY_REASONS = ("no_pin", "none_within_radius", "none_eligible")


def _quoted(values):
    return ", ".join(f"'{v}'" for v in values)


def _user_fk(column):
    return sa.Column(
        column,
        sa.Integer(),
        sa.ForeignKey("users.id", name=f"fk_{TABLE}_{column}", ondelete="SET NULL"),
        nullable=True,
    )


def upgrade():
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id", name=f"fk_{TABLE}_order_id"), nullable=False),
        sa.Column(
            "delivery_id", sa.Integer(), sa.ForeignKey("deliveries.id", name=f"fk_{TABLE}_delivery_id"), nullable=True
        ),
        sa.Column(
            "address_id", sa.Integer(), sa.ForeignKey("addresses.id", name=f"fk_{TABLE}_address_id"), nullable=False
        ),
        sa.Column("returned_qty", sa.Integer(), nullable=False),
        sa.Column("place_balance_before", sa.Numeric(12, 2), nullable=False),
        _user_fk("asked_driver_id"),
        sa.Column("candidates", sa.JSON(), nullable=False),
        sa.Column("empty_reason", sa.String(20), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("driver_answer", sa.String(8), nullable=True),
        sa.Column("picked_index", sa.Integer(), nullable=True),
        sa.Column("driver_note", sa.Text(), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("alert_posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_kind", sa.String(16), nullable=True),
        sa.Column("resolution_payload", sa.JSON(), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        _user_fk("decided_by_user_id"),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "resulting_group_id",
            sa.Integer(),
            sa.ForeignKey("address_groups.id", name=f"fk_{TABLE}_resulting_group_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("order_id", name=f"uq_{TABLE}_order_id"),
        sa.CheckConstraint(f"status IN ({_quoted(STATUSES)})", name=f"ck_{TABLE}_status"),
        sa.CheckConstraint(
            f"driver_answer IS NULL OR driver_answer IN ({_quoted(DRIVER_ANSWERS)})", name=f"ck_{TABLE}_driver_answer"
        ),
        sa.CheckConstraint(
            f"resolution_kind IS NULL OR resolution_kind IN ({_quoted(RESOLUTION_KINDS)})",
            name=f"ck_{TABLE}_resolution_kind",
        ),
        sa.CheckConstraint(
            f"empty_reason IS NULL OR empty_reason IN ({_quoted(EMPTY_REASONS)})", name=f"ck_{TABLE}_empty_reason"
        ),
        sa.CheckConstraint("returned_qty >= 1", name=f"ck_{TABLE}_returned_qty"),
        sa.CheckConstraint("(driver_answer IS NULL) = (answered_at IS NULL)", name=f"ck_{TABLE}_answered"),
        sa.CheckConstraint(
            "(COALESCE(driver_answer, '') = 'picked') = (picked_index IS NOT NULL)", name=f"ck_{TABLE}_picked"
        ),
        sa.CheckConstraint("(status IN ('resolved', 'closed')) = (decided_at IS NOT NULL)", name=f"ck_{TABLE}_decided"),
        sa.CheckConstraint("status <> 'resolved' OR resolution_kind IS NOT NULL", name=f"ck_{TABLE}_resolved_kind"),
        sa.CheckConstraint("status <> 'closed' OR resolution_kind IS NULL", name=f"ck_{TABLE}_closed_kind"),
    )
    op.create_index(f"ix_{TABLE}_status_created_at", TABLE, ["status", "created_at"])
    op.create_index(f"ix_{TABLE}_delivery_id", TABLE, ["delivery_id"])
    op.create_index(f"ix_{TABLE}_asked_driver_id", TABLE, ["asked_driver_id"])

    op.add_column("address_groups", sa.Column("notes", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("address_groups", "notes")
    op.drop_table(TABLE)
