"""support inbox: conversations + messages

Revision ID: c1f2a3b4d5e6
Revises: f3a9c7d21e84
Create Date: 2026-06-24 12:00:00.000000
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "c1f2a3b4d5e6"
down_revision = "f3a9c7d21e84"
branch_labels = None
depends_on = None

DIRECTION = "support_message_direction"
DELIVERY = "support_message_delivery_status"
CONV_STATUS = "support_conversation_status"


def upgrade():
    bind = op.get_bind()

    direction = postgresql.ENUM("inbound", "outbound", name=DIRECTION, create_type=False)
    delivery = postgresql.ENUM("pending", "sent", "failed", name=DELIVERY, create_type=False)
    conv_status = postgresql.ENUM("open", "closed", name=CONV_STATUS, create_type=False)
    direction.create(bind, checkfirst=True)
    delivery.create(bind, checkfirst=True)
    conv_status.create(bind, checkfirst=True)

    op.create_table(
        "support_conversations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", conv_status, nullable=False, server_default="open"),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_message_preview", sa.String(length=200), nullable=True),
        sa.Column("last_message_direction", direction, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", name="uq_support_conversations_user_id"),
    )
    op.create_index("ix_support_conversations_user_id", "support_conversations", ["user_id"])
    op.create_index("ix_support_conversations_last_message_at", "support_conversations", ["last_message_at"])

    op.create_table(
        "support_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.Integer(),
            sa.ForeignKey("support_conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("direction", direction, nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("sender_admin_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("telegram_message_id", sa.String(length=64), nullable=True),
        sa.Column("delivery_status", delivery, nullable=True),
        sa.Column("delivery_error", sa.String(length=500), nullable=True),
        sa.Column("is_read", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_support_messages_conversation_id", "support_messages", ["conversation_id"])
    op.create_index("idx_support_messages_conv_created", "support_messages", ["conversation_id", "created_at"])
    op.create_index("idx_support_messages_unread", "support_messages", ["conversation_id", "is_read"])


def downgrade():
    bind = op.get_bind()
    op.drop_index("idx_support_messages_unread", table_name="support_messages")
    op.drop_index("idx_support_messages_conv_created", table_name="support_messages")
    op.drop_index("ix_support_messages_conversation_id", table_name="support_messages")
    op.drop_table("support_messages")
    op.drop_index("ix_support_conversations_last_message_at", table_name="support_conversations")
    op.drop_index("ix_support_conversations_user_id", table_name="support_conversations")
    op.drop_table("support_conversations")
    postgresql.ENUM(name=CONV_STATUS).drop(bind, checkfirst=True)
    postgresql.ENUM(name=DELIVERY).drop(bind, checkfirst=True)
    postgresql.ENUM(name=DIRECTION).drop(bind, checkfirst=True)
