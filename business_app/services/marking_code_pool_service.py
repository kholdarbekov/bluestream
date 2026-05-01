"""Proactive marking-code pre-utilisation pool service.

Sizes a per-product target from recent card/click sales, then registers (utilises)
``ProductMarkingCode`` rows with the Tax Committee ahead of demand so customer
order flows do not have to wait the synchronous ~45s utilisation step.

Triggers
~~~~~~~~
- Daily Celery beat task at 00:00 UTC (`pre_register_marking_codes_daily`).
- Intra-day low-water trigger when remaining pre-utilised drops below
  MARKING_CODE_LOW_WATER_RATIO of target.
- Reactive trigger when a customer order finds an empty pre-utilised pool.

The customer reservation path stays untouched aside from a sort-key change that
prefers already-pre-utilised codes; if the pool is empty for a product the
existing synchronous slow-path still serves the order, so customers are never
blocked by this feature.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from flask import current_app
from sqlalchemy import case, func

from business_app import db, redis_client
from business_app.models.order import Order, OrderItem
from business_app.models.payment import Payment
from business_app.models.product import Product, ProductMarkingCode
from shared.enums import MarkingCodeStatus, PaymentMethod, PaymentStatus

_FISCALISATION_PAYMENT_METHODS = (PaymentMethod.CARD.value, PaymentMethod.CLICK.value)


class MarkingCodePoolService:
    """Owns proactive Tax-Committee pre-utilisation of the marking-code pool."""

    def __init__(self, tax_committee_service=None):
        self._tax_committee_service = tax_committee_service

    @property
    def tax_committee_service(self):
        if self._tax_committee_service is None:
            from business_app.services.tax_committee_service import TaxCommitteeService

            self._tax_committee_service = TaxCommitteeService()
        return self._tax_committee_service

    # ------------------------------------------------------------------
    # Sizing
    # ------------------------------------------------------------------

    def compute_daily_target(self, product: Product) -> int:
        """Target pre-utilised count for ``product`` based on recent demand.

        Formula:
            avg_daily = sum(qty over completed card/click orders in last
                        TREND_WINDOW_DAYS) / TREND_WINDOW_DAYS
            target    = ceil(avg_daily * RUNWAY_DAYS * SAFETY_MULTIPLIER)

        Clamped to [MARKING_CODE_TARGET_MIN, MARKING_CODE_TARGET_MAX] so both
        cold-start (no sales yet) and runaway (one viral product) cases stay
        bounded.
        """
        cfg = current_app.config
        window_days = int(cfg.get("MARKING_CODE_TREND_WINDOW_DAYS", 7) or 7)
        runway_days = int(cfg.get("MARKING_CODE_RUNWAY_DAYS", 1) or 1)
        safety = float(cfg.get("MARKING_CODE_SAFETY_MULTIPLIER", 1.5) or 1.5)
        floor_n = int(cfg.get("MARKING_CODE_TARGET_MIN", 5) or 5)
        cap_n = int(cfg.get("MARKING_CODE_TARGET_MAX", 500) or 500)

        cutoff = datetime.now(timezone.utc) - _timedelta_days(window_days)

        total_qty = (
            db.session.query(func.coalesce(func.sum(OrderItem.quantity), 0))
            .select_from(OrderItem)
            .join(Order, Order.id == OrderItem.order_id)
            .join(Payment, Payment.order_id == Order.id)
            .filter(
                OrderItem.product_id == product.id,
                Payment.status == PaymentStatus.COMPLETED,
                Payment.payment_method.in_(_FISCALISATION_PAYMENT_METHODS),
                Payment.paid_at >= cutoff,
            )
            .scalar()
        ) or 0

        avg_daily = float(total_qty) / float(window_days)
        target = math.ceil(avg_daily * runway_days * safety)
        return max(floor_n, min(cap_n, int(target)))

    # ------------------------------------------------------------------
    # Pool inspection
    # ------------------------------------------------------------------

    def get_pool_metrics(self, product: Product) -> Dict[str, int]:
        """Counts of available + reserved codes for ``product``.

        Returns: {pre_utilised, un_utilised, reserved, target, deficit}.
        """
        target = self.compute_daily_target(product)

        row = (
            db.session.query(
                func.count(
                    case(
                        (
                            (ProductMarkingCode.status == MarkingCodeStatus.AVAILABLE)
                            & (ProductMarkingCode.tax_committee_utilised_at.isnot(None)),
                            1,
                        )
                    )
                ).label("pre_utilised"),
                func.count(
                    case(
                        (
                            (ProductMarkingCode.status == MarkingCodeStatus.AVAILABLE)
                            & (ProductMarkingCode.tax_committee_utilised_at.is_(None)),
                            1,
                        )
                    )
                ).label("un_utilised"),
                func.count(case((ProductMarkingCode.status == MarkingCodeStatus.RESERVED, 1))).label("reserved"),
            )
            .filter(ProductMarkingCode.product_id == product.id)
            .one()
        )

        pre_utilised = int(row.pre_utilised or 0)
        un_utilised = int(row.un_utilised or 0)
        reserved = int(row.reserved or 0)
        deficit = max(0, target - pre_utilised)

        return {
            "pre_utilised": pre_utilised,
            "un_utilised": un_utilised,
            "reserved": reserved,
            "target": target,
            "deficit": deficit,
        }

    def is_below_low_water(self, product_id: int) -> bool:
        """Cheap check used after a customer reservation — true when the pool
        for ``product_id`` has fallen under the configured low-water ratio."""
        cfg = current_app.config
        ratio = float(cfg.get("MARKING_CODE_LOW_WATER_RATIO", 0.25) or 0.25)
        product = Product.query.get(product_id)
        if not product:
            return False
        metrics = self.get_pool_metrics(product)
        threshold = max(1, int(metrics["target"] * ratio))
        return metrics["pre_utilised"] < threshold

    # ------------------------------------------------------------------
    # Replenishment
    # ------------------------------------------------------------------

    def trigger_replenish_async(self, product_id: int, run_kind: str = "on_empty") -> bool:
        """Enqueue a per-product replenish task, deduped via Redis.

        Multiple back-to-back triggers (e.g. a flurry of card orders that drain
        the pool) collapse to a single task within the dedup TTL window.

        Returns True if a task was enqueued, False if suppressed by the dedup
        guard.
        """
        dedup_ttl = int(current_app.config.get("MARKING_CODE_REPLENISH_DEDUP_TTL", 300) or 300)
        dedup_key = f"marking_code:replenish_dedup:{int(product_id)}"

        try:
            acquired = redis_client.set(dedup_key, run_kind, nx=True, ex=dedup_ttl)
        except Exception:
            current_app.logger.warning(
                "marking_code_pool: redis dedup unavailable, enqueuing replenish anyway",
                extra={"product_id": product_id, "run_kind": run_kind},
                exc_info=True,
            )
            acquired = True

        if not acquired:
            current_app.logger.debug(
                "marking_code_pool: replenish dedup hit",
                extra={"product_id": product_id, "run_kind": run_kind},
            )
            return False

        # Late import to avoid Celery <-> Flask circular-import on app boot.
        from business_app.tasks.marking_code_tasks import replenish_marking_codes_for_product

        replenish_marking_codes_for_product.delay(int(product_id), str(run_kind))
        current_app.logger.info(
            "marking_code_pool: enqueued replenish task",
            extra={"product_id": product_id, "run_kind": run_kind},
        )
        return True

    def pre_utilise_for_product(
        self,
        product: Product,
        *,
        target: Optional[int] = None,
        run_kind: str = "daily",
    ) -> Dict[str, Any]:
        """Pre-utilise enough AVAILABLE+un-utilised codes to meet ``target``.

        Idempotent under the per-product Redis lock — overlapping daily and
        low-water triggers will not double-call the Tax Committee.

        Returns a summary dict suitable for logging:
            {requested, utilised, skipped_invalid, errors, run_kind, target,
             pre_utilised_before, pre_utilised_after}
        """
        if not product or not product.requires_marking_codes:
            return _empty_summary(run_kind, target=target or 0, reason="not_fiscalisable")

        if not current_app.config.get("TAX_COMMITTEE_UTILISATION_ENABLED", True):
            return _empty_summary(run_kind, target=target or 0, reason="utilisation_disabled")

        lock_key = f"marking_code:replenish_lock:{int(product.id)}"
        lock_ttl = int(current_app.config.get("MARKING_CODE_REPLENISH_LOCK_TTL", 1800) or 1800)
        lock_token = f"{run_kind}:{datetime.now(timezone.utc).timestamp()}"
        try:
            lock_acquired = redis_client.set(lock_key, lock_token, nx=True, ex=lock_ttl)
        except Exception:
            current_app.logger.warning(
                "marking_code_pool: redis lock unavailable, proceeding without lock",
                extra={"product_id": product.id, "run_kind": run_kind},
                exc_info=True,
            )
            lock_acquired = True
            lock_token = None

        if not lock_acquired:
            current_app.logger.info(
                "marking_code_pool: replenish lock held by another worker, skipping",
                extra={"product_id": product.id, "run_kind": run_kind},
            )
            return _empty_summary(run_kind, target=target or 0, reason="lock_held")

        try:
            return self._pre_utilise_for_product_locked(product, target=target, run_kind=run_kind)
        finally:
            if lock_token is not None:
                # Best-effort release; the TTL is the real safety net.
                try:
                    current_value = redis_client.get(lock_key)
                    if current_value is not None and current_value.decode() == lock_token:
                        redis_client.delete(lock_key)
                except Exception:
                    current_app.logger.debug(
                        "marking_code_pool: failed to release replenish lock",
                        extra={"product_id": product.id},
                        exc_info=True,
                    )

    # ------------------------------------------------------------------
    # Internal: utilisation under the per-product lock
    # ------------------------------------------------------------------

    def _pre_utilise_for_product_locked(
        self,
        product: Product,
        *,
        target: Optional[int],
        run_kind: str,
    ) -> Dict[str, Any]:
        metrics = self.get_pool_metrics(product)
        if target is None:
            target = metrics["target"]
        deficit = max(0, int(target) - metrics["pre_utilised"])

        summary: Dict[str, Any] = {
            "product_id": product.id,
            "run_kind": run_kind,
            "target": int(target),
            "pre_utilised_before": metrics["pre_utilised"],
            "un_utilised": metrics["un_utilised"],
            "deficit": deficit,
            "requested": 0,
            "utilised": 0,
            "skipped_invalid": 0,
            "errors": 0,
            "report_ids": [],
            "pre_utilised_after": metrics["pre_utilised"],
        }

        if deficit <= 0:
            current_app.logger.info(
                "marking_code_pool: no deficit, skipping",
                extra=summary,
            )
            return summary

        candidates = (
            db.session.query(ProductMarkingCode)
            .filter(
                ProductMarkingCode.product_id == product.id,
                ProductMarkingCode.status == MarkingCodeStatus.AVAILABLE,
                ProductMarkingCode.tax_committee_utilised_at.is_(None),
            )
            .order_by(
                ProductMarkingCode.created_at.asc(),
                ProductMarkingCode.id.asc(),
            )
            .with_for_update(skip_locked=True)
            .limit(deficit)
            .all()
        )

        if not candidates:
            current_app.logger.warning(
                "marking_code_pool: no un-utilised codes available — pool import is empty",
                extra=summary,
            )
            db.session.commit()
            return summary

        # Drop codes already invalid at the Tax Committee before we waste a
        # utilisation call on them.
        candidates, dropped_invalid = self._drop_invalid_codes(candidates)
        summary["skipped_invalid"] = dropped_invalid

        if not candidates:
            db.session.commit()
            return summary

        batch_size = int(current_app.config.get("MARKING_CODE_UTILISATION_BATCH_SIZE", 200) or 200)
        batch_size = max(1, batch_size)

        for batch in _chunked(candidates, batch_size):
            full_codes = [c.code for c in batch]
            try:
                result = self.tax_committee_service.utilise_marking_codes(full_codes, product)
            except Exception as exc:
                summary["errors"] += 1
                # Roll back any pending mutations in this batch attempt; earlier
                # successful batches stay committed.
                db.session.rollback()
                current_app.logger.exception(
                    "marking_code_pool: utilisation batch failed",
                    extra={
                        "product_id": product.id,
                        "run_kind": run_kind,
                        "batch_size": len(full_codes),
                        "error": str(exc),
                    },
                )
                # Keep going — next batch may still succeed.
                continue

            now = datetime.now(timezone.utc)
            for code in batch:
                code.tax_committee_utilised_at = now
            report_id = result.get("reportId") if isinstance(result, dict) else None
            if report_id is not None:
                summary["report_ids"].append(report_id)
            summary["requested"] += len(full_codes)
            summary["utilised"] += len(full_codes)

            try:
                db.session.commit()
            except Exception:
                summary["errors"] += 1
                db.session.rollback()
                # Subtract the optimistic counter — TC accepted but we failed
                # to record it locally. Next run will hit "already utilised"
                # for these codes via the precheck.
                summary["utilised"] -= len(full_codes)
                current_app.logger.exception(
                    "marking_code_pool: failed to commit utilisation batch",
                    extra={"product_id": product.id, "run_kind": run_kind},
                )

        # Re-read pool metrics so observability shows the resulting state.
        try:
            after = self.get_pool_metrics(product)
            summary["pre_utilised_after"] = after["pre_utilised"]
        except Exception:
            current_app.logger.debug(
                "marking_code_pool: failed to re-read metrics after run",
                extra={"product_id": product.id},
                exc_info=True,
            )

        current_app.logger.info(
            "marking_code_pool: replenish completed",
            extra=summary,
        )
        return summary

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _drop_invalid_codes(
        self,
        candidates: List[ProductMarkingCode],
    ) -> tuple[List[ProductMarkingCode], int]:
        """Remove codes whose Tax Committee status is WITHDRAWN/WRITTEN_OFF or
        already APPLIED/INTRODUCED.

        For ALREADY_UTILISED statuses we still stamp ``tax_committee_utilised_at``
        on the local row (the Tax Committee considers them utilised, so locally
        treating them as pre-utilised keeps the pool accurate).
        """
        if not candidates:
            return candidates, 0

        from business_app.services.tax_committee_service import TaxCommitteeService

        identification_codes = [_identification_part(c.code) for c in candidates]
        try:
            status_map = self.tax_committee_service.check_marking_code_statuses(identification_codes)
        except Exception:
            current_app.logger.warning(
                "marking_code_pool: status pre-check failed; proceeding without it",
                exc_info=True,
            )
            return candidates, 0

        kept: List[ProductMarkingCode] = []
        skipped = 0
        now = datetime.now(timezone.utc)
        for code in candidates:
            id_part = _identification_part(code.code)
            tc_status = status_map.get(id_part, TaxCommitteeService.STATUS_RECEIVED)
            if tc_status in TaxCommitteeService.INVALID_STATUSES:
                code.status = MarkingCodeStatus.ARCHIVED
                code.archived_at = now
                code.notes = (code.notes or "") + f"\nAuto-archived (TC status={tc_status})"
                skipped += 1
                continue
            if tc_status in TaxCommitteeService.ALREADY_UTILISED_STATUSES:
                code.tax_committee_utilised_at = now
                # Don't include in the utilise batch — it's already applied.
                continue
            kept.append(code)
        if skipped or len(kept) != len(candidates):
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
        return kept, skipped


# ----------------------------------------------------------------------
# Module-level helpers (kept private; no behaviour outside this service).
# ----------------------------------------------------------------------


def _identification_part(full_code: str) -> str:
    gs_char = "\x1d"  # ASCII 29 (Group Separator)
    idx = full_code.find(gs_char)
    return full_code if idx == -1 else full_code[:idx]


def _chunked(items: Iterable, size: int):
    bucket: List = []
    for item in items:
        bucket.append(item)
        if len(bucket) >= size:
            yield bucket
            bucket = []
    if bucket:
        yield bucket


def _timedelta_days(days: int):
    from datetime import timedelta

    return timedelta(days=int(days))


def _empty_summary(run_kind: str, *, target: int, reason: str) -> Dict[str, Any]:
    return {
        "run_kind": run_kind,
        "target": int(target),
        "requested": 0,
        "utilised": 0,
        "skipped_invalid": 0,
        "errors": 0,
        "report_ids": [],
        "skipped_reason": reason,
    }
