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
from staff_bot.utils.formatters import get_cod_cash_projection, has_cash_due


def _should_prompt_for_cash(delivery_info: dict) -> tuple[bool, float]:
    """Return (should_prompt, cash_due_amount) exactly as initiate_status_change does.

    🔴 THIS CALLS THE PRODUCTION PREDICATE. It used to RE-IMPLEMENT it — a local
    copy of `_ELECTRONIC_METHODS`, `_SETTLED_STATUSES` and the boolean — which
    meant every test in this file kept passing while production diverged from
    them. One of them (`test_payme_partially_paid_no_extra_cash_prompt`) pinned
    the prod-961 defect as CORRECT behaviour: a payme order with 10,000
    outstanding asserted to produce no cash prompt.

    Never inline the rule here again.
    """
    cash_due_amount = StatusUpdateHandler._get_expected_cash_to_collect(delivery_info)
    return has_cash_due(delivery_info), cash_due_amount


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
        'expected_cash_to_collect': 36000,
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
        'expected_cash_to_collect': 45000,
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
        'expected_cash_to_collect': 25000,
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
        'expected_cash_to_collect': 18000,
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


def test_payme_partially_paid_prompts_for_the_outstanding_delta():
    """Prod order 961 shape: part-paid online, the rest due in cash at the door.

    Asserted the OTHER WAY ROUND until 2026-08-08 — this exact payload is what
    the defect looked like, pinned as correct behaviour. The backend now sends a
    truthful `expected_cash_to_collect` for every rail, so the prompt fires for
    the delta and only the delta.
    """
    should, amount = _should_prompt_for_cash({
        'payment_method': 'payme',
        'payment_status': 'partially_paid',
        'total_amount': 50000,
        'outstanding_amount': 10000,
        'expected_cash_to_collect': 10000,
        'cod_reserved_prepayment_amount': 0,
    })
    assert should is True
    assert amount == 10000


def test_empty_payment_status_treated_as_unsettled():
    """Missing/None payment_status on an electronic order → treated as unsettled."""
    should, amount = _should_prompt_for_cash({
        'payment_method': 'click',
        'payment_status': None,
        'total_amount': 30000,
        'outstanding_amount': 30000,
        'expected_cash_to_collect': 30000,
        'cod_reserved_prepayment_amount': 0,
    })
    assert should is True
    assert amount == 30000


def test_cash_amount_never_falls_back_to_total_amount_for_electronic():
    """The prompt amount is the SERVER figure, never `total_amount`.

    Inverted on 2026-08-08. This used to assert the opposite — that an
    electronic order's prompt amount comes from `total_amount` — because the API
    sent `expected_cash_to_collect: 0` for every non-COD order and the bot
    compensated with an override. `StaffService.get_cod_collection_projection`
    is now rail-truthful, so the override is gone: keeping it would ask a
    customer who already paid 60,000 of a 90,000 order by card to hand over
    90,000 in cash at the door.

    Here the server says nothing is due, so nothing is prompted — even though
    `total_amount` is 72,000.
    """
    should, amount = _should_prompt_for_cash({
        'payment_method': 'click',
        'payment_status': 'completed',
        'total_amount': 72000,
        'outstanding_amount': 0,
        'expected_cash_to_collect': 0,
        'cod_reserved_prepayment_amount': 0,
    })
    assert should is False
    assert amount == 0
