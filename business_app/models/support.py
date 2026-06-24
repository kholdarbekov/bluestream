from __future__ import annotations

import enum

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from business_app import db
from business_app.models import TimestampMixin


class SupportMessageDirection(enum.Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class SupportMessageDeliveryStatus(enum.Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class SupportConversationStatus(enum.Enum):
    OPEN = "open"
    CLOSED = "closed"


def _enum_col(enum_cls, name, **kwargs):
    return Column(
        Enum(enum_cls, name=name, values_callable=lambda x: [e.value for e in x]),
        **kwargs,
    )


class SupportConversation(db.Model, TimestampMixin):
    __tablename__ = "support_conversations"

    id = Column(Integer, primary_key=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    status = _enum_col(
        SupportConversationStatus,
        "support_conversation_status",
        nullable=False,
        default=SupportConversationStatus.OPEN,
    )
    last_message_at = Column(DateTime(timezone=True), nullable=True, index=True)
    last_message_preview = Column(String(200), nullable=True)
    last_message_direction = _enum_col(SupportMessageDirection, "support_message_direction", nullable=True)

    user = relationship("User", foreign_keys=[user_id])
    messages = relationship(
        "SupportMessage",
        back_populates="conversation",
        order_by="SupportMessage.created_at",
        cascade="all, delete-orphan",
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "status": self.status.value if hasattr(self.status, "value") else self.status,
            "last_message_at": self.last_message_at.isoformat() if self.last_message_at else None,
            "last_message_preview": self.last_message_preview,
            "last_message_direction": (
                self.last_message_direction.value
                if hasattr(self.last_message_direction, "value")
                else self.last_message_direction
            ),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class SupportMessage(db.Model, TimestampMixin):
    __tablename__ = "support_messages"
    __table_args__ = (
        Index("idx_support_messages_conv_created", "conversation_id", "created_at"),
        Index("idx_support_messages_unread", "conversation_id", "is_read"),
    )

    id = Column(Integer, primary_key=True)
    conversation_id = Column(
        Integer,
        ForeignKey("support_conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    direction = _enum_col(SupportMessageDirection, "support_message_direction", nullable=False)
    content = Column(Text, nullable=False)
    sender_admin_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    telegram_message_id = Column(String(64), nullable=True)
    delivery_status = _enum_col(SupportMessageDeliveryStatus, "support_message_delivery_status", nullable=True)
    delivery_error = Column(String(500), nullable=True)
    is_read = Column(Boolean, nullable=False, default=False)

    conversation = relationship("SupportConversation", back_populates="messages")
    sender_admin = relationship("User", foreign_keys=[sender_admin_id])

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "direction": self.direction.value if hasattr(self.direction, "value") else self.direction,
            "content": self.content,
            "sender_admin_id": self.sender_admin_id,
            "telegram_message_id": self.telegram_message_id,
            "delivery_status": (
                self.delivery_status.value if hasattr(self.delivery_status, "value") else self.delivery_status
            ),
            "delivery_error": self.delivery_error,
            "is_read": self.is_read,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
