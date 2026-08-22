"""
Base handler class with shared error handling for Telegram bot handlers.
"""
import logging
from typing import Any
from telegram import Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from i18n import i18n
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

    async def _edit_or_replace_callback_message(
        self,
        query: Any,
        text: str,
        reply_markup: Any = None,
        parse_mode: str | None = None,
    ) -> None:
        """Edit callback text when possible, otherwise replace the message with a text reply."""
        kwargs = {"text": text}
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        if parse_mode is not None:
            kwargs["parse_mode"] = parse_mode

        try:
            await query.edit_message_text(**kwargs)
            return
        except BadRequest as exc:
            reason = str(exc).lower()
            if "message is not modified" in reason:
                # The message already shows exactly this content; nothing to do.
                return
            if "there is no text in the message" in reason:
                # Expected for media (photo/caption) messages: replace below.
                logger.info("Callback message has no editable text; replacing message: %s", exc)
            else:
                logger.warning("Failed to edit callback message text; falling back to replace message: %s", exc)
            edit_exc = exc
        except Exception as exc:
            logger.warning("Failed to edit callback message text; falling back to replace message: %s", exc)
            edit_exc = exc

        message = getattr(query, "message", None)
        if not message:
            raise edit_exc

        try:
            await message.delete()
        except Exception as delete_exc:
            logger.warning("Failed to delete callback message before fallback send: %s", delete_exc)

        await message.reply_text(**kwargs)

    async def _ensure_loyalty_eligible(self, update, context, telegram_id, language) -> bool:
        """Block loyalty actions for ineligible users: brief toast + main menu.

        Returns True when the user may proceed, False when blocked (caller returns).
        """
        import eligibility
        from keyboards import MenuKeyboards

        if await eligibility.is_loyalty_eligible(telegram_id):
            return True

        query = getattr(update, "callback_query", None)
        toast = i18n.get('telegram.loyalty.not_available', language)
        if query is not None:
            await self._ack(query, toast, show_alert=False)
            await self._edit_or_replace_callback_message(
                query,
                text=i18n.get('telegram.main_menu', language),
                reply_markup=MenuKeyboards.main_menu(language, show_loyalty=False),
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
