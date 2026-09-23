"""visit photos keep the staff bot's Telegram file id, not a stored file

Revision ID: f2a3b4c5d6e7
Revises: e0f1a2b3c4d5
Create Date: 2026-09-23 09:00:00.000000

D27 of docs/superpowers/specs/2026-09-23-outlet-editing-and-visit-photos-design.md (reverses D17
of the sales-agent spec): photo bytes are no longer written to our storage. A row now carries the
staff bot's Telegram `file_id`, and the admin UI streams the picture from Telegram on demand.

Existing rows are DELETED: their bytes live in UPLOAD_FOLDER and no Telegram file id exists to
backfill them with. Only dev holds any (the phase-2b drive's probes); prod holds none because the
sales work has not reached `main`. If that stops being true, do not run this -- write a
keep-and-backfill migration instead.

outlets.storefront_photo_path is dropped: nothing ever wrote it.

Rollback strategy:
    downgrade() restores both columns as nullable and drops telegram_file_id. Deleted photo rows
    are not restored.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "f2a3b4c5d6e7"
down_revision = "e0f1a2b3c4d5"
branch_labels = None
depends_on = None


def upgrade():
    # The self-FK first, so the DELETE never trips fk_visit_photos_duplicate_of_photo_id.
    op.execute("UPDATE visit_photos SET duplicate_of_photo_id = NULL")
    op.execute("DELETE FROM visit_photos")
    op.add_column("visit_photos", sa.Column("telegram_file_id", sa.String(length=255), nullable=False))
    op.drop_column("visit_photos", "file_path")
    op.drop_column("outlets", "storefront_photo_path")


def downgrade():
    op.add_column("outlets", sa.Column("storefront_photo_path", sa.String(length=500), nullable=True))
    op.add_column("visit_photos", sa.Column("file_path", sa.String(length=500), nullable=True))
    op.drop_column("visit_photos", "telegram_file_id")
