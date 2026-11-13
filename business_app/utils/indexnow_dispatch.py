"""Requests for an IndexNow submission of the public pages.

A save that changes what a public page says requests one submission. `request_submission` is how one is asked for,
and `_publish_submission` is the ONE place one is published. A "queued" marker (`SET NX EX`) debounces a burst of
requests into one run; the run clears it as it starts, so a request made during a run queues the next one. A publish
that fails clears it again. Without Redis a request publishes anyway: losing the marker costs an extra submission,
never a missed one.

- The detector (spec 5.9). One `before_flush` listener reads the attribute history of what the flush writes, and
  flags the session's transaction when it inserts or deletes a `Product`, changes one of `PUBLIC_PRODUCT_COLUMNS`, or
  inserts, deletes or changes the value or the switch of a translation of a public product field. Never "the product
  is dirty": every save rewrites `updated_at`, and every stock decrement dirties the row. It loads nothing and runs no
  SQL. With the switch INDEXNOW_ENABLED off it returns at once, and once the transaction is flagged it looks no
  further (one request flushes several times, and each flush resets the history). A failure inside it is logged as a
  warning, never raised into the write: that flush flags nothing.
- The drain. One `after_commit` listener requests one submission per committed transaction that was flagged, and a
  rollback drops the flag. A SAVEPOINT release or rollback is not the transaction: both listeners ignore it. A request
  it makes that fails is logged, never raised into the committed write. Modelled on `eta_recompute_dispatch`.
- Not seen, and touching no public column: the query-level stock UPDATE of `tasks/order_tasks.py`.

Eager mode. When the Celery app current at the request runs tasks eagerly (`DevelopmentConfig`), a publish would run
the submission inline, inside the caller, even inside an after-commit hook. Such a request publishes nothing and logs.
This module never imports `business_app.tasks.celery_app` and never reads the Flask `CELERY` dict (see the docstring of
`eta_recompute_dispatch` for why). Of the config it reads only the switch, in the detector; the task reads it again.
"""

import itertools
import logging

from celery import current_app as current_celery_app
from flask import current_app
from sqlalchemy import event
from sqlalchemy import inspect as sa_inspect

from business_app import redis_client
from business_app.models.product import Product
from business_app.models.translation import Translation
from shared.redis_keyspace import RedisKeyspace

logger = logging.getLogger(__name__)

INDEXNOW_COUNTDOWN_SECONDS = 60
# Longer than the countdown: a worker that starts late must not let a second publish through.
INDEXNOW_SCHEDULED_TTL_SECONDS = 120

# The session.info flag of a transaction that changed a public product fact.
PENDING_KEY = "pending_indexnow_submission"

# Guard so repeated create_app() calls (tests) don't stack duplicate listeners.
_REGISTERED = False

# What a public product page, the feeds and `llms.txt` print of a product (spec 5.9). Never watched: stock_quantity,
# updated_at, sku, barcode, category_id, is_featured, track_inventory, ingredients and the fiscal profile.
PUBLIC_PRODUCT_COLUMNS = (
    Product.name,
    Product.description,
    Product.short_description,
    Product.meta_title,
    Product.meta_description,
    Product.base_price,
    Product.discount_price,
    Product.is_active,
    Product.slug,
    Product.min_order_quantity,
    Product.size,
    Product.volume,
    Product.tracks_returnable_bottles,
    Product.returnable_bottles_per_unit,
    Product.images,
)

# The public fields that also have entity translations (`Product.<field>.<id>` rows).
_PUBLIC_TRANSLATED_FIELDS = frozenset(
    {column.key for column in PUBLIC_PRODUCT_COLUMNS} & set(Product._translatable_fields)
)

# What a translation row says on the page: its text, and whether it is shown.
_TRANSLATION_FACTS = (Translation.value, Translation.is_active)


def _touches_public_product_facts(session) -> bool:
    """True when what `session` is about to flush inserts or deletes a product, changes a public column of one, or
    inserts, deletes or changes the text or switch of a translation of a public product field. Attribute history only,
    read through `AttributeState.history` (`PASSIVE_NO_INITIALIZE`), so nothing is loaded and no SQL runs. A column set
    to the value it had is no change."""
    for instance in itertools.chain(session.new, session.deleted):
        if isinstance(instance, Product):
            return True
        if isinstance(instance, Translation) and _is_public_product_translation(instance):
            return True
    for instance in session.dirty:
        if isinstance(instance, Product) and _changes_any(instance, PUBLIC_PRODUCT_COLUMNS):
            return True
        if (
            isinstance(instance, Translation)
            and _is_public_product_translation(instance)
            and _changes_any(instance, _TRANSLATION_FACTS)
        ):
            return True
    return False


