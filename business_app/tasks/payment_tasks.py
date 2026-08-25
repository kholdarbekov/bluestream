"""
Payment-related Celery tasks for the Water Business Platform
This file should be placed in business_app/tasks/payment_tasks.py
"""

from celery import shared_task
from celery.utils.log import get_task_logger
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List
from flask import current_app

from business_app.models.payment import Payment, PaymentTransaction, PaymentFiscalization
from business_app.models.order import Order
from business_app.models.audit import AuditEventType, AuditSeverity
from business_app.services.payment_service import PaymentService
from business_app.services.payment_fiscalization_service import PaymentFiscalizationService
from business_app.services.notification_service import NotificationService
from business_app.utils.audit_logger import audit_logger
from business_app.utils.constants import NotificationChannel
from business_app.utils.exceptions import ProviderUnavailableError, PaymentError
from business_app.utils.payment_projection import FISCALIZED_RAILS, order_is_dead, order_is_resolved
from shared.enums import PaymentMethod, PaymentStatus, OrderStatus
from business_app import db

logger = get_task_logger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60, time_limit=300, soft_time_limit=270)
def process_payment_webhook(self, payment_id: int, webhook_data: Dict[str, Any]):
    """Process payment webhook from external gateway"""
    try:
        logger.info(f"Processing payment webhook for payment {payment_id}")

        payment_service = PaymentService()

        # Acquire row-level lock for idempotent processing
        payment = Payment.query.filter_by(id=payment_id).with_for_update().first()

        if not payment:
            logger.error(f"Payment {payment_id} not found")
            return {"success": False, "error": "Payment not found"}

        # Idempotency check: skip if already completed (now race-safe with row lock)
        if payment.status == PaymentStatus.COMPLETED:
            logger.info(f"Payment {payment_id} already completed, skipping webhook")
            db.session.commit()  # Release the row lock
            return {"success": True, "skipped": True, "reason": "already_completed"}

        # Process webhook based on payment method
        if payment.payment_method.value == "payme":
            result = payment_service.handle_payme_webhook(webhook_data)
        elif payment.payment_method.value == "click":
            result = payment_service.handle_click_webhook(webhook_data)
        else:
            logger.error(f"Unsupported payment method for webhook: {payment.payment_method.value}")
            db.session.commit()  # Release the row lock
            return {"success": False, "error": "Unsupported payment method"}

        db.session.commit()  # Release the row lock
        logger.info(f"Payment webhook processed successfully for payment {payment_id}")
        return result

    except Exception as exc:
        db.session.rollback()
        logger.error(f"Payment webhook processing failed: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, time_limit=300, soft_time_limit=270)
def process_payment_confirmation(self, payment_id: int):
    """Process payment confirmation and update order status"""
    try:
        logger.info(f"Processing payment confirmation for payment {payment_id}")

        payment = Payment.query.get(payment_id)
        if not payment:
            logger.error(f"Payment {payment_id} not found")
            return {"success": False, "error": "Payment not found"}

        if payment.status == PaymentStatus.COMPLETED:
            # Idempotency check: skip if order already confirmed
            order = payment.order
            if order and order.status != OrderStatus.PENDING:
                logger.info(f"Order for payment {payment_id} already confirmed, skipping")
                return {"success": True, "skipped": True, "reason": "already_confirmed"}

            # Update order status
            if order and order.status == OrderStatus.PENDING:
                from business_app.services.order_service import OrderService

                order_service = OrderService()
                order_service.update_order_status(order.id, OrderStatus.CONFIRMED)

            # Send payment confirmation notification
            notification_service = NotificationService()
            notification_service.send_payment_notification(payment_id)

            logger.info(f"Payment confirmation processed for payment {payment_id}")
            return {"success": True, "payment_id": payment_id}

        return {"success": False, "error": "Payment not completed"}

    except Exception as exc:
        logger.error(f"Payment confirmation processing failed: {exc}")
        raise self.retry(exc=exc)


_RECONCILABLE_METHODS = {
    PaymentMethod.PAYME.value,
    PaymentMethod.CLICK.value,
    PaymentMethod.CARD.value,
}


@shared_task(time_limit=300, soft_time_limit=270)
def reconcile_pending_payments():
    """Reconcile PENDING payments against their gateway.

    Runs every 15 minutes per the audit's PAY-007 recommendation. For each
    payment that has been PENDING longer than ``PAYMENT_RECONCILE_AFTER_MINUTES``
    (default 10 min), polls the gateway and COMPLETES it on affirmative success.

    It never cancels. See the long comment in the loop: the checkout link has no
    gateway-side object we could void, so a local cancellation is unenforceable —
    and under the 2026-08-24 policy the customer may pay right up until delivery.
    """
    from business_app.utils.audit_logger import audit_logger
    from business_app.models.audit import AuditEventType, AuditSeverity

    reconcile_after_minutes = int(current_app.config.get("PAYMENT_RECONCILE_AFTER_MINUTES", 10) or 10)
    now = datetime.now(timezone.utc)
    reconcile_threshold = now - timedelta(minutes=reconcile_after_minutes)

    pending_payments = Payment.query.filter(
        Payment.status == PaymentStatus.PENDING,
        Payment.created_at < reconcile_threshold,
    ).all()

    # The PENDING-payment gauges used to be sampled here. They are now computed
    # on the /metrics scrape in the WEB process (prometheus_metrics.
    # _refresh_pending_payment_gauges): monitoring/prometheus.yml scrapes
    # business_app only — there has never been a celery_worker target — so every
    # sample taken here was silently discarded.

    logger.info(
        "Reconciling %d pending payments (reconcile_after=%dm; no auto-cancel)",
        len(pending_payments),
        reconcile_after_minutes,
    )

    payment_service = PaymentService()
    counts = {"completed": 0, "cancelled": 0, "failed": 0, "unchanged": 0, "errors": 0}
    confirmed_payment_ids: List[int] = []

    for payment in pending_payments:
        try:
            method_value = (
                payment.payment_method.value if hasattr(payment.payment_method, "value") else payment.payment_method
            )
            if method_value not in _RECONCILABLE_METHODS:
                continue
            provider_value = payment.payment_provider
            # Normalize created_at to tz-aware for the timeout comparisons below.
            # Postgres preserves tz on round-trip; SQLite (tests) drops it, which
            # would otherwise raise "can't compare offset-naive and offset-aware".
            created_at = payment.created_at
            if created_at is not None and created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            status_payload = payment_service.check_payment_status(payment.id)
            normalized_status = str(status_payload.get("status") or "").lower()

            if normalized_status in {"completed", "success", PaymentStatus.COMPLETED.value}:
                confirmed_payment_ids.append(payment.id)
                counts["completed"] += 1
                audit_logger.log_event(
                    event_type=AuditEventType.PAYMENT_PROCESSED,
                    action="payment_reconciled_completed",
                    severity=AuditSeverity.MEDIUM,
                    resource_type="payment",
                    resource_id=str(payment.id),
                    description=f"Reconciliation matched gateway completion for payment {payment.id}",
                    additional_data={"provider": provider_value, "gateway_status": normalized_status},
                )
                continue

            # NOTHING HERE CANCELS ANY MORE (policy 2026-08-24).
            #
            # This task used to auto-cancel a payment that was still PENDING past
            # the old payment-timeout window, and release its marking codes. That is what
            # destroyed payment 1204 in prod incident TG_000413_26 — and the
            # cancellation was never enforceable anyway: `create_payment_link`
            # makes NO Click API call (the checkout URL is a plain urlencode), so
            # there is no gateway-side object to void. We declared the payment dead
            # while leaving a fully payable link in the customer's hands; they used
            # it 28 hours later and the money had nowhere to land.
            #
            # A gateway-reported cancel is deliberately NOT terminal either. It
            # describes ONE failed attempt, not the payability of the order — the
            # customer can reopen the same link. Writing CANCELLED here would make
            # the Phase 4A PREPARE guard (which requires PENDING/PROCESSING) refuse
            # their next attempt on an order they still owe for.
            #
            # A payment's life now ends only where the ORDER resolves:
            #   * cash at the door  -> convert_electronic_order_to_cash (case A)
            #   * paid via the link -> the normal complete flow, at any time (case B)
            #   * order cancelled   -> the order-cancellation path
            # An unpaid delivered order is surfaced by the debt cap, the debtor
            # lists and the PendingPaymentOnClosedOrder alert instead of by a
            # silent status write.
            counts["unchanged"] += 1
            continue

        except Exception as e:
            logger.error("Error reconciling pending payment %s: %s", payment.id, e)
            db.session.rollback()
            counts["errors"] += 1
            continue

    db.session.commit()

    # Trigger confirmation processing after successful commit (idempotent).
    for payment_id in confirmed_payment_ids:
        process_payment_confirmation.delay(payment_id)

    logger.info("Reconciliation summary: %s", counts)
    return counts


@shared_task(time_limit=300, soft_time_limit=270)
def reconcile_completed_payment_side_effects():
    """Repair lost post-payment side effects on COMPLETED electronic payments.

    Spec 2026-07-08 defects #5/#10: fiscalization and customer confirmation run
    after the money commit and can be lost to a crash or broker hiccup with no
    Click-retry recovery (the -4 short-circuit never re-runs them). This sweep
    re-drives both for CLICK/CARD payments completed in the last 7 days; each
    target is idempotent. Terminal fiscalization states (COMPLETED — done;
    NOT_REQUIRED — no fiscalizable items on the order) and ones already flagged
    ``retries_exhausted_at`` (admin review pending) are left alone.
    """
    from shared.enums import FiscalizationStatus

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=7)
    stale_before = now - timedelta(minutes=30)
    settled_fiscalization_statuses = {FiscalizationStatus.COMPLETED.value, FiscalizationStatus.NOT_REQUIRED.value}

    payments = Payment.query.filter(
        Payment.status == PaymentStatus.COMPLETED,
        Payment.paid_at >= window_start,
        Payment.payment_method.in_(sorted(FISCALIZED_RAILS, key=lambda m: m.value)),
    ).all()

    counts = {"scanned": len(payments), "fiscalization_requeued": 0, "confirmation_redispatched": 0, "errors": 0}
    payment_service = PaymentService()

    for payment in payments:
        try:
            # A DEAD order is out of scope for BOTH repairs. Under the owner's
            # 2026-08-24 rule a cancelled card/Click order keeps its COMPLETED
            # payment (the money was really taken and the receipt cannot be
            # un-filed), so it stays in this sweep's population for seven days.
            # Re-driving it would file a tax receipt for goods that are not
            # going out and send the customer a "payment confirmed" message for
            # an order that no longer exists. The money is settled as customer
            # prepaid credit by the cancel cascade, not here.
            if order_is_dead(payment.order):
                continue

            fisc = getattr(payment, "fiscalization", None)
            if fisc is None:
                payment_service.queue_click_fiscalization(payment.id)
                counts["fiscalization_requeued"] += 1
            else:
                fisc_status = fisc.status.value if hasattr(fisc.status, "value") else fisc.status
                # Normalize to tz-aware for the Python-side staleness compare below
                # (Postgres round-trips tzinfo; SQLite in tests drops it).
                last_touch = fisc.updated_at or fisc.created_at
                if last_touch is not None and last_touch.tzinfo is None:
                    last_touch = last_touch.replace(tzinfo=timezone.utc)
                is_stale = last_touch is None or last_touch < stale_before
                if fisc_status not in settled_fiscalization_statuses and fisc.retries_exhausted_at is None and is_stale:
                    process_click_fiscalization_task.delay(payment.id)
                    counts["fiscalization_requeued"] += 1

            post_payment = (payment.provider_data or {}).get("post_payment") or {}
            if not post_payment.get("confirmation_enqueued_at"):
                if payment_service.dispatch_payment_confirmation(payment):
                    counts["confirmation_redispatched"] += 1
        except Exception:  # noqa: BLE001
            logger.exception("Side-effect sweep failed for payment %s", payment.id)
            db.session.rollback()
            counts["errors"] += 1
            continue

    db.session.commit()
    logger.info("Completed-payment side-effect sweep: %s", counts)
    return counts


