"""support messages: typed payloads (media, location, forwards)

Revision ID: b6d1e8f4a207
Revises: f2b7c4e91a35
Create Date: 2026-08-25 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b6d1e8f4a207"
down_revision = "f2b7c4e91a35"
branch_labels = None
depends_on = None

MESSAGE_TYPE = "support_message_type"
MESSAGE_TYPE_VALUES = (
    "text",
    "photo",
    "document",
    "location",
    "voice",
    "video",
    "video_note",
    "audio",
    "unsupported",
)


def upgrade():
    bind = op.get_bind()

    # Same create_type=False + checkfirst dance as c1f2a3b4d5e6, so a re-run is safe.
    message_type = postgresql.ENUM(*MESSAGE_TYPE_VALUES, name=MESSAGE_TYPE, create_type=False)
    message_type.create(bind, checkfirst=True)

    op.add_column(
        "support_messages",
        sa.Column("message_type", message_type, nullable=False, server_default="text"),
    )
    op.add_column("support_messages", sa.Column("telegram_file_id", sa.String(length=256), nullable=True))
    op.add_column("support_messages", sa.Column("attachment_mime_type", sa.String(length=128), nullable=True))
    op.add_column("support_messages", sa.Column("attachment_file_name", sa.String(length=255), nullable=True))
    op.add_column("support_messages", sa.Column("attachment_size", sa.BigInteger(), nullable=True))
    op.add_column("support_messages", sa.Column("latitude", sa.Numeric(10, 7), nullable=True))
    op.add_column("support_messages", sa.Column("longitude", sa.Numeric(10, 7), nullable=True))
    op.add_column("support_messages", sa.Column("forwarded_from", sa.String(length=255), nullable=True))
    op.add_column("support_messages", sa.Column("forwarded_origin_type", sa.String(length=32), nullable=True))
    op.add_column("support_messages", sa.Column("forwarded_date", sa.DateTime(timezone=True), nullable=True))

    # A photo with no caption has no text. No backfill is needed for the
    # server_default above: every pre-existing row IS text.
    op.alter_column("support_messages", "content", existing_type=sa.Text(), nullable=True)


def downgrade():
    bind = op.get_bind()

    # Rows created after the upgrade may hold a NULL content that the old NOT
    # NULL constraint would reject, so give them a placeholder first.
    op.execute("UPDATE support_messages SET content = '' WHERE content IS NULL")
    op.alter_column("support_messages", "content", existing_type=sa.Text(), nullable=False)

    for column in (
        "forwarded_date",
        "forwarded_origin_type",
        "forwarded_from",
        "longitude",
        "latitude",
        "attachment_size",
        "attachment_file_name",
        "attachment_mime_type",
        "telegram_file_id",
        "message_type",
    ):
        op.drop_column("support_messages", column)

    postgresql.ENUM(name=MESSAGE_TYPE).drop(bind, checkfirst=True)
