"""The admin cash-correction preview warns about the cap the engine enforces.

A correction that re-opens a few hundred sum of debt does not lock anyone out of
COD, so the preview must not tell the admin that it does. The warning reads the
NET scope totals the restriction context publishes; it never re-derives them.
"""

from decimal import Decimal

import pytest

from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.order_cash_edit_service import OrderCashEditService
from shared.business_config import COD_ACTIVE_DEBT_LIMIT
from shared.enums import CashCollectionSource
from tests.unit._scope_money_helpers import delivered_cod_order, make_address, make_user

_WARNING = "correction_pushes_cod_over_cap"


def _settle_via_correction_target(db, admin, order, *, total):
    """Settle `order` in full through the real collection path, then retarget
    the event onto DELIVERY_COMPLETION so OrderCashEditService._resolve_event
    picks it up (mirrors tests/unit/test_corrections_frozen_scope.py — posting
    directly as DELIVERY_COMPLETION would require a Delivery row this preview
    path never touches).

    `order` must be the OLDEST open debt in its scope: `_allocate_scoped`
    settles oldest-first, so a target created after the background debts would
    have its cash swallowed by one of THEM instead (test_corrections_frozen_scope
    calls this out explicitly: "Target is the OLDEST, so the collection settles
    it rather than the other debt")."""
    svc = CashCollectionService()
    event = svc.post_collection(
        customer_id=order.user_id,
        amount=total,
        source="standalone_meeting",
        order_id=order.id,
        recorded_by_user_id=admin.id,
        notes="collected",
    )
    event.source = CashCollectionSource.DELIVERY_COMPLETION
    db.session.commit()
    return event


@pytest.mark.unit
class TestCapWarningAmountArm:
    def test_no_warning_when_the_reopened_debt_is_tiny(self, db):
        """LIMIT-1 shortfalls of 280 plus one more: at the count limit, far under
        the amount floor. Nobody is locked out, so nobody is warned."""
        u, admin = make_user(db), make_user(db)
        a = make_address(db, u)
        order, _payment = delivered_cod_order(db, u, address=a, total=Decimal("280.00"))
        for _ in range(COD_ACTIVE_DEBT_LIMIT - 1):
            delivered_cod_order(db, u, address=a, total=Decimal("280.00"))
        _settle_via_correction_target(db, admin, order, total=Decimal("280.00"))

        plan = OrderCashEditService().preview(order_id=order.id, new_amount=Decimal("0.00"))
        assert not any(w.startswith(_WARNING) for w in plan.warnings), plan.warnings

    def test_warning_when_the_reopened_debt_is_real(self, db):
        u, admin = make_user(db), make_user(db)
        a = make_address(db, u)
        order, _payment = delivered_cod_order(db, u, address=a, total=Decimal("6000.00"))
        for _ in range(COD_ACTIVE_DEBT_LIMIT - 1):
            delivered_cod_order(db, u, address=a, total=Decimal("6000.00"))
        _settle_via_correction_target(db, admin, order, total=Decimal("6000.00"))

        plan = OrderCashEditService().preview(order_id=order.id, new_amount=Decimal("0.00"))
        assert any(w.startswith(_WARNING) for w in plan.warnings), plan.warnings

    def test_exempt_cluster_is_never_warned(self, db):
        u, admin = make_user(db, exempt=True), make_user(db)
        a = make_address(db, u)
        order, _payment = delivered_cod_order(db, u, address=a, total=Decimal("6000.00"))
        for _ in range(COD_ACTIVE_DEBT_LIMIT - 1):
            delivered_cod_order(db, u, address=a, total=Decimal("6000.00"))
        _settle_via_correction_target(db, admin, order, total=Decimal("6000.00"))

        plan = OrderCashEditService().preview(order_id=order.id, new_amount=Decimal("0.00"))
        assert not any(w.startswith(_WARNING) for w in plan.warnings), plan.warnings
