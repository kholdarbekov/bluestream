"""add visit_photos and product_tryouts.outlet_id

Revision ID: b7c8d9e0f1a2
Revises: f1a2b3c4d5e6
Create Date: 2026-09-15 09:00:00.000000

Phase 2b of the Sales Agent design (docs/superpowers/specs/2026-09-07-sales-agent-role-design.md).
Hand-written so every constraint is named, following d4e7f9a2b6c1. The column add
on the legacy product_tryouts table goes through batch_alter_table, following
e7b1c2d3f4a5.

Tables added:
  * visit_photos  — one row per photo an agent sent during a visit (D17). The
                    file itself lives wherever FileStorageService put it; this
                    row carries the path, the SHA-256 of the ORIGINAL bytes and
                    the pointer to an earlier photo with the same digest.

Columns added:
  * product_tryouts.outlet_id — the outlet a field try-out was left at (D8).
                    Nullable: every try-out created before phase 2b, and every
                    one an operator or admin creates afterwards, has none.

ix_visit_photos_sha256 is deliberately NOT unique. Duplicate detection is a
per-agent SERVICE rule that stores the second photo and flags it; a unique index
would turn the detected case into a 500 and lose the row the exception feed
needs.

Rollback strategy:
    downgrade() drops product_tryouts.outlet_id (index and FK first) and then
    the visit_photos table with its indexes. Photo ROWS are lost — the stored
    files are not deleted and are simply orphaned in the storage backend, which
    is the recoverable direction. product_tryouts itself is untouched apart from
    the dropped column, and nothing else references either object.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "b7c8d9e0f1a2"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None

# Mirrors business_app/models/sales_visits.py::PHOTO_KINDS, asserted against the
# migrated schema in tests/integration/test_sales_agent_pg_constraints.py.
PHOTO_KINDS = ("storefront", "shelf", "other")


def _quoted(values):
    return ", ".join(f"'{v}'" for v in values)


def upgrade():
    op.create_table(
        "visit_photos",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "visit_id", sa.Integer(), sa.ForeignKey("visits.id", name="fk_visit_photos_visit_id"), nullable=False
        ),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("file_path", sa.String(length=500), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("telegram_file_unique_id", sa.String(length=100), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "duplicate_of_photo_id",
            sa.Integer(),
            sa.ForeignKey("visit_photos.id", name="fk_visit_photos_duplicate_of_photo_id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"kind IN ({_quoted(PHOTO_KINDS)})", name="ck_visit_photos_kind"),
    )
    op.create_index("ix_visit_photos_visit_id", "visit_photos", ["visit_id"])
    op.create_index("ix_visit_photos_sha256", "visit_photos", ["sha256"])

    with op.batch_alter_table("product_tryouts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("outlet_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key("fk_product_tryouts_outlet_id", "outlets", ["outlet_id"], ["id"])
        batch_op.create_index(batch_op.f("ix_product_tryouts_outlet_id"), ["outlet_id"], unique=False)


def downgrade():
    with op.batch_alter_table("product_tryouts", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_product_tryouts_outlet_id"))
        batch_op.drop_constraint("fk_product_tryouts_outlet_id", type_="foreignkey")
        batch_op.drop_column("outlet_id")

    op.drop_index("ix_visit_photos_sha256", table_name="visit_photos")
    op.drop_index("ix_visit_photos_visit_id", table_name="visit_photos")
    op.drop_table("visit_photos")
