"""A customer's own account data as a download: POST /api/v1/auth/export-data, the cabinet's "Download my account data".

The customer's own rows only, as plain JSON values: enums as their values, every instant as ISO 8601 in UTC
(+00:00), calendar dates as YYYY-MM-DD, money as plain numbers. The route sends it as an attachment, indented UTF-8
text, and the API's TimezoneMiddleware leaves attachments alone, so the downloaded file is this document as written
(review EXP-1). The parts:

- `user_profile`, `orders` (Order.user_id) and `addresses` (review CAB-R02 fixed only why the endpoint answered 500);
- `subscriptions` (owner 2026-11-14): Subscription.user_id, as the customer's list (GET /api/v1/subscriptions/)
  filters. The name as it is stored; the schedule as stored (`delivery_day_of_week` 0 = Monday, the chosen slot's
  window or none for "Any time"); each item's product by the name the cabinet shows it (`line_labels`). Paused at
  `paused_at`; a cancelled one ended at `end_date`, which the cancel stamps;
- `payments` (owner 2026-11-14): Payment.user_id, the customer the payment is for, as the payment history
  (GET /api/v1/payments/) filters. Never `collected_by`, the second foreign key to users, which names the staff
  member who took the cash. Nothing from the gateway: no provider ids or data, payment links, idempotency keys or
  failure text. A refund shows in `status`; no refunded amount is recorded anywhere, so none is exported;
- `export_date`.

Hiding a field or adding a category (AquaCoins, bottles) is the owner's call.
"""

from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import joinedload, selectinload

from business_app.models.order import Order
from business_app.models.payment import Payment
from business_app.models.subscription import Subscription, SubscriptionItem
from business_app.models.user import User, UserAddress


def _value(field: Any) -> Any:
    """An enum column as its stored value; anything else as it is."""
    return getattr(field, "value", field)


def _iso(moment: Optional[date]) -> Optional[str]:
    """An instant as ISO 8601 in UTC (a naive one is UTC: every write stamps `datetime.now(UTC)`, and only the
    test database drops the zone), a calendar date as YYYY-MM-DD; None stays None."""
    if moment is None:
        return None
    if isinstance(moment, datetime):
        moment = moment.astimezone(timezone.utc) if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    return moment.isoformat()


def _money(amount: Any) -> float:
    return float(amount or 0)


class AccountExportService:
    @staticmethod
    def export_for_user(user: User) -> Dict[str, Any]:
        """The export document for `user`, every value JSON-serialisable."""
        orders = Order.query.filter_by(user_id=user.id).order_by(Order.created_at.desc()).all()
        addresses = UserAddress.query.filter_by(user_id=user.id).order_by(UserAddress.id).all()
        return {
            "user_profile": {
                "id": user.id,
                "email": user.email,
                "phone": user.phone,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "date_of_birth": _iso(user.date_of_birth),
                "gender": _value(user.gender),
                "preferred_language": user.preferred_language,
                "status": _value(user.status),
                "registration_source": user.registration_source,
                "created_at": _iso(user.created_at),
                "email_verified_at": _iso(user.email_verified_at),
                "phone_verified_at": _iso(user.phone_verified_at),
            },
            "orders": [
                {
                    "id": order.id,
                    "order_number": order.order_number,
                    "status": _value(order.status),
                    "platform": _value(order.order_source),
                    "total_amount": _money(order.total_amount),
                    "created_at": _iso(order.created_at),
                }
                for order in orders
            ],
            "addresses": [
                {
                    "id": address.id,
                    "title": address.title,
                    "full_address": address.full_address,
                    "city": address.city,
                    "is_default": bool(address.is_default),
                    "is_business": bool(address.is_business),
                }
                for address in addresses
            ],
            "subscriptions": AccountExportService._subscriptions(user),
            "payments": AccountExportService._payments(user),
            "export_date": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _subscriptions(user: User) -> List[Dict[str, Any]]:
        from business_app.services.subscription_service import SubscriptionService

        subscriptions = (
            Subscription.query.filter_by(user_id=user.id)
            .options(
                selectinload(Subscription.subscription_items).joinedload(SubscriptionItem.product),
                joinedload(Subscription.delivery_address),
                joinedload(Subscription.delivery_time_slot),
            )
            .order_by(Subscription.created_at.desc(), Subscription.id.desc())
            .all()
        )
        exported = []
        for subscription in subscriptions:
            slot = subscription.delivery_time_slot
            address = subscription.delivery_address
            exported.append(
                {
                    "id": subscription.id,
                    "subscription_number": subscription.subscription_number,
                    "name": subscription.name,
                    "status": _value(subscription.status),
                    "frequency": _value(subscription.frequency),
                    "delivery_day_of_week": subscription.delivery_day_of_week,
                    "delivery_day_of_month": subscription.delivery_day_of_month,
                    "delivery_time_slot": (
                        {"id": slot.id, "start_time": slot.start_time, "end_time": slot.end_time} if slot else None
                    ),
                    "delivery_address_id": subscription.delivery_address_id,
                    "delivery_address_title": address.title if address else None,
                    "payment_method": _value(subscription.payment_method),
                    "items": [
                        {
                            "product_id": item.product_id,
                            "product_name": SubscriptionService.line_labels(item.product, item.quantity)[
                                "product_name"
                            ],
                            "quantity": item.quantity,
                        }
                        for item in sorted(subscription.subscription_items, key=lambda line: line.id)
                    ],
                    "next_delivery_date": _iso(subscription.next_delivery_date),
                    "last_delivery_date": _iso(subscription.last_delivery_date),
                    "start_date": _iso(subscription.start_date),
                    "paused_at": _iso(subscription.paused_at),
                    "end_date": _iso(subscription.end_date),
                    "created_at": _iso(subscription.created_at),
                }
            )
        return exported

    @staticmethod
    def _payments(user: User) -> List[Dict[str, Any]]:
        payments = (
            Payment.query.filter(Payment.user_id == user.id)
            .options(joinedload(Payment.order))
            .order_by(Payment.created_at.desc(), Payment.id.desc())
            .all()
        )
        return [
            {
                "id": payment.id,
                "order_number": payment.order.order_number if payment.order else None,
                "subscription_id": payment.subscription_id,
                "amount": _money(payment.amount),
                "amount_collected": _money(payment.amount_collected),
                "outstanding_amount": _money(payment.outstanding_amount),
                "currency": payment.currency,
                "payment_method": _value(payment.payment_method),
                "status": _value(payment.status),
                "created_at": _iso(payment.created_at),
                "paid_at": _iso(payment.paid_at),
            }
            for payment in payments
        ]