@shared_task(bind=True, max_retries=3, default_retry_delay=60, time_limit=300, soft_time_limit=270)
def reverse_click_payment_task(self, payment_id: int, click_paydoc_id: str, click_trans_id: str):
    """Reverse a duplicate Click charge (spec 2026-07-08 Case B).

    click_paydoc_id/click_trans_id identify the INCOMING duplicate charge —
    never resolved from the payment row (those ids belong to the winner).
    """
    from business_app.utils.audit_logger import audit_logger
    from business_app.models.audit import AuditEventType, AuditSeverity

    payment = Payment.query.get(payment_id)
    if not payment:
        logger.error("reverse_click_payment_task: payment %s not found", payment_id)
        return {"status": "payment_missing"}

    txn = PaymentTransaction.query.filter_by(
        payment_id=payment_id,
        transaction_type="click_duplicate_charge",
        provider_transaction_id=str(click_trans_id),
    ).first()
    if txn is not None and txn.status == "reversed":
        return {"status": "already_reversed"}

    service = PaymentService()._get_click_provider_service()
    try:
        response = service.reverse_by_click_payment_id(int(click_paydoc_id))
    except ProviderUnavailableError as exc:
        raise self.retry(exc=exc)
    except PaymentError as exc:
        if txn is not None:
            txn.status = "reversal_rejected"
            txn.failure_reason = str(exc)[:500]
        audit_logger.log_event(
            event_type=AuditEventType.PAYMENT_FAILED,
            action="click_duplicate_reversal_rejected",
            severity=AuditSeverity.HIGH,
            resource_type="payment",
            resource_id=str(payment_id),
            description=f"Click rejected reversal of duplicate charge {click_trans_id}: {exc}",
            additional_data={"click_paydoc_id": str(click_paydoc_id), "click_trans_id": str(click_trans_id)},
        )
        db.session.commit()
        return {"status": "rejected"}

    if txn is not None:
        txn.status = "reversed"
        txn.provider_response = {**(txn.provider_response or {}), "reversal": response}
    audit_logger.log_event(
        event_type=AuditEventType.PAYMENT_PROCESSED,
        action="click_duplicate_charge_reversed",
        severity=AuditSeverity.MEDIUM,
        resource_type="payment",
        resource_id=str(payment_id),
        description=f"Reversed duplicate Click charge {click_trans_id}",
        additional_data={"click_paydoc_id": str(click_paydoc_id)},
    )
    db.session.commit()
    return {"status": "reversed"}


