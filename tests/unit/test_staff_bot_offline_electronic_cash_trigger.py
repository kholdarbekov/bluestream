"""
Regression tests for the broadened cash-prompt trigger that covers
unsuccessful-electronic (Click/Payme/Card) orders at delivery time.

Task 5 of plan 2026-06-22-offline-payment-on-failed-electronic-orders:
  - The existing cash-collection prompt must now also fire when
    payment_method in {click, payme, card} AND payment_status is NOT
    completed / paid / partially_paid.
  - The driver enters the collected amount; the bot sends it as
    metadata.cash_collected, which the backend (Task 4) uses to settle
    the payment.

These tests exercise the pure-Python helper logic extracted from
`initiate_status_change` without spinning up the async PTB machinery.
They mirror the style of test_staff_bot_cod_collection_clarity.py.
"""

from staff_bot.handlers.delivery.status_update import StatusUpdateHandler
from staff_bot.utils.formatters import get_cod_cash_projection


# ---------------------------------------------------------------------------
# Helper: replicate the trigger logic from initiate_status_change so we can
# unit-test it without mocking PTB context objects.
# ---------------------------------------------------------------------------

_ELECTRONIC_METHODS = {'click', 'payme', 'card'}
_SETTLED_STATUSES = {'completed', 'paid', 'partially_paid'}


def _should_prompt_for_cash(delivery_info: dict) -> tuple[bool, float]:
    """
    Return (should_prompt, cash_due_amount) following the logic in
    StatusUpdateHandler.initiate_status_change.
    """
    payment_method = delivery_info.get('payment_method', '')
    cash_due_amount = StatusUpdateHandler._get_expected_cash_to_collect(delivery_info)
    payment_status_lower = str(delivery_info.get('payment_status') or '').lower()
    is_unsettled_electronic = (
        payment_method in _ELECTRONIC_METHODS
        and payment_status_lower not in _SETTLED_STATUSES
    )
    if is_unsettled_electronic:
        cash_due_amount = float(delivery_info.get('total_amount') or 0)

    should_prompt = (payment_method == 'cash' and cash_due_amount > 0) or is_unsettled_electronic
    return should_prompt, cash_due_amount


# ---------------------------------------------------------------------------
# COD (cash) — existing behaviour must be unchanged
# ---------------------------------------------------------------------------

def test_cash_order_outstanding_prompts():
    """Original COD flow: prompt when payment_method=cash and cash_due > 0."""
    should, amount = _should_prompt_for_cash({
        'payment_method': 'cash',
        'payment_status': 'pending',
        'outstanding_amount': 36000,
        'total_amount': 36000,
        'expected_cash_to_collect': 36000,
        'cod_reserved_prepayment_amount': 0,
    })
    assert should is True
    assert amount == 36000


def test_cash_order_fully_settled_no_prompt():
    """Already-collected COD order must NOT re-prompt (expected_cash_to_collect == 0)."""
    should, amount = _should_prompt_for_cash({
        'payment_method': 'cash',
        'payment_status': 'completed',
        'outstanding_amount': 0,
        'total_amount': 36000,
        'expected_cash_to_collect': 0,
        'cod_reserved_prepayment_amount': 0,
    })
    assert should is False


# ---------------------------------------------------------------------------
# Unsuccessful-electronic orders — new behaviour
# ---------------------------------------------------------------------------

def test_click_pending_payment_prompts_for_cash():
    """Click order with PENDING payment → driver must be asked for cash."""
    should, amount = _should_prompt_for_cash({
        'payment_method': 'click',
        'payment_status': 'pending',
        'total_amount': 36000,
        'outstanding_amount': 36000,
        'expected_cash_to_collect': 0,  # no COD projection set
        'cod_reserved_prepayment_amount': 0,
    })
    assert should is True
    assert amount == 36000


def test_click_cancelled_payment_prompts_for_cash():
    """Click order with CANCELLED payment (timeout) → driver must be asked for cash."""
    should, amount = _should_prompt_for_cash({
        'payment_method': 'click',
        'payment_status': 'cancelled',
        'total_amount': 45000,
        'outstanding_amount': 45000,
        'expected_cash_to_collect': 0,
        'cod_reserved_prepayment_amount': 0,
    })
    assert should is True
    assert amount == 45000


def test_payme_failed_payment_prompts_for_cash():
    """Payme order with FAILED payment → driver must be asked for cash."""
    should, amount = _should_prompt_for_cash({
        'payment_method': 'payme',
        'payment_status': 'failed',
        'total_amount': 25000,
        'outstanding_amount': 25000,
        'expected_cash_to_collect': 0,
        'cod_reserved_prepayment_amount': 0,
    })
    assert should is True
    assert amount == 25000


def test_card_pending_payment_prompts_for_cash():
    """Card order with PENDING payment → driver must be asked for cash."""
    should, amount = _should_prompt_for_cash({
        'payment_method': 'card',
        'payment_status': 'pending',
        'total_amount': 18000,
        'outstanding_amount': 18000,
        'expected_cash_to_collect': 0,
        'cod_reserved_prepayment_amount': 0,
    })
    assert should is True
    assert amount == 18000


def test_click_completed_payment_no_cash_prompt():
    """Click order whose payment already COMPLETED → no cash prompt (paid online)."""
    should, _ = _should_prompt_for_cash({
        'payment_method': 'click',
        'payment_status': 'completed',
        'total_amount': 36000,
        'outstanding_amount': 0,
        'expected_cash_to_collect': 0,
        'cod_reserved_prepayment_amount': 0,
    })
    assert should is False


def test_payme_partially_paid_no_extra_cash_prompt():
    """Payme partially_paid → still treated as settled; no cash-at-door prompt."""
    should, _ = _should_prompt_for_cash({
        'payment_method': 'payme',
        'payment_status': 'partially_paid',
        'total_amount': 50000,
        'outstanding_amount': 10000,
        'expected_cash_to_collect': 0,
        'cod_reserved_prepayment_amount': 0,
    })
    assert should is False


def test_empty_payment_status_treated_as_unsettled():
    """Missing/None payment_status on an electronic order → treated as unsettled."""
    should, amount = _should_prompt_for_cash({
        'payment_method': 'click',
        'payment_status': None,
        'total_amount': 30000,
        'outstanding_amount': 30000,
        'expected_cash_to_collect': 0,
        'cod_reserved_prepayment_amount': 0,
    })
    assert should is True
    assert amount == 30000


def test_cash_amount_uses_total_amount_for_electronic():
    """For electronic orders the prompt amount comes from total_amount, not projection."""
    should, amount = _should_prompt_for_cash({
        'payment_method': 'click',
        'payment_status': 'pending',
        'total_amount': 72000,
        # outstanding_amount and expected_cash_to_collect are both 0
        # (as the API sends them for non-COD orders)
        'outstanding_amount': 0,
        'expected_cash_to_collect': 0,
        'cod_reserved_prepayment_amount': 0,
    })
    assert should is True
    assert amount == 72000
