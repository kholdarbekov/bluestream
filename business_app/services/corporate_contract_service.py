"""Corporate contract and prepayment accounting workflows."""

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, exists, func, or_
from sqlalchemy.orm import joinedload

from business_app import db
from business_app.models.corporate import (
    CorporateContract,
    CorporateContractProductPrice,
    CorporateContractStatus,
    CorporatePrepaymentAccount,
    CorporatePrepaymentBalance,
    CorporatePrepaymentEventType,
    CorporatePrepaymentLedger,
)
from business_app.models.order import Order, OrderItem
from business_app.models.product import Product
from business_app.models.user import User
from business_app.utils.audit_logger import audit_logger, AuditEventType, AuditSeverity
from shared.enums import UserType
from business_app.utils.exceptions import NotFoundError, ValidationError
from business_app.utils.translations import get_translation
from shared.enums import CorporateContractTrackingMode


class CorporateContractService:
    """Service for corporate contract pricing and prepayment ledgers."""

    LEGAL_ENTITY_TYPE = UserType.ENTITY.value
    TRUE_VALUES = {"1", "true", "yes", "on"}
    FALSE_VALUES = {"0", "false", "no", "off"}

    def _is_legal_entity_user(self, user: Optional[User]) -> bool:
        return bool(user and user.is_entity_user)

    @staticmethod
    def _translate(key: str, default: str, **kwargs) -> str:
        translated = get_translation(key, **kwargs)
        if translated == key:
            try:
                return default.format(**kwargs)
            except (KeyError, ValueError):
                return default
        return translated

    @classmethod
    def _normalize_bool(cls, value: Any, *, default: Optional[bool] = None) -> bool:
        if value is None:
            if default is None:
                raise ValidationError("Boolean value is required")
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in cls.TRUE_VALUES:
                return True
            if normalized in cls.FALSE_VALUES:
                return False
        if isinstance(value, (int, float)):
            return bool(value)
        raise ValidationError("Invalid boolean value")

    def validate_business_account_order(
        self,
        user: User,
        order_items: List[Dict[str, Any]],
    ) -> None:
        """Ensure business-account settlement is only used for eligible corporate orders."""
        if not user or not user.is_entity_user:
            raise ValidationError(
                self._translate(
                    "api.orders.error.business_account_entity_only",
                    "Business Account payment is only available for entity customers.",
                )
            )

        # Grocery stores never use prepaid business-account settlement;
        # they pay cash/card on or after delivery and track money debt.
        if user.is_grocery_store:
            raise ValidationError(
                self._translate(
                    "api.orders.error.business_account_grocery_disallowed",
                    "Business Account payment is not available for grocery store accounts.",
                )
            )

        if not order_items:
            raise ValidationError(
                self._translate(
                    "api.orders.error.business_account_contract_required",
                    "Business Account payment requires active corporate contract coverage for the order items.",
                )
            )

        effective_at = self._normalize_effective_at()
        contract_ids = {item.get("contract_id") for item in order_items if item.get("contract_id")}
        contracts = self._get_contracts_for_business_account_validation(
            user_id=user.id,
            contract_ids=contract_ids,
        )
        validation_errors = self._collect_business_account_contract_errors(
            order_items=order_items,
            contracts=contracts,
            effective_at=effective_at,
        )
        if validation_errors:
            raise ValidationError(
                self._translate(
                    "api.orders.error.business_account_contract_required",
                    "Business Account payment requires every order item to be covered by an active corporate contract.",
                ),
                validation_errors=validation_errors,
            )

        shortage_errors = self._collect_business_account_balance_shortages(
            order_items=order_items,
            contracts=contracts,
        )
        if shortage_errors:
            raise ValidationError(
                self._translate(
                    "api.orders.error.business_account_insufficient_prepayment",
                    "Corporate prepayment balance is insufficient for one or more order items.",
                ),
                validation_errors=shortage_errors,
            )

    @staticmethod
    def _format_units(value: Any) -> str:
        units = Decimal(str(value or 0))
        if units == units.to_integral():
            return str(int(units))
        return format(units.normalize(), "f")

    def _get_contracts_for_business_account_validation(
        self,
        *,
        user_id: int,
        contract_ids: set[int],
    ) -> Dict[int, CorporateContract]:
        if not contract_ids:
            return {}

        contracts = (
            CorporateContract.query.options(
                joinedload(CorporateContract.product_prices).joinedload(CorporateContractProductPrice.product),
                joinedload(CorporateContract.prepayment_account)
                .joinedload(CorporatePrepaymentAccount.product_balances)
                .joinedload(CorporatePrepaymentBalance.product),
            )
            .filter(
                CorporateContract.user_id == user_id,
                CorporateContract.id.in_(contract_ids),
            )
            .all()
        )
        return {contract.id: contract for contract in contracts}

    def _collect_business_account_contract_errors(
        self,
        *,
        order_items: List[Dict[str, Any]],
        contracts: Dict[int, CorporateContract],
        effective_at: datetime,
    ) -> List[str]:
        errors: List[str] = []

        for item in order_items:
            product_id = item.get("product_id")
            contract_id = item.get("contract_id")
            price_row_id = item.get("contract_product_price_id")

            if not contract_id or not price_row_id:
                errors.append(
                    self._translate(
                        "api.orders.error.business_account_all_items_must_be_contract_backed",
                        "Product {product_id} is not covered by an active corporate contract for Business Account payment.",  # noqa: E501
                        product_id=product_id,
                    )
                )
                continue

            contract = contracts.get(contract_id)
            if not contract:
                errors.append(
                    self._translate(
                        "api.orders.error.business_account_contract_line_invalid",
                        "Contract linkage for product {product_id} is invalid.",
                        product_id=product_id,
                    )
                )
                continue

            if not self._contract_is_applicable_at(contract, effective_at):
                errors.append(
                    self._translate(
                        "api.orders.error.business_account_contract_line_invalid",
                        "Contract {contract_number} for product {product_id} is not active for Business Account payment.",  # noqa: E501
                        contract_number=contract.contract_number,
                        product_id=product_id,
                    )
                )
                continue

            price_row = next(
                (row for row in (contract.product_prices or []) if row.id == price_row_id),
                None,
            )
            if not price_row or not price_row.is_active or price_row.product_id != product_id:
                errors.append(
                    self._translate(
                        "api.orders.error.business_account_contract_line_invalid",
                        "Contract price row for product {product_id} is invalid or inactive.",
                        product_id=product_id,
                    )
                )

        return errors

    def _collect_business_account_balance_shortages(
        self,
        *,
        order_items: List[Dict[str, Any]],
        contracts: Dict[int, CorporateContract],
    ) -> List[str]:
        requested_units_by_line: Dict[Tuple[int, int], Decimal] = defaultdict(lambda: Decimal("0.00"))
        shortages: List[str] = []

        for item in order_items:
            contract_id = item.get("contract_id")
            product_id = item.get("product_id")
            if not contract_id or not product_id:
                continue

            contract = contracts.get(contract_id)
            if not contract or contract.allows_debt:
                continue

            price_row = next(
                (row for row in (contract.product_prices or []) if row.id == item.get("contract_product_price_id")),
                None,
            )
            if not price_row or not price_row.is_prepayment_eligible:
                continue

            requested_units_by_line[(contract_id, product_id)] += Decimal(str(item.get("quantity") or 0))

        for (contract_id, product_id), requested_units in requested_units_by_line.items():
            contract = contracts[contract_id]
            balance_map = {
                balance.product_id: balance
                for balance in (contract.prepayment_account.product_balances if contract.prepayment_account else [])
            }
            balance = balance_map.get(product_id)
            available_units = Decimal(str(balance.available_units if balance else 0))
            if requested_units <= available_units:
                continue

            price_row = next(
                (row for row in (contract.product_prices or []) if row.product_id == product_id and row.is_active),
                None,
            )
            product_name = None
            if price_row and price_row.product:
                product_name = price_row.product.name
            elif balance and balance.product:
                product_name = balance.product.name

            shortages.append(
                self._translate(
                    "api.orders.error.business_account_insufficient_prepayment",
                    "Contract {contract_number} has insufficient prepaid units for {product_name}: requested {requested_units}, available {available_units}, shortage {shortage_units}.",  # noqa: E501
                    contract_number=contract.contract_number,
                    product_name=product_name or f"product {product_id}",
                    requested_units=self._format_units(requested_units),
                    available_units=self._format_units(available_units),
                    shortage_units=self._format_units(requested_units - available_units),
                )
            )

        return shortages

    @staticmethod
    def _normalize_effective_at(effective_at: Optional[datetime] = None) -> datetime:
        effective_at = effective_at or datetime.now(timezone.utc)
        if effective_at.tzinfo is None:
            effective_at = effective_at.replace(tzinfo=timezone.utc)
        return effective_at

    @staticmethod
    def _date_ranges_overlap(
        start_a: Optional[datetime],
        end_a: Optional[datetime],
        start_b: Optional[datetime],
        end_b: Optional[datetime],
    ) -> bool:
        start_a = start_a or datetime.min.replace(tzinfo=timezone.utc)
        start_b = start_b or datetime.min.replace(tzinfo=timezone.utc)
        end_a = end_a or datetime.max.replace(tzinfo=timezone.utc)
        end_b = end_b or datetime.max.replace(tzinfo=timezone.utc)
        if start_a.tzinfo is None:
            start_a = start_a.replace(tzinfo=timezone.utc)
        if start_b.tzinfo is None:
            start_b = start_b.replace(tzinfo=timezone.utc)
        if end_a.tzinfo is None:
            end_a = end_a.replace(tzinfo=timezone.utc)
        if end_b.tzinfo is None:
            end_b = end_b.replace(tzinfo=timezone.utc)
        return start_a <= end_b and start_b <= end_a

    def _contract_is_applicable_at(self, contract: CorporateContract, effective_at: datetime) -> bool:
        if not contract.is_active:
            return False
        if contract.status != CorporateContractStatus.ACTIVE:
            return False
        start_date = contract.start_date
        end_date = contract.end_date
        if start_date and start_date.tzinfo is None:
            start_date = start_date.replace(tzinfo=timezone.utc)
        if end_date and end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)
        return (start_date is None or start_date <= effective_at) and (end_date is None or end_date >= effective_at)

    def _build_overlap_conflicts(
        self,
        *,
        user_id: int,
        start_date: Optional[datetime],
        end_date: Optional[datetime],
        prices: List[Dict[str, Any]],
        exclude_contract_id: Optional[int] = None,
        current_contract_label: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        active_prices = [
            price for price in (prices or []) if price.get("product_id") and bool(price.get("is_active", True))
        ]
        if not active_prices:
            return []

        other_contracts = CorporateContract.query.options(
            joinedload(CorporateContract.product_prices).joinedload(CorporateContractProductPrice.product)
        ).filter(
            CorporateContract.user_id == user_id,
            CorporateContract.is_active.is_(True),
            CorporateContract.status == CorporateContractStatus.ACTIVE,
        )
        if exclude_contract_id:
            other_contracts = other_contracts.filter(CorporateContract.id != exclude_contract_id)
        other_contracts = other_contracts.all()

        conflicts: List[Dict[str, Any]] = []
        seen = set()
        for price in active_prices:
            product_id = price["product_id"]
            for other_contract in other_contracts:
                if not self._date_ranges_overlap(
                    start_date, end_date, other_contract.start_date, other_contract.end_date
                ):
                    continue
                for other_row in other_contract.product_prices:
                    if not other_row.is_active or other_row.product_id != product_id:
                        continue
                    key = (product_id, other_contract.id, other_row.id)
                    if key in seen:
                        continue
                    seen.add(key)
                    conflicts.append(
                        {
                            "product_id": product_id,
                            "product_name": getattr(other_row.product, "name", None),
                            "current_contract": {
                                "id": exclude_contract_id,
                                "label": current_contract_label,
                                "start_date": start_date.isoformat() if start_date else None,
                                "end_date": end_date.isoformat() if end_date else None,
                            },
                            "conflicting_contract": {
                                "id": other_contract.id,
                                "contract_number": other_contract.contract_number,
                                "name": other_contract.name,
                                "start_date": (
                                    other_contract.start_date.isoformat() if other_contract.start_date else None
                                ),
                                "end_date": other_contract.end_date.isoformat() if other_contract.end_date else None,
                            },
                            "conflicting_price_row": {
                                "id": other_row.id,
                                "unit_price": float(other_row.unit_price) if other_row.unit_price is not None else None,
                                "is_prepayment_eligible": other_row.is_prepayment_eligible,
                            },
                        }
                    )
        return conflicts

    def preview_contract_price_overlaps(
        self,
        *,
        contract_id: Optional[int] = None,
        user_id: Optional[int] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        status: Optional[str] = None,
        is_active: Optional[bool] = None,
        prices: Optional[List[Dict[str, Any]]] = None,
        contract_number: Optional[str] = None,
        contract_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        contract = self.get_contract_by_id(contract_id) if contract_id else None
        resolved_user_id = user_id or (contract.user_id if contract else None)
        if not resolved_user_id:
            raise ValidationError("user_id is required to preview contract overlaps")

        resolved_start = start_date or (contract.start_date if contract else None)
        resolved_end = end_date if end_date is not None else (contract.end_date if contract else None)
        resolved_status = status or (
            contract.status.value if contract and contract.status else CorporateContractStatus.ACTIVE.value
        )
        resolved_is_active = bool(is_active if is_active is not None else (contract.is_active if contract else True))
        resolved_prices = (
            prices
            if prices is not None
            else [
                {
                    "product_id": row.product_id,
                    "is_active": row.is_active,
                }
                for row in (contract.product_prices if contract else [])
            ]
        )

        if isinstance(resolved_start, str):
            resolved_start = datetime.fromisoformat(resolved_start)
        if isinstance(resolved_end, str):
            resolved_end = datetime.fromisoformat(resolved_end)
        if resolved_start and resolved_start.tzinfo is None:
            resolved_start = resolved_start.replace(tzinfo=timezone.utc)
        if resolved_end and resolved_end.tzinfo is None:
            resolved_end = resolved_end.replace(tzinfo=timezone.utc)

        if not resolved_is_active or resolved_status != CorporateContractStatus.ACTIVE.value:
            return {
                "has_conflicts": False,
                "conflicts": [],
                "summary": {
                    "conflicts_count": 0,
                    "products_count": 0,
                    "conflicting_contracts_count": 0,
                },
            }

        conflicts = self._build_overlap_conflicts(
            user_id=resolved_user_id,
            start_date=resolved_start,
            end_date=resolved_end,
            prices=resolved_prices,
            exclude_contract_id=contract.id if contract else None,
            current_contract_label=contract_number
            or (contract.contract_number if contract else contract_name or "Draft Contract"),
        )
        return {
            "has_conflicts": bool(conflicts),
            "conflicts": conflicts,
            "summary": {
                "conflicts_count": len(conflicts),
                "products_count": len({item["product_id"] for item in conflicts}),
                "conflicting_contracts_count": len({item["conflicting_contract"]["id"] for item in conflicts}),
            },
        }

    def get_contract_by_id(self, contract_id: int) -> CorporateContract:
        contract = CorporateContract.query.options(
            joinedload(CorporateContract.user),
            joinedload(CorporateContract.product_prices).joinedload(CorporateContractProductPrice.product),
            joinedload(CorporateContract.prepayment_account)
            .joinedload(CorporatePrepaymentAccount.product_balances)
            .joinedload(CorporatePrepaymentBalance.product),
        ).get(contract_id)
        if not contract:
            raise NotFoundError("Corporate contract not found")
        return contract

    def get_active_contract_for_user(self, user_id: int) -> Optional[CorporateContract]:
        contracts = self.list_active_contracts_for_user(user_id=user_id)
        if len(contracts) == 1:
            return contracts[0]
        return None

    def list_active_contracts_for_user(
        self,
        user_id: int,
        effective_at: Optional[datetime] = None,
    ) -> List[CorporateContract]:
        user = User.query.get(user_id)
        if not self._is_legal_entity_user(user):
            return []

        effective_at = self._normalize_effective_at(effective_at)
        return (
            CorporateContract.query.options(
                joinedload(CorporateContract.user),
                joinedload(CorporateContract.product_prices).joinedload(CorporateContractProductPrice.product),
                joinedload(CorporateContract.prepayment_account)
                .joinedload(CorporatePrepaymentAccount.product_balances)
                .joinedload(CorporatePrepaymentBalance.product),
            )
            .filter(
                CorporateContract.user_id == user_id,
                CorporateContract.is_active.is_(True),
                CorporateContract.status == CorporateContractStatus.ACTIVE,
                CorporateContract.start_date <= effective_at,
                or_(
                    CorporateContract.end_date.is_(None),
                    CorporateContract.end_date >= effective_at,
                ),
            )
            .order_by(CorporateContract.start_date.desc(), CorporateContract.created_at.desc())
            .all()
        )

    def _get_applicable_contract_price_rows(
        self,
        user_id: int,
        product_id: int,
        effective_at: Optional[datetime] = None,
    ) -> List[CorporateContractProductPrice]:
        effective_at = self._normalize_effective_at(effective_at)
        contracts = self.list_active_contracts_for_user(user_id=user_id, effective_at=effective_at)
        matches: List[CorporateContractProductPrice] = []
        for contract in contracts:
            for price_row in contract.product_prices:
                if price_row.product_id == product_id and price_row.is_active:
                    matches.append(price_row)
        return matches

    def resolve_contract_pricing_for_user_product(
        self,
        user_id: int,
        product_id: int,
        fallback_price: Decimal,
        effective_at: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        matches = self._get_applicable_contract_price_rows(
            user_id=user_id,
            product_id=product_id,
            effective_at=effective_at,
        )
        if not matches:
            return {
                "unit_price": Decimal(str(fallback_price)),
                "contract": None,
                "contract_price_row": None,
            }
        if len(matches) > 1:
            contract_numbers = [row.contract.contract_number for row in matches if row.contract]
            raise ValidationError(
                self._translate(
                    "api.orders.error.ambiguous_contract_pricing",
                    "Ambiguous contract pricing for product "
                    f"{product_id}. Multiple active contracts match: {', '.join(contract_numbers)}",
                    product_id=product_id,
                    contract_numbers=", ".join(contract_numbers),
                )
            )
        match = matches[0]
        return {
            "unit_price": Decimal(str(match.unit_price)),
            "contract": match.contract,
            "contract_price_row": match,
        }

    def resolve_unit_price(
        self,
        user_id: int,
        product_id: int,
        fallback_price: Decimal,
    ) -> Decimal:
        resolved = self.resolve_contract_pricing_for_user_product(
            user_id=user_id,
            product_id=product_id,
            fallback_price=fallback_price,
        )
        return Decimal(str(resolved["unit_price"]))

    def resolve_pricing_for_user_products(
        self,
        *,
        user_id: int,
        product_ids: List[int],
        fallback_prices: Dict[int, Decimal],
        effective_at: Optional[datetime] = None,
    ) -> Dict[int, Dict[str, Any]]:
        """Resolve effective pricing metadata for multiple products for one user."""
        unique_product_ids: List[int] = []
        for product_id in product_ids or []:
            normalized_id = int(product_id)
            if normalized_id not in unique_product_ids:
                unique_product_ids.append(normalized_id)

        if not unique_product_ids:
            return {}

        fallback_lookup = {
            int(product_id): Decimal(str(price)) for product_id, price in (fallback_prices or {}).items()
        }
        pricing_map: Dict[int, Dict[str, Any]] = {}

        user = User.query.get(user_id)
        if not self._is_legal_entity_user(user):
            for product_id in unique_product_ids:
                fallback_price = Decimal(str(fallback_lookup.get(product_id, Decimal("0.00"))))
                pricing_map[product_id] = {
                    "unit_price": fallback_price,
                    "contract": None,
                    "contract_price_row": None,
                    "pricing_source": "fallback",
                }
            return pricing_map

        effective_at = self._normalize_effective_at(effective_at)
        contracts = self.list_active_contracts_for_user(user_id=user_id, effective_at=effective_at)
        matches_by_product_id: Dict[int, List[CorporateContractProductPrice]] = defaultdict(list)

        for contract in contracts:
            for price_row in contract.product_prices:
                if not price_row.is_active or price_row.product_id not in unique_product_ids:
                    continue
                matches_by_product_id[price_row.product_id].append(price_row)

        for product_id in unique_product_ids:
            fallback_price = Decimal(str(fallback_lookup.get(product_id, Decimal("0.00"))))
            matches = matches_by_product_id.get(product_id, [])

            if not matches:
                pricing_map[product_id] = {
                    "unit_price": fallback_price,
                    "contract": None,
                    "contract_price_row": None,
                    "pricing_source": "fallback",
                }
                continue

            if len(matches) > 1:
                contract_numbers = [row.contract.contract_number for row in matches if row.contract]
                raise ValidationError(
                    self._translate(
                        "api.orders.error.ambiguous_contract_pricing",
                        "Ambiguous contract pricing for product "
                        f"{product_id}. Multiple active contracts match: {', '.join(contract_numbers)}",
                        product_id=product_id,
                        contract_numbers=", ".join(contract_numbers),
                    )
                )

            match = matches[0]
            pricing_map[product_id] = {
                "unit_price": Decimal(str(match.unit_price)),
                "contract": match.contract,
                "contract_price_row": match,
                "pricing_source": "contract",
            }

        return pricing_map

    @staticmethod
    def _normalize_status(status: Optional[str]) -> Optional[CorporateContractStatus]:
        if not status:
            return None
        try:
            return CorporateContractStatus(status)
        except ValueError as exc:
            raise ValidationError("Invalid contract status") from exc

    def _apply_contract_list_filters(
        self,
        query,
        *,
        user_id: Optional[int] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
    ):
        """Apply the user/status/search filters shared by listing and summary.

        The search term matches the contract number/name and the related
        customer's name, phone, and email so admins can find a contract the
        same way they recognise it in the list.
        """
        if user_id:
            query = query.filter(CorporateContract.user_id == user_id)

        status_enum = self._normalize_status(status)
        if status_enum:
            query = query.filter(CorporateContract.status == status_enum)

        normalized_search = (search or "").strip()
        if normalized_search:
            term = f"%{normalized_search}%"
            query = query.join(User, CorporateContract.user_id == User.id).filter(
                or_(
                    CorporateContract.contract_number.ilike(term),
                    CorporateContract.name.ilike(term),
                    User.first_name.ilike(term),
                    User.last_name.ilike(term),
                    func.concat(User.first_name, " ", User.last_name).ilike(term),
                    User.phone.ilike(term),
                    User.email.ilike(term),
                    User.company_name.ilike(term),
                )
            )
        return query

    def list_contracts(
        self,
        user_id: Optional[int] = None,
        status: Optional[str] = None,
        page: int = 1,
        per_page: int = 20,
        search: Optional[str] = None,
    ) -> Dict[str, Any]:
        query = CorporateContract.query.options(
            joinedload(CorporateContract.user),
            joinedload(CorporateContract.prepayment_account).joinedload(CorporatePrepaymentAccount.product_balances),
        )
        query = self._apply_contract_list_filters(query, user_id=user_id, status=status, search=search)

        pagination = query.order_by(CorporateContract.created_at.desc()).paginate(
            page=page,
            per_page=per_page,
            error_out=False,
        )
        return {
            "items": pagination.items,
            "total": pagination.total,
            "page": page,
            "per_page": per_page,
        }

    def get_contracts_summary(
        self,
        user_id: Optional[int] = None,
        status: Optional[str] = None,
        search: Optional[str] = None,
    ) -> Dict[str, int]:
        """Aggregate counts for the contract list KPIs across the whole filter.

        Returned independently of pagination so the dashboard cards reflect every
        matching contract, not just the current page. ``with_debt`` spans both
        tracking modes: AMOUNT-mode contracts with a positive ``outstanding_amount``
        and UNITS-mode contracts with any product balance whose available units
        (prepaid - reserved - consumed) are negative.
        """
        base = self._apply_contract_list_filters(
            CorporateContract.query,
            user_id=user_id,
            status=status,
            search=search,
        )

        total = base.count()
        active = base.filter(
            CorporateContract.is_active.is_(True),
            CorporateContract.status == CorporateContractStatus.ACTIVE,
        ).count()

        money_debt = and_(
            CorporateContract.tracking_mode == CorporateContractTrackingMode.AMOUNT,
            CorporatePrepaymentAccount.outstanding_amount > 0,
        )
        units_debt = exists().where(
            and_(
                CorporatePrepaymentBalance.account_id == CorporatePrepaymentAccount.id,
                (
                    CorporatePrepaymentBalance.prepaid_units
                    - CorporatePrepaymentBalance.reserved_units
                    - CorporatePrepaymentBalance.consumed_units
                )
                < 0,
            )
        )
        with_debt = (
            base.join(
                CorporatePrepaymentAccount,
                CorporatePrepaymentAccount.contract_id == CorporateContract.id,
            )
            .filter(or_(money_debt, units_debt))
            .count()
        )

        return {
            "total": total,
            "active": active,
            "with_debt": with_debt,
        }

    def create_contract(self, payload: Dict[str, Any], actor_user_id: Optional[int] = None) -> CorporateContract:
        user_id = payload.get("user_id")
        if not user_id:
            raise ValidationError("user_id is required")

        user = User.query.get(user_id)
        if not user:
            raise NotFoundError("User not found")
        if not self._is_legal_entity_user(user):
            raise ValidationError("User is not a legal entity client")

        # Resolve tracking_mode from the user's entity_subtype. Workplaces use
        # bottle-unit accounting; grocery stores use money-only accounting.
        # Subtype must be assigned before creating any contract.
        if user.normalized_entity_subtype is None:
            raise ValidationError("Entity subtype must be assigned before creating a contract")
        expected_tracking_mode = (
            CorporateContractTrackingMode.AMOUNT if user.is_grocery_store else CorporateContractTrackingMode.UNITS
        )
        tracking_mode_raw = payload.get("tracking_mode")
        if tracking_mode_raw is not None:
            try:
                requested_mode = CorporateContractTrackingMode(tracking_mode_raw)
            except ValueError as exc:
                raise ValidationError("Invalid tracking_mode") from exc
            if requested_mode != expected_tracking_mode:
                raise ValidationError(
                    f"tracking_mode must be '{expected_tracking_mode.value}' for this user's entity_subtype"
                )
        tracking_mode = expected_tracking_mode

        contract_number = payload.get("contract_number")
        if not contract_number:
            raise ValidationError("contract_number is required")

        existing = CorporateContract.query.filter_by(contract_number=contract_number).first()
        if existing:
            raise ValidationError("contract_number already exists")

        status_raw = payload.get("status", CorporateContractStatus.ACTIVE.value)
        try:
            status = CorporateContractStatus(status_raw)
        except ValueError as exc:
            raise ValidationError("Invalid contract status") from exc

        start_date = payload.get("start_date")
        if isinstance(start_date, str):
            start_date = datetime.fromisoformat(start_date)
        if start_date is None:
            start_date = datetime.now(timezone.utc)
        if start_date.tzinfo is None:
            start_date = start_date.replace(tzinfo=timezone.utc)

        end_date = payload.get("end_date")
        if isinstance(end_date, str):
            end_date = datetime.fromisoformat(end_date)
        if end_date and end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)

        contract = CorporateContract(
            user_id=user_id,
            contract_number=contract_number,
            name=payload.get("name") or contract_number,
            status=status,
            start_date=start_date,
            end_date=end_date,
            currency=payload.get("currency", "UZS"),
            bank_details=payload.get("bank_details") or {},
            notes=payload.get("notes"),
            is_active=bool(payload.get("is_active", True)),
            is_loyalty_points_eligible=self._normalize_bool(
                payload.get("is_loyalty_points_eligible"),
                default=False,
            ),
            allows_debt=self._normalize_bool(
                payload.get("allows_debt"),
                default=False,
            ),
            tracking_mode=tracking_mode,
            created_by_user_id=actor_user_id,
            updated_by_user_id=actor_user_id,
        )
        db.session.add(contract)
        db.session.flush()

        account = CorporatePrepaymentAccount(
            contract_id=contract.id,
            is_active=True,
        )
        db.session.add(account)
        db.session.flush()
        self._validate_contract_price_overlaps(contract)
        return contract

    def update_contract(
        self, contract_id: int, payload: Dict[str, Any], actor_user_id: Optional[int] = None
    ) -> CorporateContract:
        contract = self.get_contract_by_id(contract_id)

        if "name" in payload:
            contract.name = payload.get("name") or contract.name
        if "status" in payload:
            try:
                contract.status = CorporateContractStatus(payload["status"])
            except ValueError as exc:
                raise ValidationError("Invalid contract status") from exc
        if "is_active" in payload:
            contract.is_active = bool(payload["is_active"])
        if "is_loyalty_points_eligible" in payload:
            contract.is_loyalty_points_eligible = self._normalize_bool(payload.get("is_loyalty_points_eligible"))
        if "allows_debt" in payload:
            contract.allows_debt = self._normalize_bool(payload.get("allows_debt"))
        if "notes" in payload:
            contract.notes = payload.get("notes")
        if "bank_details" in payload:
            contract.bank_details = payload.get("bank_details") or {}
        if "currency" in payload:
            contract.currency = payload["currency"]
        if "start_date" in payload:
            start_date = payload.get("start_date")
            if isinstance(start_date, str):
                start_date = datetime.fromisoformat(start_date)
            if start_date and start_date.tzinfo is None:
                start_date = start_date.replace(tzinfo=timezone.utc)
            contract.start_date = start_date
        if "end_date" in payload:
            end_date = payload.get("end_date")
            if isinstance(end_date, str):
                end_date = datetime.fromisoformat(end_date)
            if end_date and end_date.tzinfo is None:
                end_date = end_date.replace(tzinfo=timezone.utc)
            contract.end_date = end_date

        contract.updated_by_user_id = actor_user_id
        self._validate_contract_price_overlaps(contract)
        db.session.flush()
        return contract

    def get_loyalty_eligible_amount_for_order(self, order: Order) -> Decimal:
        if not order:
            raise ValidationError("Order is required")

        order_items = list(getattr(order, "order_items", None) or [])
        if not order_items:
            return Decimal("0.00")

        contract_ids = {item.contract_id for item in order_items if item.contract_id}
        if not contract_ids:
            return Decimal(str(order.total_amount or 0))

        contract_flags = {
            contract_id: is_eligible
            for contract_id, is_eligible in db.session.query(
                CorporateContract.id,
                CorporateContract.is_loyalty_points_eligible,
            )
            .filter(CorporateContract.id.in_(contract_ids))
            .all()
        }

        eligible_amount = Decimal("0.00")
        for item in order_items:
            if not item.contract_id or contract_flags.get(item.contract_id, False):
                eligible_amount += Decimal(str(item.total_price or 0))

        return eligible_amount

    def upsert_contract_prices(
        self,
        contract_id: int,
        prices: List[Dict[str, Any]],
        actor_user_id: Optional[int] = None,
    ) -> List[CorporateContractProductPrice]:
        contract = self.get_contract_by_id(contract_id)
        updated: List[CorporateContractProductPrice] = []

        for entry in prices or []:
            product_id = entry.get("product_id")
            if not product_id:
                raise ValidationError("product_id is required for every price entry")
            if not Product.query.get(product_id):
                raise NotFoundError(f"Product {product_id} not found")

            unit_price = entry.get("unit_price")
            if unit_price is None:
                raise ValidationError("unit_price is required for every price entry")

            row = CorporateContractProductPrice.query.filter_by(
                contract_id=contract.id,
                product_id=product_id,
            ).first()
            if not row:
                row = CorporateContractProductPrice(
                    contract_id=contract.id,
                    product_id=product_id,
                )
                db.session.add(row)

            row.unit_price = Decimal(str(unit_price))
            row.is_prepayment_eligible = bool(entry.get("is_prepayment_eligible", True))
            row.is_active = bool(entry.get("is_active", True))
            row.notes = entry.get("notes")
            updated.append(row)

        self._validate_contract_price_overlaps(contract)
        db.session.flush()
        return updated

    def _validate_contract_price_overlaps(self, contract: CorporateContract) -> None:
        preview = self.preview_contract_price_overlaps(
            contract_id=contract.id,
            user_id=contract.user_id,
            start_date=contract.start_date,
            end_date=contract.end_date,
            status=contract.status.value if contract.status else CorporateContractStatus.ACTIVE.value,
            is_active=contract.is_active,
            prices=[
                {
                    "product_id": row.product_id,
                    "is_active": row.is_active,
                }
                for row in contract.product_prices
            ],
            contract_number=contract.contract_number,
            contract_name=contract.name,
        )
        if preview["has_conflicts"]:
            first = preview["conflicts"][0]
            raise ValidationError(
                "Overlapping active contract coverage detected for "
                f"user {contract.user_id}, product {first['product_id']}, "
                f"contracts {contract.contract_number} and {first['conflicting_contract']['contract_number']}"
            )

    def _get_or_create_locked_account(self, contract_id: int) -> CorporatePrepaymentAccount:
        account = CorporatePrepaymentAccount.query.filter_by(contract_id=contract_id).with_for_update().first()
        if account:
            return account

        account = CorporatePrepaymentAccount(
            contract_id=contract_id,
            is_active=True,
        )
        db.session.add(account)
        db.session.flush()
        return account

    def _get_or_create_locked_balance(self, account_id: int, product_id: int) -> CorporatePrepaymentBalance:
        balance = (
            CorporatePrepaymentBalance.query.filter_by(
                account_id=account_id,
                product_id=product_id,
            )
            .with_for_update()
            .first()
        )
        if balance:
            return balance

        balance = CorporatePrepaymentBalance(
            account_id=account_id,
            product_id=product_id,
            prepaid_units=Decimal("0.00"),
            reserved_units=Decimal("0.00"),
            consumed_units=Decimal("0.00"),
            is_active=True,
        )
        db.session.add(balance)
        db.session.flush()
        return balance

    def _get_contract_price_rows(
        self, contract_id: int, product_ids: List[int]
    ) -> Dict[int, CorporateContractProductPrice]:
        if not product_ids:
            return {}

        rows = (
            CorporateContractProductPrice.query.options(joinedload(CorporateContractProductPrice.product))
            .filter(
                CorporateContractProductPrice.contract_id == contract_id,
                CorporateContractProductPrice.product_id.in_(product_ids),
                CorporateContractProductPrice.is_active.is_(True),
            )
            .all()
        )
        return {row.product_id: row for row in rows}

    def _get_prepayment_lines_for_order(self, order_items: List[OrderItem]) -> List[Dict[str, Any]]:
        if not order_items:
            return []
        lines: List[Dict[str, Any]] = []

        for item in order_items:
            if not item.contract_id or not item.contract_product_price_id:
                continue

            price_row = (
                CorporateContractProductPrice.query.options(
                    joinedload(CorporateContractProductPrice.contract),
                    joinedload(CorporateContractProductPrice.product),
                )
                .filter_by(
                    id=item.contract_product_price_id,
                    contract_id=item.contract_id,
                )
                .first()
            )
            if not price_row or not price_row.is_active or not price_row.is_prepayment_eligible:
                continue

            units = Decimal(str(item.quantity or 0))
            if units <= 0:
                continue

            unit_price = Decimal(str(item.unit_price if item.unit_price is not None else price_row.unit_price))
            lines.append(
                {
                    "order_item": item,
                    "price_row": price_row,
                    "contract_id": item.contract_id,
                    "product_id": item.product_id,
                    "units": units,
                    "unit_price": unit_price,
                    "amount": units * unit_price,
                }
            )

        return lines

    def _get_order_reserve_entries(self, order_id: int) -> List[CorporatePrepaymentLedger]:
        return (
            CorporatePrepaymentLedger.query.filter(
                CorporatePrepaymentLedger.order_id == order_id,
                CorporatePrepaymentLedger.event_type == CorporatePrepaymentEventType.RESERVE,
            )
            .order_by(CorporatePrepaymentLedger.id.asc())
            .all()
        )

    def reserve_for_order(self, order_id: int, actor_user_id: Optional[int] = None) -> List[CorporatePrepaymentLedger]:
        order = Order.query.options(
            joinedload(Order.order_items).joinedload(OrderItem.product),
            joinedload(Order.user),
        ).get(order_id)
        if not order:
            raise NotFoundError("Order not found")
        # Grocery-store users with an active AMOUNT-mode contract skip
        # per-product unit reservation; money debt is posted at delivery via
        # charge_on_delivery. Legacy grocery-store users still on a UNITS-mode
        # (bottle) contract fall through to the normal reservation path.
        if order.user and order.user.is_grocery_store and self.get_active_amount_contract_for_user(order.user.id):
            return []
        lines = self._get_prepayment_lines_for_order(order.order_items)
        if not lines:
            return []

        ledger_entries: List[CorporatePrepaymentLedger] = []
        total_reserved_units = Decimal("0.00")
        contract_totals: Dict[int, Decimal] = {}

        for line in lines:
            order_item = line["order_item"]
            idempotency_key = f"reserve:order_item:{order_item.id}"
            existing = CorporatePrepaymentLedger.query.filter_by(idempotency_key=idempotency_key).first()
            if existing:
                ledger_entries.append(existing)
                total_reserved_units += Decimal(str(existing.units or 0))
                contract_totals[existing.contract_id] = contract_totals.get(
                    existing.contract_id, Decimal("0.00")
                ) + Decimal(str(existing.units or 0))
                continue

            account = self._get_or_create_locked_account(line["contract_id"])
            balance = self._get_or_create_locked_balance(account.id, line["product_id"])
            balance.reserved_units = Decimal(str(balance.reserved_units or 0)) + line["units"]
            contract = line["price_row"].contract

            ledger_entry = CorporatePrepaymentLedger(
                contract_id=line["contract_id"],
                account_id=account.id,
                balance_id=balance.id,
                product_id=line["product_id"],
                order_id=order.id,
                order_item_id=order_item.id,
                actor_user_id=actor_user_id,
                event_type=CorporatePrepaymentEventType.RESERVE,
                units=line["units"],
                unit_price_snapshot=line["unit_price"],
                amount=line["amount"],
                currency=contract.currency,
                notes="Reserved product units on order creation",
                idempotency_key=idempotency_key,
                entry_metadata={
                    "order_number": order.order_number,
                    "product_name": getattr(order_item.product, "name", None),
                },
            )
            db.session.add(ledger_entry)
            ledger_entries.append(ledger_entry)
            total_reserved_units += line["units"]
            contract_totals[line["contract_id"]] = (
                contract_totals.get(line["contract_id"], Decimal("0.00")) + line["units"]
            )

        db.session.flush()
        for contract_id, reserved_units in contract_totals.items():
            audit_logger.log_event(
                event_type=AuditEventType.ORDER_UPDATED,
                action="corporate_prepayment_reserved",
                severity=AuditSeverity.MEDIUM,
                resource_type="corporate_contract",
                resource_id=str(contract_id),
                additional_data={
                    "order_id": order.id,
                    "reserved_units": float(reserved_units),
                    "reserved_products": len([entry for entry in ledger_entries if entry.contract_id == contract_id]),
                },
            )
        return ledger_entries

    def consume_for_order(
        self,
        order_id: int,
        delivery_id: Optional[int] = None,
        actor_user_id: Optional[int] = None,
    ) -> List[CorporatePrepaymentLedger]:
        reserve_entries = self._get_order_reserve_entries(order_id)
        if not reserve_entries:
            return []

        ledger_entries: List[CorporatePrepaymentLedger] = []
        total_consumed_units = Decimal("0.00")

        for reserve_entry in reserve_entries:
            idempotency_key = f"consume:reserve:{reserve_entry.id}"
            existing = CorporatePrepaymentLedger.query.filter_by(idempotency_key=idempotency_key).first()
            if existing:
                ledger_entries.append(existing)
                total_consumed_units += Decimal(str(existing.units or 0))
                continue

            already_released = CorporatePrepaymentLedger.query.filter_by(
                idempotency_key=f"release:reserve:{reserve_entry.id}"
            ).first()
            if already_released:
                continue

            balance = CorporatePrepaymentBalance.query.filter_by(id=reserve_entry.balance_id).with_for_update().first()
            if not balance:
                raise NotFoundError("Corporate prepayment balance not found")

            units = Decimal(str(reserve_entry.units or 0))
            balance.reserved_units = max(Decimal("0.00"), Decimal(str(balance.reserved_units or 0)) - units)
            balance.consumed_units = Decimal(str(balance.consumed_units or 0)) + units

            ledger_entry = CorporatePrepaymentLedger(
                contract_id=reserve_entry.contract_id,
                account_id=reserve_entry.account_id,
                balance_id=reserve_entry.balance_id,
                product_id=reserve_entry.product_id,
                order_id=order_id,
                order_item_id=reserve_entry.order_item_id,
                delivery_id=delivery_id,
                actor_user_id=actor_user_id,
                event_type=CorporatePrepaymentEventType.CONSUME,
                units=units,
                unit_price_snapshot=reserve_entry.unit_price_snapshot,
                amount=reserve_entry.amount,
                currency=reserve_entry.currency,
                notes="Consumed reserved product units on successful delivery",
                idempotency_key=idempotency_key,
                entry_metadata={"source_reserve_entry_id": reserve_entry.id},
            )
            db.session.add(ledger_entry)
            ledger_entries.append(ledger_entry)
            total_consumed_units += units

        db.session.flush()

        audit_logger.log_event(
            event_type=AuditEventType.ORDER_DELIVERED,
            action="corporate_prepayment_consumed",
            severity=AuditSeverity.MEDIUM,
            resource_type="corporate_contract",
            resource_id=str(reserve_entries[0].contract_id),
            additional_data={
                "order_id": order_id,
                "delivery_id": delivery_id,
                "consumed_units": float(total_consumed_units),
                "consumed_products": len(ledger_entries),
            },
        )
        return ledger_entries

    def release_for_order(
        self,
        order_id: int,
        reason: Optional[str] = None,
        actor_user_id: Optional[int] = None,
    ) -> List[CorporatePrepaymentLedger]:
        reserve_entries = self._get_order_reserve_entries(order_id)
        if not reserve_entries:
            return []

        ledger_entries: List[CorporatePrepaymentLedger] = []

        for reserve_entry in reserve_entries:
            already_consumed = CorporatePrepaymentLedger.query.filter_by(
                idempotency_key=f"consume:reserve:{reserve_entry.id}"
            ).first()
            if already_consumed:
                continue

            idempotency_key = f"release:reserve:{reserve_entry.id}"
            existing = CorporatePrepaymentLedger.query.filter_by(idempotency_key=idempotency_key).first()
            if existing:
                ledger_entries.append(existing)
                continue

            balance = CorporatePrepaymentBalance.query.filter_by(id=reserve_entry.balance_id).with_for_update().first()
            if not balance:
                raise NotFoundError("Corporate prepayment balance not found")

            units = Decimal(str(reserve_entry.units or 0))
            balance.reserved_units = max(Decimal("0.00"), Decimal(str(balance.reserved_units or 0)) - units)

            ledger_entry = CorporatePrepaymentLedger(
                contract_id=reserve_entry.contract_id,
                account_id=reserve_entry.account_id,
                balance_id=reserve_entry.balance_id,
                product_id=reserve_entry.product_id,
                order_id=order_id,
                order_item_id=reserve_entry.order_item_id,
                actor_user_id=actor_user_id,
                event_type=CorporatePrepaymentEventType.RELEASE,
                units=units,
                unit_price_snapshot=reserve_entry.unit_price_snapshot,
                amount=reserve_entry.amount,
                currency=reserve_entry.currency,
                notes=reason or "Released reserved product units on order cancellation",
                idempotency_key=idempotency_key,
                entry_metadata={"source_reserve_entry_id": reserve_entry.id},
            )
            db.session.add(ledger_entry)
            ledger_entries.append(ledger_entry)

        db.session.flush()
        return ledger_entries

    def get_active_amount_contract_for_user(
        self,
        user_id: int,
        effective_at: Optional[datetime] = None,
    ) -> Optional[CorporateContract]:
        """Return the user's single active AMOUNT-mode contract, if any.

        Grocery-store users are expected to have at most one active AMOUNT
        contract. Returns None if the user has no eligible contract.
        """
        contracts = self.list_active_contracts_for_user(user_id=user_id, effective_at=effective_at)
        amount_contracts = [c for c in contracts if c.tracking_mode == CorporateContractTrackingMode.AMOUNT]
        if not amount_contracts:
            return None
        if len(amount_contracts) > 1:
            raise ValidationError(
                f"User {user_id} has multiple active AMOUNT-mode contracts; resolve before continuing."
            )
        return amount_contracts[0]

    def _require_amount_contract(self, contract: CorporateContract) -> None:
        if contract.tracking_mode != CorporateContractTrackingMode.AMOUNT:
            raise ValidationError(f"Contract {contract.id} is not configured for money-mode tracking.")

    def charge_on_delivery(
        self,
        order: Order,
        delivery_id: Optional[int] = None,
        actor_user_id: Optional[int] = None,
    ) -> Optional[CorporatePrepaymentLedger]:
        """Post a CHARGE ledger entry for an AMOUNT-mode contract on delivery.

        Idempotent: a second call for the same order returns the existing entry.
        """
        if not order:
            raise ValidationError("Order is required")

        user = order.user or User.query.get(order.user_id)
        if not user or not user.is_grocery_store:
            return None

        contract = self.get_active_amount_contract_for_user(user.id)
        if not contract:
            raise ValidationError("No active AMOUNT-mode contract found for grocery store user")
        self._require_amount_contract(contract)

        idempotency_key = f"charge:order:{order.id}"
        existing = CorporatePrepaymentLedger.query.filter_by(idempotency_key=idempotency_key).first()
        if existing:
            return existing

        amount = Decimal(str(order.total_amount or 0))
        if amount <= 0:
            return None

        account = self._get_or_create_locked_account(contract.id)
        now = datetime.now(timezone.utc)
        account.outstanding_amount = Decimal(str(account.outstanding_amount or 0)) + amount
        account.lifetime_charged = Decimal(str(account.lifetime_charged or 0)) + amount
        account.last_charged_at = now

        ledger_entry = CorporatePrepaymentLedger(
            contract_id=contract.id,
            account_id=account.id,
            balance_id=None,
            product_id=None,
            order_id=order.id,
            order_item_id=None,
            delivery_id=delivery_id,
            actor_user_id=actor_user_id,
            event_type=CorporatePrepaymentEventType.CHARGE,
            units=None,
            unit_price_snapshot=None,
            amount=amount,
            currency=contract.currency,
            notes="Order delivered (grocery store charge)",
            idempotency_key=idempotency_key,
            entry_metadata={
                "order_number": order.order_number,
                "outstanding_after": float(account.outstanding_amount),
            },
        )
        db.session.add(ledger_entry)
        db.session.flush()

        audit_logger.log_event(
            event_type=AuditEventType.ORDER_DELIVERED,
            action="grocery_debt_charged",
            severity=AuditSeverity.MEDIUM,
            resource_type="corporate_contract",
            resource_id=str(contract.id),
            additional_data={
                "order_id": order.id,
                "delivery_id": delivery_id,
                "amount": float(amount),
                "outstanding_after": float(account.outstanding_amount),
            },
        )
        return ledger_entry

    def record_money_collection(
        self,
        contract: CorporateContract,
        amount: Decimal,
        *,
        source: Optional[str] = None,
        order_id: Optional[int] = None,
        delivery_id: Optional[int] = None,
        cash_event_id: Optional[int] = None,
        actor_user_id: Optional[int] = None,
        notes: Optional[str] = None,
    ) -> Optional[CorporatePrepaymentLedger]:
        """Post a COLLECT ledger entry against an AMOUNT-mode contract.

        Idempotent. Drives outstanding_amount down (and into negative/credit
        territory if collected total exceeds lifetime charges).
        """
        self._require_amount_contract(contract)
        amount_decimal = Decimal(str(amount or 0))
        if amount_decimal <= 0:
            return None

        # Idempotency key prefers cash_event_id when present; otherwise tag by
        # order. A residual collection (no order_id) gets a unique cash_event-only
        # key so the residual posts exactly once per cash event.
        if cash_event_id is not None and order_id is not None:
            idempotency_key = f"collect:cash_event:{cash_event_id}:order:{order_id}"
        elif cash_event_id is not None:
            idempotency_key = f"collect:cash_event:{cash_event_id}:residual"
        elif order_id is not None:
            idempotency_key = f"collect:order:{order_id}:adhoc:{datetime.now(timezone.utc).timestamp()}"
        else:
            idempotency_key = f"collect:contract:{contract.id}:adhoc:{datetime.now(timezone.utc).timestamp()}"

        existing = CorporatePrepaymentLedger.query.filter_by(idempotency_key=idempotency_key).first()
        if existing:
            return existing

        account = self._get_or_create_locked_account(contract.id)
        now = datetime.now(timezone.utc)
        account.outstanding_amount = Decimal(str(account.outstanding_amount or 0)) - amount_decimal
        account.lifetime_collected = Decimal(str(account.lifetime_collected or 0)) + amount_decimal
        account.last_collected_at = now

        ledger_entry = CorporatePrepaymentLedger(
            contract_id=contract.id,
            account_id=account.id,
            balance_id=None,
            product_id=None,
            order_id=order_id,
            order_item_id=None,
            delivery_id=delivery_id,
            actor_user_id=actor_user_id,
            event_type=CorporatePrepaymentEventType.COLLECT,
            units=None,
            unit_price_snapshot=None,
            amount=amount_decimal,
            currency=contract.currency,
            notes=notes or "Cash/card collected against grocery store debt",
            idempotency_key=idempotency_key,
            entry_metadata={
                "source": source,
                "cash_event_id": cash_event_id,
                "outstanding_after": float(account.outstanding_amount),
            },
        )
        db.session.add(ledger_entry)
        db.session.flush()

        audit_logger.log_event(
            event_type=AuditEventType.PAYMENT_PROCESSED,
            action="grocery_debt_collected",
            severity=AuditSeverity.MEDIUM,
            resource_type="corporate_contract",
            resource_id=str(contract.id),
            additional_data={
                "amount": float(amount_decimal),
                "source": source,
                "order_id": order_id,
                "delivery_id": delivery_id,
                "cash_event_id": cash_event_id,
                "outstanding_after": float(account.outstanding_amount),
            },
        )
        return ledger_entry

    def _require_units_contract(self, contract: CorporateContract) -> None:
        if contract.tracking_mode != CorporateContractTrackingMode.UNITS:
            raise ValidationError(f"Contract {contract.id} is not configured for units-mode tracking.")

    def topup_from_cash_collection(
        self,
        contract: CorporateContract,
        *,
        order_id: int,
        cash_event_id: int,
        delivery_id: Optional[int] = None,
        actor_user_id: Optional[int] = None,
        source: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> List[CorporatePrepaymentLedger]:
        """Mirror cash collected at delivery onto a UNITS-mode contract.

        Posts one TOPUP ledger entry per CONSUME entry written at delivery for
        ``order_id`` on ``contract``. Bridges the legacy gap where grocery-store
        users remain on a UNITS-mode contract (current code forces AMOUNT-mode
        for new grocery stores, but legacy contracts still hit
        ``reserve_for_order`` -> ``consume_for_order``). Without this mirror,
        cash collected at delivery would leave ``prepaid_units`` untouched while
        ``consumed_units`` grows, producing a perpetually negative available
        balance.

        Idempotent via
        ``topup:cash_event:{cash_event_id}:consume:{consume_entry.id}``.
        Returns existing entries on repost and an empty list when no CONSUME
        entries exist yet (cash collected before delivery; defer).
        """
        self._require_units_contract(contract)

        consume_entries = (
            CorporatePrepaymentLedger.query.filter(
                CorporatePrepaymentLedger.order_id == order_id,
                CorporatePrepaymentLedger.contract_id == contract.id,
                CorporatePrepaymentLedger.event_type == CorporatePrepaymentEventType.CONSUME,
            )
            .order_by(CorporatePrepaymentLedger.id.asc())
            .all()
        )
        if not consume_entries:
            return []

        account = self._get_or_create_locked_account(contract.id)
        now = datetime.now(timezone.utc)
        ledger_entries: List[CorporatePrepaymentLedger] = []
        total_topped_up_units = Decimal("0.00")
        new_entry_count = 0

        for consume_entry in consume_entries:
            idempotency_key = f"topup:cash_event:{cash_event_id}:consume:{consume_entry.id}"
            existing = CorporatePrepaymentLedger.query.filter_by(idempotency_key=idempotency_key).first()
            if existing:
                ledger_entries.append(existing)
                continue

            units = Decimal(str(consume_entry.units or 0))
            if units <= 0:
                continue

            balance = CorporatePrepaymentBalance.query.filter_by(id=consume_entry.balance_id).with_for_update().first()
            if not balance:
                raise NotFoundError("Corporate prepayment balance not found")

            balance.prepaid_units = Decimal(str(balance.prepaid_units or 0)) + units
            balance.last_topup_at = now

            unit_price = (
                Decimal(str(consume_entry.unit_price_snapshot))
                if consume_entry.unit_price_snapshot is not None
                else None
            )
            amount = (units * unit_price) if unit_price is not None else None

            ledger_entry = CorporatePrepaymentLedger(
                contract_id=contract.id,
                account_id=account.id,
                balance_id=consume_entry.balance_id,
                product_id=consume_entry.product_id,
                order_id=order_id,
                order_item_id=consume_entry.order_item_id,
                delivery_id=delivery_id,
                actor_user_id=actor_user_id,
                event_type=CorporatePrepaymentEventType.TOPUP,
                units=units,
                unit_price_snapshot=unit_price,
                amount=amount,
                currency=consume_entry.currency,
                notes=notes or "Auto topup from cash collection (legacy UNITS-mode grocery)",
                idempotency_key=idempotency_key,
                entry_metadata={
                    "auto_topup": True,
                    "source": source,
                    "cash_event_id": cash_event_id,
                    "source_consume_entry_id": consume_entry.id,
                },
            )
            db.session.add(ledger_entry)
            ledger_entries.append(ledger_entry)
            total_topped_up_units += units
            new_entry_count += 1

        if new_entry_count:
            account.last_topup_at = now
        db.session.flush()

        if new_entry_count:
            audit_logger.log_event(
                event_type=AuditEventType.PAYMENT_PROCESSED,
                action="corporate_units_auto_topup_from_cash",
                severity=AuditSeverity.MEDIUM,
                resource_type="corporate_contract",
                resource_id=str(contract.id),
                additional_data={
                    "order_id": order_id,
                    "delivery_id": delivery_id,
                    "cash_event_id": cash_event_id,
                    "source": source,
                    "topup_entry_count": new_entry_count,
                    "total_units": float(total_topped_up_units),
                },
            )

        return ledger_entries

    def post_money_adjustment(
        self,
        contract: CorporateContract,
        amount: Decimal,
        actor_user_id: Optional[int] = None,
        reason: Optional[str] = None,
    ) -> CorporatePrepaymentLedger:
        """Post a manual ADJUSTMENT entry on an AMOUNT-mode contract.

        Positive `amount` increases outstanding (customer owes more);
        negative `amount` decreases outstanding (write-off / credit).
        """
        self._require_amount_contract(contract)
        amount_decimal = Decimal(str(amount))
        if amount_decimal == 0:
            raise ValidationError("Adjustment amount must be non-zero")
        if not reason:
            raise ValidationError("Adjustment reason is required")

        account = self._get_or_create_locked_account(contract.id)
        account.outstanding_amount = Decimal(str(account.outstanding_amount or 0)) + amount_decimal

        ledger_entry = CorporatePrepaymentLedger(
            contract_id=contract.id,
            account_id=account.id,
            balance_id=None,
            product_id=None,
            actor_user_id=actor_user_id,
            event_type=CorporatePrepaymentEventType.ADJUSTMENT,
            units=None,
            unit_price_snapshot=None,
            amount=amount_decimal,
            currency=contract.currency,
            notes=reason,
            idempotency_key=f"adjustment:contract:{contract.id}:{datetime.now(timezone.utc).timestamp()}",
            entry_metadata={"outstanding_after": float(account.outstanding_amount)},
        )
        db.session.add(ledger_entry)
        db.session.flush()
        return ledger_entry

    def topup_contract(
        self,
        contract_id: int,
        product_id: int,
        units: Decimal,
        amount: Optional[Decimal] = None,
        transfer_ref: Optional[str] = None,
        actor_user_id: Optional[int] = None,
        notes: Optional[str] = None,
    ) -> CorporatePrepaymentLedger:
        if Decimal(str(units)) <= 0:
            raise ValidationError("Topup units must be positive")

        contract = self.get_contract_by_id(contract_id)
        account = self._get_or_create_locked_account(contract.id)
        price_row = CorporateContractProductPrice.query.filter(
            CorporateContractProductPrice.contract_id == contract.id,
            CorporateContractProductPrice.product_id == product_id,
            CorporateContractProductPrice.is_active.is_(True),
        ).first()
        if not price_row:
            raise ValidationError("Product is not configured on this contract")
        if not price_row.is_prepayment_eligible:
            raise ValidationError("Product is not eligible for corporate prepayment")

        balance = self._get_or_create_locked_balance(account.id, product_id)
        units_decimal = Decimal(str(units))
        amount_decimal = Decimal(str(amount)) if amount is not None else None
        unit_price_snapshot = (
            (amount_decimal / units_decimal)
            if amount_decimal is not None and units_decimal != 0
            else Decimal(str(price_row.unit_price))
        )

        balance.prepaid_units = Decimal(str(balance.prepaid_units or 0)) + units_decimal
        balance.last_topup_at = datetime.now(timezone.utc)
        account.last_topup_at = balance.last_topup_at

        ledger_entry = CorporatePrepaymentLedger(
            contract_id=contract.id,
            account_id=account.id,
            balance_id=balance.id,
            product_id=product_id,
            actor_user_id=actor_user_id,
            event_type=CorporatePrepaymentEventType.TOPUP,
            units=units_decimal,
            unit_price_snapshot=unit_price_snapshot,
            amount=amount_decimal,
            currency=contract.currency,
            transfer_reference=transfer_ref,
            notes=notes or "Corporate prepayment topup",
            idempotency_key=f"topup:contract:{contract.id}:product:{product_id}:{datetime.now(timezone.utc).timestamp()}",  # noqa: E501
            entry_metadata={
                "product_name": getattr(price_row.product, "name", None),
            },
        )
        db.session.add(ledger_entry)
        db.session.flush()

        audit_logger.log_event(
            event_type=AuditEventType.PAYMENT_PROCESSED,
            action="corporate_prepayment_topped_up",
            severity=AuditSeverity.MEDIUM,
            resource_type="corporate_contract",
            resource_id=str(contract.id),
            additional_data={
                "product_id": product_id,
                "units": float(units_decimal),
                "amount": float(amount) if amount is not None else None,
                "transfer_reference": transfer_ref,
            },
        )
        return ledger_entry

    def get_balance(self, contract_id: int) -> Dict[str, Any]:
        contract = self.get_contract_by_id(contract_id)
        account = contract.prepayment_account
        if not account:
            account = CorporatePrepaymentAccount(
                contract_id=contract.id,
                is_active=True,
            )
            db.session.add(account)
            db.session.flush()

        # AMOUNT-mode contracts (grocery stores): money-only summary, no
        # per-product unit balances are tracked.
        if contract.tracking_mode == CorporateContractTrackingMode.AMOUNT:
            return {
                "contract_id": contract.id,
                "currency": contract.currency,
                "account_id": account.id,
                "tracking_mode": CorporateContractTrackingMode.AMOUNT.value,
                "summary": {
                    "outstanding_amount": float(account.outstanding_amount or 0),
                    "lifetime_charged": float(account.lifetime_charged or 0),
                    "lifetime_collected": float(account.lifetime_collected or 0),
                    "last_charged_at": account.last_charged_at.isoformat() if account.last_charged_at else None,
                    "last_collected_at": account.last_collected_at.isoformat() if account.last_collected_at else None,
                },
                "products": [],
            }

        price_rows = {
            row.product_id: row for row in contract.product_prices if row.is_active and row.is_prepayment_eligible
        }
        balance_rows = {row.product_id: row for row in account.product_balances or []}
        product_ids = sorted(set(price_rows.keys()) | set(balance_rows.keys()))
        products: List[Dict[str, Any]] = []

        for product_id in product_ids:
            balance_row = balance_rows.get(product_id)
            price_row = price_rows.get(product_id)
            product = None
            if balance_row and balance_row.product:
                product = balance_row.product
            elif price_row and price_row.product:
                product = price_row.product

            if balance_row:
                product_balance = balance_row.to_dict()
            else:
                product_balance = {
                    "id": None,
                    "account_id": account.id,
                    "product_id": product_id,
                    "product_name": getattr(product, "name", None),
                    "product_sku": getattr(product, "sku", None),
                    "product_size": getattr(getattr(product, "size", None), "value", getattr(product, "size", None)),
                    "prepaid_units": 0.0,
                    "reserved_units": 0.0,
                    "consumed_units": 0.0,
                    "available_units": 0.0,
                    "debt_units": 0.0,
                    "is_active": True,
                    "last_topup_at": None,
                }

            product_balance.update(
                {
                    "contract_unit_price": (
                        float(price_row.unit_price) if price_row and price_row.unit_price is not None else None
                    ),
                    "is_prepayment_eligible": bool(price_row.is_prepayment_eligible) if price_row else False,
                    "contract_price_row_id": price_row.id if price_row else None,
                }
            )
            products.append(product_balance)

        summary = {
            "tracked_products_count": len(products),
            "products_with_reservations_count": sum(1 for item in products if Decimal(str(item["reserved_units"])) > 0),
            "products_in_debt_count": sum(1 for item in products if Decimal(str(item["available_units"])) < 0),
            "last_topup_at": account.last_topup_at.isoformat() if account.last_topup_at else None,
        }

        return {
            "contract_id": contract.id,
            "currency": contract.currency,
            "account_id": account.id,
            "tracking_mode": CorporateContractTrackingMode.UNITS.value,
            "summary": summary,
            "products": products,
        }

    def get_active_contract_balances_for_user(
        self,
        user_id: int,
        effective_at: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        contracts = self.list_active_contracts_for_user(user_id=user_id, effective_at=effective_at)
        results: List[Dict[str, Any]] = []
        for contract in contracts:
            balance = self.get_balance(contract.id)
            results.append(
                {
                    "contract": {
                        "id": contract.id,
                        "contract_number": contract.contract_number,
                        "name": contract.name,
                        "currency": contract.currency,
                        "start_date": contract.start_date.isoformat() if contract.start_date else None,
                        "end_date": contract.end_date.isoformat() if contract.end_date else None,
                    },
                    "balance": balance,
                }
            )
        return results

    def get_ledger(
        self,
        contract_id: int,
        page: int = 1,
        per_page: int = 50,
        event_type: Optional[str] = None,
        product_id: Optional[int] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        self.get_contract_by_id(contract_id)

        query = CorporatePrepaymentLedger.query.options(joinedload(CorporatePrepaymentLedger.product)).filter_by(
            contract_id=contract_id
        )
        if event_type:
            try:
                query = query.filter(CorporatePrepaymentLedger.event_type == CorporatePrepaymentEventType(event_type))
            except ValueError as exc:
                raise ValidationError("Invalid ledger event_type") from exc
        if product_id:
            query = query.filter(CorporatePrepaymentLedger.product_id == product_id)

        if start_date:
            query = query.filter(CorporatePrepaymentLedger.created_at >= start_date)
        if end_date:
            query = query.filter(CorporatePrepaymentLedger.created_at <= end_date)

        pagination = query.order_by(CorporatePrepaymentLedger.created_at.desc()).paginate(
            page=page,
            per_page=min(per_page, 200),
            error_out=False,
        )

        order_product_names = self._resolve_order_product_names(pagination.items)
        items = []
        for entry in pagination.items:
            payload = entry.to_dict()
            # Money-mode rows (CHARGE/COLLECT) carry no product_id because the
            # charge is posted against the whole delivered order. Surface the
            # order's product names so the ledger stays meaningful instead of
            # showing a bare "#-".
            if not entry.product_id and entry.order_id:
                payload["order_product_names"] = order_product_names.get(entry.order_id, [])
            else:
                payload["order_product_names"] = []
            items.append(payload)

        return {
            "items": items,
            "total": pagination.total,
            "page": page,
            "per_page": min(per_page, 200),
        }

    @staticmethod
    def _resolve_order_product_names(ledger_entries: List[CorporatePrepaymentLedger]) -> Dict[int, List[str]]:
        """Map order_id -> ordered, de-duplicated product names for product-less rows.

        Only product-less, order-linked ledger rows (money-mode charges) need
        this; their amount covers an entire order rather than a single product.
        Resolved in one batched query to avoid an N+1 over the ledger page.
        """
        order_ids = {entry.order_id for entry in ledger_entries if entry.order_id and not entry.product_id}
        if not order_ids:
            return {}

        rows = (
            db.session.query(OrderItem.order_id, Product.name)
            .join(Product, Product.id == OrderItem.product_id)
            .filter(OrderItem.order_id.in_(order_ids))
            .order_by(OrderItem.order_id, OrderItem.id)
            .all()
        )

        names_by_order: Dict[int, List[str]] = {}
        for order_id, product_name in rows:
            if not product_name:
                continue
            names = names_by_order.setdefault(order_id, [])
            if product_name not in names:
                names.append(product_name)
        return names_by_order


_corporate_contract_service = None


def get_corporate_contract_service() -> CorporateContractService:
    global _corporate_contract_service
    if _corporate_contract_service is None:
        _corporate_contract_service = CorporateContractService()
    return _corporate_contract_service