def _mark_fiscalization_retries_exhausted(payment_id: int, exc: Exception) -> None:
    """Record terminal failure of the Click fiscalization Celery task.

    Called from the task's MaxRetriesExceededError handler. Sets
    retries_exhausted_at so the admin UI surfaces the order, and writes a
    CRITICAL audit row for compliance traceability (missed fiscal receipts
    have direct tax implications).
    """
    try:
        fiscalization = PaymentFiscalization.query.filter_by(payment_id=payment_id).first()
        if fiscalization is None:
            logger.error(
                "Cannot mark retries_exhausted: PaymentFiscalization for payment %s not found",
                payment_id,
            )
            audit_logger.log_event(
                event_type=AuditEventType.PAYMENT_FAILED,
                action="payment_fiscalization_retries_exhausted",
                severity=AuditSeverity.CRITICAL,
                resource_type="payment_fiscalization",
                resource_id=str(payment_id),
                description=f"Click fiscalization gave up after all retries for payment {payment_id}",
                success=False,
                error_message=str(exc),
                additional_data={"payment_id": payment_id},
            )
            return

        # Snapshot fields before commit — SQLAlchemy's default expire_on_commit
        # would otherwise trigger lazy reloads when read after commit, which
        # would fail under the same DB pressure that caused this exhaustion.
        fiscalization.retries_exhausted_at = datetime.now(timezone.utc)
        payment = getattr(fiscalization, "payment", None)
        order = getattr(payment, "order", None) if payment is not None else None
        fiscalization_id = fiscalization.id
        attempts = int(fiscalization.attempts or 0)
        order_number = getattr(order, "order_number", None)
        db.session.commit()

        audit_logger.log_event(
            event_type=AuditEventType.PAYMENT_FAILED,
            action="payment_fiscalization_retries_exhausted",
            severity=AuditSeverity.CRITICAL,
            resource_type="payment_fiscalization",
            resource_id=str(fiscalization_id),
            description=(
                f"Click fiscalization gave up after all retries for payment {payment_id}"
                + (f" (order {order_number})" if order_number else "")
            ),
            success=False,
            error_message=str(exc),
            additional_data={
                "payment_id": payment_id,
                "order_number": order_number,
                "attempts": attempts,
            },
        )
    except Exception:
        db.session.rollback()
        logger.exception(
            "Failed to record fiscalization retries-exhausted state for payment %s",
            payment_id,
        )


