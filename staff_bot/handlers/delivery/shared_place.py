"""The shared-place question on the driver's Delivered screen.

When a first-order customer hands back empties, the backend opens a case and the
driver is asked ONE question under the "Delivered" message. This module owns the
question screen (best-effort end to end: the delivery is already recorded and never
waits on it), the tap handler, and the optional typed note that follows an answer.
The text router hands that note to `receive_note` while `pending_shared_place_note`
is armed; the key is in `flow_state.PENDING_FLOW_USER_DATA_KEYS`, so every way out
of a flow drops it, and it never arms `mark_active` (a note holds no money).
"""

import asyncio
import html
import logging
import re
from typing import Any, Mapping, Optional, Tuple

from telegram import InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from shared import business_config
from staff_bot.handlers.base import BaseHandler
from staff_bot.api_client import api_client
from staff_bot.i18n import i18n
from staff_bot.keyboards.common import CommonKeyboards
from staff_bot.keyboards.shared_place import SharedPlaceKeyboards, display_name
from staff_bot.permissions import require_auth, require_delivery_driver

logger = logging.getLogger(__name__)

# The client retries a GET up to three times at 30 s each; the driver at a door
# must not wait on that. Module-level so tests can shorten it.
QUESTION_FETCH_TIMEOUT_S = 5

PENDING_NOTE_KEY = "pending_shared_place_note"
REFUSED_ERROR_CODE = "SHARED_PLACE_ANSWER_REFUSED"
# The backend refuses a longer note too; one value for both.
NOTE_MAX_CHARS = business_config.SHARED_PLACE_DRIVER_NOTE_MAX_CHARS
# A candidate's index is 0..2 (at most three places are offered): the bound the tap regex below routes back.
MAX_CANDIDATE_INDEX = 2
_TAP = re.compile(r"^staff_sp_(\d+)_(p[0-2]|none|skip|done)$")


async def fetch_question(client, token: str, delivery_id: int) -> Optional[dict]:
    """The question's `data` when the backend wants one asked, else None.

    ANY exception, timeout or failed response logs a warning and returns None.
    """
    try:
        response = await asyncio.wait_for(
            client.get_shared_place_question(token, delivery_id),
            timeout=QUESTION_FETCH_TIMEOUT_S,
        )
        if not getattr(response, "success", False):
            logger.warning(
                "shared-place question for delivery %s refused: %s",
                delivery_id,
                getattr(response, "error", None),
            )
            return None
        data = response.data
        if isinstance(data, dict) and data.get("ask") is True:
            return data
        return None
    except Exception as exc:  # noqa: BLE001 -- the delivery never waits on this step
        logger.warning("shared-place question for delivery %s unavailable: %r", delivery_id, exc)
        return None


def _escaped_name(name: Any) -> str:
    """A name for `parse_mode='HTML'` text: escaped, and an em dash when blank."""
    return html.escape(display_name(name), quote=False)


