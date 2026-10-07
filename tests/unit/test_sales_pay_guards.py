"""Static guards for decisions the pay module shares, which no single route can pin.

Compensation spec §4.13, §5.6, T-VISIB-4.

* `SALES_PAY_SELF_DECISION` has one raise site, `pay_rules.check_self_decision` (§4.13). A
  second raise site is a second copy of the D-Q11 rule, free to drift from the check-mode twin
  that publishes `can_decide`.
* No pay service reaches a manager (C11). A pay module that names `UserRole.MANAGER`, or the
  in-app notification service, is how an amount leaks into a manager's inbox. The agent is
  told through `services/sales/notifications.py` alone.
* A penalty type is named in exactly the languages the service writes (§4.11). The payload
  schema and the writer read one tuple.
"""

import re
from pathlib import Path

import pytest

from business_app.serializers.sales_pay_serializers import PenaltyTypeNamesPayload
from business_app.services.sales.pay_penalty_service import PENALTY_TYPE_NAME_LANGUAGES

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
# The two raise-site shapes `test_staff_error_code_coverage._CODE` recognises.
_RAISES_SELF_DECISION = re.compile(
    r"""error_code\s*=\s*["']SALES_PAY_SELF_DECISION["']|["']error_code["']\s*:\s*["']SALES_PAY_SELF_DECISION["']"""
)
PAY_SERVICES = sorted((ROOT / "business_app" / "services" / "sales").glob("pay_*.py"))


def test_the_self_decision_refusal_has_one_raise_site():
    raising = sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "business_app").rglob("*.py")
        if _RAISES_SELF_DECISION.search(path.read_text(encoding="utf-8"))
    )

    assert raising == ["business_app/services/sales/pay_rules.py"]


def test_every_pay_module_is_scanned():
    """The parametrized guard below reads the directory, so an empty glob would pass silently."""
    assert {path.name for path in PAY_SERVICES} >= {
        "pay_rules.py",
        "pay_calculator.py",
        "pay_plan_service.py",
        "pay_terms_service.py",
        "pay_period_service.py",
        "pay_ledger_service.py",
        "pay_bonus_service.py",
        "pay_statement_service.py",
        "pay_penalty_service.py",
    }


@pytest.mark.parametrize("path", PAY_SERVICES, ids=lambda path: path.name)
def test_no_pay_service_reaches_a_manager(path):
    source = path.read_text(encoding="utf-8")

    assert "UserRole.MANAGER" not in source
    assert "NotificationService" not in source


def test_a_penalty_type_is_named_in_exactly_the_languages_the_service_writes():
    assert tuple(PenaltyTypeNamesPayload.model_fields) == PENALTY_TYPE_NAME_LANGUAGES