@shared_task(bind=True, max_retries=3, default_retry_delay=60, time_limit=300, soft_time_limit=270)
def process_payment_fiscalization(self, payment_id: int, force: bool = False):
    """Process Click fiscalization asynchronously after payment success."""
    try:
        logger.info("Processing payment fiscalization for payment %s", payment_id)
        service = PaymentFiscalizationService()
        fiscalization = service.process_click_fiscalization(payment_id, force=force)
        db.session.commit()
        return {
            "success": True,
            "payment_id": payment_id,
            "status": fiscalization.status.value if hasattr(fiscalization.status, "value") else fiscalization.status,
        }
    except Exception as exc:
        db.session.rollback()
        logger.error("Payment fiscalization failed for payment %s: %s", payment_id, exc)
        try:
            raise self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            logger.error(
                "Payment fiscalization retries exhausted for payment %s; flagging for admin review",
                payment_id,
            )
            _mark_fiscalization_retries_exhausted(payment_id, exc)
            return {"success": False, "payment_id": payment_id, "retries_exhausted": True}


@shared_task(bind=True, max_retries=3, default_retry_delay=60, time_limit=300, soft_time_limit=270)
def process_click_fiscalization_task(self, payment_id: int, force: bool = False):
    """Compatibility alias for Click fiscalization task name."""
    try:
        logger.info("Processing Click fiscalization task for payment %s", payment_id)
        service = PaymentFiscalizationService()
        fiscalization = service.process_click_fiscalization(payment_id, force=force)
        db.session.commit()
        return {
            "success": True,
            "payment_id": payment_id,
            "status": fiscalization.status.value if hasattr(fiscalization.status, "value") else fiscalization.status,
        }
    except Exception as exc:
        db.session.rollback()
        logger.error("Click fiscalization task failed for payment %s: %s", payment_id, exc)
        try:
            raise self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            logger.error(
                "Click fiscalization retries exhausted for payment %s; flagging for admin review",
                payment_id,
            )
            _mark_fiscalization_retries_exhausted(payment_id, exc)
            return {"success": False, "payment_id": payment_id, "retries_exhausted": True}


