import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, or_

from business_app import db
from business_app.models.support import (
    SupportConversation,
    SupportConversationStatus,
    SupportMessage,
    SupportMessageDeliveryStatus,
    SupportMessageDirection,
)
from business_app.models.user import User
from business_app.utils.exceptions import NotFoundError, ValidationError
from business_app.utils.service_factory import get_notification_service
from shared.enums import SupportMessageType

logger = logging.getLogger(__name__)

PREVIEW_LEN = 200

# Telegram rejects any sendPhoto/sendDocument/sendVideo caption longer than
# this — reject it here with a clean 400 instead of letting it become a
# FAILED delivery only after we've already round-tripped to Telegram.
MAX_CAPTION_LENGTH = 1024

MAX_ATTACHMENT_FILE_NAME_LENGTH = 255  # SupportMessage.attachment_file_name column width

# Types whose payload IS a Telegram file.
_FILE_TYPES = frozenset(
    {
        SupportMessageType.PHOTO,
        SupportMessageType.DOCUMENT,
        SupportMessageType.VOICE,
        SupportMessageType.VIDEO,
        SupportMessageType.VIDEO_NOTE,
        SupportMessageType.AUDIO,
    }
)

_TYPE_LABELS = {
    SupportMessageType.PHOTO: "📷 Photo",
    SupportMessageType.DOCUMENT: "📎 Document",
    SupportMessageType.LOCATION: "📍 Location",
    SupportMessageType.VOICE: "🎤 Voice message",
    SupportMessageType.VIDEO: "🎬 Video",
    SupportMessageType.VIDEO_NOTE: "🎬 Video note",
    SupportMessageType.AUDIO: "🎵 Audio",
    SupportMessageType.UNSUPPORTED: "📩 Attachment",
}


def _now():
    return datetime.now(timezone.utc)


def build_preview(message_type, content, attachment_file_name=None) -> str:
    """The conversation-list label for one message.

    SSOT on purpose: the admin UI renders whatever this returns instead of
    re-deriving a label, so the list and the thread can never disagree.
    """
    text = (content or "").strip()
    if text:
        return text[:PREVIEW_LEN]
    if message_type == SupportMessageType.DOCUMENT and attachment_file_name:
        return f"📎 {attachment_file_name}"[:PREVIEW_LEN]
    return _TYPE_LABELS.get(message_type, _TYPE_LABELS[SupportMessageType.UNSUPPORTED])


