"""
Customer concern (support message) capture flow.

Armed by the delivered-summary "Report an issue" inline button. Sets a DB-backed
awaiting_input state; the next free-text message is posted to the admin Support
Inbox prefixed with the order number, and the customer is acknowledged.
"""
import logging
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.ext import ContextTypes

from api_client import api_client
from handlers.base import BaseHandler
from i18n import i18n
from keyboards import KeyboardBuilder
from support_capture import capture_support_message
from utils import get_auth_token

logger = logging.getLogger('handlers')

# A Report tap arms the state; typing much later means the order reference is no
# longer trustworthy, so we fall back to an unprefixed silent capture.
_SUPPORT_STALE_MINUTES = 30


class SupportHandlers(BaseHandler):
    """Guided 'Report an issue' concern flow off the delivered summary."""

    async def start_order_issue_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Answer the report_issue_<id> callback, resolve the order number via one
        authed fetch (fallback to the raw id), arm the capture state, and prompt."""
        query = update.callback_query
        try:
            await query.answer()

            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # callback_data is "report_issue_<order_id>".
            order_id = int(query.data.rsplit('_', 1)[1])

            # Resolve the human-facing order number now so the text handler never
            # needs a fetch. A failed lookup must NOT break arming.
            order_number = str(order_id)
            try:
                async with api_client as client:
                    user_token = await get_auth_token(update, context, client)
                    if user_token:
                        response = await client.get_order(user_token, order_id)
                        if response.success and response.data:
                            order = response.data.get('data', {}).get('order', {})
                            order_number = str(order.get('order_number') or order_id)
            except Exception as exc:
                logger.warning("Order lookup failed while arming issue report %s: %s", order_id, exc)

            await self.user_repo.arm_awaiting_input(
                user_id,
                'support_message',
                support_order_id=order_id,
                support_order_number=order_number,
                support_armed_at=datetime.now(timezone.utc).isoformat(),
            )

            cancel_keyboard = KeyboardBuilder.build_inline_keyboard([
                [{
                    'text': i18n.get('telegram.support.cancel_button', language),
                    'callback_data': 'support_cancel',
                }]
            ])
            # New message (not an edit): the delivered summary + its button stay
            # in place so the customer can retry if capture later fails.
            await query.message.reply_text(
                i18n.get('telegram.support.describe_issue_prompt', language, order_number=order_number),
                reply_markup=cancel_keyboard,
            )
        except Exception as exc:
            await self._handle_error(update, exc=exc, operation="start_order_issue_report")

    async def handle_support_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
        """Consume free text armed by start_order_issue_report: prefix with the
        order number, post to the admin inbox, acknowledge the customer.

        Stale/missing-reference arming falls back to the unprefixed silent
        capture. Missing token / API failure surfaces a failure notice with NO
        false acknowledgement (the delivered message's button stays tappable)."""
        user_id = update.effective_user.id
        language = await i18n.get_user_language(user_id)

        state = await self.user_repo.get_user_state(user_id)
        order_number = state.get('support_order_number')

        # Stale or malformed arming: clear state and record the raw text without a
        # misleading order prefix (mirrors the bot's general silent capture).
        # Reached only via `_handle_contextual_input`'s `input_type ==
        # 'support_message'` branch, so `awaiting_input` is guaranteed ours.
        if self._is_stale(state.get('support_armed_at')) or not order_number:
            await self.user_repo.disarm(user_id, 'support_message')
            await self._silent_capture(update, context, text)
            return

        prefix = f"[Order #{order_number}] "
        ok = await capture_support_message(update, context, prefix=prefix)
        await self.user_repo.disarm(user_id, 'support_message')
        if ok:
            await update.message.reply_text(i18n.get('telegram.support.ack', language))
        else:
            logger.warning("Support concern capture failed for user %s", user_id)
            await update.message.reply_text(i18n.get('telegram.support.send_failed', language))

    async def handle_support_attachment(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """An attachment sent while the concern flow is armed: same order prefix
        and same acknowledgement as the text path."""
        user_id = update.effective_user.id
        language = await i18n.get_user_language(user_id)

        state = await self.user_repo.get_user_state(user_id)
        order_number = state.get('support_order_number')

        # Reached only via `_capture_support_with_guards`'s `awaiting_input ==
        # 'support_message'` check, so `awaiting_input` is guaranteed ours.
        if self._is_stale(state.get('support_armed_at')) or not order_number:
            await self.user_repo.disarm(user_id, 'support_message')
            await capture_support_message(update, context)
            return

        ok = await capture_support_message(update, context, prefix=f"[Order #{order_number}] ")
        await self.user_repo.disarm(user_id, 'support_message')
        await update.message.reply_text(
            i18n.get('telegram.support.ack' if ok else 'telegram.support.send_failed', language)
        )

    @staticmethod
    def _is_stale(armed_at_raw) -> bool:
        """True when the arming timestamp is missing/invalid or older than 30 min."""
        if not armed_at_raw:
            return True
        try:
            armed_at = datetime.fromisoformat(armed_at_raw)
        except (TypeError, ValueError):
            return True
        if armed_at.tzinfo is None:
            armed_at = armed_at.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - armed_at) > timedelta(minutes=_SUPPORT_STALE_MINUTES)

    async def _silent_capture(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = None):
        """Persist unprefixed input for admin reply; no ack."""
        await capture_support_message(update, context)

    async def cancel_issue_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel an armed concern flow: clear state and confirm.

        Converted to `disarm(user_id, 'support_message')` (2026-08-26
        address-flow-bot-state, Task 5) — a DELIBERATE behaviour change, not a
        mechanical rename. Unlike every other call in this module, this
        handler is wired with no `awaiting_input` gate at all
        (`CallbackQueryHandler(cancel_issue_report, pattern="^support_cancel$")`
        in bot.py) — the Cancel button lives on its own message and stays
        tappable after the customer has moved on and armed something else.
        The old unconditional wipe silently destroyed whichever OTHER flow
        (and any open `address_draft`) happened to be armed at tap time. With
        `disarm`, a stale Cancel tap only cancels the currently armed
        `support_message` flow — if the customer is no longer in that flow,
        there is nothing for Cancel to cancel, so it correctly does nothing
        instead of blanket-wiping unrelated state.

        NOTE what this does NOT give: per-order precision. `disarm` names a
        FLOW (`support_message`), not a specific report — tapping Report A's
        stale Cancel button after Report B (a different order) has since
        armed still cancels B. Unchanged from the old behaviour; not
        introduced or fixed by this conversion.
        """
        query = update.callback_query
        try:
            await query.answer()
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            await self.user_repo.disarm(user_id, 'support_message')
            await self._edit_or_replace_callback_message(
                query,
                text=i18n.get('telegram.support.cancelled', language),
            )
        except Exception as exc:
            await self._handle_error(update, exc=exc, operation="cancel_issue_report")


support_flow_handlers = SupportHandlers()