@shared_task(time_limit=300, soft_time_limit=270)
def retry_failed_payments():
    """Retry failed payments that might be recoverable"""
    try:
        logger.info("Retrying failed payments")

        # Get payments that failed in the last 24 hours
        threshold_time = datetime.now(timezone.utc) - timedelta(hours=24)
        failed_payments = Payment.query.filter(
            Payment.status == PaymentStatus.FAILED, Payment.updated_at > threshold_time
        ).all()

        retried_count = 0

        for payment in failed_payments:
            try:
                # Only retry certain types of failures (network issues, temporary gateway problems)
                if payment.gateway_response and "temporary" in payment.gateway_response.get("error", "").lower():
                    # Reset payment status to pending for retry
                    payment.status = PaymentStatus.PENDING
                    payment.updated_at = datetime.now(timezone.utc)
                    db.session.commit()

                    # Send notification to user about retry
                    notification_service = NotificationService()
                    notification_service.send_notification(
                        payment.user_id,
                        "payment_retry",
                        template_data={"order_number": payment.order.order_number, "payment_amount": payment.amount},
                    )

                    retried_count += 1
                    logger.info(f"Retrying payment {payment.id}")

            except Exception as e:
                logger.error(f"Error retrying payment {payment.id}: {e}")
                continue

        logger.info(f"Retried {retried_count} failed payments")
        return {"retried_count": retried_count}

    except Exception as e:
        logger.error(f"Error retrying failed payments: {e}")
        return {"error": str(e)}


