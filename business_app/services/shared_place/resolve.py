"""The admin's resolution of a shared-place case (spec 2026-11-10 §4.5; §9 binds): one call settles a case with one
of six outcomes.

- **A (shared place), B (same home) and C (another place)** are verified against live state: the grouping and the
  link the admin made through the existing dialogs. A and B are never gated by what the screen offers
  (`allowed_choices`), or a grouping the admin just made would forbid recording it.
- **C with a count, D (own bottles) and E (wrong count)** book their bottle moves here, through `admin_adjust_balance`,
  keyed `shared_place:{case_id}:{leg}` with "Case #id" in the notes.
- **Close** records no kind; its payload (`{"kind": "close"}`) is stored like every other claim.

`admin_adjust_balance` commits on its own, so the case lock cannot be held across the moves. The outcome is first
CLAIMED under the case row lock (`resolution_kind` + `resolution_payload`), the case is re-locked before each leg,
and it is settled under a final lock. A different payload while a claim exists is a 409, and a stored claim is not
re-verified: bottles may already have moved. There is no way out of a claim but to repeat it; the legs are keyed,
so a repeat always finishes.

A replay of the stored outcome is a no-op that answers the case. The claim compared is the payload without the
reason and without empty fields.
"""

import logging
from decimal import Decimal
from typing import List, Optional, Tuple

from business_app import db
from business_app.models.shared_place_case import SHARED_PLACE_OPEN_STATUSES, SharedPlaceCase
from business_app.models.user import UserAddress
from business_app.services.bottle_tracking_service import BottleTrackingService
from business_app.services.customer_link_service import CustomerLinkService
from business_app.services.shared_place.review import SharedPlaceReview, allowed_choices
from business_app.utils.exceptions import ConflictError, NotFoundError, ValidationError
from business_app.utils.timezone_utils import get_utc_now
from business_app.utils.transactions import atomic_transaction

logger = logging.getLogger(__name__)

# Verified against live state only, never gated by the screen's offer (spec §9). Both need the live grouping.
_VERIFIED_ONLY = ("shared_place", "same_home")
# Both need the named account linked to the customer now.
_LINKED_KINDS = ("same_home", "other_place")

Leg = Tuple[str, int, int]  # (name, address_id, quantity)


def _invalid(message: str) -> ValidationError:
    return ValidationError(message, error_code="SHARED_PLACE_RESOLVE_INVALID")


def _not_verified(message: str) -> ConflictError:
    return ConflictError(message, error_code="SHARED_PLACE_NOT_VERIFIED")


def _conflict(message: str) -> ConflictError:
    return ConflictError(message, error_code="SHARED_PLACE_RESOLUTION_CONFLICT")


