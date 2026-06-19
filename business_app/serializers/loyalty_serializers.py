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

from typing import Any, Dict


def serialize_loyalty_transaction(transaction) -> Dict[str, Any]:
    """Serialize a loyalty transaction (model ``to_dict`` + ``user_id``)."""
    data = transaction.to_dict()
    data["user_id"] = getattr(transaction, "user_id", None)
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