@shared_task(bind=True, max_retries=2, default_retry_delay=300, time_limit=300, soft_time_limit=270)
def mark_overdue_cod_reconciliation_sessions(self):
    """Mark active driver COD sessions past the warning window for manager visibility."""
    try:
        from business_app.services.driver_reconciliation_service import DriverReconciliationService

        service = DriverReconciliationService()
        updated = service.mark_overdue_sessions()
        manager_notifications = service.notify_managers_about_exception_sessions()
        logger.info(
            "Marked %s driver COD reconciliation sessions as warning-due (manager notifications=%s)",
            updated,
            manager_notifications,
        )
        return {"updated_sessions": updated, "manager_notifications": manager_notifications}
    except Exception as exc:
        logger.error("Failed to mark overdue COD reconciliation sessions: %s", exc)
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=2, default_retry_delay=300, time_limit=300, soft_time_limit=270)
def send_cod_reconciliation_reminders(self):
    """Send periodic reconciliation reminders to drivers with open sessions."""
    try:
        from business_app.services.driver_reconciliation_service import DriverReconciliationService

        service = DriverReconciliationService()
        result = service.send_due_reconciliation_reminders()
        manager_notifications = service.notify_managers_about_exception_sessions()
        logger.info(
            "Sent COD reconciliation reminders: sent=%s failed=%s (manager notifications=%s)",
            result.get("sent", 0),
            result.get("failed", 0),
            manager_notifications,
        )
        return {
            "sent": result.get("sent", 0),
            "failed": result.get("failed", 0),
            "manager_notifications": manager_notifications,
        }
    except Exception as exc:
        logger.error("Failed to send COD reconciliation reminders: %s", exc)
        raise self.retry(exc=exc)


# NOTE: process_loyalty_points_payment task removed — loyalty points are spent
# only on rewards (LoyaltyReward.points_cost), never as a direct payment method.


@shared_task(bind=True, max_retries=3, time_limit=300, soft_time_limit=270)
def send_payment_reminder(self, order_id: int):
    """Send payment reminder for unpaid orders"""
    try:
        logger.info(f"Sending payment reminder for order {order_id}")

        order = Order.query.get(order_id)
        if not order:
            logger.error(f"Order {order_id} not found")
            return {"success": False, "error": "Order not found"}

        # Check if order still needs payment
        if order.status != OrderStatus.PENDING:
            logger.info(f"Order {order_id} no longer needs payment reminder")
            return {"success": False, "error": "Order status changed"}

        # Send reminder notification
        notification_service = NotificationService()
        notification_service.send_notification(
            order.user_id,
            "payment_reminder",
            channels=[NotificationChannel.TELEGRAM],
            template_data={
                "order_number": order.order_number,
                "order_total": float(order.total_amount) if order.total_amount is not None else None,
                "payment_deadline": (order.created_at + timedelta(hours=24)).isoformat(),
            },
        )

        logger.info(f"Payment reminder sent for order {order_id}")
        return {"success": True, "order_id": order_id}

    except Exception as exc:
        logger.error(f"Failed to send payment reminder: {exc}")
        raise self.retry(exc=exc)


@shared_task(time_limit=300, soft_time_limit=270)
def cleanup_old_payment_records():
    """Clean up old payment transaction records"""
    try:
        logger.info("Cleaning up old payment records")

        # Delete transaction records older than 1 year
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=365)

        deleted_count = PaymentTransaction.query.filter(PaymentTransaction.created_at < cutoff_date).delete()

        db.session.commit()

        logger.info(f"Cleaned up {deleted_count} old payment transaction records")
        return {"deleted_count": deleted_count}

    except Exception as e:
        logger.error(f"Error cleaning up payment records: {e}")
        return {"error": str(e)}


