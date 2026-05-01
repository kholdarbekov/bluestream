"""ARCH-006: tests for state-machine validators (utils/state_validators.py)."""

from types import SimpleNamespace

import pytest

from shared.enums import (
    DeliveryStatus,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
)
from business_app.utils.exceptions import InvalidStateTransition
from business_app.utils.state_validators import (
    assert_cash_payment_collector,
    assert_delivery_person_for_status,
    assert_order_address_for_status,
    assert_order_creator_for_source,
)


def _order(status=OrderStatus.PENDING, delivery_address_id=None, order_id=42):
    return SimpleNamespace(
        id=order_id,
        status=status,
        delivery_address_id=delivery_address_id,
    )


def _delivery(status=DeliveryStatus.SCHEDULED, delivery_person_id=None, delivery_id=7):
    return SimpleNamespace(
        id=delivery_id,
        status=status,
        delivery_person_id=delivery_person_id,
    )


def _payment(method=PaymentMethod.CASH, status=PaymentStatus.PENDING, collected_by=None, payment_id=1):
    return SimpleNamespace(
        id=payment_id,
        payment_method=method,
        status=status,
        collected_by=collected_by,
    )


@pytest.mark.unit
class TestOrderAddressGuard:
    def test_pending_does_not_require_address(self):
        assert_order_address_for_status(_order(), OrderStatus.PENDING)

    def test_cancelled_does_not_require_address(self):
        # Cancelled is reachable from PENDING with no address — must not block.
        assert_order_address_for_status(_order(), OrderStatus.CANCELLED)

    def test_confirmed_without_address_raises(self):
        with pytest.raises(InvalidStateTransition) as exc_info:
            assert_order_address_for_status(_order(), OrderStatus.CONFIRMED)
        assert exc_info.value.missing_field == 'delivery_address_id'
        assert exc_info.value.to_state == 'confirmed'
        assert exc_info.value.entity == 'order'

    def test_confirmed_with_address_passes(self):
        assert_order_address_for_status(_order(delivery_address_id=99), OrderStatus.CONFIRMED)

    def test_override_address_id_takes_precedence(self):
        # Caller is about to assign an address as part of the same write.
        assert_order_address_for_status(_order(), OrderStatus.CONFIRMED, delivery_address_id=12)

    @pytest.mark.parametrize('status', [
        OrderStatus.PREPARING,
        OrderStatus.OUT_FOR_DELIVERY,
        OrderStatus.DELIVERED,
        OrderStatus.RETURNED,
    ])
    def test_post_pending_states_require_address(self, status):
        with pytest.raises(InvalidStateTransition):
            assert_order_address_for_status(_order(), status)

    def test_string_status_is_coerced(self):
        with pytest.raises(InvalidStateTransition):
            assert_order_address_for_status(_order(), 'confirmed')


@pytest.mark.unit
class TestOrderCreatorGuard:
    @pytest.mark.parametrize('source', ['phone', 'admin'])
    def test_staff_source_without_creator_raises(self, source):
        with pytest.raises(InvalidStateTransition) as exc_info:
            assert_order_creator_for_source(order_source=source, created_by_staff_id=None)
        assert exc_info.value.missing_field == 'created_by_staff_id'

    @pytest.mark.parametrize('source', ['phone', 'admin'])
    def test_staff_source_with_creator_passes(self, source):
        assert_order_creator_for_source(order_source=source, created_by_staff_id=5)

    @pytest.mark.parametrize('source', ['web', 'telegram', 'mobile', 'api', None])
    def test_self_service_sources_do_not_require_creator(self, source):
        assert_order_creator_for_source(order_source=source, created_by_staff_id=None)


@pytest.mark.unit
class TestDeliveryPersonGuard:
    def test_scheduled_does_not_require_person(self):
        assert_delivery_person_for_status(_delivery(), DeliveryStatus.SCHEDULED)

    def test_pending_does_not_require_person(self):
        assert_delivery_person_for_status(_delivery(), DeliveryStatus.PENDING)

    def test_assigned_without_person_raises(self):
        with pytest.raises(InvalidStateTransition) as exc_info:
            assert_delivery_person_for_status(_delivery(), DeliveryStatus.ASSIGNED)
        assert exc_info.value.missing_field == 'delivery_person_id'
        assert exc_info.value.entity == 'delivery'

    def test_assigned_with_person_passes(self):
        assert_delivery_person_for_status(_delivery(delivery_person_id=11), DeliveryStatus.ASSIGNED)

    def test_override_person_id_takes_precedence(self):
        assert_delivery_person_for_status(
            _delivery(),
            DeliveryStatus.ASSIGNED,
            delivery_person_id=22,
        )

    @pytest.mark.parametrize('status', [
        DeliveryStatus.PICKED_UP,
        DeliveryStatus.IN_TRANSIT,
        DeliveryStatus.ARRIVED,
        DeliveryStatus.DELIVERED,
    ])
    def test_active_states_require_person(self, status):
        with pytest.raises(InvalidStateTransition):
            assert_delivery_person_for_status(_delivery(), status)

    @pytest.mark.parametrize('status', [
        DeliveryStatus.FAILED,
        DeliveryStatus.CANCELLED,
        DeliveryStatus.RETURNED,
    ])
    def test_terminal_failure_states_do_not_require_person(self, status):
        # A delivery may fail/cancel before assignment.
        assert_delivery_person_for_status(_delivery(), status)


@pytest.mark.unit
class TestCashPaymentCollectorGuard:
    def test_non_cash_payment_completion_passes_without_collector(self):
        payment = _payment(method=PaymentMethod.PAYME, collected_by=None)
        assert_cash_payment_collector(payment, PaymentStatus.COMPLETED)

    def test_cash_pending_does_not_require_collector(self):
        assert_cash_payment_collector(_payment(), PaymentStatus.PENDING)

    def test_cash_partially_paid_does_not_require_collector(self):
        assert_cash_payment_collector(_payment(), PaymentStatus.PARTIALLY_PAID)

    def test_cash_completed_without_collector_raises(self):
        with pytest.raises(InvalidStateTransition) as exc_info:
            assert_cash_payment_collector(_payment(), PaymentStatus.COMPLETED)
        assert exc_info.value.missing_field == 'collected_by'
        assert exc_info.value.entity == 'payment'

    def test_cash_completed_with_collector_passes(self):
        assert_cash_payment_collector(
            _payment(collected_by=33),
            PaymentStatus.COMPLETED,
        )

    def test_override_collector_takes_precedence(self):
        assert_cash_payment_collector(
            _payment(),
            PaymentStatus.COMPLETED,
            collected_by=44,
        )
