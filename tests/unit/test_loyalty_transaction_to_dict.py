"""LoyaltyTransaction.to_dict must expose ``action_type`` (from extra_data).

Clients that render a customer-facing history (the Telegram bot) need a stable,
machine-readable category to localize labels — the coarse ``transaction_type``
(BONUS covers referral/welcome/birthday; EARNED covers purchase/streak) is not
enough on its own, and the free-text English ``description`` should not be shown
to customers.
"""

import pytest

from business_app.models.loyalty import LoyaltyTransaction
from business_app.utils.constants import LoyaltyTransactionType


@pytest.mark.unit
def test_to_dict_exposes_action_type_from_extra_data():
    txn = LoyaltyTransaction(
        transaction_type=LoyaltyTransactionType.BONUS,
        points=500,
        description="Referral bonus for user #75",
        extra_data={"action_type": "referral"},
    )
    assert txn.to_dict()["action_type"] == "referral"


@pytest.mark.unit
def test_to_dict_action_type_none_when_absent():
    # A redemption stores no action_type; the field must be present and None,
    # not missing, so clients can rely on it.
    txn = LoyaltyTransaction(
        transaction_type=LoyaltyTransactionType.REDEEMED,
        points=-4000,
        description="Redeemed reward: 19 litrlik suv",
    )
    data = txn.to_dict()
    assert "action_type" in data
    assert data["action_type"] is None
