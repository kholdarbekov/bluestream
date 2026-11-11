"""The driver's one question about a shared-place case (spec 2026-11-10 §4.4; §9 binds).

`question_for_delivery` is a plain read. The two writers lock the case row, so a double tap or a lost ack that
replays the same body is a no-op, and an answer racing an admin's resolution is refused rather than overwritten.
"""

import logging
from typing import Optional

from business_app import db
from business_app.models.shared_place_case import SHARED_PLACE_OPEN_STATUSES, SharedPlaceCase
from business_app.utils.exceptions import ConflictError, NotFoundError, ValidationError
from business_app.utils.shared_place_case_dispatch import queue_case_alert
from business_app.utils.timezone_utils import get_utc_now
from business_app.utils.transactions import transactional
from shared import business_config

logger = logging.getLogger(__name__)

# The staff bot refuses a longer note before sending it, from the same value.
NOTE_MAX_CHARS = business_config.SHARED_PLACE_DRIVER_NOTE_MAX_CHARS


def _not_found() -> NotFoundError:
    return NotFoundError("Shared-place case not found", error_code="SHARED_PLACE_CASE_NOT_FOUND")


def _invalid(message: str) -> ValidationError:
    return ValidationError(message, error_code="SHARED_PLACE_ANSWER_INVALID")


def _refused(message: str) -> ConflictError:
    return ConflictError(message, error_code="SHARED_PLACE_ANSWER_REFUSED")


def _clean_note(note: Optional[str]) -> str:
    """The stripped note; 1-500 characters."""
    text = (note or "").strip()
    if not text or len(text) > NOTE_MAX_CHARS:
        raise _invalid(f"The note must be 1-{NOTE_MAX_CHARS} characters")
    return text


def _locked_case_of_driver(case_id: int, driver_user_id: int) -> SharedPlaceCase:
    case = (
        db.session.query(SharedPlaceCase)
        .filter(SharedPlaceCase.id == case_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if case is None or case.asked_driver_id != driver_user_id:
        raise _not_found()
    return case


def _result(case: SharedPlaceCase) -> dict:
    picked = None
    if case.driver_answer == "picked":
        picked = case.candidates[case.picked_index]["first_name"]
    return {
        "case_id": case.id,
        "answer": case.driver_answer,
        "picked_first_name": picked,
        "note_saved": bool(case.driver_note),
    }


class SharedPlaceDriverStep:
    """What the asked driver sees and sends."""

    @staticmethod
    def question_for_delivery(delivery_id: int, driver_user_id: int) -> dict:
        case = (
            SharedPlaceCase.query.filter(SharedPlaceCase.delivery_id == delivery_id)
            .order_by(SharedPlaceCase.id)
            .first()
        )
        if (
            case is None
            or case.status != "awaiting_driver"
            or case.driver_answer is not None
            or case.asked_driver_id != driver_user_id
        ):
            return {"ask": False}
        return {
            "ask": True,
            "case_id": case.id,
            "customer_first_name": case.order.user.first_name,
            "candidates": [
                {
                    "index": index,
                    "first_name": candidate["first_name"],
                    "empties": candidate["empties"],
                    "distance_m": candidate["distance_m"],
                }
                for index, candidate in enumerate(case.candidates)
            ],
        }

    @staticmethod
    @transactional
    def answer(
        case_id: int,
        driver_user_id: int,
        *,
        answer: str,
        candidate_index: Optional[int],
        note: Optional[str],
    ) -> dict:
        case = _locked_case_of_driver(case_id, driver_user_id)

        if answer == "picked":
            if candidate_index is None or not 0 <= candidate_index < len(case.candidates):
                raise _invalid("Pick one of the places offered")
        elif candidate_index is not None:
            raise _invalid("This answer takes no place")
        if note is not None and answer == "skipped":
            raise _invalid("A skipped report takes no note")
        clean_note = _clean_note(note) if note is not None else None

        if case.driver_answer is not None:
            if (
                case.driver_answer == answer
                and case.picked_index == candidate_index
                and (clean_note is None or clean_note == case.driver_note)
            ):
                return _result(case)
            raise _refused("This report already has a different answer")
        if case.status != "awaiting_driver":
            raise _refused("This report is already settled")
        # Write-once: a note stored by `add_note` before this answer is kept, and a different one is refused.
        if case.driver_note is not None and clean_note is not None and clean_note != case.driver_note:
            raise _refused("This report already has a different note")

        case.driver_answer = answer
        case.picked_index = candidate_index
        if clean_note is not None:
            case.driver_note = clean_note
        case.answered_at = get_utc_now()
        case.status = "awaiting_admin"
        db.session.flush()
        queue_case_alert(case.id, countdown=0)
        logger.info("[SHARED_PLACE] driver_answered case=%s answer=%s", case.id, answer)
        return _result(case)

    @staticmethod
    @transactional
    def add_note(case_id: int, driver_user_id: int, note: Optional[str]) -> dict:
        case = _locked_case_of_driver(case_id, driver_user_id)
        clean_note = _clean_note(note)

        if case.driver_note == clean_note:
            return {"case_id": case.id, "note_saved": True}
        if case.driver_note is not None or case.status not in SHARED_PLACE_OPEN_STATUSES:
            raise _refused("This report already has a note or is settled")

        case.driver_note = clean_note
        db.session.flush()
        logger.info("[SHARED_PLACE] driver_note_saved case=%s", case.id)
        return {"case_id": case.id, "note_saved": True}
