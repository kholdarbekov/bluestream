"""A prepayment return paid from the driver's trip cash cannot eat into a claim still waiting (spec 2026-11-17).

The return's cap is the trip's "Hand in now" (`cash_held_now`): on hand minus handed in minus the claims waiting.
Every call goes through the admin payout routes the Prepayments page calls, with the body it sends.
"""

import pytest

from shared.enums import DriverCashSessionStatus
from tests.integration._driver_expense_helpers import claim
from tests.integration._prepayment_payout_helpers import (
    apply_payout,
    collect_on_trip,
    from_trip,
    preview_payout,
    stored,
)

pytestmark = pytest.mark.integration


def test_the_return_cap_is_net_of_waiting_claims(
    app, client, db, admin_claim_headers, sample_user, second_delivery_driver
):
    driver = second_delivery_driver
    sid = collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 100000)
    status, body = claim(client, app, driver, 30000)
    assert status == 201, body

    _, preview = preview_payout(client, admin_claim_headers, sample_user.id, **from_trip(driver, amount=70001))
    eff = preview["data"]["session_effect"]
    assert (eff["cap"], eff["expenses_waiting"]) == (70000.0, 30000.0)
    assert (eff["on_hand_before"], eff["handed_in"], eff["on_hand_after"]) == (100000.0, 0.0, 29999.0)
    assert preview["data"]["blocking_reasons"][0].startswith("amount_exceeds_driver_cash: ")

    status, body = apply_payout(
        client, admin_claim_headers, sample_user.id, key="net", **from_trip(driver, amount=70000)
    )
    assert status == 201, body
    # The claim's 30000 keeps the trip open.
    assert stored(db, sid).status == DriverCashSessionStatus.OPEN


def test_with_no_claim_waiting_the_cap_is_unchanged(
    app, client, db, admin_claim_headers, sample_user, second_delivery_driver
):
    driver = second_delivery_driver
    collect_on_trip(client, admin_claim_headers, sample_user.id, driver.id, 100000)

    _, preview = preview_payout(client, admin_claim_headers, sample_user.id, **from_trip(driver, amount=70000))

    eff = preview["data"]["session_effect"]
    assert (eff["cap"], eff["expenses_waiting"]) == (100000.0, 0.0)
