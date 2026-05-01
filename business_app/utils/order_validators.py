"""
Comprehensive validation utilities for Order API endpoints
"""

import re
from datetime import datetime, date, UTC, timedelta
from typing import Any, Dict, List
from decimal import Decimal, InvalidOperation
import bleach

from shared.enums import OrderStatus, PaymentMethod


class OrderInputValidator:
    """Comprehensive order input validation"""

    def __init__(self):
        self.errors = {}

    def validate_create_order(self, data: Dict[str, Any]) -> Dict[str, List[str]]:
        """Validate order creation data"""
        self.errors = {}

        # Required fields validation
        self._validate_required_fields(data, ["items", "delivery_address_id"])

        # Validate items
        if "items" in data:
            self._validate_order_items(data["items"])

        # Validate delivery address
        if "delivery_address_id" in data:
            self._validate_delivery_address_id(data["delivery_address_id"])

        # Validate delivery date and time
        if "delivery_date" in data:
            self._validate_delivery_date(data["delivery_date"])

        if "delivery_time_slot" in data:
            self._validate_delivery_time_slot(data["delivery_time_slot"])

        # Validate payment method
        if "payment_method" in data:
            self._validate_payment_method(data["payment_method"])

        # Validate loyalty points
        if "loyalty_points_used" in data:
            self._validate_loyalty_points(data["loyalty_points_used"])

        # Validate promo code
        if "promo_code" in data:
            self._validate_promo_code(data["promo_code"])

        # Validate delivery notes (sanitize for XSS)
        if "delivery_notes" in data:
            self._validate_delivery_notes(data["delivery_notes"])

        # Validate order source
        if "order_source" in data:
            self._validate_order_source(data["order_source"])

        # Validate emergency flag
        if "is_urgent" in data:
            self._validate_urgent_flag(data["is_urgent"])

        return self.errors

    def validate_cart_estimate(self, data: Dict[str, Any]) -> Dict[str, List[str]]:
        """Validate cart estimate data"""
        self.errors = {}

        # Required fields
        self._validate_required_fields(data, ["items"])

        # Validate items
        if "items" in data:
            self._validate_order_items(data["items"])

        # Optional fields validation
        if "delivery_address_id" in data:
            self._validate_delivery_address_id(data["delivery_address_id"])

        if "delivery_date" in data:
            self._validate_delivery_date(data["delivery_date"])

        if "delivery_time_slot" in data:
            self._validate_delivery_time_slot(data["delivery_time_slot"])

        if "loyalty_points_used" in data:
            self._validate_loyalty_points(data["loyalty_points_used"])

        if "promo_code" in data:
            self._validate_promo_code(data["promo_code"])

        return self.errors

    def validate_order_feedback(self, data: Dict[str, Any]) -> Dict[str, List[str]]:
        """Validate order feedback data"""
        self.errors = {}

        # Required fields
        self._validate_required_fields(data, ["rating"])

        # Validate rating
        if "rating" in data:
            self._validate_rating(data["rating"])

        # Validate comment (optional but sanitize if present)
        if "comment" in data:
            self._validate_feedback_comment(data["comment"])

        return self.errors

    def validate_emergency_order(self, data: Dict[str, Any]) -> Dict[str, List[str]]:
        """Validate emergency order data"""
        self.errors = {}

        # Required fields for emergency orders
        self._validate_required_fields(data, ["items", "delivery_address_id"])

        # Validate items with stricter limits for emergency orders
        if "items" in data:
            self._validate_emergency_order_items(data["items"])

        # Validate delivery address
        if "delivery_address_id" in data:
            self._validate_delivery_address_id(data["delivery_address_id"])

        # Validate delivery notes (sanitize)
        if "delivery_notes" in data:
            self._validate_emergency_delivery_notes(data["delivery_notes"])

        # Payment method is required for emergency orders
        if "payment_method" not in data:
            self._add_error("payment_method", "Payment method is required for emergency orders")
        else:
            self._validate_payment_method(data["payment_method"])
            # Emergency orders don't support cash on delivery
            if data["payment_method"] == PaymentMethod.CASH.value:
                self._add_error("payment_method", "Cash payment not allowed for emergency orders")

        return self.errors

    def validate_bulk_action(self, data: Dict[str, Any]) -> Dict[str, List[str]]:
        """Validate bulk action data"""
        self.errors = {}

        # Required fields
        self._validate_required_fields(data, ["action", "order_ids"])

        # Validate action
        if "action" in data:
            self._validate_bulk_action_type(data["action"])

        # Validate order IDs
        if "order_ids" in data:
            self._validate_order_ids_list(data["order_ids"])

        return self.errors

    def validate_subscription_order(self, data: Dict[str, Any]) -> Dict[str, List[str]]:
        """Validate subscription order data"""
        self.errors = {}

        # Required fields
        self._validate_required_fields(data, ["items", "frequency", "delivery_address_id"])

        # Validate items
        if "items" in data:
            self._validate_subscription_items(data["items"])

        # Validate frequency
        if "frequency" in data:
            self._validate_subscription_frequency(data["frequency"])

        # Validate start date
        if "start_date" in data:
            self._validate_subscription_start_date(data["start_date"])

        # Validate delivery address
        if "delivery_address_id" in data:
            self._validate_delivery_address_id(data["delivery_address_id"])

        # Validate payment method
        if "payment_method" in data:
            self._validate_payment_method(data["payment_method"])

        # Validate auto pay flag
        if "auto_pay" in data:
            self._validate_auto_pay_flag(data["auto_pay"])

        return self.errors

    def validate_scheduled_order(self, data: Dict[str, Any]) -> Dict[str, List[str]]:
        """Validate scheduled order data"""
        self.errors = {}

        # Required fields
        self._validate_required_fields(data, ["items", "scheduled_date", "delivery_address_id"])

        # Validate items
        if "items" in data:
            self._validate_order_items(data["items"])

        # Validate scheduled date
        if "scheduled_date" in data:
            self._validate_scheduled_date(data["scheduled_date"])

        # Validate delivery address
        if "delivery_address_id" in data:
            self._validate_delivery_address_id(data["delivery_address_id"])

        # Validate payment method
        if "payment_method" in data:
            self._validate_payment_method(data["payment_method"])

        return self.errors

    def validate_export(self, data: Dict[str, Any]) -> Dict[str, List[str]]:
        """Validate export data"""
        self.errors = {}

        # Required fields
        self._validate_required_fields(data, ["format"])

        # Validate format
        if "format" in data:
            self._validate_export_format(data["format"])

        # Validate date range
        if "start_date" in data:
            self._validate_export_date(data["start_date"], "start_date")

        if "end_date" in data:
            self._validate_export_date(data["end_date"], "end_date")

        # Validate filters
        if "filters" in data:
            self._validate_export_filters(data["filters"])

        return self.errors

    # Private validation methods

    def _validate_required_fields(self, data: Dict[str, Any], required_fields: List[str]):
        """Validate required fields are present and not empty"""
        for field in required_fields:
            if field not in data or data[field] is None:
                self._add_error(field, f"{field} is required")
            elif isinstance(data[field], str) and not data[field].strip():
                self._add_error(field, f"{field} cannot be empty")
            elif isinstance(data[field], list) and len(data[field]) == 0:
                self._add_error(field, f"{field} cannot be empty")

    def _validate_order_items(self, items: Any):
        """Validate order items array"""
        if not isinstance(items, list):
            self._add_error("items", "Items must be a list")
            return

        if len(items) == 0:
            self._add_error("items", "Order must contain at least one item")
            return

        if len(items) > 50:  # Reasonable limit
            self._add_error("items", "Order cannot contain more than 50 items")
            return

        for i, item in enumerate(items):
            if not isinstance(item, dict):
                self._add_error("items", f"Item {i+1} must be an object")
                continue

            # Required item fields
            if "product_id" not in item:
                self._add_error("items", f"Item {i+1}: product_id is required")
            else:
                self._validate_product_id(item["product_id"], f"Item {i+1}")

            if "quantity" not in item:
                self._add_error("items", f"Item {i+1}: quantity is required")
            else:
                self._validate_item_quantity(item["quantity"], f"Item {i+1}")

            # Optional item fields validation
            if "custom_price" in item:
                self._validate_custom_price(item["custom_price"], f"Item {i+1}")

            if "notes" in item:
                self._validate_item_notes(item["notes"], f"Item {i+1}")

    def _validate_emergency_order_items(self, items: Any):
        """Validate emergency order items with stricter limits"""
        self._validate_order_items(items)

        if isinstance(items, list) and len(items) > 10:
            self._add_error("items", "Emergency orders cannot contain more than 10 items")

        # Additional validation for emergency orders
        total_quantity = 0
        for item in items:
            if isinstance(item, dict) and "quantity" in item:
                try:
                    total_quantity += int(item["quantity"])
                except (ValueError, TypeError):
                    pass

        if total_quantity > 50:
            self._add_error("items", "Emergency orders cannot have more than 50 total items")

    def _validate_subscription_items(self, items: Any):
        """Validate subscription order items"""
        self._validate_order_items(items)

        # Subscription orders have different limits
        if isinstance(items, list) and len(items) > 20:
            self._add_error("items", "Subscription orders cannot contain more than 20 different products")

    def _validate_product_id(self, product_id: Any, context: str = ""):
        """Validate product ID"""
        prefix = f"{context}: " if context else ""

        if not isinstance(product_id, int):
            try:
                product_id = int(product_id)
            except (ValueError, TypeError):
                self._add_error("items", f"{prefix}product_id must be a valid integer")
                return

        if product_id <= 0:
            self._add_error("items", f"{prefix}product_id must be positive")

        if product_id > 2147483647:  # PostgreSQL integer limit
            self._add_error("items", f"{prefix}product_id is too large")

    def _validate_item_quantity(self, quantity: Any, context: str = ""):
        """Validate item quantity"""
        prefix = f"{context}: " if context else ""

        if not isinstance(quantity, int):
            try:
                quantity = int(quantity)
            except (ValueError, TypeError):
                self._add_error("items", f"{prefix}quantity must be a valid integer")
                return

        if quantity <= 0:
            self._add_error("items", f"{prefix}quantity must be positive")

        if quantity > 1000:  # Reasonable per-item limit
            self._add_error("items", f"{prefix}quantity cannot exceed 1000 per item")

    def _validate_custom_price(self, price: Any, context: str = ""):
        """Validate custom price"""
        prefix = f"{context}: " if context else ""

        try:
            price_val = Decimal(str(price))
            if price_val <= 0:
                self._add_error("items", f"{prefix}custom_price must be positive")
            if price_val > Decimal("10000000"):  # 10 million limit
                self._add_error("items", f"{prefix}custom_price is too large")
        except (ValueError, TypeError, InvalidOperation):
            self._add_error("items", f"{prefix}custom_price must be a valid number")

    def _validate_item_notes(self, notes: Any, context: str = ""):
        """Validate and sanitize item notes"""
        prefix = f"{context}: " if context else ""

        if not isinstance(notes, str):
            self._add_error("items", f"{prefix}notes must be a string")
            return

        # Sanitize for XSS
        sanitized_notes = bleach.clean(notes, tags=[], strip=True)

        if len(sanitized_notes) > 500:
            self._add_error("items", f"{prefix}notes cannot exceed 500 characters")

        # Check for suspicious patterns
        if re.search(r"<script|javascript:|data:|vbscript:", sanitized_notes, re.IGNORECASE):
            self._add_error("items", f"{prefix}notes contain invalid content")

    def _validate_delivery_address_id(self, address_id: Any):
        """Validate delivery address ID"""
        if not isinstance(address_id, int):
            try:
                address_id = int(address_id)
            except (ValueError, TypeError):
                self._add_error("delivery_address_id", "delivery_address_id must be a valid integer")
                return

        if address_id <= 0:
            self._add_error("delivery_address_id", "delivery_address_id must be positive")

    def _validate_delivery_date(self, delivery_date: Any):
        """Validate delivery date"""
        if not isinstance(delivery_date, str):
            self._add_error("delivery_date", "delivery_date must be a string")
            return

        try:
            parsed_date = datetime.fromisoformat(delivery_date).date()

            # Cannot be in the past
            if parsed_date < date.today():
                self._add_error("delivery_date", "delivery_date cannot be in the past")

            # Cannot be more than 30 days in the future
            max_date = date.today() + timedelta(days=30)
            if parsed_date > max_date:
                self._add_error("delivery_date", "delivery_date cannot be more than 30 days in the future")

        except ValueError:
            self._add_error("delivery_date", "delivery_date must be in ISO format (YYYY-MM-DD)")

    def _validate_delivery_time_slot(self, time_slot: Any):
        """Validate delivery time slot"""
        if not isinstance(time_slot, str):
            self._add_error("delivery_time_slot", "delivery_time_slot must be a string")
            return

        # Sanitize input
        time_slot = bleach.clean(time_slot, tags=[], strip=True)

        if len(time_slot) > 50:
            self._add_error("delivery_time_slot", "delivery_time_slot cannot exceed 50 characters")

        # Valid time slot patterns
        valid_slots = [
            "morning",
            "afternoon",
            "evening",
            "emergency",
            "09:00-12:00",
            "12:00-15:00",
            "15:00-18:00",
            "18:00-21:00",
        ]

        if time_slot not in valid_slots:
            # Allow custom time slot format HH:MM-HH:MM
            if not re.match(r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$", time_slot):
                self._add_error("delivery_time_slot", "delivery_time_slot has invalid format")

    def _validate_payment_method(self, payment_method: Any):
        """Validate payment method"""
        if not isinstance(payment_method, str):
            self._add_error("payment_method", "payment_method must be a string")
            return

        valid_methods = [method.value for method in PaymentMethod]
        if payment_method not in valid_methods:
            self._add_error("payment_method", f'payment_method must be one of: {", ".join(valid_methods)}')

    def _validate_loyalty_points(self, points: Any):
        """Validate loyalty points"""
        if not isinstance(points, int):
            try:
                points = int(points)
            except (ValueError, TypeError):
                self._add_error("loyalty_points_used", "loyalty_points_used must be a valid integer")
                return

        if points < 0:
            self._add_error("loyalty_points_used", "loyalty_points_used cannot be negative")

        if points > 1000000:  # Reasonable upper limit
            self._add_error("loyalty_points_used", "loyalty_points_used is too large")

    def _validate_promo_code(self, promo_code: Any):
        """Validate and sanitize promo code"""
        if not isinstance(promo_code, str):
            self._add_error("promo_code", "promo_code must be a string")
            return

        # Sanitize and normalize
        sanitized_code = bleach.clean(promo_code, tags=[], strip=True).upper()

        if len(sanitized_code) > 50:
            self._add_error("promo_code", "promo_code cannot exceed 50 characters")

        # Allow only alphanumeric and common symbols
        if not re.match(r"^[A-Z0-9_-]+$", sanitized_code):
            self._add_error("promo_code", "promo_code contains invalid characters")

    def _validate_delivery_notes(self, notes: Any):
        """Validate and sanitize delivery notes"""
        if not isinstance(notes, str):
            self._add_error("delivery_notes", "delivery_notes must be a string")
            return

        # Sanitize for XSS
        sanitized_notes = bleach.clean(notes, tags=[], strip=True)

        if len(sanitized_notes) > 1000:
            self._add_error("delivery_notes", "delivery_notes cannot exceed 1000 characters")

        # Check for suspicious patterns
        if re.search(r"<script|javascript:|data:|vbscript:", sanitized_notes, re.IGNORECASE):
            self._add_error("delivery_notes", "delivery_notes contain invalid content")

    def _validate_emergency_delivery_notes(self, notes: Any):
        """Validate emergency delivery notes with stricter limits"""
        self._validate_delivery_notes(notes)

        if isinstance(notes, str):
            sanitized_notes = bleach.clean(notes, tags=[], strip=True)
            if len(sanitized_notes) > 500:  # Stricter limit for emergency orders
                self._add_error("delivery_notes", "delivery_notes for emergency orders cannot exceed 500 characters")

    def _validate_order_source(self, source: Any):
        """Validate order source"""
        if not isinstance(source, str):
            self._add_error("order_source", "order_source must be a string")
            return

        valid_sources = ["web", "mobile", "telegram", "admin", "api", "phone"]
        if source not in valid_sources:
            self._add_error("order_source", f'order_source must be one of: {", ".join(valid_sources)}')

    def _validate_urgent_flag(self, is_urgent: Any):
        """Validate urgent flag"""
        if not isinstance(is_urgent, bool):
            self._add_error("is_urgent", "is_urgent must be a boolean")

    def _validate_rating(self, rating: Any):
        """Validate feedback rating"""
        if not isinstance(rating, int):
            try:
                rating = int(rating)
            except (ValueError, TypeError):
                self._add_error("rating", "rating must be a valid integer")
                return

        if rating < 1 or rating > 5:
            self._add_error("rating", "rating must be between 1 and 5")

    def _validate_feedback_comment(self, comment: Any):
        """Validate and sanitize feedback comment"""
        if not isinstance(comment, str):
            self._add_error("comment", "comment must be a string")
            return

        # Sanitize for XSS
        sanitized_comment = bleach.clean(comment, tags=[], strip=True)

        if len(sanitized_comment) > 2000:
            self._add_error("comment", "comment cannot exceed 2000 characters")

        # Check for suspicious patterns
        if re.search(r"<script|javascript:|data:|vbscript:", sanitized_comment, re.IGNORECASE):
            self._add_error("comment", "comment contains invalid content")

    def _validate_bulk_action_type(self, action: Any):
        """Validate bulk action type"""
        if not isinstance(action, str):
            self._add_error("action", "action must be a string")
            return

        valid_actions = ["confirm", "cancel", "mark_priority", "assign_delivery"]
        if action not in valid_actions:
            self._add_error("action", f'action must be one of: {", ".join(valid_actions)}')

    def _validate_order_ids_list(self, order_ids: Any):
        """Validate order IDs list"""
        if not isinstance(order_ids, list):
            self._add_error("order_ids", "order_ids must be a list")
            return

        if len(order_ids) == 0:
            self._add_error("order_ids", "order_ids cannot be empty")
            return

        if len(order_ids) > 100:
            self._add_error("order_ids", "order_ids cannot contain more than 100 items")
            return

        for i, order_id in enumerate(order_ids):
            if not isinstance(order_id, int):
                try:
                    order_id = int(order_id)
                except (ValueError, TypeError):
                    self._add_error("order_ids", f"order_ids[{i}] must be a valid integer")
                    continue

            if order_id <= 0:
                self._add_error("order_ids", f"order_ids[{i}] must be positive")

    def _validate_subscription_frequency(self, frequency: Any):
        """Validate subscription frequency"""
        if not isinstance(frequency, str):
            self._add_error("frequency", "frequency must be a string")
            return

        valid_frequencies = ["weekly", "biweekly", "monthly", "quarterly"]
        if frequency not in valid_frequencies:
            self._add_error("frequency", f'frequency must be one of: {", ".join(valid_frequencies)}')

    def _validate_subscription_start_date(self, start_date: Any):
        """Validate subscription start date"""
        if not isinstance(start_date, str):
            self._add_error("start_date", "start_date must be a string")
            return

        try:
            parsed_date = datetime.fromisoformat(start_date).date()

            # Cannot be more than 1 day in the past
            min_date = date.today() - timedelta(days=1)
            if parsed_date < min_date:
                self._add_error("start_date", "start_date cannot be more than 1 day in the past")

            # Cannot be more than 90 days in the future
            max_date = date.today() + timedelta(days=90)
            if parsed_date > max_date:
                self._add_error("start_date", "start_date cannot be more than 90 days in the future")

        except ValueError:
            self._add_error("start_date", "start_date must be in ISO format (YYYY-MM-DD)")

    def _validate_auto_pay_flag(self, auto_pay: Any):
        """Validate auto pay flag"""
        if not isinstance(auto_pay, bool):
            self._add_error("auto_pay", "auto_pay must be a boolean")

    def _validate_scheduled_date(self, scheduled_date: Any):
        """Validate scheduled date"""
        if not isinstance(scheduled_date, str):
            self._add_error("scheduled_date", "scheduled_date must be a string")
            return

        try:
            parsed_datetime = datetime.fromisoformat(scheduled_date)

            # Must be in the future
            if parsed_datetime <= datetime.now(UTC):
                self._add_error("scheduled_date", "scheduled_date must be in the future")

            # Cannot be more than 7 days in the future for scheduled orders
            max_datetime = datetime.now(UTC) + timedelta(days=7)
            if parsed_datetime > max_datetime:
                self._add_error("scheduled_date", "scheduled_date cannot be more than 7 days in the future")

        except ValueError:
            self._add_error("scheduled_date", "scheduled_date must be in ISO format")

    def _validate_export_format(self, format_type: Any):
        """Validate export format"""
        if not isinstance(format_type, str):
            self._add_error("format", "format must be a string")
            return

        valid_formats = ["csv", "excel"]
        if format_type not in valid_formats:
            self._add_error("format", f'format must be one of: {", ".join(valid_formats)}')

    def _validate_export_date(self, date_value: Any, field_name: str):
        """Validate export date"""
        if not isinstance(date_value, str):
            self._add_error(field_name, f"{field_name} must be a string")
            return

        try:
            datetime.fromisoformat(date_value)
        except ValueError:
            self._add_error(field_name, f"{field_name} must be in ISO format")

    def _validate_export_filters(self, filters: Any):
        """Validate export filters"""
        if not isinstance(filters, dict):
            self._add_error("filters", "filters must be an object")
            return

        # Validate filter values
        for key, value in filters.items():
            if not isinstance(key, str):
                self._add_error("filters", f"filter key must be a string: {key}")

            # Sanitize filter values to prevent injection
            if isinstance(value, str):
                sanitized_value = bleach.clean(value, tags=[], strip=True)
                if len(sanitized_value) > 100:
                    self._add_error("filters", f"filter value too long for key {key}")

    def _add_error(self, field: str, message: str):
        """Add validation error"""
        if field not in self.errors:
            self.errors[field] = []
        self.errors[field].append(message)


def validate_order_query_params(args: Dict[str, str]) -> Dict[str, List[str]]:
    """Validate order listing query parameters"""
    errors = {}

    # Validate page
    if "page" in args:
        try:
            page = int(args["page"])
            if page < 1:
                errors["page"] = ["Page must be positive"]
            if page > 10000:  # Reasonable upper limit
                errors["page"] = ["Page number too large"]
        except ValueError:
            errors["page"] = ["Page must be a valid integer"]

    # Validate per_page
    if "per_page" in args:
        try:
            per_page = int(args["per_page"])
            if per_page < 1:
                errors["per_page"] = ["Per page must be positive"]
            if per_page > 100:  # Limit to prevent abuse
                errors["per_page"] = ["Per page cannot exceed 100"]
        except ValueError:
            errors["per_page"] = ["Per page must be a valid integer"]

    # Validate status
    if "status" in args:
        try:
            OrderStatus(args["status"])
        except ValueError:
            valid_statuses = [s.value for s in OrderStatus]
            errors["status"] = [f'Status must be one of: {", ".join(valid_statuses)}']

    # Validate dates
    if "start_date" in args:
        try:
            datetime.fromisoformat(args["start_date"])
        except ValueError:
            errors["start_date"] = ["Start date must be in ISO format"]

    if "end_date" in args:
        try:
            datetime.fromisoformat(args["end_date"])
        except ValueError:
            errors["end_date"] = ["End date must be in ISO format"]

    # Validate date range
    if "start_date" in args and "end_date" in args and not errors.get("start_date") and not errors.get("end_date"):
        try:
            start_dt = datetime.fromisoformat(args["start_date"])
            end_dt = datetime.fromisoformat(args["end_date"])
            if start_dt > end_dt:
                errors["date_range"] = ["Start date cannot be after end date"]
        except ValueError:
            pass  # Already handled above

    return errors


def sanitize_search_query(query: str) -> str:
    """Sanitize search query to prevent injection attacks"""
    if not isinstance(query, str):
        return ""

    # Remove HTML tags and scripts
    sanitized = bleach.clean(query, tags=[], strip=True)

    # Remove SQL injection patterns
    sql_patterns = [
        r"[;']",
        r"--",
        r"/\*",
        r"\*/",
        r"union\s+select",
        r"drop\s+table",
        r"delete\s+from",
        r"insert\s+into",
        r"update\s+set",
        r"exec\s*\(",
    ]

    for pattern in sql_patterns:
        sanitized = re.sub(pattern, "", sanitized, flags=re.IGNORECASE)

    # Limit length
    return sanitized[:200]


def validate_export_params(data: Dict[str, Any]) -> Dict[str, List[str]]:
    """Validate export parameters"""
    errors = {}

    # Validate format
    if "format" not in data:
        errors["format"] = ["Format is required"]
    elif data["format"] not in ["csv", "excel"]:
        errors["format"] = ["Format must be csv or excel"]

    # Validate date range
    if "start_date" in data:
        try:
            datetime.fromisoformat(data["start_date"])
        except ValueError:
            errors["start_date"] = ["Start date must be in ISO format"]

    if "end_date" in data:
        try:
            datetime.fromisoformat(data["end_date"])
        except ValueError:
            errors["end_date"] = ["End date must be in ISO format"]

    # Validate filters
    if "filters" in data and not isinstance(data["filters"], dict):
        errors["filters"] = ["Filters must be an object"]

    return errors
