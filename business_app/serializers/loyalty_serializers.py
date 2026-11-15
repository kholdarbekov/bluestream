"""Loyalty serialization helpers.

Single source of truth for loyalty serialization is the model ``to_dict``
methods (``LoyaltyProgram.to_dict`` / ``LoyaltyReward.to_dict`` /
``LoyaltyTransaction.to_dict``). The program/reward helpers below delegate to
those; the transaction helper adds ``user_id`` for the admin/customer payloads.

The previous module carried ~16 orphan Pydantic schemas and ~10 orphan helper
functions (referencing a nonexistent ``UserLoyalty`` model and phantom fields
like ``points_per_currency`` / ``referral_points``). They were removed in the
loyalty SSOT cleanup (Phase 2); the wired code only ever used the three
serializers below.
"""

from typing import Any, Dict, Optional

# A transaction's stable display category, by its granular ``action_type`` (extra_data), then its coarse
# ``transaction_type``. The ONE mapping: the website labels history rows by it and the customer bot reads the
# published ``display_category`` (telegram_bot/handlers/loyalty.py), so a client labels a row in its own language
# instead of printing the English ``description``.
_ACTION_CATEGORIES = {
    "referral": "referral",
    "welcome_bonus": "welcome",
    "birthday_bonus": "birthday",
    "streak_bonus": "streak",
    "surprise_reward": "surprise",
    "purchase": "order_earn",
    "reward_refund": "refund",
    "order_edit_reversal": "adjustment",
    "order_edit_award": "adjustment",
}
_TYPE_CATEGORIES = {
    "redeemed": "redeem",
    "earned": "order_earn",
    "bonus": "bonus",
    "expired": "expired",
    "adjustment": "adjustment",
}

# The history filter (/my-loyalty's chips, GET /loyalty/points/history?group=): each group lists the display
# categories it shows, so the chip a row sits under and the label it carries come from the one classification above.
# Refunds and order-edit adjustments are listed only under "all".
HISTORY_GROUPS = {
    "earned": frozenset({"order_earn"}),
    "bonus": frozenset({"bonus", "referral", "welcome", "birthday", "streak", "surprise"}),
    "redeemed": frozenset({"redeem"}),
    "expired": frozenset({"expired"}),
}

# LoyaltyService.apply_reward_to_order writes the deduction's description as "Redeemed reward: <name>".
_REDEEMED_REWARD_PREFIX = "Redeemed reward:"


def display_category(action_type: Optional[str], transaction_type: Optional[str]) -> str:
    """The display category of a transaction from its ``action_type`` and ``transaction_type``.

    ``other`` when unknown.
    """
    action = (action_type or "").lower()
    if action in _ACTION_CATEGORIES:
        return _ACTION_CATEGORIES[action]
    return _TYPE_CATEGORIES.get((transaction_type or "").lower(), "other")


def transaction_display_category(data: Dict[str, Any]) -> str:
    """The display category of a serialized transaction (``LoyaltyTransaction.to_dict``)."""
    return display_category(data.get("action_type"), data.get("transaction_type"))


def _reward_name(data: Dict[str, Any], category: str) -> Optional[str]:
    """The redeemed reward's name, from the deduction's own description format; None for any other row."""
    description = data.get("description") or ""
    if category != "redeem" or not description.startswith(_REDEEMED_REWARD_PREFIX):
        return None
    return description[len(_REDEEMED_REWARD_PREFIX) :].strip() or None


def serialize_loyalty_transaction(transaction) -> Dict[str, Any]:
    """Serialize a loyalty transaction: model ``to_dict`` + ``user_id`` + the display fields clients label it by
    (``display_category``, the redeemed ``reward_name`` and the ``order_number`` of the order it belongs to)."""
    data = transaction.to_dict()
    data["user_id"] = getattr(transaction, "user_id", None)
    data["display_category"] = transaction_display_category(data)
    data["reward_name"] = _reward_name(data, data["display_category"])
    order = getattr(transaction, "order", None) if data.get("order_id") else None
    data["order_number"] = getattr(order, "order_number", None)
    return data


def serialize_loyalty_reward(reward, user=None, language=None) -> Dict[str, Any]:
    """Serialize a reward via the model SSOT (``LoyaltyReward.to_dict``).

    ``user`` is accepted for call-site compatibility; per-user fields
    (``can_redeem`` / ``points_needed``) are layered on by the API route.
    """
    return reward.to_dict(language)


def serialize_loyalty_program(program) -> Dict[str, Any]:
    """Serialize a program via the model SSOT (``LoyaltyProgram.to_dict``)."""
    return program.to_dict()