def _an_id(value: Any, what: str) -> int:
    """`value` when it is a real int (a bool is not); a button built from anything else is wrong."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{what} is not an integer: {value!r}")
    return value


def render_question(question: Mapping[str, Any], language: str) -> Tuple[str, InlineKeyboardMarkup, Optional[dict]]:
    """(text, keyboard, pending note state) for a `{"ask": true}` payload.

    The third item is `{"case_id", "mode": "answer_none"}` when there are no
    candidates (the typed note then files the `none` answer), else None.
    Raises on a payload it cannot draw (`question_screen` turns that into no question).
    """
    case_id = _an_id(question.get("case_id"), "case_id")
    candidates = question.get("candidates") or []
    for candidate in candidates:
        index = _an_id(candidate.get("index"), "candidate index")
        if not 0 <= index <= MAX_CANDIDATE_INDEX:
            raise ValueError(f"candidate index out of range: {index!r}")
    name = _escaped_name(question.get("customer_first_name"))
    if candidates:
        text = i18n.get("staff.delivery.shared_place.question", language, name=name)
        return text, SharedPlaceKeyboards.question(case_id, candidates, language), None
    text = i18n.get("staff.delivery.shared_place.no_candidates", language, name=name)
    return (
        text,
        SharedPlaceKeyboards.skip_only(case_id, "skip", language),
        {"case_id": case_id, "mode": "answer_none"},
    )


async def question_screen(
    client, token: str, delivery_id: int, language: str
) -> Optional[Tuple[str, InlineKeyboardMarkup, Optional[dict]]]:
    """`render_question` of the question owed for this delivery, or None.

    Best-effort END TO END, fetch and drawing alike: it runs after the delivery is
    recorded, so a payload the bot cannot draw logs a warning and leaves the plain
    Delivered screen, never an error after a delivery that landed.
    """
    question = await fetch_question(client, token, delivery_id)
    if question is None:
        return None
    try:
        return render_question(question, language)
    except Exception as exc:  # noqa: BLE001 -- the delivery is already recorded
        logger.warning("shared-place question for delivery %s malformed: %r", delivery_id, exc)
        return None


def _disarm(context: ContextTypes.DEFAULT_TYPE, case_id: int) -> None:
    """Drop the note prompt only when it is `case_id`'s.

    Old messages keep their buttons, so the prompt armed now may belong to a NEWER
    case than the one just tapped. The typed note uses the same guard, so ending a
    step can only ever end the case it answered.
    """
    pending = context.user_data.get(PENDING_NOTE_KEY)
    if isinstance(pending, dict) and pending.get("case_id") == case_id:
        context.user_data.pop(PENDING_NOTE_KEY, None)


class SharedPlaceHandler(BaseHandler):
    """Taps on the shared-place question's buttons, and the typed note after them."""

    @require_auth
    @require_delivery_driver
    async def on_tap(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        try:
            match = _TAP.match(query.data or "")
            if match is None:
                return
            case_id, action = int(match.group(1)), match.group(2)
            language = await self._get_language(update, context)

            if action == "done":
                _disarm(context, case_id)
                await self._show_closing(update, i18n.get("staff.delivery.shared_place.thanks", language), language)
                return

            token = await self._get_auth_token(update, context)
            if not token:
                await self._handle_auth_error(update, language)
                return

            if action == "skip":
                answer, candidate_index = "skipped", None
            elif action == "none":
                answer, candidate_index = "none", None
            else:
                answer, candidate_index = "picked", int(action[1:])

            async with api_client as client:
                response = await client.answer_shared_place_case(
                    token, case_id, answer, candidate_index=candidate_index
                )

            if not response.success:
                if response.status_code == 409 and response.error_code == REFUSED_ERROR_CODE:
                    _disarm(context, case_id)
                    await self._show_closing(
                        update,
                        self._resolve_response_error(language, response, html=True),
                        language,
                    )
                    return
                await self._handle_api_response_error(update, response, language)
                return

            if action == "skip":
                _disarm(context, case_id)
                await self._show_closing(update, i18n.get("staff.delivery.shared_place.skipped", language), language)
                return

            if action == "none":
                choice = html.escape(i18n.get("staff.delivery.shared_place.none", language), quote=False)
            else:
                picked = (response.data or {}).get("picked_first_name")
                choice = _escaped_name(picked)
            text = (
                i18n.get("staff.delivery.shared_place.picked", language, choice=choice)
                + "\n"
                + i18n.get("staff.delivery.shared_place.note_prompt", language)
            )
            await self._edit(update, text, SharedPlaceKeyboards.skip_only(case_id, "done", language))
            context.user_data[PENDING_NOTE_KEY] = {
                "case_id": case_id,
                "mode": "note",
                "chat_id": update.effective_chat.id,
                "message_id": query.message.message_id,
            }
        except Exception as exc:  # noqa: BLE001
            logger.error("Error handling shared-place tap: %s", exc, exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def receive_note(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """The driver's typed note for the admin, while `pending_shared_place_note` is armed.

        After a pick or None (`mode == "note"`) it is the write-once note; on the
        no-candidates screen (`mode == "answer_none"`) it files the `none` answer with
        the note in one PUT. A note that is too long is refused here and the prompt
        keeps waiting; any answer from the backend ends the step. Never the
        `staff.flow_timed_out` copy: the answer was already written.
        """
        pending = context.user_data.get(PENDING_NOTE_KEY) or {}
        case_id = pending.get("case_id")
        message = update.effective_message
        try:
            language = await self._get_language(update, context)
            note = (message.text or "").strip()
            if len(note) > NOTE_MAX_CHARS:
                await message.reply_text(
                    i18n.get(
                        "staff.delivery.shared_place.note_too_long",
                        language,
                        max=NOTE_MAX_CHARS,
                    )
                )
                return

            token = await self._get_auth_token(update, context)
            if not token:
                _disarm(context, case_id)
                await self._handle_auth_error(update, language)
                return

            async with api_client as client:
                if pending.get("mode") == "answer_none":
                    response = await client.answer_shared_place_case(token, case_id, "none", note=note)
                else:
                    response = await client.note_shared_place_case(token, case_id, note)
            _disarm(context, case_id)

            back = CommonKeyboards.back_button(language, "staff_active_deliveries")
            if response.success:
                await self._drop_prompt_buttons(context, pending)
                await message.reply_text(i18n.get("staff.delivery.shared_place.thanks", language), reply_markup=back)
                return
            if response.status_code == 409 and response.error_code == REFUSED_ERROR_CODE:
                await self._drop_prompt_buttons(context, pending)
                await message.reply_text(self._resolve_response_error(language, response), reply_markup=back)
                return
            # Not written, so the prompt keeps its Skip: the driver can still close the step there.
            await self._handle_api_response_error(update, response, language)
        except Exception as exc:  # noqa: BLE001
            _disarm(context, case_id)
            logger.error("Error handling shared-place note: %s", exc, exc_info=True)
            await self._handle_error(update, context)

    @staticmethod
    async def _drop_prompt_buttons(context: ContextTypes.DEFAULT_TYPE, pending: Mapping[str, Any]):
        """Best-effort: the prompt's Skip would only re-open a closed step."""
        chat_id, message_id = pending.get("chat_id"), pending.get("message_id")
        if chat_id is None or message_id is None:
            return
        try:
            await context.bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
        except Exception as exc:  # noqa: BLE001 -- the note is saved; the button is cosmetic
            logger.warning(
                "Could not drop the shared-place prompt's buttons (message %s): %r",
                message_id,
                exc,
            )

    async def _show_closing(self, update: Update, text: str, language: str):
        await self._edit(update, text, CommonKeyboards.back_button(language, "staff_active_deliveries"))

    @staticmethod
    async def _edit(update: Update, text: str, markup: InlineKeyboardMarkup):
        """Edit the tapped message; a double tap's "message is not modified" is not an error."""
        try:
            await update.callback_query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                raise
