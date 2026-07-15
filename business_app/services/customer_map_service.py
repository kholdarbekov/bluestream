"""Read model for the admin customer map (one pin per geocoded address)."""

from collections import defaultdict
from decimal import Decimal
from typing import Any, Dict, List

from sqlalchemy import func

from business_app import db
from business_app.models.user import User, UserAddress
from business_app.models.order import Order
from business_app.models.bottle import BottleBalance
from business_app.models.payment import Payment
from shared.enums import (
    UserRole,
    UserType,
    EntitySubtype,
    OrderStatus,
    PaymentMethod,
)

# SSOT threshold used to flag COD-restricted customers (class attr, cash_collection_service.py:40).
from business_app.services.cash_collection_service import CashCollectionService

_COD_LIMIT = CashCollectionService.COD_ACTIVE_DEBT_LIMIT


class CustomerMapService:
    @staticmethod
    def get_customer_map_pins() -> List[Dict[str, Any]]:
        # Last non-cancelled order per user (recency + count).
        last_order = (
            db.session.query(
                Order.user_id.label("user_id"),
                func.max(Order.created_at).label("last_order_date"),
                func.count(Order.id).label("order_count"),
            )
            .filter(Order.status != OrderStatus.CANCELLED)
            .group_by(Order.user_id)
            .subquery()
        )

        # Open delivered COD debt per user.
        cod_debt = (
            db.session.query(
                Payment.user_id.label("user_id"),
                func.count(Payment.id).label("active_cod_debt_count"),
                func.coalesce(func.sum(Payment.outstanding_amount), Decimal("0.00")).label("outstanding_debt"),
            )
            .join(Order, Order.id == Payment.order_id)
            .filter(
                Payment.payment_method == PaymentMethod.CASH,
                Payment.outstanding_amount > 0,
                Order.status == OrderStatus.DELIVERED,
            )
            .group_by(Payment.user_id)
            .subquery()
        )

        rows = (
            db.session.query(
                UserAddress.id.label("address_id"),
                UserAddress.user_id.label("user_id"),
                UserAddress.latitude,
                UserAddress.longitude,
                UserAddress.is_default,
                UserAddress.full_address,
                UserAddress.district,
                User.first_name,
                User.last_name,
                User.phone,
                User.user_type,
                User.entity_subtype,
                User.cod_debt_check_exempt,
                last_order.c.last_order_date,
                last_order.c.order_count,
                func.coalesce(BottleBalance.balance, Decimal("0.00")).label("bottle_balance"),
                func.coalesce(cod_debt.c.active_cod_debt_count, 0).label("active_cod_debt_count"),
                func.coalesce(cod_debt.c.outstanding_debt, Decimal("0.00")).label("outstanding_debt"),
            )
            .join(User, UserAddress.user_id == User.id)
            .join(last_order, last_order.c.user_id == User.id)  # INNER -> ordered customers only
            .outerjoin(
                BottleBalance,
                (BottleBalance.user_id == UserAddress.user_id) & (BottleBalance.address_id == UserAddress.id),
            )
            .outerjoin(cod_debt, cod_debt.c.user_id == User.id)
            .filter(
                UserAddress.latitude.isnot(None),
                UserAddress.longitude.isnot(None),
                User.role == UserRole.CUSTOMER,
                User.user_type.in_([UserType.INDIVIDUAL, UserType.ENTITY]),
            )
            .order_by(UserAddress.user_id, UserAddress.id)
            .all()
        )

        by_user: Dict[int, list] = defaultdict(list)
        for r in rows:
            by_user[r.user_id].append(r)

        def _val(x):
            return x.value if hasattr(x, "value") else x

        pins: List[Dict[str, Any]] = []
        for _uid, urows in by_user.items():
            count = len(urows)
            for idx, r in enumerate(urows, start=1):
                active_count = int(r.active_cod_debt_count or 0)
                # Mirrors User.is_grocery_store inline (row values from the aggregate
                # query above) rather than loading ORM User objects per row, so this
                # stays a single query instead of N+1 per-customer property access.
                is_grocery = r.user_type == UserType.ENTITY and r.entity_subtype == EntitySubtype.GROCERY_STORE
                exempt = bool(r.cod_debt_check_exempt) or is_grocery
                pins.append(
                    {
                        "address_id": r.address_id,
                        "user_id": r.user_id,
                        "full_name": f"{r.first_name or ''} {r.last_name or ''}".strip(),
                        "phone": r.phone,
                        "user_type": _val(r.user_type),
                        "entity_subtype": _val(r.entity_subtype) if r.entity_subtype else None,
                        "lat": float(r.latitude),
                        "lng": float(r.longitude),
                        "is_default": bool(r.is_default),
                        "address_label": r.full_address or r.district or "",
                        "address_index": idx,
                        "address_count": count,
                        "last_order_date": r.last_order_date,
                        "order_count": int(r.order_count or 0),
                        "bottle_balance": r.bottle_balance or Decimal("0.00"),
                        "outstanding_debt": r.outstanding_debt or Decimal("0.00"),
                        "active_cod_debt_count": active_count,
                        "cod_restricted": (active_count >= _COD_LIMIT) and not exempt,
                    }
                )
        return pins
