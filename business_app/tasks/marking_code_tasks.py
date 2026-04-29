"""Proactive Asl Belgisi marking-code pre-utilisation Celery tasks.

Two tasks:

* ``pre_register_marking_codes_daily`` — beat-scheduled at 00:00 UTC. Fans out a
  per-product replenish task for every fiscalisable product so customer card
  orders during the day skip the synchronous Tax Committee call.
* ``replenish_marking_codes_for_product`` — does the actual TC `/utilisation`
  call for one product. Re-used by the daily fan-out, the low-water trigger
  fired after a customer reservation, and the on-empty trigger fired when the
  pool was drained mid-day.
"""

from datetime import datetime, timezone
from typing import Any, Dict

from celery import shared_task
from celery.utils.log import get_task_logger

from business_app.models.product import Product, ProductFiscalProfile
from business_app.services.marking_code_pool_service import MarkingCodePoolService

logger = get_task_logger(__name__)


@shared_task(bind=True, max_retries=3, time_limit=3600, soft_time_limit=3300)
def pre_register_marking_codes_daily(self) -> Dict[str, Any]:
    """Daily 00:00 UTC sweep — fan out per-product replenish tasks."""
    logger.info("pre_register_marking_codes_daily: starting")

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
            replenish_marking_codes_for_product.delay(int(product.id), "daily")
            enqueued += 1
        except Exception:
            failed += 1
            logger.exception("pre_register_marking_codes_daily: failed to enqueue product %s", product.id)

    logger.info(
        "pre_register_marking_codes_daily: completed",
        extra={"enqueued": enqueued, "failed": failed, "total_products": len(products)},
    )
    return {
        "success": failed == 0,
        "enqueued": enqueued,
        "failed": failed,
        "total_products": len(products),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@shared_task(bind=True, max_retries=3, time_limit=900, soft_time_limit=840)
def replenish_marking_codes_for_product(
    self,
    product_id: int,
    run_kind: str = "on_empty",
) -> Dict[str, Any]:
    """Pre-utilise marking codes for one product up to its computed target.

    Idempotent under the per-product Redis lock inside MarkingCodePoolService.
    Safe to invoke from the daily fan-out and the intra-day triggers
    simultaneously — the lock collapses overlap.
    """
    product = Product.query.get(int(product_id))
    if not product:
        logger.warning("replenish_marking_codes_for_product: product %s not found", product_id)
        return {"success": False, "error": "product_not_found", "product_id": int(product_id)}

    if not product.requires_marking_codes:
        logger.info(
            "replenish_marking_codes_for_product: product %s does not require marking codes",
            product_id,
        )
        return {
            "success": True,
            "skipped": True,
            "reason": "not_fiscalisable",
            "product_id": int(product_id),
        }

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
            return {
                "success": False,
                "error": str(exc),
                "product_id": int(product_id),
                "run_kind": run_kind,
            }

    summary["success"] = summary.get("errors", 0) == 0
    summary["product_id"] = int(product_id)
    return summary
