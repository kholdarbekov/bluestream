from __future__ import annotations

import enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from business_app import db
from business_app.models import TimestampMixin
from shared.enums import SupportMessageType


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
    content = Column(Text, nullable=True)
    sender_admin_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    telegram_message_id = Column(String(64), nullable=True)
    delivery_status = _enum_col(SupportMessageDeliveryStatus, "support_message_delivery_status", nullable=True)
    delivery_error = Column(String(500), nullable=True)
    is_read = Column(Boolean, nullable=False, default=False)
    message_type = _enum_col(
        SupportMessageType,
        "support_message_type",
        nullable=False,
        default=SupportMessageType.TEXT,
        server_default=SupportMessageType.TEXT.value,
    )
    telegram_file_id = Column(String(256), nullable=True)
    attachment_mime_type = Column(String(128), nullable=True)
    attachment_file_name = Column(String(255), nullable=True)
    attachment_size = Column(BigInteger, nullable=True)
    latitude = Column(Numeric(10, 7), nullable=True)
    longitude = Column(Numeric(10, 7), nullable=True)
    forwarded_from = Column(String(255), nullable=True)
    forwarded_origin_type = Column(String(32), nullable=True)
    forwarded_date = Column(DateTime(timezone=True), nullable=True)

    conversation = relationship("SupportConversation", back_populates="messages")
    sender_admin = relationship("User", foreign_keys=[sender_admin_id])

    def to_dict(self) -> dict:
        # Local import: `support_attachment_service` imports `SupportMessage`
        # from this module, so a module-level import here would cycle. The
        # backend constant (`TELEGRAM_MAX_DOWNLOAD_BYTES`) stays the single
        # definition of the 20 MB rule — this just publishes the derived
        # answer as a field so the frontend never re-implements the rule.
        from business_app.services.support_attachment_service import TELEGRAM_MAX_DOWNLOAD_BYTES

        attachment_too_large = self.attachment_size is not None and self.attachment_size > TELEGRAM_MAX_DOWNLOAD_BYTES

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
            "message_type": (self.message_type.value if hasattr(self.message_type, "value") else self.message_type),
            "telegram_file_id": self.telegram_file_id,
            "attachment_mime_type": self.attachment_mime_type,
            "attachment_file_name": self.attachment_file_name,
            "attachment_size": int(self.attachment_size) if self.attachment_size is not None else None,
            "attachment_too_large": attachment_too_large,
            # react-leaflet cannot take a Decimal or a string.
            "latitude": float(self.latitude) if self.latitude is not None else None,
            "longitude": float(self.longitude) if self.longitude is not None else None,
            "forwarded_from": self.forwarded_from,
            "forwarded_origin_type": self.forwarded_origin_type,
            "forwarded_date": self.forwarded_date.isoformat() if self.forwarded_date else None,
            "has_attachment": bool(self.telegram_file_id),
        }
