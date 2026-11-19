"""The one self-decision rule shared by sales pay and driver expenses."""

import pytest

from business_app.services.self_decision import check_self_decision
from business_app.utils.exceptions import ForbiddenError


def test_the_shared_rule_raises_the_callers_code(db, manager_user, admin_user):
    with pytest.raises(ForbiddenError) as exc:
        check_self_decision(
            manager_user.id,
            manager_user,
            error_code="DRIVER_EXPENSE_SELF_DECISION",
            message="m",
            details={"expense_id": 7},
        )
    assert (exc.value.error_code, exc.value.details) == ("DRIVER_EXPENSE_SELF_DECISION", {"expense_id": 7})
    assert check_self_decision(admin_user.id, admin_user, error_code="X", message="m") is True
    assert check_self_decision(admin_user.id, manager_user, error_code="X", message="m") is False