@shared_task(bind=True, max_retries=3, time_limit=300, soft_time_limit=270)
def validate_payment_integrity(self, payment_id: int):
    """Validate payment data integrity and consistency"""
    try:
        logger.info(f"Validating payment integrity for payment {payment_id}")

        payment = Payment.query.get(payment_id)
        if not payment:
            return {"success": False, "error": "Payment not found"}

        issues = []

        # Check if payment amount matches order total
        if payment.order and payment.amount != payment.order.total_amount:
            issues.append("Payment amount does not match order total")

        # Check payment status consistency
        if payment.status == PaymentStatus.COMPLETED and not payment.paid_at:
            issues.append("Completed payment missing paid_at timestamp")

        # Check for duplicate payments
        duplicate_payments = Payment.query.filter(
            Payment.order_id == payment.order_id, Payment.status == PaymentStatus.COMPLETED, Payment.id != payment.id
        ).count()

        if duplicate_payments > 0:
            issues.append(f"Found {duplicate_payments} duplicate completed payments")

        if issues:
            logger.warning(f"Payment integrity issues found for payment {payment_id}: {issues}")
            return {"success": False, "issues": issues}
        else:
            logger.info(f"Payment integrity validation passed for payment {payment_id}")
            return {"success": True, "payment_id": payment_id}

    except Exception as exc:
        logger.error(f"Payment integrity validation failed: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=30, time_limit=300, soft_time_limit=270)
def process_payment_verification(self, payment_id: int, verification_data: Dict[str, Any] = None):
    """Process payment verification from external gateway"""
    try:
        logger.info(f"Processing payment verification for payment {payment_id}")

        verification_data = verification_data or {}
        payment = Payment.query.get(payment_id)
        if not payment:
            logger.error(f"Payment {payment_id} not found")
            return {"success": False, "error": "Payment not found"}

        payment_service = PaymentService()

        # Verify payment with gateway
        verification_result = payment_service.verify_payment(payment_id, verification_data)

        if verification_result.get("success"):
            # Update payment status
            payment.status = PaymentStatus.COMPLETED
            payment.paid_at = datetime.now(timezone.utc)
            payment.provider_transaction_id = verification_result.get("transaction_id") or verification_data.get(
                "transaction_id"
            )

            # Create transaction record
            transaction = PaymentTransaction(
                payment_id=payment.id,
                transaction_type="verification",
                amount=payment.amount,
                status="success",
                provider_transaction_id=payment.provider_transaction_id,
                provider_response={
                    "verification_data": verification_data,
                    "verification_result": verification_result,
                },
                success=True,
                processed_at=datetime.now(timezone.utc),
            )

            db.session.add(transaction)
            db.session.commit()

            # Trigger payment confirmation processing
            process_payment_confirmation.delay(payment_id)

            logger.info(f"Payment verification successful for payment {payment_id}")
            return {"success": True, "payment_id": payment_id}
        else:
            failure_reason = verification_result.get("error", "Verification failed")

            # B1 — the THIRD expression of "may one gateway answer end this
            # payment", and the one that used to defeat the other two inside a
            # single request:
            #
            #   verify_payment -> check_payment_status -> update_payment_status
            #     -> B1's guard correctly refuses to end the payment (PENDING)
            #   -> verify_payment sees "not COMPLETED" -> {"success": False}
            #   -> this branch wrote FAILED anyway, and committed.
            #
            # `PaymentService.verify_payment` returns success=False for ANY
            # non-COMPLETED payment, so a perfectly healthy PENDING Click payment
            # on a live order was "verification failed". FAILED fails
            # `order_is_payable_online`, so the customer's next PREPARE gets -9:
            # the same permanent lockout, on the same population, triggered by
            # their own POST /payments/<id>/verify.
            #
            # Two conditions, both load-bearing:
            #  * order_is_resolved — same predicate as update_payment_status and
            #    the three Click COMPLETE branches, so all four agree.
            #  * status == PENDING — writing a terminal status OVER an already
            #    terminal one is its own bug: it would overwrite a CANCELLED
            #    payment's real reason with a generic "Verification failed", and
            #    a stray verification could downgrade a COMPLETED payment.
            may_end = payment.status == PaymentStatus.PENDING and order_is_resolved(payment.order)
            if may_end:
                payment.status = PaymentStatus.FAILED
                payment.failure_reason = failure_reason
            else:
                logger.info(
                    "Verification did not confirm payment %s; leaving status %s untouched "
                    "(order unresolved or payment already terminal)",
                    payment_id,
                    payment.status,
                )

            # The audit row is written either way — the verification attempt and
            # its outcome really happened, whatever we did with the payment.
            transaction = PaymentTransaction(
                payment_id=payment.id,
                transaction_type="verification",
                amount=payment.amount,
                status="failed",
                provider_response={
                    "verification_data": verification_data,
                    "verification_result": verification_result,
                },
                success=False,
                failure_reason=failure_reason,
                processed_at=datetime.now(timezone.utc),
            )

            db.session.add(transaction)
            db.session.commit()

            logger.error(f"Payment verification failed for payment {payment_id}: {failure_reason}")
            return {"success": False, "error": failure_reason}

    except Exception as exc:
        logger.error(f"Payment verification processing failed: {exc}")
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=3, default_retry_delay=60, time_limit=300, soft_time_limit=270)
def handle_payment_webhook(self, webhook_metadata: Dict[str, Any], webhook_source: str):
    """Handle payment webhook from external payment providers with enhanced security"""
    try:
        logger.info(f"Handling payment webhook from {webhook_source}")

        # Extract webhook data from metadata
        webhook_data = webhook_metadata.get("webhook_data", {})
        provider = webhook_metadata.get("provider", webhook_source)
        remote_addr = webhook_metadata.get("remote_addr")
        received_at = webhook_metadata.get("received_at")

        # Log webhook receipt for audit
        from business_app.utils.audit_logger import audit_logger, AuditEventType, AuditSeverity

        audit_logger.log_event(
            event_type=AuditEventType.WEBHOOK_RECEIVED,
            action="webhook_processing_started",
            severity=AuditSeverity.MEDIUM,
            resource_type="payment_webhook",
            description=f"Processing webhook from {provider}",
            additional_data={
                "provider": provider,
                "remote_addr": remote_addr,
                "received_at": received_at,
                "has_payment_data": bool(webhook_data),
            },
        )

        payment_service = PaymentService()

        # Process webhook using existing provider-specific handlers
        if provider == "payme":
            result = payment_service.handle_payme_webhook(webhook_data)
        elif provider == "click":
            result = payment_service.handle_click_webhook(webhook_data)
        else:
            logger.error(f"Unsupported webhook provider: {provider}")
            audit_logger.log_event(
                event_type=AuditEventType.SUSPICIOUS_ACTIVITY,
                action="unsupported_webhook_provider",
                severity=AuditSeverity.MEDIUM,
                resource_type="payment_webhook",
                description=f"Received webhook from unsupported provider: {provider}",
                additional_data={"provider": provider, "remote_addr": remote_addr},
            )
            return {"success": False, "error": "Unsupported provider"}

        # Log successful processing
        audit_logger.log_event(
            event_type=AuditEventType.WEBHOOK_RECEIVED,
            action="webhook_processing_completed",
            severity=AuditSeverity.LOW,
            resource_type="payment_webhook",
            description=f"Successfully processed webhook from {provider}",
            additional_data={"provider": provider, "result": result, "remote_addr": remote_addr},
        )

        logger.info(f"Payment webhook processed successfully from {provider}")
        return result

    except Exception as exc:
        logger.error(f"Payment webhook handling failed: {exc}")

        # Log webhook processing failure
        audit_logger.log_event(
            event_type=AuditEventType.SUSPICIOUS_ACTIVITY,
            action="webhook_processing_failed",
            severity=AuditSeverity.HIGH,
            success=False,
            resource_type="payment_webhook",
            description=f"Webhook processing failed for {webhook_source}: {str(exc)}",
            additional_data={
                "provider": webhook_source,
                "error": str(exc),
                "remote_addr": webhook_metadata.get("remote_addr") if isinstance(webhook_metadata, dict) else None,
            },
        )

        raise self.retry(exc=exc)
