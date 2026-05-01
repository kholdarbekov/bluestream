"""
Staff notification Celery tasks.
Handles sending notifications to staff bot via internal webhooks.
"""

import logging
import requests
import os
from datetime import datetime, timezone
from uuid import uuid4
from celery import shared_task

logger = logging.getLogger(__name__)

STAFF_BOT_WEBHOOK_URL = os.environ.get("STAFF_BOT_WEBHOOK_URL", "http://staff_bot:8081")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")


def _get_webhook_headers():
    """Build webhook headers with HMAC signature."""

    return {
        "Content-Type": "application/json",
    }


def _send_staff_webhook(endpoint: str, data: dict, timeout: int = 10) -> bool:
    """Send webhook to staff bot's internal server."""
    url = f"{STAFF_BOT_WEBHOOK_URL}{endpoint}"
    try:
        payload = dict(data or {})
        payload.setdefault("event_id", f"{endpoint.strip('/').replace('/', '_')}:{uuid4().hex}")
        payload.setdefault("event_timestamp", datetime.now(timezone.utc).isoformat())

        # Build HMAC signature
        import hmac
        import hashlib
        import json

        body = json.dumps(payload).encode("utf-8")
        secret = WEBHOOK_SECRET or os.environ.get("JWT_SECRET_KEY", "")
        if not secret:
            logger.error(
                "Staff webhook secret is not configured; refusing to send unsigned webhook " "for endpoint %s",
                endpoint,
            )
            return False

        signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

        headers = {
            "Content-Type": "application/json",
            "X-Bot-Webhook-Signature": signature,
        }

        response = requests.post(url, json=payload, headers=headers, timeout=timeout)
        if response.status_code == 200:
            logger.info(f"Staff webhook sent successfully: {endpoint}")
            return True
        else:
            logger.warning(f"Staff webhook failed: {endpoint} -> {response.status_code}: {response.text}")
            return False
    except requests.exceptions.ConnectionError:
        logger.warning(f"Staff bot not reachable at {url} - notification skipped")
        return False
    except Exception as e:
        logger.error(f"Error sending staff webhook {endpoint}: {e}")
        return False


@shared_task(name="staff.notify_new_order", bind=True, max_retries=2, default_retry_delay=30)
def notify_staff_new_order(self, order_id: int, order_info: dict = None):
    """
    Notify delivery persons about a new order available for pickup.

    Args:
        order_id: ID of the new order
        order_info: Pre-built order info dict (order_number, district, etc.)
    """
    try:
        from business_app import create_app, db
        from business_app.models.delivery import DeliveryPerson
        from business_app.models.user import User

        app = create_app()
        with app.app_context():
            # Get all active delivery persons who haven't muted notifications
            delivery_persons = (
                db.session.query(User.telegram_id)
                .join(DeliveryPerson, DeliveryPerson.user_id == User.id)
                .filter(
                    DeliveryPerson.notifications_muted == False, User.telegram_id.isnot(None), User.status == "active"
                )
                .all()
            )

            telegram_ids = [dp.telegram_id for dp in delivery_persons if dp.telegram_id]

            if not telegram_ids:
                logger.info(f"No delivery persons to notify for order {order_id}")
                return

            # Build order info if not provided
            if not order_info:
                from business_app.models.order import Order

                order = Order.query.get(order_id)
                if not order:
                    logger.warning(f"Order {order_id} not found for notification")
                    return

                order_info = {
                    "order_id": order_id,
                    "order_number": order.order_number,
                    "total_amount": float(order.total_amount) if order.total_amount else 0,
                    "payment_method": order.payment_method.value if order.payment_method else "cash",
                    "item_count": len(order.order_items) if order.order_items else 0,
                }

            data = {
                "event_id": f"new_order:{self.request.id}",
                "order_id": order_id,
                "delivery_person_telegram_ids": telegram_ids,
                "order_info": order_info,
            }

            _send_staff_webhook("/internal/new-order", data)

    except Exception as e:
        logger.error(f"Error in notify_staff_new_order: {e}")
        raise self.retry(exc=e)


@shared_task(name="staff.notify_order_assigned", bind=True)
def notify_staff_order_assigned(self, telegram_id: str, order_info: dict):
    """Notify a delivery person that an order was assigned to them by admin."""
    _send_staff_webhook(
        "/internal/order-assigned",
        {
            "event_id": f"order_assigned:{self.request.id}",
            "telegram_id": telegram_id,
            "order_info": order_info,
        },
    )


@shared_task(name="staff.notify_order_reassigned", bind=True)
def notify_staff_order_reassigned(self, old_telegram_id: str, new_telegram_id: str, order_info: dict):
    """Notify both old and new delivery persons about a reassignment."""
    _send_staff_webhook(
        "/internal/order-reassigned",
        {
            "event_id": f"order_reassigned:{self.request.id}",
            "old_telegram_id": old_telegram_id,
            "new_telegram_id": new_telegram_id,
            "order_info": order_info,
        },
    )


@shared_task(name="staff.notify_order_cancelled", bind=True)
def notify_staff_order_cancelled(self, telegram_id: str, order_info: dict):
    """Notify delivery person that an assigned order was cancelled."""
    if not telegram_id:
        return
    _send_staff_webhook(
        "/internal/order-cancelled",
        {
            "event_id": f"order_cancelled:{self.request.id}",
            "telegram_id": telegram_id,
            "order_info": order_info,
        },
    )
