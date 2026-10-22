"""
Base handler class with shared error handling for Telegram bot handlers.
"""
import logging
from types import SimpleNamespace
from typing import Any
from telegram import Message, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from i18n import i18n
from menu_keyboard import assert_editable_markup, send_menu_keyboard
from database import db_manager, BotUserRepository
from handlers.errors import (
    BotAPIError,
    BotAuthError,
    BotError,
    BotNetworkError,
    BotValidationError,
)

logger = logging.getLogger(__name__)


class BaseHandler:
    """Base class for bot handler groups with shared error handling and user repository."""

    def __init__(self):
        self.user_repo = BotUserRepository(db_manager)

    @staticmethod
    def as_message_update(update: Update):
        """A message-shaped view of a tap, so the next screen is a NEW message.

        Handlers that render by replying (checkout, cart) edit the tapped message when they
        see a callback query. Wrapping the tap hides the query, so the tapped message (a
        reminder, the Products screen) is never edited or deleted. PTB 22.3 gives no
        `effective_message` for an inline or inaccessible callback message; such an update is
        returned unchanged. It acks nothing: the caller acks first.
        """
        message = update.effective_message
        if message is None:
            return update
        return SimpleNamespace(
            effective_user=update.effective_user,
            callback_query=None,
            message=message,
        )

    async def _ack(self, query: Any, text: str | None = None, **kwargs) -> bool:
        """Dismiss the spinner on a tap — and never let that cost the work.

        Telegram discards callback queries after ~60s and refuses a late
        ``answerCallbackQuery`` with "query is too old and response timeout
        expired or query id is invalid". That is routine, not exceptional:
        every redeploy ends with Telegram redelivering the taps that piled up
        while the bot was down, so the first taps a restarted bot processes are
        frequently already expired.

        The ack only stops the spinner in the customer's client. The fetch and
        the render that follow it are the reason they tapped. An ack allowed to
        escape into a handler's ``except Exception`` turns a cosmetic failure
        into a flow failure: nothing is fetched, nothing is drawn, and the
        global error handler's one user-facing action — answering the SAME dead
        query — fails for exactly the same reason. From the customer's side
        that is a screen that never opens, with no error to report.

        Lives here, beside ``_edit_or_replace_callback_message`` (the same
        shape of fix for ``editMessageText``), so every handler group inherits
        ONE expression of the rule instead of importing a helper, borrowing a
        sibling handler's private method, or hand-rolling a try/except per call
        site.

        Returns True when Telegram accepted the ack, False when there was no
        callback query to answer or Telegram refused it. Callers that only want
        the spinner to stop can ignore the result; the point is that they keep
        running.
        """
        if query is None:
            return False

        try:
            # Forward exactly what the caller passed, so a caller's contract
            # with Telegram (and with the tests pinning it) is unchanged.
            if text is None:
                await query.answer(**kwargs)
            else:
                await query.answer(text, **kwargs)
            return True
        except Exception as exc:
            # INFO, not WARNING: an expired callback is Telegram working as
            # designed, and the handler carries on regardless.
            logger.info("Callback ack refused (cosmetic; the handler continues): %s", exc)
            return False

    async def _ack_failed_call(self, query: Any, response: Any, language: str, *, operation: str) -> bool:
        """Answer a tap whose backend call failed for a reason the button does not branch on.

        `response.error` is technical text the customer can do nothing with, and it is English:
        the API client's 'HTTP 502', 'Connection failed after 3 retries: ...', 'Request timed out
        ...', 'Service temporarily unavailable (circuit breaker open)', or the backend's own
        `message` (a 429's "Rate limit exceeded ..."). It goes to the log; the customer reads the
        seeded `telegram.error.generic` in their own language (final review E2). The message keeps
        its buttons, because nothing was answered: the customer can tap again. One expression for
        every inline-button flow that answers this way (the reorder-reminder Later/Stop buttons and
        the agent-order proposal), through `_ack`, so a refused ack never costs the flow either.
        """
        logger.warning(
            "%s: the backend call failed (status %s): %s",
            operation,
            getattr(response, "status_code", None),
            getattr(response, "error", None),
        )
        return await self._ack(query, i18n.get('telegram.error.generic', language))

    async def _delete_callback_message(self, query: Any) -> bool:
        """Drop the bubble a tap came from — and never let that cost the work.

        The third member of the family beside :meth:`_ack` and
        :meth:`_edit_or_replace_callback_message`, and it exists for the same
        reason: deleting is TIDY-UP, the render that follows is the work, and a
        bare ``delete()`` puts them in the same fate.

        Telegram refuses ``deleteMessage`` routinely and for reasons the bot
        cannot control: "Message can't be deleted for everyone" past the ~48h
        window, and "Message to delete not found" when the customer removed it
        first. Neither says anything about whether the screen the customer
        asked for can be drawn.

        Reported from the running dev bot on 2026-08-22: a category tap on a
        PHOTO message — which cannot be edited to text, so the handler deletes
        and re-sends — hit the 48h window, the BadRequest escaped into
        ``category_handler``'s blanket except, and the customer got an error
        toast instead of the products. Deterministically, for that message.

        Returns True when Telegram accepted the delete, so a caller that must
        know (for instance to choose between replying and editing) can ask.
        """
        if query is None:
            return False

        try:
            await query.delete_message()
            return True
        except Exception as exc:
            # INFO, not WARNING: an undeletable bubble is Telegram working as
            # designed, and the handler carries on regardless.
            logger.info("Could not delete the callback message (cosmetic): %s", exc)
            return False

    @staticmethod
    def _is_entity_parse_error(exc: BaseException) -> bool:
        """The one rule for "Telegram refused the markup".

        Telegram words every such refusal "can't parse entities: ...". Any other
        BadRequest (message too long, chat not found, bad button data, message
        to edit not found) does not depend on ``parse_mode``, so a plain-text
        retry would fail identically and must not be attempted.
        """
        return isinstance(exc, BadRequest) and "can't parse entities" in str(exc).lower()

    @staticmethod
    async def _reply_markdown_or_plain(
        message,
        text: str,
        reply_markup=None,
        *,
        parse_mode: str | None = 'Markdown',
    ) -> Message:
        """Send `text` with `parse_mode`, falling back to plain text if Telegram can't parse it.

        Escaping the interpolated geocoder string fixes the hazard we know
        about; it cannot fix the one we do not. The cost of being wrong here is
        total — a refused prompt used to end the conversation, leaving the
        customer on a dead screen with no way to self-rescue, because
        re-sharing the pin re-runs the same deterministic failure. Losing the
        bold is a much smaller price than losing the flow.

        Only a markup refusal is retried (``_is_entity_parse_error``); every
        other BadRequest propagates, because a plain resend cannot cure it.
        """
        kwargs: dict[str, Any] = {}
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        if parse_mode is not None:
            kwargs["parse_mode"] = parse_mode
        try:
            return await message.reply_text(text=text, **kwargs)
        except BadRequest as exc:
            if parse_mode is None or not BaseHandler._is_entity_parse_error(exc):
                raise
            logger.warning("%s prompt refused (%s); resending as plain text", parse_mode, exc)
            kwargs.pop("parse_mode")
            return await message.reply_text(text=text, **kwargs)

    async def _edit_or_replace_callback_message(
        self,
        query: Any,
        text: str,
        reply_markup: Any = None,
        parse_mode: str | None = None,
    ) -> Message | None:
        """Edit callback text when possible, otherwise replace the message with a text reply.

        Returns the message now showing ``text`` — the edited one, or the
        replacement — for callers that need to know which bubble holds the
        screen (the quantity screen records it so a typed number can replace
        it). Callers that do not care ignore it.

        A refused-markup edit is retried once as plain text before anything is
        replaced. A replacement is SENT before the old bubble is deleted, so a
        send that fails leaves the old screen in place instead of nothing.
        """
        assert_editable_markup(reply_markup)
        kwargs = {"text": text}
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        if parse_mode is not None:
            kwargs["parse_mode"] = parse_mode

        edit_exc: Exception
        try:
            edited = await query.edit_message_text(**kwargs)
            # `True` instead of a Message only for inline-mode messages.
            return edited if isinstance(edited, Message) else getattr(query, "message", None)
        except Exception as exc:
            edit_exc = exc

        if parse_mode is not None and self._is_entity_parse_error(edit_exc):
            logger.warning(
                "Telegram refused the %s markup; retrying the edit as plain text: %s", parse_mode, edit_exc
            )
            kwargs.pop("parse_mode")
            parse_mode = None
            try:
                edited = await query.edit_message_text(**kwargs)
                return edited if isinstance(edited, Message) else getattr(query, "message", None)
            except Exception as exc:
                edit_exc = exc

        reason = str(edit_exc).lower()
        if isinstance(edit_exc, BadRequest) and "message is not modified" in reason:
            # The message already shows exactly this content; nothing to do.
            return getattr(query, "message", None)
        if isinstance(edit_exc, BadRequest) and "there is no text in the message" in reason:
            # Expected for media (photo/caption) messages: replace below.
            logger.info("Callback message has no editable text; replacing message: %s", edit_exc)
        else:
            logger.warning("Failed to edit callback message text; falling back to replace message: %s", edit_exc)

        message = getattr(query, "message", None)
        if not message:
            raise edit_exc

        # Send first: if this raises, the old message is untouched.
        sent = await self._reply_markdown_or_plain(message, text, reply_markup, parse_mode=parse_mode)

        try:
            await message.delete()
        except Exception as delete_exc:
            logger.warning("Failed to delete callback message after replacement send: %s", delete_exc)

        return sent

    async def _ensure_loyalty_eligible(self, update, context, telegram_id, language) -> bool:
        """Block loyalty actions for ineligible users: the notice, sent with the bottom menu.

        The menu follows eligibility, so the keyboard that comes with the notice has no
        Aqua Club button. Sent as a new message for a tap and a typed label alike: an
        edit can never carry that keyboard.

        Returns True when the user may proceed, False when blocked (caller returns).
        """
        import eligibility

        if await eligibility.is_loyalty_eligible(telegram_id):
            return True

        query = getattr(update, "callback_query", None)
        if query is not None:
            await self._ack(query)
        await send_menu_keyboard(
            context.bot, update.effective_chat.id, telegram_id,
            i18n.get('telegram.loyalty.not_available', language), language,
        )
        return False

    async def _resolve_language(self, update: Update) -> str:
        """Best-effort language lookup for error replies. Falls back to 'en'."""
        try:
            user = update.effective_user
            if user is not None:
                return await i18n.get_user_language(user.id)
        except Exception as e:
            logger.warning(f"Failed to resolve user language for error reply: {e}")
        return 'en'

    async def _reply_error(self, update: Update, text: str) -> None:
        """Deliver `text` via callback answer or message reply, whichever fits the update."""
        try:
            if update.callback_query:
                await update.callback_query.answer(text)
            elif update.message:
                await update.message.reply_text(text)
        except Exception as e:
            logger.error(f"Failed to deliver error reply to user: {e}")

    @staticmethod
    def _response_error_code(response) -> str | None:
        """The structured `error_code` off a failed APIResponse, or None.

        `@handle_api_exception` puts it at the TOP level of the error body
        (business_app/utils/error_handlers.py:76-77), and `_make_request` hands the whole body
        back as `APIResponse.data`. The other envelope, `validation_error_response`, nests it
        under `data.error_code`: reading that shape here would make a branch dead in
        production while a fabricated test payload kept it green.
        """
        body = getattr(response, 'data', None)
        if not isinstance(body, dict):
            return None
        return body.get('error_code')

    async def _settle_callback_message(self, query, note: str) -> None:
        """Retire an answered message: drop its buttons, append the answer.

        Editing the text with NO `reply_markup` does both in one call, because Telegram removes
        the inline keyboard of a message edited without one. The message's own text stays as
        the record of what was answered. `note` is our own seeded copy, so it needs no escaping
        under the parse_mode='HTML' the message was sent with.

        Never lets tidy-up cost the answer, like `_ack`: the backend has already committed by
        the time this runs, and Telegram refuses `editMessageText` routinely (past the 48h
        window, a deleted message, "message is not modified"). It is deliberately NOT
        `_edit_or_replace_callback_message`, whose fallback deletes the bubble: the message is
        the record, and a refused edit is no reason to destroy it.
        """
        message = getattr(query, 'message', None)
        body = getattr(message, 'text_html', None) if message is not None else None
        try:
            await query.edit_message_text(
                text=f'{body}\n\n{note}' if body else note,
                parse_mode='HTML',
            )
        except Exception as exc:
            logger.info("Could not retire the answered message (cosmetic): %s", exc)

    async def _handle_error(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE = None,
        *,
        exc: Exception | None = None,
        operation: str | None = None,
    ):
        """Unified error handler.

        Back-compat: callers that pass only `(update, context)` continue to work and
        receive the generic `telegram.error_occurred` reply. New callers pass the
        raised exception via `exc=` and a short `operation=` tag so we can dispatch
        by error category and emit a structured log line.
        """
        language = await self._resolve_language(update)

        if isinstance(exc, BotError):
            i18n_key = exc.i18n_key
            localized = i18n.get(i18n_key, language)
            # BotValidationError carries the user-presentable reason in `message`
            # and should surface it verbatim when the i18n key is generic.
            if isinstance(exc, BotValidationError) and exc.message:
                text = f"❌ {exc.message}"
            elif isinstance(exc, BotAPIError) and exc.message and i18n_key == BotAPIError.default_i18n_key:
                text = f"❌ {exc.message}"
            else:
                text = localized
        else:
            text = i18n.get('telegram.error_occurred', language)

        log_extra = {
            'operation': operation,
            'update_id': getattr(update, 'update_id', None),
            'telegram_id': getattr(update.effective_user, 'id', None) if update.effective_user else None,
            'error_type': type(exc).__name__ if exc is not None else None,
        }
        if exc is not None:
            logger.error("Bot handler error in %s: %s", operation or "unknown", exc, exc_info=exc, extra=log_extra)
        else:
            logger.error("Bot handler error in %s (no exception context)", operation or "unknown", extra=log_extra)

        await self._reply_error(update, text)

    async def _handle_auth_error(self, update: Update, language: str):
        """Back-compat shim: prefer raising `BotAuthError` and letting `_handle_error` dispatch."""
        error_msg = i18n.get('telegram.error.auth_failed', language)

        if update.callback_query:
            await self._edit_or_replace_callback_message(update.callback_query, error_msg)
            await update.callback_query.answer()
        else:
            await update.message.reply_text(error_msg)

    async def _handle_api_error(self, update: Update, error: str, language: str):
        """Back-compat shim: prefer raising `BotAPIError(error)` and letting `_handle_error` dispatch."""
        error_msg = f"❌ {error}"

        if update.callback_query:
            await update.callback_query.answer(error_msg)
        else:
            await update.message.reply_text(error_msg)

    async def _send_error_message(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        message: str,
    ) -> None:
        """Send a formatted validation error. Separate from `_handle_error` because
        validation messages are already localized by the caller and do not map to a
        typed exception."""
        try:
            if update.callback_query:
                await self._edit_or_replace_callback_message(
                    update.callback_query,
                    f"❌ {message}",
                )
                # Dismiss the spinner: the dedup middleware no longer
                # pre-answers, so whoever ends the interaction must answer.
                await update.callback_query.answer()
            elif update.message:
                await update.message.reply_text(f"❌ {message}")
        except Exception as e:
            logger.error(f"Error sending error message: {e}")
