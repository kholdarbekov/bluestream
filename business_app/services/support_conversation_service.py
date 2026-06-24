import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, or_

from business_app import db
from business_app.models.support import (
    SupportConversation,
    SupportConversationStatus,
    SupportMessage,
    SupportMessageDirection,
)
from business_app.models.user import User
from business_app.utils.exceptions import NotFoundError, ValidationError
from business_app.utils.service_factory import get_notification_service

logger = logging.getLogger(__name__)

PREVIEW_LEN = 200


def _now():
    return datetime.now(timezone.utc)


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

    def _touch(self, conv: SupportConversation, content: str, direction: SupportMessageDirection):
        conv.last_message_at = _now()
        conv.last_message_preview = (content or "")[:PREVIEW_LEN]
        conv.last_message_direction = direction
        conv.status = SupportConversationStatus.OPEN

    def record_inbound_message(
        self, user_id: int, content: str, telegram_message_id: Optional[str] = None
    ) -> SupportMessage:
        content = (content or "").strip()
        if not content:
            raise ValidationError("Message content is required")
        user = User.query.get(user_id)
        if not user:
            raise NotFoundError(f"User with ID {user_id} not found")

        conv = self._get_or_create_conversation(user_id)
        msg = SupportMessage(
            conversation_id=conv.id,
            direction=SupportMessageDirection.INBOUND,
            content=content,
            telegram_message_id=str(telegram_message_id) if telegram_message_id is not None else None,
            is_read=False,
        )
        db.session.add(msg)
        db.session.flush()
        self._touch(conv, content, SupportMessageDirection.INBOUND)
        db.session.commit()
        logger.info("Recorded inbound support message %s for user %s", msg.id, user_id)
        return msg

    def send_message_to_user(self, target_user_id: int, admin_user_id: int, content: str) -> dict:
        from business_app.models.support import SupportMessageDeliveryStatus

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
        if delivery.get("success"):
            msg.delivery_status = SupportMessageDeliveryStatus.SENT
            message_id = delivery.get("message_id")
            msg.telegram_message_id = str(message_id) if message_id is not None else None
        else:
            msg.delivery_status = SupportMessageDeliveryStatus.FAILED
            msg.delivery_error = (delivery.get("error") or "Delivery failed")[:500]

        self._touch(conv, content, SupportMessageDirection.OUTBOUND)
        db.session.commit()
        logger.info(
            "Admin %s sent support message %s to user %s (delivery=%s)",
            admin_user_id,
            msg.id,
            target_user_id,
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
