"""Celery entry point of the IndexNow submission (registered in tasks/celery_app.py `include`).

Published by `utils/indexnow_dispatch.request_submission` through `_publish_submission`.
"""

import logging
from datetime import timedelta

from celery import shared_task
from flask import current_app

from business_app.services.public_urls_service import (
    indexnow_urls,
    language_urls,
    recently_deactivated_product_paths,
)
from business_app.utils.indexnow import ACCEPTED_STATUSES, submit_urls
from business_app.utils.indexnow_dispatch import clear_scheduled_marker
from business_app.utils.timezone_utils import get_utc_now

logger = logging.getLogger(__name__)

DEACTIVATED_WINDOW = timedelta(days=1)


@shared_task(name="seo.indexnow_submit", bind=True, max_retries=3, default_retry_delay=600)
def submit_public_urls(self) -> dict:
    """Submit every public URL, then the products switched off in the last day in every site language, as the live
    pages are listed (read from the database, so a retry finds the same list). Returns `{"skipped": "disabled"}` or
    `{"status": int, "urls": int}`."""
    clear_scheduled_marker()  # an edit made during this run queues the next one
    if not current_app.config["INDEXNOW_ENABLED"]:
        return {"skipped": "disabled"}
    retired = recently_deactivated_product_paths(get_utc_now() - DEACTIVATED_WINDOW)
    urls = indexnow_urls() + [url for path in retired for url in language_urls(path)]
    status = submit_urls(urls)
    if status in ACCEPTED_STATUSES:
        return {"status": status, "urls": len(urls)}
    if status is None or status == 429 or status >= 500:
        raise self.retry(exc=RuntimeError(f"IndexNow answered {status}"))
    logger.error("indexnow_submit_refused status=%s urls=%s", status, len(urls))
    return {"status": status, "urls": len(urls)}
