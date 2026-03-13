import csv
import io
from collections import Counter
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional

from flask import current_app
from sqlalchemy import func

from business_app import db
from business_app.models.product import Product, ProductFiscalProfile, ProductMarkingCode
from business_app.models.audit import AuditEventType, AuditSeverity
from business_app.utils.audit_logger import audit_logger
from business_app.utils.constants import MarkingCodeStatus
from business_app.utils.exceptions import NotFoundError, ValidationError


class ProductFiscalService:
    """Manage product fiscal metadata and marking-code inventory."""

    REQUIRED_FISCAL_FIELDS = ("barcode", "spic", "units")
    LOW_STOCK_THRESHOLD = 10

    def get_product_or_raise(self, product_id: int) -> Product:
        product = Product.query.get(product_id)
        if not product:
            raise NotFoundError("Product not found")
        return product

    def get_or_create_fiscal_profile(self, product: Product) -> ProductFiscalProfile:
        profile = product.fiscal_profile
        if profile:
            return profile

        profile = ProductFiscalProfile(product=product)
        db.session.add(profile)
        db.session.flush()
        return profile

    def update_product_fiscal_profile(
        self,
        product: Product,
        payload: Dict[str, Any],
        *,
        actor_user_id: Optional[int] = None,
    ) -> Product:
        profile = self.get_or_create_fiscal_profile(product)

        if "barcode" in payload:
            product.barcode = self._clean_optional_string(payload.get("barcode"))
        if "spic" in payload:
            profile.spic = self._clean_optional_string(payload.get("spic"))
        if "package_code" in payload:
            profile.package_code = self._clean_optional_string(payload.get("package_code"))
        if "units" in payload:
            profile.units = self._clean_optional_string(payload.get("units"))
        if "vat_percent" in payload and payload.get("vat_percent") not in (None, ""):
            profile.vat_percent = Decimal(str(payload.get("vat_percent")))
        if "requires_marking_codes" in payload:
            profile.requires_marking_codes = bool(payload.get("requires_marking_codes"))
        if "fiscalization_enabled" in payload:
            profile.fiscalization_enabled = bool(payload.get("fiscalization_enabled"))
        if "fiscal_extra_data" in payload and isinstance(payload.get("fiscal_extra_data"), dict):
            profile.extra_data = dict(payload.get("fiscal_extra_data") or {})

        if profile.fiscalization_enabled:
            missing_fields = self.get_missing_required_fiscal_fields(product)
            if missing_fields:
                raise ValidationError(
                    f"Fiscalization cannot be enabled until these fields are set: {', '.join(missing_fields)}"
                )

        if profile.requires_marking_codes and not profile.fiscalization_enabled:
            raise ValidationError("Marked products must have fiscalization enabled")

        audit_logger.log_event(
            event_type=AuditEventType.PRODUCT_UPDATED,
            action="product_fiscal_profile_updated",
            severity=AuditSeverity.MEDIUM,
            resource_type="product",
            resource_id=str(product.id),
            description=f"Updated fiscal profile for product {product.id}",
            additional_data={
                "product_id": product.id,
                "actor_user_id": actor_user_id,
                "fiscalization_enabled": bool(profile.fiscalization_enabled),
                "requires_marking_codes": bool(profile.requires_marking_codes),
            },
        )
        return product

    def get_missing_required_fiscal_fields(self, product: Product) -> List[str]:
        missing: List[str] = []
        if not product.barcode:
            missing.append("barcode")
        if not product.spic:
            missing.append("spic")
        if not product.units:
            missing.append("units")
        return missing

    def get_marking_code_counts(self, product_id: int) -> Dict[str, int]:
        rows = (
            db.session.query(
                ProductMarkingCode.status,
                func.count(ProductMarkingCode.id),
            )
            .filter(ProductMarkingCode.product_id == product_id)
            .group_by(ProductMarkingCode.status)
            .all()
        )
        counts = {
            MarkingCodeStatus.AVAILABLE.value: 0,
            MarkingCodeStatus.RESERVED.value: 0,
            MarkingCodeStatus.USED.value: 0,
            MarkingCodeStatus.ARCHIVED.value: 0,
        }
        for status, count in rows:
            status_value = status.value if hasattr(status, "value") else str(status)
            counts[status_value] = int(count)
        return counts

    def build_product_fiscal_snapshot(self, product: Product) -> Dict[str, Any]:
        counts = self.get_marking_code_counts(product.id)
        low_stock_threshold = max(int(product.min_stock_level or 0), self.LOW_STOCK_THRESHOLD)
        available_count = counts.get(MarkingCodeStatus.AVAILABLE.value, 0)
        missing_fields = self.get_missing_required_fiscal_fields(product) if product.fiscal_profile else []
        return {
            "barcode": product.barcode,
            "spic": product.spic,
            "package_code": product.package_code,
            "units": product.units,
            "vat_percent": float(product.vat_percent or 0),
            "fiscalization_enabled": bool(product.fiscalization_enabled),
            "requires_marking_codes": bool(product.requires_marking_codes),
            "missing_required_fields": missing_fields,
            "marking_code_counts": counts,
            "marking_codes_low_stock": bool(product.requires_marking_codes and available_count < low_stock_threshold),
            "marking_codes_low_stock_threshold": low_stock_threshold,
        }

    def list_marking_codes(
        self,
        product_id: int,
        *,
        page: int = 1,
        per_page: int = 50,
        search: str = "",
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        self.get_product_or_raise(product_id)
        query = ProductMarkingCode.query.filter_by(product_id=product_id)
        if search:
            query = query.filter(ProductMarkingCode.code.ilike(f"%{search.strip()}%"))
        if status:
            try:
                query = query.filter(ProductMarkingCode.status == MarkingCodeStatus(status))
            except ValueError as exc:
                raise ValidationError("Invalid marking code status") from exc

        pagination = query.order_by(ProductMarkingCode.created_at.desc(), ProductMarkingCode.id.desc()).paginate(
            page=page,
            per_page=min(per_page, 200),
            error_out=False,
        )
        return {
            "items": [code.to_dict() for code in pagination.items],
            "total": pagination.total,
            "page": page,
            "per_page": min(per_page, 200),
            "pages": pagination.pages,
            "summary": self.get_marking_code_counts(product_id),
        }

    def create_marking_codes(
        self,
        product_id: int,
        codes: Iterable[str],
        *,
        actor_user_id: Optional[int] = None,
        notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        product = self.get_product_or_raise(product_id)
        normalized_codes = self._normalize_codes(codes)
        if not normalized_codes:
            raise ValidationError("At least one marking code is required")

        duplicates = [code for code, count in Counter(normalized_codes).items() if count > 1]
        if duplicates:
            raise ValidationError(f"Duplicate marking codes in request: {', '.join(duplicates[:10])}")

        existing_codes = {
            row[0]
            for row in db.session.query(ProductMarkingCode.code)
            .filter(ProductMarkingCode.code.in_(normalized_codes))
            .all()
        }
        if existing_codes:
            raise ValidationError(
                f"Marking codes already exist: {', '.join(sorted(existing_codes)[:10])}"
            )

        created_items: List[ProductMarkingCode] = []
        for code in normalized_codes:
            item = ProductMarkingCode(
                product_id=product.id,
                code=code,
                status=MarkingCodeStatus.AVAILABLE,
                created_by_user_id=actor_user_id,
                notes=notes,
            )
            db.session.add(item)
            created_items.append(item)

        audit_logger.log_event(
            event_type=AuditEventType.PRODUCT_UPDATED,
            action="product_marking_codes_created",
            severity=AuditSeverity.MEDIUM,
            resource_type="product",
            resource_id=str(product.id),
            description=f"Created {len(created_items)} marking codes for product {product.id}",
            additional_data={
                "product_id": product.id,
                "actor_user_id": actor_user_id,
                "codes_created": len(created_items),
            },
        )
        return {
            "created": len(created_items),
            "codes": [item.to_dict() for item in created_items],
            "summary": self.get_marking_code_counts(product.id),
        }

    def update_marking_code(
        self,
        product_id: int,
        marking_code_id: int,
        payload: Dict[str, Any],
        *,
        actor_user_id: Optional[int] = None,
    ) -> ProductMarkingCode:
        marking_code = ProductMarkingCode.query.filter_by(
            id=marking_code_id,
            product_id=product_id,
        ).first()
        if not marking_code:
            raise NotFoundError("Marking code not found")

        if "code" in payload:
            next_code = self._clean_optional_string(payload.get("code"))
            if not next_code:
                raise ValidationError("Marking code cannot be empty")
            existing = ProductMarkingCode.query.filter(
                ProductMarkingCode.code == next_code,
                ProductMarkingCode.id != marking_code.id,
            ).first()
            if existing:
                raise ValidationError("Marking code already exists")
            marking_code.code = next_code

        if "notes" in payload:
            marking_code.notes = payload.get("notes")

        if "status" in payload:
            next_status = MarkingCodeStatus(payload.get("status"))
            current_status = marking_code.status
            if current_status == MarkingCodeStatus.USED and next_status != MarkingCodeStatus.USED:
                raise ValidationError("Used marking codes cannot be restored")
            if current_status == MarkingCodeStatus.RESERVED and next_status == MarkingCodeStatus.ARCHIVED:
                raise ValidationError("Reserved marking codes cannot be archived")

            marking_code.status = next_status
            if next_status == MarkingCodeStatus.ARCHIVED:
                marking_code.archived_at = db.func.now()
            elif next_status == MarkingCodeStatus.AVAILABLE:
                marking_code.archived_at = None
                marking_code.reserved_at = None
            elif next_status == MarkingCodeStatus.RESERVED:
                marking_code.reserved_at = db.func.now()

        audit_logger.log_event(
            event_type=AuditEventType.PRODUCT_UPDATED,
            action="product_marking_code_updated",
            severity=AuditSeverity.MEDIUM,
            resource_type="product_marking_code",
            resource_id=str(marking_code.id),
            description=f"Updated marking code {marking_code.id}",
            additional_data={
                "product_id": product_id,
                "actor_user_id": actor_user_id,
                "marking_code_id": marking_code.id,
                "status": marking_code.status.value if hasattr(marking_code.status, 'value') else marking_code.status,
            },
        )
        return marking_code

    def archive_marking_code(
        self,
        product_id: int,
        marking_code_id: int,
        *,
        actor_user_id: Optional[int] = None,
    ) -> ProductMarkingCode:
        return self.update_marking_code(
            product_id,
            marking_code_id,
            {"status": MarkingCodeStatus.ARCHIVED.value},
            actor_user_id=actor_user_id,
        )

    def import_marking_codes_csv(
        self,
        product_id: int,
        csv_content: str,
        *,
        actor_user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        rows = self._parse_csv_codes(csv_content)
        valid_entries: List[Dict[str, Any]] = []
        invalid_rows: List[Dict[str, Any]] = []
        seen = set()

        for row in rows:
            code = self._clean_optional_string(row.get("code"))
            if not code:
                invalid_rows.append({"row": row.get("row"), "reason": "Missing code"})
                continue
            if code in seen:
                invalid_rows.append(
                    {
                        "row": row.get("row"),
                        "reason": "Duplicate code in CSV",
                        "code": code,
                    }
                )
                continue
            seen.add(code)
            valid_entries.append({"row": row.get("row"), "code": code})

        existing_codes = set()
        if valid_entries:
            existing_codes = {
                row[0]
                for row in db.session.query(ProductMarkingCode.code)
                .filter(ProductMarkingCode.code.in_([entry["code"] for entry in valid_entries]))
                .all()
            }

        valid_codes = []
        for entry in valid_entries:
            if entry["code"] in existing_codes:
                invalid_rows.append(
                    {
                        "row": entry["row"],
                        "reason": "Marking code already exists",
                        "code": entry["code"],
                    }
                )
                continue
            valid_codes.append(entry["code"])

        created_payload = self.create_marking_codes(
            product_id,
            valid_codes,
            actor_user_id=actor_user_id,
            notes="Imported from CSV",
        ) if valid_codes else {"created": 0, "codes": [], "summary": self.get_marking_code_counts(product_id)}

        return {
            "created": created_payload["created"],
            "invalid_rows": invalid_rows,
            "summary": created_payload["summary"],
        }

    def export_marking_codes_csv(self, product_id: int, *, status: Optional[str] = None) -> str:
        payload = self.list_marking_codes(product_id, page=1, per_page=100000, status=status)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["code", "status", "created_at", "reserved_at", "used_at", "archived_at", "notes"])
        for row in payload["items"]:
            writer.writerow(
                [
                    row.get("code"),
                    row.get("status"),
                    row.get("created_at"),
                    row.get("reserved_at"),
                    row.get("used_at"),
                    row.get("archived_at"),
                    row.get("notes"),
                ]
            )
        return output.getvalue()

    @staticmethod
    def _clean_optional_string(value: Any) -> Optional[str]:
        if value is None:
            return None
        value = str(value).strip()
        return value or None

    def _normalize_codes(self, codes: Iterable[str]) -> List[str]:
        return [code for code in (self._clean_optional_string(item) for item in codes) if code]

    def _parse_csv_codes(self, csv_content: str) -> List[Dict[str, Any]]:
        stream = io.StringIO(csv_content or "")
        sample = stream.read(1024)
        stream.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample or "code\n")
        except csv.Error:
            dialect = csv.excel

        reader = csv.reader(stream, dialect)
        rows = list(reader)
        if not rows:
            return []

        header_candidates = [cell.strip().lower() for cell in rows[0]]
        has_header = any(candidate in {"code", "marking_code", "label", "labels"} for candidate in header_candidates)
        parsed: List[Dict[str, Any]] = []

        if has_header:
            dict_reader = csv.DictReader(io.StringIO(csv_content), dialect=dialect)
            for index, row in enumerate(dict_reader, start=2):
                code = row.get("code") or row.get("marking_code") or row.get("label") or row.get("labels")
                parsed.append({"row": index, "code": code})
            return parsed

        for index, row in enumerate(rows, start=1):
            code = row[0] if row else None
            parsed.append({"row": index, "code": code})
        return parsed
