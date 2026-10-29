"""The pay writers no screen reaches are gone (step 4, spec §4.2, §17 Q8).

The card-charge route, the Payme Subscribe API receipts, and the daily retry of failed payments each could start a
payment outside ``retry-payment``'s rules. Payme's Merchant API refusing new charges is pinned in
``tests/integration/test_payment_matrix.py``.
"""

import sys
from unittest import mock

import pytest

from business_app.services.payment_service import PaymentService
from business_app.services.providers.payme_provider import PaymeProvider
from business_app.tasks import payment_tasks


@pytest.fixture(scope="module")
def celery_app_module(app):
    """Import ``business_app.tasks.celery_app`` once, as ``tests/unit/test_order_tasks_abandoned.py`` does."""
    module_name = "business_app.tasks.celery_app"
    if module_name not in sys.modules:
        with mock.patch("business_app.create_app", return_value=app):
            import business_app.tasks.celery_app  # noqa: F401
    return sys.modules[module_name]


def test_the_card_charge_route_is_gone(client):
    assert client.post("/api/v1/payments/process-card-payment", json={}).status_code == 404


def test_the_retry_failed_payments_task_is_gone(celery_app_module):
    assert not hasattr(payment_tasks, "retry_failed_payments")
    scheduled = {entry["task"] for entry in celery_app_module.celery.conf.beat_schedule.values()}
    assert "business_app.tasks.payment_tasks.retry_failed_payments" not in scheduled
    assert "business_app.tasks.payment_tasks.retry_failed_payments" not in celery_app_module.celery.conf.task_annotations


def test_no_service_can_charge_a_payme_card():
    for name in ("process_payme_payment_full", "create_payme_receipt", "pay_payme_receipt"):
        assert not hasattr(PaymentService, name), name
        assert not hasattr(PaymeProvider, name), name


def test_the_card_charge_checkout_script_is_gone():
    from pathlib import Path

    script = Path("business_app/static/js/pages/checkout.js").read_text(encoding="utf-8")
    assert "process-card-payment" not in script
    assert "verificationState" not in script