def _changes_any(instance, columns) -> bool:
    attrs = sa_inspect(instance).attrs
    return any(attrs[column.key].history.has_changes() for column in columns)


def _is_public_product_translation(row) -> bool:
    """True when the row's key is `Product.<public translated field>.<id>`. The key is read as loaded: `row.key` on an
    expired row would run a SELECT inside the flush, so a key that is not loaded is not a match."""
    key = sa_inspect(row).attrs.key.loaded_value
    if not isinstance(key, str):
        return False
    parts = key.split(".")
    return (
        len(parts) == 3
        and parts[0] == Product.__name__
        and parts[1] in _PUBLIC_TRANSLATED_FIELDS
        and parts[2].isdigit()
    )


def request_submission() -> bool:
    """Queue a submission INDEXNOW_COUNTDOWN_SECONDS from now. True when this call published it; False when one is
    already queued, when Celery runs tasks eagerly here, or when the publish failed. Never raises."""
    if current_celery_app.conf.task_always_eager:
        logger.info("indexnow_request_skipped: Celery runs tasks eagerly here")
        return False
    marker = RedisKeyspace.indexnow_submit_scheduled()
    try:
        marked = bool(redis_client.set(marker, "1", nx=True, ex=INDEXNOW_SCHEDULED_TTL_SECONDS))
    except Exception:  # noqa: BLE001 - a cache: without the marker the request still publishes
        logger.warning("indexnow: the queued marker is unavailable; publishing without it", exc_info=True)
        marked = None
    if marked is False:
        return False  # a submission is already queued, and it reads everything this request would
    try:
        _publish_submission(INDEXNOW_COUNTDOWN_SECONDS)
    except Exception:  # noqa: BLE001 - the caller's work is done; the next request queues again
        logger.exception("indexnow: the publish failed (countdown=%s)", INDEXNOW_COUNTDOWN_SECONDS)
        if marked:
            clear_scheduled_marker()
        return False
    return True


def _publish_submission(countdown: int) -> None:
    """THE publish of a submission, `countdown` seconds out. No expiry: nothing else re-submits, so a run that starts
    late is still wanted."""
    from business_app.tasks.seo_tasks import submit_public_urls

    submit_public_urls.apply_async(countdown=countdown)


def clear_scheduled_marker() -> None:
    """Best effort: the marker expires by itself."""
    try:
        redis_client.delete(RedisKeyspace.indexnow_submit_scheduled())
    except Exception:  # noqa: BLE001
        logger.warning("indexnow: could not clear the queued marker", exc_info=True)


def register_indexnow_dispatch(db) -> None:
    """Attach the detector (before_flush) and the drain (after_commit / after_rollback) to db.session once."""
    global _REGISTERED
    if _REGISTERED:
        return

    session_cls = db.session

    @event.listens_for(session_cls, "before_flush")
    def _detect_public_product_facts(session, flush_context, instances):  # noqa: ANN001
        try:
            if not current_app.config["INDEXNOW_ENABLED"] or session.info.get(PENDING_KEY):
                return
            if _touches_public_product_facts(session):
                session.info[PENDING_KEY] = True
        except Exception:  # noqa: BLE001 - never fail the write
            logger.warning("indexnow: the commit detector failed; this flush flags no submission", exc_info=True)

    @event.listens_for(session_cls, "after_commit")
    def _drain_on_commit(session):  # noqa: ANN001
        if session.in_nested_transaction():
            return  # a SAVEPOINT release: the transaction has not committed yet
        if not session.info.pop(PENDING_KEY, None):
            return
        try:
            request_submission()
        except Exception:  # best-effort — never break the committed write
            logger.exception("indexnow: the request after a commit failed")

    @event.listens_for(session_cls, "after_rollback")
    def _discard_on_rollback(session):  # noqa: ANN001
        if session.in_nested_transaction():
            return  # a SAVEPOINT rollback: the outer transaction may still commit what was flagged before it
        session.info.pop(PENDING_KEY, None)

    _REGISTERED = True
