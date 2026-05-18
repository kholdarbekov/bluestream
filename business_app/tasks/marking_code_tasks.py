"""Proactive Asl Belgisi marking-code pre-utilisation Celery tasks.

Two tasks:

* ``pre_register_marking_codes_daily`` — beat-scheduled per the
  ``MarkingCodeTaskConfig`` row. Fans out a per-product replenish task for
  every fiscalisable product so customer card orders during the day skip the
  synchronous Tax Committee call.
* ``replenish_marking_codes_for_product`` — does the actual TC `/utilisation`
  call for one product. Re-used by the daily fan-out, the low-water trigger
  fired after a customer reservation, and the on-empty trigger fired when the
  pool was drained mid-day.

Each invocation writes one row to ``marking_code_task_runs`` so the Admin UI
can surface execution history, success rate, and per-product details.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from celery import shared_task
from celery.utils.log import get_task_logger

from business_app import db
from business_app.models.marking_code_task_run import (
    MarkingCodeRunStatus,
    MarkingCodeTaskRun,
)
from business_app.models.product import Product, ProductFiscalProfile
from business_app.services.marking_code_pool_service import MarkingCodePoolService

logger = get_task_logger(__name__)


def _utcnow():
    return datetime.now(timezone.utc)


def _new_run(
    *,
    task_name: str,
    run_kind: str,
    parent_run_id: Optional[int] = None,
    product_id: Optional[int] = None,
    triggered_by_user_id: Optional[int] = None,
) -> MarkingCodeTaskRun:
    """Create and commit a RUNNING ledger row, returning the persisted instance."""
    row = MarkingCodeTaskRun(
        task_name=task_name,
        run_kind=run_kind,
        parent_run_id=parent_run_id,
        product_id=product_id,
        status=MarkingCodeRunStatus.RUNNING,
        started_at=_utcnow(),
        triggered_by_user_id=triggered_by_user_id,
    )
    db.session.add(row)
    db.session.commit()
    return row


def _finalize_run(
    run_id: int,
    *,
    status: MarkingCodeRunStatus,
    summary: Optional[Dict[str, Any]] = None,
    error_message: Optional[str] = None,
) -> None:
    """Look up the ledger row and stamp it with terminal state.

    Uses a fresh query so it works even if the task got requeued / lost the
    session that created the row.
    """
    row = MarkingCodeTaskRun.query.get(run_id)
    if row is None:
        logger.warning("marking_code_task_run %s not found at finalize", run_id)
        return

    now = _utcnow()
    row.status = status
    row.finished_at = now
    if row.started_at:
        delta = now - row.started_at
        row.duration_ms = int(delta.total_seconds() * 1000)
    if error_message:
        row.error_message = error_message[:5000]
    if summary:
        row.result_summary = summary
        row.requested = int(summary.get("requested", 0) or 0)
        row.utilised = int(summary.get("utilised", 0) or 0)
        row.skipped_invalid = int(summary.get("skipped_invalid", 0) or 0)
        row.errors = int(summary.get("errors", 0) or 0)
        row.pre_utilised_before = summary.get("pre_utilised_before")
        row.pre_utilised_after = summary.get("pre_utilised_after")
        row.target_value = summary.get("target")
    db.session.commit()


@shared_task(bind=True, max_retries=3, time_limit=3600, soft_time_limit=3300)
def pre_register_marking_codes_daily(
    self,
    triggered_by_user_id: Optional[int] = None,
    run_kind: str = "daily",
) -> Dict[str, Any]:
    """Beat-scheduled fan-out — opens a parent run row + enqueues children."""
    parent = _new_run(
        task_name="pre_register_marking_codes_daily",
        run_kind=run_kind,
        triggered_by_user_id=triggered_by_user_id,
    )
    logger.info("pre_register_marking_codes_daily: starting (run %s)", parent.id)

    try:
        products = (
            Product.query.join(
                ProductFiscalProfile,
                ProductFiscalProfile.product_id == Product.id,
            )
            .filter(
                ProductFiscalProfile.requires_marking_codes.is_(True),
                ProductFiscalProfile.fiscalization_enabled.is_(True),
            )
            .all()
        )

        enqueued = 0
        failed = 0
        for product in products:
            try:
                replenish_marking_codes_for_product.delay(
                    int(product.id),
                    run_kind,
                    parent.id,
                )
                enqueued += 1
            except Exception:
                failed += 1
                logger.exception(
                    "pre_register_marking_codes_daily: failed to enqueue product %s",
                    product.id,
                )

        summary = {
            "success": failed == 0,
            "enqueued": enqueued,
            "failed": failed,
            "total_products": len(products),
            "timestamp": _utcnow().isoformat(),
            # The parent ledger row has no per-code counters; expose intent
            # via the summary JSON for the UI drawer.
            "requested": 0,
            "utilised": 0,
            "skipped_invalid": 0,
            "errors": failed,
        }
        _finalize_run(
            parent.id,
            status=MarkingCodeRunStatus.SUCCESS if failed == 0 else MarkingCodeRunStatus.FAILED,
            summary=summary,
        )
        logger.info(
            "pre_register_marking_codes_daily: completed",
            extra={"enqueued": enqueued, "failed": failed, "total_products": len(products)},
        )
        return summary
    except Exception as exc:
        logger.exception("pre_register_marking_codes_daily: unexpected failure")
        _finalize_run(parent.id, status=MarkingCodeRunStatus.FAILED, error_message=str(exc))
        raise


@shared_task(bind=True, max_retries=3, time_limit=900, soft_time_limit=840)
def replenish_marking_codes_for_product(
    self,
    product_id: int,
    run_kind: str = "on_empty",
    parent_run_id: Optional[int] = None,
    triggered_by_user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Pre-utilise marking codes for one product up to its computed target.

    Idempotent under the per-product Redis lock inside MarkingCodePoolService.
    Safe to invoke from the daily fan-out and the intra-day triggers
    simultaneously — the lock collapses overlap.
    """
    run = _new_run(
        task_name="replenish_marking_codes_for_product",
        run_kind=str(run_kind),
        parent_run_id=parent_run_id,
        product_id=int(product_id),
        triggered_by_user_id=triggered_by_user_id,
    )

    product = Product.query.get(int(product_id))
    if not product:
        logger.warning("replenish_marking_codes_for_product: product %s not found", product_id)
        result = {"success": False, "error": "product_not_found", "product_id": int(product_id)}
        _finalize_run(
            run.id,
            status=MarkingCodeRunStatus.FAILED,
            summary=result,
            error_message="product_not_found",
        )
        return result

    if not product.requires_marking_codes:
        logger.info(
            "replenish_marking_codes_for_product: product %s does not require marking codes",
            product_id,
        )
        result = {
            "success": True,
            "skipped": True,
            "reason": "not_fiscalisable",
            "product_id": int(product_id),
        }
        _finalize_run(run.id, status=MarkingCodeRunStatus.SKIPPED, summary=result)
        return result

    service = MarkingCodePoolService()
    try:
        summary = service.pre_utilise_for_product(product, run_kind=str(run_kind))
    except Exception as exc:
        logger.exception(
            "replenish_marking_codes_for_product: unexpected failure for product %s",
            product_id,
        )
        try:
            raise self.retry(exc=exc, countdown=60)
        except self.MaxRetriesExceededError:
            result = {
                "success": False,
                "error": str(exc),
                "product_id": int(product_id),
                "run_kind": run_kind,
            }
            _finalize_run(
                run.id,
                status=MarkingCodeRunStatus.FAILED,
                summary=result,
                error_message=str(exc),
            )
            return result

    summary["success"] = summary.get("errors", 0) == 0
    summary["product_id"] = int(product_id)

    if summary.get("reason") in ("not_fiscalisable", "utilisation_disabled", "lock_held"):
        terminal = MarkingCodeRunStatus.SKIPPED
    elif summary["success"]:
        terminal = MarkingCodeRunStatus.SUCCESS
    else:
        terminal = MarkingCodeRunStatus.FAILED
    _finalize_run(run.id, status=terminal, summary=summary)
    return summary