class SupportConversationService:
    """Two-way support conversation store between customers (Telegram) and admins."""

    def _get_or_create_conversation(self, user_id: int) -> SupportConversation:
        conv = SupportConversation.query.filter_by(user_id=user_id).first()
        if conv is None:
            conv = SupportConversation(
                user_id=user_id,
                status=SupportConversationStatus.OPEN,
                last_message_at=_now(),
            )
            db.session.add(conv)
            db.session.flush()
        return conv

    def _touch(self, conv: SupportConversation, preview: str, direction: SupportMessageDirection):
        conv.last_message_at = _now()
        conv.last_message_preview = preview
        conv.last_message_direction = direction
        conv.status = SupportConversationStatus.OPEN

    def _apply_delivery(self, msg: SupportMessage, delivery: dict) -> None:
        """Write a Telegram send outcome onto a PENDING outbound message.

        The one place that performs the PENDING -> SENT/FAILED transition, so
        every outbound message type (text, media, location) reports delivery
        the same way and the admin UI's "Not delivered" tag means the same
        thing everywhere.
        """
        if delivery.get("success"):
            msg.delivery_status = SupportMessageDeliveryStatus.SENT
            message_id = delivery.get("message_id")
            msg.telegram_message_id = str(message_id) if message_id is not None else None
        else:
            msg.delivery_status = SupportMessageDeliveryStatus.FAILED
            msg.delivery_error = (delivery.get("error") or "Delivery failed")[:500]

    def record_inbound_message(
        self,
        user_id: int,
        content: Optional[str] = None,
        telegram_message_id: Optional[str] = None,
        *,
        message_type: SupportMessageType = SupportMessageType.TEXT,
        telegram_file_id: Optional[str] = None,
        attachment_mime_type: Optional[str] = None,
        attachment_file_name: Optional[str] = None,
        attachment_size: Optional[int] = None,
        latitude=None,
        longitude=None,
        forwarded_from: Optional[str] = None,
        forwarded_origin_type: Optional[str] = None,
        forwarded_date=None,
    ) -> SupportMessage:
        content = (content or "").strip() or None

        # The write guard that matches the widened read: a message is valid
        # when it carries text, OR a file, OR coordinates. `unsupported` is the
        # deliberate exception — its whole point is to record that something
        # arrived when we can carry none of the payload.
        if message_type in _FILE_TYPES and not telegram_file_id:
            raise ValidationError(f"{message_type.value} message requires a telegram_file_id")
        if message_type == SupportMessageType.LOCATION and (latitude is None or longitude is None):
            raise ValidationError("Location message requires latitude and longitude")
        # Matches the serializer's own `has_coords` (support_serializers.py):
        # a lone coordinate is not a usable payload, so it must not count as
        # one here either — checking `latitude` alone would let a
        # longitude-only, content-less, file-less message through.
        has_coords = latitude is not None and longitude is not None
        if not content and not telegram_file_id and not has_coords and message_type != SupportMessageType.UNSUPPORTED:
            raise ValidationError("Message content is required")

        user = User.query.get(user_id)
        if not user:
            raise NotFoundError(f"User with ID {user_id} not found")

        conv = self._get_or_create_conversation(user_id)
        msg = SupportMessage(
            conversation_id=conv.id,
            direction=SupportMessageDirection.INBOUND,
            content=content,
            message_type=message_type,
            telegram_file_id=telegram_file_id,
            attachment_mime_type=attachment_mime_type,
            attachment_file_name=attachment_file_name,
            attachment_size=attachment_size,
            latitude=latitude,
            longitude=longitude,
            forwarded_from=forwarded_from,
            forwarded_origin_type=forwarded_origin_type,
            forwarded_date=forwarded_date,
            telegram_message_id=str(telegram_message_id) if telegram_message_id is not None else None,
            is_read=False,
        )
        db.session.add(msg)
        db.session.flush()
        self._touch(
            conv,
            build_preview(message_type, content, attachment_file_name),
            SupportMessageDirection.INBOUND,
        )
        db.session.commit()
        logger.info(
            "Recorded inbound support message %s (%s) for user %s",
            msg.id,
            message_type.value,
            user_id,
        )
        return msg

    def send_message_to_user(self, target_user_id: int, admin_user_id: int, content: str) -> dict:
        content = (content or "").strip()
        if not content:
            raise ValidationError("Message content is required")
        user = User.query.get(target_user_id)
        if not user:
            raise NotFoundError(f"User with ID {target_user_id} not found")
        if not getattr(user, "telegram_id", None):
            raise ValidationError("This user is not connected to Telegram")

        conv = self._get_or_create_conversation(target_user_id)
        msg = SupportMessage(
            conversation_id=conv.id,
            direction=SupportMessageDirection.OUTBOUND,
            content=content,
            sender_admin_id=admin_user_id,
            delivery_status=SupportMessageDeliveryStatus.PENDING,
            is_read=True,
        )
        db.session.add(msg)
        db.session.flush()

        delivery = get_notification_service().send_user_telegram_message(user, content)
        self._apply_delivery(msg, delivery)

        self._touch(conv, build_preview(SupportMessageType.TEXT, content), SupportMessageDirection.OUTBOUND)
        db.session.commit()
        logger.info(
            "Admin %s sent support message %s to user %s (delivery=%s)",
            admin_user_id,
            msg.id,
            target_user_id,
            msg.delivery_status,
        )
        return {"message": msg, "delivery": delivery}

    def send_media_to_user(
        self,
        conversation_id: int,
        admin_user_id: int,
        *,
        file_bytes: bytes = None,
        filename: str = None,
        mime_type: str = None,
        caption: str = None,
        latitude=None,
        longitude=None,
    ) -> dict:
        """Send an attachment or a pin into an existing conversation.

        Shares the PENDING -> SENT/FAILED lifecycle with `send_message_to_user`
        (via `_apply_delivery`) rather than duplicating it, so the admin UI's
        "Not delivered" tag means the same thing for every outbound message.
        """
        conv = SupportConversation.query.get(conversation_id)
        if not conv:
            raise NotFoundError(f"Conversation {conversation_id} not found")
        user = User.query.get(conv.user_id)
        if not user:
            raise NotFoundError(f"User with ID {conv.user_id} not found")
        if not getattr(user, "telegram_id", None):
            raise ValidationError("This user is not connected to Telegram")

        is_location = latitude is not None and longitude is not None
        if not is_location and not file_bytes:
            raise ValidationError("A file or a latitude/longitude pair is required")
        if caption and len(caption) > MAX_CAPTION_LENGTH:
            raise ValidationError(f"Caption must be at most {MAX_CAPTION_LENGTH} characters")

        notification_service = get_notification_service()
        if is_location:
            delivery = notification_service.send_user_telegram_location(user, latitude, longitude)
            message_type = SupportMessageType.LOCATION
        else:
            delivery = notification_service.send_user_telegram_media(
                user, file_bytes, filename, mime_type, caption=caption
            )
            message_type = SupportMessageType(delivery.get("message_type", "document"))

        content = (caption or "").strip() or None
        stored_filename = filename[:MAX_ATTACHMENT_FILE_NAME_LENGTH] if (filename and not is_location) else None
        msg = SupportMessage(
            conversation_id=conv.id,
            direction=SupportMessageDirection.OUTBOUND,
            content=content,
            message_type=message_type,
            sender_admin_id=admin_user_id,
            telegram_file_id=delivery.get("file_id"),
            attachment_mime_type=mime_type if not is_location else None,
            attachment_file_name=stored_filename,
            attachment_size=len(file_bytes) if file_bytes else None,
            latitude=latitude,
            longitude=longitude,
            is_read=True,
        )
        self._apply_delivery(msg, delivery)

        db.session.add(msg)
        db.session.flush()
        self._touch(
            conv,
            build_preview(message_type, content, filename),
            SupportMessageDirection.OUTBOUND,
        )
        db.session.commit()
        logger.info(
            "Admin %s sent %s message %s to conversation %s (delivery=%s)",
            admin_user_id,
            message_type.value,
            msg.id,
            conversation_id,
            msg.delivery_status,
        )
        return {"message": msg, "delivery": delivery}

    def send_reply(self, conversation_id: int, admin_user_id: int, content: str) -> dict:
        conv = SupportConversation.query.get(conversation_id)
        if not conv:
            raise NotFoundError(f"Conversation {conversation_id} not found")
        return self.send_message_to_user(conv.user_id, admin_user_id, content)

    def _unread_subquery(self):
        return (
            db.session.query(
                SupportMessage.conversation_id.label("cid"),
                func.count(SupportMessage.id).label("unread"),
            )
            .filter(
                SupportMessage.direction == SupportMessageDirection.INBOUND,
                SupportMessage.is_read.is_(False),
            )
            .group_by(SupportMessage.conversation_id)
            .subquery()
        )

    def list_conversations(self, page=1, per_page=20, search=None, unread_only=False) -> dict:
        page = max(int(page), 1)
        per_page = min(max(int(per_page), 1), 100)
        unread_sq = self._unread_subquery()
        unread_col = func.coalesce(unread_sq.c.unread, 0)

        query = (
            db.session.query(SupportConversation, User, unread_col)
            .join(User, SupportConversation.user_id == User.id)
            .outerjoin(unread_sq, unread_sq.c.cid == SupportConversation.id)
        )
        if search:
            term = f"%{search.strip()}%"
            query = query.filter(
                or_(
                    User.first_name.ilike(term),
                    User.last_name.ilike(term),
                    User.phone.ilike(term),
                    User.telegram_username.ilike(term),
                )
            )
        if unread_only:
            query = query.filter(unread_col > 0)

        query = query.order_by(SupportConversation.last_message_at.desc().nullslast())
        total = query.count()
        rows = query.limit(per_page).offset((page - 1) * per_page).all()

        items = []
        for conv, user, unread in rows:
            name = f"{user.first_name or ''} {user.last_name or ''}".strip() or (user.email or f"User {user.id}")
            data = conv.to_dict()
            data["user"] = {
                "id": user.id,
                "name": name,
                "phone": user.phone,
                "telegram_username": getattr(user, "telegram_username", None),
            }
            data["unread_count"] = int(unread or 0)
            items.append(data)

        total_unread = (
            db.session.query(func.count(SupportMessage.id))
            .filter(
                SupportMessage.direction == SupportMessageDirection.INBOUND,
                SupportMessage.is_read.is_(False),
            )
            .scalar()
        ) or 0

        return {
            "items": items,
            "total": total,
            "total_unread": int(total_unread),
            "page": page,
            "per_page": per_page,
        }

    def get_thread(self, conversation_id: int, page=1, per_page=50) -> dict:
        page = max(int(page), 1)
        per_page = min(max(int(per_page), 1), 100)
        conv = SupportConversation.query.get(conversation_id)
        if not conv:
            raise NotFoundError(f"Conversation {conversation_id} not found")

        base = SupportMessage.query.filter_by(conversation_id=conversation_id)
        total = base.count()
        messages = base.order_by(SupportMessage.created_at.asc()).limit(per_page).offset((page - 1) * per_page).all()
        user = User.query.get(conv.user_id)
        conv_data = conv.to_dict()
        if user:
            conv_data["user"] = {
                "id": user.id,
                "name": f"{user.first_name or ''} {user.last_name or ''}".strip() or (user.email or f"User {user.id}"),
                "phone": user.phone,
                "telegram_username": getattr(user, "telegram_username", None),
            }
        return {
            "conversation": conv_data,
            "items": [m.to_dict() for m in messages],
            "total": total,
            "page": page,
            "per_page": per_page,
        }

    def mark_read(self, conversation_id: int) -> int:
        conv = SupportConversation.query.get(conversation_id)
        if not conv:
            raise NotFoundError(f"Conversation {conversation_id} not found")
        count = SupportMessage.query.filter(
            SupportMessage.conversation_id == conversation_id,
            SupportMessage.direction == SupportMessageDirection.INBOUND,
            SupportMessage.is_read.is_(False),
        ).update({SupportMessage.is_read: True}, synchronize_session=False)
        db.session.commit()
        return int(count or 0)
