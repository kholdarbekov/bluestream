"""Try-out product and bottle-custody workflows."""

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import or_
from sqlalchemy.orm import joinedload, selectinload

from business_app import db
from business_app.models.product import Product
from business_app.models.tryout import (
    ProductTryout,
    ProductTryoutItem,
    TrialContact,
    TrialContactAddress,
    TryoutBottleLedger,
    TryoutTask,
)
from business_app.models.user import User, UserAddress
from business_app.services.auth_service import AuthService
from business_app.services.maps_service import MapsService
from business_app.utils.constants import (
    TryoutBottleLedgerEventType,
    TryoutOutcome,
    TryoutStatus,
    TryoutTaskStatus,
    TryoutTaskType,
)
from business_app.utils.exceptions import ConflictError, NotFoundError, ValidationError


class TryoutService:
    """Business logic for try-outs and bottle custody."""

    DEFAULT_RETURN_DUE_DAYS = 14
    DUE_SOON_DAYS = 2

    @staticmethod
    def _as_decimal(value: Any) -> Decimal:
        return Decimal(str(value or 0))

    @staticmethod
    def _status_value(value: Any) -> str:
        return value.value if hasattr(value, "value") else str(value)

    @staticmethod
    def _as_utc_datetime(value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    def _load_tryout(tryout_id: int) -> ProductTryout:
        tryout = ProductTryout.query.options(
            joinedload(ProductTryout.trial_contact),
            joinedload(ProductTryout.converted_user),
            selectinload(ProductTryout.items).joinedload(ProductTryoutItem.product),
            selectinload(ProductTryout.tasks).joinedload(TryoutTask.assigned_driver),
            selectinload(ProductTryout.bottle_ledger_entries).joinedload(TryoutBottleLedger.product),
        ).get(tryout_id)
        if not tryout:
            raise NotFoundError("Try-out not found")
        return tryout

    @staticmethod
    def _load_task(task_id: int) -> TryoutTask:
        task = TryoutTask.query.options(
            joinedload(TryoutTask.tryout).joinedload(ProductTryout.trial_contact),
            joinedload(TryoutTask.tryout).selectinload(ProductTryout.items).joinedload(ProductTryoutItem.product),
            joinedload(TryoutTask.tryout).selectinload(ProductTryout.tasks).joinedload(TryoutTask.assigned_driver),
            joinedload(TryoutTask.tryout).selectinload(ProductTryout.bottle_ledger_entries).joinedload(TryoutBottleLedger.product),
        ).get(task_id)
        if not task:
            raise NotFoundError("Try-out task not found")
        return task

    @staticmethod
    def _get_or_create_contact(payload: Dict[str, Any]) -> TrialContact:
        phone = (payload.get("phone") or "").strip()
        if not phone:
            raise ValidationError("Trial contact phone is required")

        existing = TrialContact.query.filter_by(phone=phone).order_by(TrialContact.id.desc()).first()
        if existing:
            existing.first_name = payload.get("first_name", existing.first_name)
            existing.last_name = payload.get("last_name") or existing.last_name
            existing.company_name = payload.get("company_name") or existing.company_name
            existing.preferred_language = payload.get("preferred_language") or existing.preferred_language or "uz"
            existing.notes = payload.get("notes") or existing.notes
            return existing

        contact = TrialContact(
            first_name=payload["first_name"].strip(),
            last_name=(payload.get("last_name") or "").strip() or None,
            phone=phone,
            company_name=(payload.get("company_name") or "").strip() or None,
            preferred_language=payload.get("preferred_language") or "uz",
            notes=payload.get("notes"),
        )
        db.session.add(contact)
        db.session.flush()
        return contact

    @staticmethod
    def _create_or_update_address(contact: TrialContact, payload: Dict[str, Any]) -> TrialContactAddress:
        if not payload.get("full_address"):
            raise ValidationError("Try-out address is required")

        full_address = payload["full_address"].strip()
        if payload.get("is_default", True):
            TrialContactAddress.query.filter_by(
                trial_contact_id=contact.id,
                is_default=True,
            ).update({"is_default": False})

        address = TrialContactAddress.query.filter_by(
            trial_contact_id=contact.id,
            full_address=full_address,
        ).first()
        if not address:
            address = TrialContactAddress(
                trial_contact_id=contact.id,
                full_address=full_address,
            )
            db.session.add(address)

        address.label = (payload.get("label") or "").strip() or "Try-out"
        address.full_address = full_address
        address.district = payload.get("district")
        address.city = payload.get("city") or "Tashkent"
        address.latitude = payload.get("latitude")
        address.longitude = payload.get("longitude")
        address.delivery_notes = payload.get("delivery_notes")
        address.is_default = bool(payload.get("is_default", True))
        if address.latitude is None or address.longitude is None:
            TryoutService._populate_missing_coordinates(address)
        db.session.flush()
        return address

    @staticmethod
    def _populate_missing_coordinates(address: TrialContactAddress) -> None:
        if not address.full_address or (address.latitude is not None and address.longitude is not None):
            return

        try:
            result = MapsService().geocode_address(address.full_address, city=address.city or "Tashkent")
        except Exception:
            return

        latitude = result.get("latitude") if isinstance(result, dict) else None
        longitude = result.get("longitude") if isinstance(result, dict) else None
        if latitude is not None and longitude is not None:
            address.latitude = latitude
            address.longitude = longitude

    @staticmethod
    def _build_address_snapshot(contact: TrialContact, address: TrialContactAddress) -> Dict[str, Any]:
        return {
            "trial_contact_id": contact.id,
            "trial_contact_name": contact.full_name,
            "phone": contact.phone,
            "company_name": contact.company_name,
            "label": address.label,
            "full_address": address.full_address,
            "district": address.district,
            "city": address.city,
            "latitude": float(address.latitude) if address.latitude is not None else None,
            "longitude": float(address.longitude) if address.longitude is not None else None,
            "delivery_notes": address.delivery_notes,
        }

    @staticmethod
    def _upsert_converted_user_address(auth_service: AuthService, user_id: int, snapshot: Dict[str, Any]) -> None:
        payload = {
            "title": snapshot.get("label") or "Try-out",
            "full_address": snapshot.get("full_address"),
            "district": snapshot.get("district"),
            "city": snapshot.get("city") or "Tashkent",
            "latitude": snapshot.get("latitude"),
            "longitude": snapshot.get("longitude"),
            "delivery_notes": snapshot.get("delivery_notes"),
            "is_default": True,
        }
        existing = UserAddress.query.filter_by(
            user_id=user_id,
            full_address=payload["full_address"],
        ).order_by(UserAddress.id.desc()).first()
        if existing:
            auth_service.update_user_address(user_id, existing.id, payload)
            if not existing.is_default:
                auth_service.set_default_user_address(user_id, existing.id)
            return

        auth_service.add_user_address(user_id, payload)

    @staticmethod
    def _validate_and_build_items(items_payload: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        built_items: List[Dict[str, Any]] = []
        for item_payload in items_payload:
            product = Product.query.get(int(item_payload["product_id"]))
            if not product:
                raise NotFoundError(f"Product {item_payload['product_id']} not found")
            if not product.is_active:
                raise ValidationError(f"Product {product.name} is not active")
            if not getattr(product, "is_tryout_eligible", True):
                raise ValidationError(f"Product {product.name} is not eligible for try-outs")

            quantity = int(item_payload["quantity"])
            if quantity < 1:
                raise ValidationError("Try-out item quantity must be positive")

            returnable_due = Decimal("0.00")
            if getattr(product, "tracks_returnable_bottles", False):
                returnable_due = TryoutService._as_decimal(product.returnable_bottles_per_unit) * Decimal(str(quantity))

            built_items.append(
                {
                    "product": product,
                    "quantity": quantity,
                    "unit_price_snapshot": TryoutService._as_decimal(product.base_price),
                    "returnable_bottles_due": returnable_due,
                }
            )
        return built_items

    @staticmethod
    def _update_trial_contact(contact: TrialContact, payload: Dict[str, Any]) -> None:
        if "first_name" in payload and payload.get("first_name"):
            contact.first_name = payload["first_name"].strip()
        if "last_name" in payload:
            contact.last_name = (payload.get("last_name") or "").strip() or None
        if "phone" in payload and payload.get("phone"):
            contact.phone = payload["phone"].strip()
        if "company_name" in payload:
            contact.company_name = (payload.get("company_name") or "").strip() or None
        if "preferred_language" in payload and payload.get("preferred_language"):
            contact.preferred_language = payload["preferred_language"]
        if "notes" in payload:
            contact.notes = payload.get("notes")

    @staticmethod
    def _replace_tryout_items(tryout: ProductTryout, items_payload: Iterable[Dict[str, Any]]) -> None:
        if tryout.handoff_completed_at:
            raise ValidationError("Try-out items cannot be changed after handoff is completed")

        built_items = TryoutService._validate_and_build_items(items_payload)
        for item in list(tryout.items or []):
            db.session.delete(item)
        db.session.flush()

        for item_data in built_items:
            db.session.add(
                ProductTryoutItem(
                    tryout_id=tryout.id,
                    product_id=item_data["product"].id,
                    quantity=item_data["quantity"],
                    unit_price_snapshot=item_data["unit_price_snapshot"],
                    returnable_bottles_due=item_data["returnable_bottles_due"],
                )
            )
        db.session.flush()

    @staticmethod
    def _get_open_handoff_task(tryout: ProductTryout) -> Optional[TryoutTask]:
        return next(
            (
                task for task in (tryout.tasks or [])
                if TryoutService._status_value(task.task_type) == TryoutTaskType.HANDOFF.value
                and TryoutService._status_value(task.status) in {TryoutTaskStatus.OPEN.value, TryoutTaskStatus.ASSIGNED.value}
            ),
            None,
        )

    @staticmethod
    def _get_open_pickup_task(tryout: ProductTryout) -> Optional[TryoutTask]:
        return next(
            (
                task for task in (tryout.tasks or [])
                if TryoutService._status_value(task.task_type) == TryoutTaskType.PICKUP.value
                and TryoutService._status_value(task.status) in {TryoutTaskStatus.OPEN.value, TryoutTaskStatus.ASSIGNED.value}
            ),
            None,
        )

    @staticmethod
    def _assign_tryout_number(tryout: ProductTryout) -> None:
        if tryout.tryout_number:
            return
        year_suffix = datetime.now(UTC).strftime("%y")
        tryout.tryout_number = f"TRY_{tryout.id:06d}_{year_suffix}"

    @staticmethod
    def _calculate_outstanding_from_entries(entries: Iterable[TryoutBottleLedger]) -> Dict[int, Decimal]:
        totals: Dict[int, Decimal] = defaultdict(lambda: Decimal("0.00"))
        for entry in entries:
            units = TryoutService._as_decimal(entry.units)
            event_type = TryoutService._status_value(entry.event_type)
            if event_type == TryoutBottleLedgerEventType.HANDOFF.value:
                totals[entry.product_id] += units
            elif event_type == TryoutBottleLedgerEventType.PICKUP.value:
                totals[entry.product_id] -= units
            elif event_type == TryoutBottleLedgerEventType.ADJUSTMENT.value:
                totals[entry.product_id] += units
        return totals

    @staticmethod
    def get_outstanding_bottles_by_product(tryout: ProductTryout) -> Dict[int, Decimal]:
        outstanding = TryoutService._calculate_outstanding_from_entries(tryout.bottle_ledger_entries or [])
        return {
            product_id: units
            for product_id, units in outstanding.items()
            if units > 0
        }

    @staticmethod
    def _compute_total_returnables_due(tryout: ProductTryout) -> Decimal:
        total = Decimal("0.00")
        for item in tryout.items or []:
            total += TryoutService._as_decimal(item.returnable_bottles_due)
        return total

    @staticmethod
    def _compute_pickup_state(tryout: ProductTryout) -> str:
        total_due = TryoutService._compute_total_returnables_due(tryout)
        outstanding = sum(TryoutService.get_outstanding_bottles_by_product(tryout).values(), Decimal("0.00"))
        handoff_recorded = bool(tryout.handoff_completed_at) or any(
            TryoutService._status_value(entry.event_type) == TryoutBottleLedgerEventType.HANDOFF.value
            for entry in (tryout.bottle_ledger_entries or [])
        )
        if total_due <= 0:
            return "no_returnables"
        if not handoff_recorded:
            return "not_due"
        if outstanding <= 0:
            return "returned"
        if outstanding < total_due:
            return "partial"
        return_due_at = TryoutService._as_utc_datetime(tryout.return_due_at)
        if not return_due_at:
            return "not_due"

        now = datetime.now(UTC)
        if return_due_at < now:
            return "overdue"
        if return_due_at <= now + timedelta(days=TryoutService.DUE_SOON_DAYS):
            return "due_soon"
        return "not_due"

    @staticmethod
    def _quantize_units(value: Any) -> Decimal:
        return TryoutService._as_decimal(value).quantize(Decimal("0.01"))

    @staticmethod
    def _sync_tryout_status(tryout: ProductTryout) -> None:
        if TryoutService._status_value(tryout.status) == TryoutStatus.CANCELLED.value:
            return

        pickup_state = TryoutService._compute_pickup_state(tryout)
        has_open_tasks = any(
            TryoutService._status_value(task.status) in {TryoutTaskStatus.OPEN.value, TryoutTaskStatus.ASSIGNED.value}
            for task in (tryout.tasks or [])
        )

        if pickup_state == "returned" and not has_open_tasks:
            tryout.status = TryoutStatus.CLOSED
            tryout.closed_at = tryout.closed_at or datetime.now(UTC)
            return

        if tryout.handoff_completed_at:
            tryout.status = TryoutStatus.ACTIVE
        else:
            tryout.status = TryoutStatus.SCHEDULED

    @staticmethod
    def _create_task(
        tryout: ProductTryout,
        task_type: TryoutTaskType,
        actor_user_id: Optional[int],
        *,
        assigned_driver_user_id: Optional[int] = None,
        due_at: Optional[datetime] = None,
        notes: Optional[str] = None,
        status: Optional[TryoutTaskStatus] = None,
    ) -> TryoutTask:
        task = TryoutTask(
            tryout_id=tryout.id,
            task_type=task_type,
            status=status or (TryoutTaskStatus.ASSIGNED if assigned_driver_user_id else TryoutTaskStatus.OPEN),
            assigned_driver_user_id=assigned_driver_user_id,
            created_by_user_id=actor_user_id,
            due_at=due_at,
            notes=notes,
        )
        db.session.add(task)
        db.session.flush()
        return task

    @staticmethod
    def _ensure_pickup_task(tryout: ProductTryout, actor_user_id: Optional[int], assigned_driver_user_id: Optional[int] = None) -> Optional[TryoutTask]:
        if TryoutService._compute_total_returnables_due(tryout) <= 0:
            return None

        existing = next(
            (
                task for task in (tryout.tasks or [])
                if TryoutService._status_value(task.task_type) == TryoutTaskType.PICKUP.value
                and TryoutService._status_value(task.status) in {TryoutTaskStatus.OPEN.value, TryoutTaskStatus.ASSIGNED.value}
            ),
            None,
        )
        if existing:
            return existing

        return TryoutService._create_task(
            tryout,
            TryoutTaskType.PICKUP,
            actor_user_id,
            assigned_driver_user_id=assigned_driver_user_id,
            due_at=tryout.return_due_at,
            notes="Auto-created pickup task",
        )

    @staticmethod
    def _serialize_outstanding_products(tryout: ProductTryout) -> List[Dict[str, Any]]:
        outstanding = TryoutService.get_outstanding_bottles_by_product(tryout)
        products = {item.product_id: item.product for item in (tryout.items or [])}
        rows = []
        for product_id, units in sorted(outstanding.items()):
            product = products.get(product_id)
            rows.append(
                {
                    "product_id": product_id,
                    "product_name": product.name if product else None,
                    "units": float(units),
                }
            )
        return rows

    @staticmethod
    def serialize_task(task: TryoutTask) -> Dict[str, Any]:
        tryout = task.tryout
        outstanding_products = TryoutService._serialize_outstanding_products(tryout) if tryout else []
        return {
            **task.to_dict(),
            "tryout_number": tryout.tryout_number if tryout else None,
            "trial_contact": tryout.trial_contact.to_dict() if tryout and tryout.trial_contact else None,
            "address_snapshot": tryout.address_snapshot if tryout else {},
            "pickup_state": TryoutService._compute_pickup_state(tryout) if tryout else None,
            "outstanding_bottles_total": sum(row["units"] for row in outstanding_products),
            "outstanding_bottle_products": outstanding_products,
            "assigned_driver_name": task.assigned_driver.full_name if task.assigned_driver else None,
        }

    @staticmethod
    def serialize_tryout(tryout: ProductTryout) -> Dict[str, Any]:
        outstanding_products = TryoutService._serialize_outstanding_products(tryout)
        handoff_task = next((task for task in tryout.tasks if TryoutService._status_value(task.task_type) == TryoutTaskType.HANDOFF.value), None)
        pickup_task = next((task for task in tryout.tasks if TryoutService._status_value(task.task_type) == TryoutTaskType.PICKUP.value and TryoutService._status_value(task.status) in {TryoutTaskStatus.OPEN.value, TryoutTaskStatus.ASSIGNED.value, TryoutTaskStatus.COMPLETED.value}), None)
        converted_user = tryout.converted_user
        return {
            **tryout.to_dict(),
            "trial_contact": tryout.trial_contact.to_dict() if tryout.trial_contact else None,
            "items": [item.to_dict() for item in tryout.items],
            "tasks": [TryoutService.serialize_task(task) for task in sorted(tryout.tasks, key=lambda item: item.id)],
            "ledger": [entry.to_dict() for entry in sorted(tryout.bottle_ledger_entries, key=lambda item: item.id)],
            "pickup_state": TryoutService._compute_pickup_state(tryout),
            "outstanding_bottles_total": sum(row["units"] for row in outstanding_products),
            "outstanding_bottle_products": outstanding_products,
            "assigned_handoff_driver": {
                "user_id": handoff_task.assigned_driver_user_id,
                "name": handoff_task.assigned_driver.full_name if handoff_task and handoff_task.assigned_driver else None,
            } if handoff_task else None,
            "assigned_pickup_driver": {
                "user_id": pickup_task.assigned_driver_user_id,
                "name": pickup_task.assigned_driver.full_name if pickup_task and pickup_task.assigned_driver else None,
            } if pickup_task else None,
            "converted_user": {
                "id": converted_user.id,
                "full_name": converted_user.full_name,
                "phone": converted_user.phone,
            } if converted_user else None,
        }

    @staticmethod
    def create_tryout(payload: Dict[str, Any], actor_user_id: Optional[int], *, source: str = "admin") -> ProductTryout:
        contact = TryoutService._get_or_create_contact(payload["trial_contact"])
        address = TryoutService._create_or_update_address(contact, payload["address"])
        built_items = TryoutService._validate_and_build_items(payload["items"])

        tryout = ProductTryout(
            trial_contact_id=contact.id,
            created_by_user_id=actor_user_id,
            status=TryoutStatus.SCHEDULED,
            outcome=TryoutOutcome.PENDING,
            source=source,
            notes=payload.get("notes"),
            internal_notes=payload.get("internal_notes"),
            address_snapshot=TryoutService._build_address_snapshot(contact, address),
            return_due_at=payload.get("return_due_at"),
        )
        db.session.add(tryout)
        db.session.flush()
        TryoutService._assign_tryout_number(tryout)

        for item_data in built_items:
            db.session.add(
                ProductTryoutItem(
                    tryout_id=tryout.id,
                    product_id=item_data["product"].id,
                    quantity=item_data["quantity"],
                    unit_price_snapshot=item_data["unit_price_snapshot"],
                    returnable_bottles_due=item_data["returnable_bottles_due"],
                )
            )
        db.session.flush()

        assigned_driver_user_id = payload.get("assigned_driver_user_id")
        complete_handoff = bool(payload.get("complete_handoff"))

        if complete_handoff:
            handoff_task = TryoutService._create_task(
                tryout,
                TryoutTaskType.HANDOFF,
                actor_user_id,
                assigned_driver_user_id=assigned_driver_user_id or actor_user_id,
                notes="Completed on create",
                status=TryoutTaskStatus.COMPLETED,
            )
            handoff_task.completed_at = datetime.now(UTC)
            handoff_task.completed_by_user_id = actor_user_id
            TryoutService._apply_handoff(tryout, handoff_task, actor_user_id, payload.get("notes"))
        else:
            TryoutService._create_task(
                tryout,
                TryoutTaskType.HANDOFF,
                actor_user_id,
                assigned_driver_user_id=assigned_driver_user_id,
                notes="Initial handoff task",
            )
            TryoutService._sync_tryout_status(tryout)

        db.session.commit()
        return TryoutService._load_tryout(tryout.id)

    @staticmethod
    def _apply_handoff(tryout: ProductTryout, task: TryoutTask, actor_user_id: Optional[int], notes: Optional[str]) -> None:
        handoff_at = task.completed_at or datetime.now(UTC)
        tryout.handoff_completed_at = handoff_at
        if not tryout.return_due_at:
            tryout.return_due_at = handoff_at + timedelta(days=TryoutService.DEFAULT_RETURN_DUE_DAYS)

        for item in tryout.items:
            product = item.product
            if product.track_inventory:
                product.stock_quantity = max(0, int(product.stock_quantity or 0) - int(item.quantity))
                product.updated_at = datetime.now(UTC)

            if TryoutService._as_decimal(item.returnable_bottles_due) <= 0:
                continue

            idempotency_key = f"handoff:task:{task.id}:item:{item.id}"
            existing = TryoutBottleLedger.query.filter_by(idempotency_key=idempotency_key).first()
            if existing:
                continue
            db.session.add(
                TryoutBottleLedger(
                    tryout_id=tryout.id,
                    tryout_item_id=item.id,
                    product_id=item.product_id,
                    task_id=task.id,
                    actor_user_id=actor_user_id,
                    event_type=TryoutBottleLedgerEventType.HANDOFF,
                    units=item.returnable_bottles_due,
                    occurred_at=handoff_at,
                    notes=notes or "Try-out handoff completed",
                    idempotency_key=idempotency_key,
                )
            )

        db.session.flush()
        db.session.expire(tryout, ["bottle_ledger_entries"])
        TryoutService._ensure_pickup_task(tryout, actor_user_id, assigned_driver_user_id=task.assigned_driver_user_id)
        TryoutService._sync_tryout_status(tryout)

    @staticmethod
    def complete_handoff_task(task_id: int, actor_user_id: int, notes: Optional[str] = None) -> ProductTryout:
        task = TryoutService._load_task(task_id)
        if TryoutService._status_value(task.task_type) != TryoutTaskType.HANDOFF.value:
            raise ValidationError("Task is not a handoff task")
        if TryoutService._status_value(task.status) == TryoutTaskStatus.COMPLETED.value:
            return TryoutService._load_tryout(task.tryout_id)

        task.status = TryoutTaskStatus.COMPLETED
        task.completed_at = datetime.now(UTC)
        task.completed_by_user_id = actor_user_id
        task.assigned_driver_user_id = task.assigned_driver_user_id or actor_user_id
        task.notes = notes or task.notes
        TryoutService._apply_handoff(task.tryout, task, actor_user_id, notes)
        db.session.commit()
        return TryoutService._load_tryout(task.tryout_id)

    @staticmethod
    def create_task(tryout_id: int, payload: Dict[str, Any], actor_user_id: int) -> TryoutTask:
        tryout = TryoutService._load_tryout(tryout_id)
        task_type = TryoutTaskType(payload["task_type"])
        if (
            task_type == TryoutTaskType.PICKUP
            and TryoutService._compute_total_returnables_due(tryout) <= 0
        ):
            raise ValidationError("Pickup task cannot be created for a try-out without returnable bottles")
        due_at = payload.get("due_at") or (tryout.return_due_at if task_type == TryoutTaskType.PICKUP else None)
        task = TryoutService._create_task(
            tryout,
            task_type,
            actor_user_id,
            assigned_driver_user_id=payload.get("assigned_driver_user_id"),
            due_at=due_at,
            notes=payload.get("notes"),
        )
        db.session.commit()
        return TryoutService._load_task(task.id)

    @staticmethod
    def assign_task(task_id: int, driver_user_id: int) -> TryoutTask:
        task = TryoutService._load_task(task_id)
        if TryoutService._status_value(task.status) == TryoutTaskStatus.COMPLETED.value:
            raise ConflictError("Completed task cannot be reassigned")
        task.assigned_driver_user_id = driver_user_id
        task.status = TryoutTaskStatus.ASSIGNED
        db.session.commit()
        return TryoutService._load_task(task.id)

    @staticmethod
    def accept_task(task_id: int, driver_user_id: int) -> TryoutTask:
        task = TryoutService._load_task(task_id)
        if TryoutService._status_value(task.status) == TryoutTaskStatus.COMPLETED.value:
            raise ConflictError("Completed task cannot be accepted")
        if task.assigned_driver_user_id and task.assigned_driver_user_id != driver_user_id:
            raise ConflictError("Task is already assigned to another driver")
        task.assigned_driver_user_id = driver_user_id
        task.status = TryoutTaskStatus.ASSIGNED
        db.session.commit()
        return TryoutService._load_task(task.id)

    @staticmethod
    def record_pickup(task_id: int, pickups: List[Dict[str, Any]], actor_user_id: int, *, notes: Optional[str] = None, idempotency_key: Optional[str] = None) -> ProductTryout:
        task = TryoutService._load_task(task_id)
        if TryoutService._status_value(task.task_type) != TryoutTaskType.PICKUP.value:
            raise ValidationError("Task is not a pickup task")
        if TryoutService._status_value(task.status) == TryoutTaskStatus.COMPLETED.value:
            raise ConflictError("Pickup task already completed")

        task.assigned_driver_user_id = task.assigned_driver_user_id or actor_user_id
        task.status = TryoutTaskStatus.ASSIGNED

        tryout = task.tryout
        outstanding = TryoutService.get_outstanding_bottles_by_product(tryout)
        for pickup in pickups:
            product_id = int(pickup["product_id"])
            units = TryoutService._quantize_units(pickup["units"])
            if units <= 0:
                raise ValidationError("Pickup units must be positive")
            if product_id not in outstanding:
                raise ValidationError(f"Product {product_id} has no outstanding returnable bottles")
            if units > outstanding[product_id]:
                raise ValidationError(f"Pickup units for product {product_id} exceed outstanding bottles")

            line_idempotency_key = (
                f"{idempotency_key}:product:{product_id}"
                if idempotency_key
                else f"pickup:task:{task.id}:product:{product_id}:{datetime.now(UTC).isoformat()}"
            )
            existing = TryoutBottleLedger.query.filter_by(idempotency_key=line_idempotency_key).first()
            if existing:
                continue
            db.session.add(
                TryoutBottleLedger(
                    tryout_id=tryout.id,
                    product_id=product_id,
                    task_id=task.id,
                    actor_user_id=actor_user_id,
                    event_type=TryoutBottleLedgerEventType.PICKUP,
                    units=units,
                    occurred_at=datetime.now(UTC),
                    notes=notes or "Bottle pickup recorded",
                    idempotency_key=line_idempotency_key,
                )
            )

        db.session.flush()
        db.session.expire(tryout, ["bottle_ledger_entries"])
        remaining = TryoutService.get_outstanding_bottles_by_product(tryout)
        task.notes = notes or task.notes
        if not remaining:
            task.status = TryoutTaskStatus.COMPLETED
            task.completed_at = datetime.now(UTC)
            task.completed_by_user_id = actor_user_id
        task.completion_payload = {
            "pickups": [{"product_id": int(line["product_id"]), "units": float(TryoutService._as_decimal(line["units"]))} for line in pickups],
            "notes": notes,
        }
        TryoutService._sync_tryout_status(tryout)
        db.session.commit()
        return TryoutService._load_tryout(tryout.id)

    @staticmethod
    def adjust_bottles(tryout_id: int, product_id: int, units: Any, actor_user_id: int, *, notes: Optional[str] = None, idempotency_key: Optional[str] = None) -> ProductTryout:
        tryout = TryoutService._load_tryout(tryout_id)
        units_decimal = TryoutService._as_decimal(units)
        if units_decimal == 0:
            raise ValidationError("Adjustment units cannot be zero")

        key = idempotency_key or f"adjustment:tryout:{tryout_id}:product:{product_id}:{units_decimal}:{datetime.now(UTC).isoformat()}"
        existing = TryoutBottleLedger.query.filter_by(idempotency_key=key).first()
        if existing:
            return TryoutService._load_tryout(tryout_id)

        projected = dict(TryoutService.get_outstanding_bottles_by_product(tryout))
        projected[product_id] = projected.get(product_id, Decimal("0.00")) + TryoutService._quantize_units(units_decimal)
        if projected[product_id] < 0:
            raise ValidationError("Bottle adjustment would make outstanding quantity negative")

        db.session.add(
            TryoutBottleLedger(
                tryout_id=tryout_id,
                product_id=product_id,
                actor_user_id=actor_user_id,
                event_type=TryoutBottleLedgerEventType.ADJUSTMENT,
                units=TryoutService._quantize_units(units_decimal),
                occurred_at=datetime.now(UTC),
                notes=notes or "Bottle adjustment",
                idempotency_key=key,
            )
        )
        db.session.flush()
        db.session.expire(tryout, ["bottle_ledger_entries"])
        TryoutService._sync_tryout_status(tryout)
        db.session.commit()
        return TryoutService._load_tryout(tryout_id)

    @staticmethod
    def update_tryout(tryout_id: int, payload: Dict[str, Any], actor_user_id: Optional[int] = None) -> ProductTryout:
        tryout = TryoutService._load_tryout(tryout_id)
        contact_updated = False
        address_updated = False

        if payload.get("trial_contact"):
            TryoutService._update_trial_contact(tryout.trial_contact, payload["trial_contact"])
            contact_updated = True

        if payload.get("address"):
            address = TryoutService._create_or_update_address(tryout.trial_contact, payload["address"])
            tryout.address_snapshot = TryoutService._build_address_snapshot(tryout.trial_contact, address)
            address_updated = True
        if payload.get("items") is not None:
            TryoutService._replace_tryout_items(tryout, payload["items"])
        if "notes" in payload:
            tryout.notes = payload.get("notes")
        if "internal_notes" in payload:
            tryout.internal_notes = payload.get("internal_notes")
        if "return_due_at" in payload:
            tryout.return_due_at = payload.get("return_due_at")
        if payload.get("outcome"):
            tryout.outcome = TryoutOutcome(payload["outcome"])
        if payload.get("status"):
            tryout.status = TryoutStatus(payload["status"])

        if contact_updated and not address_updated:
            snapshot = dict(tryout.address_snapshot or {})
            snapshot.update(
                {
                    "trial_contact_id": tryout.trial_contact.id,
                    "trial_contact_name": tryout.trial_contact.full_name,
                    "phone": tryout.trial_contact.phone,
                    "company_name": tryout.trial_contact.company_name,
                }
            )
            tryout.address_snapshot = snapshot

        assigned_driver_user_id = payload.get("assigned_driver_user_id")
        if assigned_driver_user_id:
            task = TryoutService._get_open_handoff_task(tryout) or TryoutService._get_open_pickup_task(tryout)
            if task:
                task.assigned_driver_user_id = assigned_driver_user_id
                task.status = TryoutTaskStatus.ASSIGNED

        if payload.get("complete_handoff") and not tryout.handoff_completed_at:
            handoff_task = TryoutService._get_open_handoff_task(tryout)
            if not handoff_task:
                handoff_task = TryoutService._create_task(
                    tryout,
                    TryoutTaskType.HANDOFF,
                    actor_user_id,
                    assigned_driver_user_id=assigned_driver_user_id or actor_user_id,
                    notes="Completed from edit",
                )
            handoff_task.assigned_driver_user_id = handoff_task.assigned_driver_user_id or assigned_driver_user_id or actor_user_id
            handoff_task.status = TryoutTaskStatus.COMPLETED
            handoff_task.completed_at = datetime.now(UTC)
            handoff_task.completed_by_user_id = actor_user_id
            TryoutService._apply_handoff(tryout, handoff_task, actor_user_id, payload.get("notes"))

        if tryout.converted_user_id and contact_updated and actor_user_id:
            auth_service = AuthService()
            auth_service.update_user_by_admin(
                tryout.converted_user_id,
                first_name=tryout.trial_contact.first_name,
                last_name=tryout.trial_contact.last_name,
                phone=tryout.trial_contact.phone,
                company_name=tryout.trial_contact.company_name,
                user_type="entity" if tryout.trial_contact.company_name else "individual",
                updated_by_admin_id=actor_user_id,
            )

        TryoutService._sync_tryout_status(tryout)
        db.session.commit()
        return TryoutService._load_tryout(tryout.id)

    @staticmethod
    def convert_tryout(tryout_id: int, actor_user_id: int) -> Dict[str, Any]:
        tryout = TryoutService._load_tryout(tryout_id)
        if tryout.converted_user_id:
            loaded_tryout = TryoutService._load_tryout(tryout_id)
            return {
                "tryout": loaded_tryout,
                "action": "already_converted",
                "user": loaded_tryout.converted_user,
            }

        contact = tryout.trial_contact
        user = User.query.filter_by(phone=contact.phone).first()
        action = "linked_existing_user"
        if not user:
            auth_service = AuthService()
            user = auth_service.create_user_by_admin(
                phone=contact.phone,
                first_name=contact.first_name,
                last_name=contact.last_name,
                company_name=contact.company_name,
                user_type="entity" if contact.company_name else "individual",
                created_by_admin_id=actor_user_id,
            )
            action = "created_user"
        else:
            auth_service = AuthService()
        snapshot = tryout.address_snapshot or {}
        TryoutService._upsert_converted_user_address(auth_service, user.id, snapshot)
        tryout.converted_user_id = user.id
        tryout.converted_at = datetime.now(UTC)
        tryout.outcome = TryoutOutcome.CONVERTED
        db.session.commit()
        return {
            "tryout": TryoutService._load_tryout(tryout_id),
            "action": action,
            "user": user,
        }

    @staticmethod
    def get_tryout(tryout_id: int) -> ProductTryout:
        return TryoutService._load_tryout(tryout_id)

    @staticmethod
    def get_task(task_id: int) -> Dict[str, Any]:
        return TryoutService.serialize_task(TryoutService._load_task(task_id))

    @staticmethod
    def list_tasks_for_driver(driver_user_id: int, *, include_pool: bool = False) -> List[TryoutTask]:
        query = TryoutTask.query.options(
            joinedload(TryoutTask.tryout).joinedload(ProductTryout.trial_contact),
            joinedload(TryoutTask.tryout).selectinload(ProductTryout.items).joinedload(ProductTryoutItem.product),
            joinedload(TryoutTask.tryout).selectinload(ProductTryout.tasks).joinedload(TryoutTask.assigned_driver),
            joinedload(TryoutTask.tryout).selectinload(ProductTryout.bottle_ledger_entries).joinedload(TryoutBottleLedger.product),
            joinedload(TryoutTask.assigned_driver),
        ).filter(
            TryoutTask.status.in_([TryoutTaskStatus.OPEN, TryoutTaskStatus.ASSIGNED]),
        )
        if include_pool:
            query = query.filter(
                or_(
                    TryoutTask.assigned_driver_user_id.is_(None),
                    TryoutTask.assigned_driver_user_id == driver_user_id,
                )
            )
        else:
            query = query.filter(TryoutTask.assigned_driver_user_id == driver_user_id)
        return query.order_by(TryoutTask.due_at.asc().nullslast(), TryoutTask.id.asc()).all()

    @staticmethod
    def list_history_for_driver(driver_user_id: int) -> List[TryoutTask]:
        return TryoutTask.query.options(
            joinedload(TryoutTask.tryout).joinedload(ProductTryout.trial_contact),
            joinedload(TryoutTask.tryout).selectinload(ProductTryout.items).joinedload(ProductTryoutItem.product),
            joinedload(TryoutTask.tryout).selectinload(ProductTryout.tasks).joinedload(TryoutTask.assigned_driver),
            joinedload(TryoutTask.tryout).selectinload(ProductTryout.bottle_ledger_entries).joinedload(TryoutBottleLedger.product),
            joinedload(TryoutTask.assigned_driver),
        ).filter(
            TryoutTask.completed_by_user_id == driver_user_id,
            TryoutTask.status == TryoutTaskStatus.COMPLETED,
        ).order_by(TryoutTask.completed_at.desc().nullslast(), TryoutTask.id.desc()).all()

    @staticmethod
    def list_active_tryouts_for_driver(driver_user_id: int) -> List[ProductTryout]:
        tryouts = ProductTryout.query.options(
            joinedload(ProductTryout.trial_contact),
            joinedload(ProductTryout.converted_user),
            selectinload(ProductTryout.items).joinedload(ProductTryoutItem.product),
            selectinload(ProductTryout.tasks).joinedload(TryoutTask.assigned_driver),
            selectinload(ProductTryout.bottle_ledger_entries).joinedload(TryoutBottleLedger.product),
        ).filter(ProductTryout.status.in_([TryoutStatus.ACTIVE, TryoutStatus.SCHEDULED])).order_by(ProductTryout.id.desc()).all()

        filtered = []
        for tryout in tryouts:
            if TryoutService.get_outstanding_bottles_by_product(tryout):
                if any(task.assigned_driver_user_id == driver_user_id for task in tryout.tasks):
                    filtered.append(tryout)
        return filtered


class AdminTryoutService:
    """Admin-oriented querying and reporting for try-outs."""

    @staticmethod
    def _parse_date_boundary(value: str, *, end_of_day: bool = False) -> datetime:
        date_value = datetime.fromisoformat(value).date()
        if end_of_day:
            return datetime(date_value.year, date_value.month, date_value.day, tzinfo=UTC) + timedelta(days=1)
        return datetime(date_value.year, date_value.month, date_value.day, tzinfo=UTC)

    @staticmethod
    def list_tryouts(
        *,
        page: int = 1,
        per_page: int = 20,
        search: Optional[str] = None,
        status: Optional[str] = None,
        outcome: Optional[str] = None,
        pickup_state: Optional[str] = None,
        driver_id: Optional[int] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        due_start_date: Optional[str] = None,
        due_end_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        query = ProductTryout.query.options(
            joinedload(ProductTryout.trial_contact),
            joinedload(ProductTryout.converted_user),
            selectinload(ProductTryout.items).joinedload(ProductTryoutItem.product),
            selectinload(ProductTryout.tasks).joinedload(TryoutTask.assigned_driver),
            selectinload(ProductTryout.bottle_ledger_entries).joinedload(TryoutBottleLedger.product),
        )

        if search:
            term = f"%{search.strip()}%"
            query = query.join(TrialContact).filter(
                db.or_(
                    ProductTryout.tryout_number.ilike(term),
                    TrialContact.first_name.ilike(term),
                    TrialContact.last_name.ilike(term),
                    TrialContact.phone.ilike(term),
                    TrialContact.company_name.ilike(term),
                )
            )
        if status:
            query = query.filter(ProductTryout.status == TryoutStatus(status))
        if outcome:
            query = query.filter(ProductTryout.outcome == TryoutOutcome(outcome))
        if start_date:
            query = query.filter(ProductTryout.created_at >= AdminTryoutService._parse_date_boundary(start_date))
        if end_date:
            query = query.filter(ProductTryout.created_at < AdminTryoutService._parse_date_boundary(end_date, end_of_day=True))
        if due_start_date:
            query = query.filter(ProductTryout.return_due_at >= AdminTryoutService._parse_date_boundary(due_start_date))
        if due_end_date:
            query = query.filter(ProductTryout.return_due_at < AdminTryoutService._parse_date_boundary(due_end_date, end_of_day=True))

        rows = query.order_by(ProductTryout.id.desc()).all()
        serialized = [TryoutService.serialize_tryout(row) for row in rows]

        if pickup_state:
            serialized = [row for row in serialized if row["pickup_state"] == pickup_state]
        if driver_id:
            serialized = [
                row for row in serialized
                if (row.get("assigned_handoff_driver") or {}).get("user_id") == driver_id
                or (row.get("assigned_pickup_driver") or {}).get("user_id") == driver_id
            ]

        total = len(serialized)
        start = max(page - 1, 0) * per_page
        end = start + per_page
        page_items = serialized[start:end]

        summary = {
            "total_tryouts": total,
            "active_tryouts": sum(1 for row in serialized if row["status"] == TryoutStatus.ACTIVE.value),
            "outstanding_bottles_total": round(sum(row["outstanding_bottles_total"] for row in serialized), 2),
            "due_soon_count": sum(1 for row in serialized if row["pickup_state"] == "due_soon"),
            "overdue_count": sum(1 for row in serialized if row["pickup_state"] == "overdue"),
            "converted_count": sum(1 for row in serialized if row["outcome"] == TryoutOutcome.CONVERTED.value),
            "returned_count": sum(1 for row in serialized if row["pickup_state"] == "returned"),
        }
        summary["collection_rate"] = round(
            (summary["returned_count"] / total) * 100, 2
        ) if total else 0.0

        return {
            "items": page_items,
            "page": page,
            "per_page": per_page,
            "total": total,
            "summary": summary,
        }

    @staticmethod
    def get_tryout(tryout_id: int) -> Dict[str, Any]:
        return TryoutService.serialize_tryout(TryoutService.get_tryout(tryout_id))

    @staticmethod
    def get_due_reminder_candidates() -> Dict[str, List[Dict[str, Any]]]:
        rows = AdminTryoutService.list_tryouts(page=1, per_page=10000)
        items = rows["items"]
        return {
            "due_soon": [row for row in items if row["pickup_state"] == "due_soon"],
            "overdue": [row for row in items if row["pickup_state"] == "overdue"],
        }

    @staticmethod
    def export_tryouts(**filters: Any) -> List[Dict[str, Any]]:
        result = AdminTryoutService.list_tryouts(page=1, per_page=10000, **filters)
        return result["items"]