def _locked_case(case_id: int) -> SharedPlaceCase:
    case = (
        db.session.query(SharedPlaceCase)
        .filter(SharedPlaceCase.id == case_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if case is None:
        raise NotFoundError("Shared-place case not found", error_code="SHARED_PLACE_CASE_NOT_FOUND")
    return case


def _settled_as(case: SharedPlaceCase, kind: str, claim: dict) -> bool:
    """True when the case is already settled with exactly this outcome (a replay). Open: False. Settled with any
    other outcome: a 409."""
    if case.status in SHARED_PLACE_OPEN_STATUSES:
        return False
    expected_status = "closed" if kind == "close" else "resolved"
    if case.status == expected_status and case.resolution_payload == claim:
        return True
    raise _conflict("This case is already settled with another outcome")


def _verify_grouped(case: SharedPlaceCase, target_address_id: Optional[int], links: CustomerLinkService) -> None:
    """A and B: the case address is in a live place group, with the target address when one is named. The same
    predicate decides the screen's `grouping_done`."""
    grouped = links.is_in_live_place_group(case.address_id)
    if not grouped or (target_address_id is not None and not links.same_place(case.address_id, target_address_id)):
        raise _not_verified("The address is not in one place group with the claimed place")


def _verify_linked(case: SharedPlaceCase, target_user_id: Optional[int], links: CustomerLinkService) -> None:
    """B and C: the named account is linked to the case's customer now."""
    if target_user_id is None:
        raise _invalid("Name the customer's linked account")
    if not links.are_linked(case.order.user_id, target_user_id):
        raise _not_verified("The two accounts are not linked")


def _verify_move(case: SharedPlaceCase, payload: dict, move_allowed: bool, links: CustomerLinkService) -> None:
    """C with a count: K empties from one of the linked cluster's other places, never more than that place holds."""
    count, source_id = payload["move_count"], payload.get("source_address_id")
    if not move_allowed:
        raise _invalid("Moving empties into a place that is already shared would count them twice")
    if source_id is None:
        raise _invalid("Name the place the empties move from")
    # The source address's OWN owner, the rule the case detail's move_sources publishes.
    source_owner_id = db.session.query(UserAddress.user_id).filter(UserAddress.id == source_id).scalar()
    if source_owner_id is None or source_owner_id not in links.get_cluster_user_ids(case.order.user_id):
        raise _invalid("The empties can only move from a place of the customer's linked accounts")
    if source_id == case.address_id or links.same_place(source_id, case.address_id):
        raise _invalid("The empties must move from another place")
    if not 1 <= count <= int(BottleTrackingService.get_place_balance(source_id)):
        raise _invalid("The count must be between 1 and the empties that place holds")


def _verify(case: SharedPlaceCase, payload: dict, move_allowed: bool) -> None:
    """The claimed outcome against live state, in this order: A's and B's grouping first, then B's and C's named
    account and its link, then C's move or E's count. A missing or out-of-range field is a 400, a grouping or
    link not there is a 409. D and Close have nothing to check."""
    links = CustomerLinkService()
    kind = payload["kind"]
    if kind in _VERIFIED_ONLY:
        _verify_grouped(case, payload.get("target_address_id"), links)
    if kind in _LINKED_KINDS:
        _verify_linked(case, payload.get("target_user_id"), links)
    if kind == "other_place" and payload.get("move_count") is not None:
        _verify_move(case, payload, move_allowed, links)
    elif kind == "wrong_count":
        actual = payload.get("actual_returned")
        if actual is None or actual < 0 or actual == case.returned_qty:
            raise _invalid("Give the number of empties actually handed back, different from the one typed")


def _legs(case: SharedPlaceCase, payload: dict) -> List[Leg]:
    """The bottle moves the outcome books. A, B, a C without a count, and Close book none."""
    kind = payload["kind"]
    if kind == "other_place" and payload.get("move_count") is not None:
        count = payload["move_count"]
        return [("out", payload["source_address_id"], -count), ("in", case.address_id, count)]
    if kind == "own_bottles":
        return [("in", case.address_id, case.returned_qty)]
    if kind == "wrong_count":
        return [("fix", case.address_id, case.returned_qty - payload["actual_returned"])]
    return []


def _finish(case: SharedPlaceCase, kind: str, claim: dict, reason: str, actor_user_id: int) -> Optional[int]:
    """Settle the case: `closed` with no kind for Close, else `resolved` with its kind and the place group now.
    Returns the resulting group id."""
    closing = kind == "close"
    case.status = "closed" if closing else "resolved"
    case.resolution_kind = None if closing else kind
    case.resolution_payload = claim
    case.decision_reason = reason
    case.decided_by_user_id = actor_user_id
    case.decided_at = get_utc_now()
    case.resulting_group_id = None if closing else CustomerLinkService().get_address_place_group_id(case.address_id)
    return case.resulting_group_id


class SharedPlaceResolve:
    """The admin's one call that settles a case."""

    @staticmethod
    def resolve(case_id: int, *, actor_user_id: int, payload: dict) -> dict:
        """Settle the case with `payload` (`kind`, `reason` and the fields of that kind). Returns `{"case": detail}`."""
        kind, reason = payload["kind"], payload["reason"]
        claim = {key: value for key, value in payload.items() if key != "reason" and value is not None}
        settled_now, group_id = False, None

        with atomic_transaction():
            case = _locked_case(case_id)
            legs: List[Leg] = []
            if not _settled_as(case, kind, claim):
                claimed = case.resolution_kind is not None  # only kinds with legs ever claim
                if claimed and case.resolution_payload != claim:
                    raise _conflict("Another outcome is being recorded on this case")
                if not claimed:
                    choices = allowed_choices(case)
                    if kind not in _VERIFIED_ONLY and kind not in choices["kinds"]:
                        raise _invalid("This choice is not offered for this case")
                    _verify(case, payload, choices["move_allowed"])  # skipped when claimed: it passed at the claim
                legs = _legs(case, payload)
                if not legs:
                    # Close, A, B and a C without a count: settled under this one lock.
                    settled_now, group_id = True, _finish(case, kind, claim, reason, actor_user_id)
                elif not claimed:
                    case.resolution_kind, case.resolution_payload = kind, claim  # the claim, committed on exit

        if legs:
            for name, address_id, quantity in legs:
                with atomic_transaction():
                    _locked_case(case_id)  # serialises the legs of concurrent resolves; the leg's commit releases it
                    BottleTrackingService().admin_adjust_balance(
                        None,
                        address_id,
                        Decimal(quantity),
                        actor_user_id,
                        notes=f"Case #{case_id}: {reason}",
                        idempotency_key=f"shared_place:{case_id}:{name}",
                    )
            with atomic_transaction():
                case = _locked_case(case_id)
                if not _settled_as(case, kind, claim):  # settled already: a concurrent repeat of this claim finished
                    settled_now, group_id = True, _finish(case, kind, claim, reason, actor_user_id)

        if settled_now:
            # Close logs too (kind=close, the kind the case detail publishes): spec §4.8 counts every one of the six
            # settlements with one LogQL query. A replay logs nothing.
            logger.info("[SHARED_PLACE] case_resolved case=%s kind=%s group=%s", case_id, kind, group_id)
        return {"case": SharedPlaceReview.case_detail(case_id)}
