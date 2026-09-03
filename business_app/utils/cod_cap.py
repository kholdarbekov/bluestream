"""The COD debt cap predicate — the ONE place "is this scope capped" is decided.

Two arms, both required. A scope (a customer's linked cluster, or a grouped
place) is capped only when it carries at least ``COD_ACTIVE_DEBT_LIMIT`` open
delivered COD debts AND those debts together exceed
``COD_DEBT_AMOUNT_THRESHOLD`` in NET open receivable.

``net_debt_total`` must ALWAYS be net of reserved prepayment — i.e. summed from
``business_app.utils.payment_projection.net_open_receivable_amount`` — never a
gross ``Payment.outstanding_amount`` sum. Gating on gross where a prepayment
reservation exists tells a customer they owe money they have already handed
over, and is the documented cause of a prior production incident.

Pure module: no Flask, no SQLAlchemy, no service imports. Every surface that
offers cash and every guard that accepts it calls THIS, so a widened offer and
the write path that must honour it can never drift apart.
"""

from decimal import Decimal
from typing import Any, Dict

from shared.business_config import COD_ACTIVE_DEBT_LIMIT, COD_DEBT_AMOUNT_THRESHOLD


def cod_cap_reached(debt_count: int, net_debt_total: Decimal) -> bool:
    """True when BOTH cap arms fire for one scope.

    ``net_debt_total`` is coerced through ``str`` rather than compared directly:
    read surfaces publish money as ``float`` and a binary-float comparison
    against the threshold is not the comparison this rule means.
    """
    if int(debt_count or 0) < COD_ACTIVE_DEBT_LIMIT:
        return False
    return Decimal(str(net_debt_total or 0)) > Decimal(COD_DEBT_AMOUNT_THRESHOLD)


# The one key `CashCollectionService.get_cod_restriction_context` publishes
# that is NOT the customer's own money.
_PLACE_SCOPE_MONEY_KEY = "place_net_open_cod_debt_total"


def strip_place_scope_money_for_customer(context: Dict[str, Any]) -> Dict[str, Any]:
    """Redact the one figure in a COD restriction context a customer must never see.

    ``get_cod_restriction_context`` publishes two NET totals: the customer's
    own linked-cluster money (theirs — safe to show) and, when the delivery
    address is grouped, the PLACE's — summed over every member's open COD
    debts, including coworkers who are not this customer. In a two-member
    address group that figure is one coworker's exact outstanding balance, so
    it must never reach a customer-facing response. Spec §7's privacy
    boundary lets only the place-scope COUNT cross; this is that boundary for
    the money.

    Every customer-authenticated route that returns this context as
    ``payment_restrictions`` must call this before returning it. Internal /
    staff callers — ``order_cash_edit_service``, and the staff- and
    admin-only payment-methods endpoints (``require_staff_roles`` /
    ``manager_or_higher_required``) — call ``get_cod_restriction_context``
    directly and keep the full dict: the boundary is "does a customer see
    this response", never "was this money computed at all".
    """
    return {key: value for key, value in context.items() if key != _PLACE_SCOPE_MONEY_KEY}
