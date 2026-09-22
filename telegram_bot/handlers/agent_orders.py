"""Confirm / Decline on an order a sales agent placed for this store.

Armed by the `/internal/agent-order-proposed` push (telegram_bot/webhook_server.py),
which renders `agent_order_confirm_<order_id>` and `agent_order_decline_<order_id>`.

An answer is a toast AND a retired message: the proposal's text stays — it is
the record of what was agreed — but its buttons go and the answer is appended
to it, so the store can still read the order and can no longer answer it twice.
Telegram refuses that edit often enough (past the 48h window, a deleted message)
that a second tap is still ordinary, and the backend answers it with
`SALES_CONFIRMATION_NOT_PENDING` — turned below into copy the store can read.
"""
import logging

from telegram import Update
from telegram.ext import ContextTypes

from api_client import api_client
from handlers.base import BaseHandler
from i18n import i18n
from utils import get_auth_token

logger = logging.getLogger(__name__)

# Ruling 26: a local constant, not a map. The one code this screen branches on.
#
# DEPTH MATTERS, and this codebase has two envelopes. `validation_error_response(
# ..., error_code=...)` nests it under `data.error_code` — that is the shape
# `handlers/payments.py`'s pool-guard branch reads. But the agent-confirmation
# route (business_app/api/orders.py, Task 5b) raises through
# `@handle_api_exception`, and `ErrorResponse.build_error_response`
# (business_app/utils/error_handlers.py:76-77) puts `error_code` at the TOP
# level of the body. `_make_request` (api_client.py:346-373) hands the whole
# error body back as `APIResponse.data`, so the code is `response.data['error_code']`.
# Reading the nested shape here would make this branch dead in production while
# a fabricated test payload kept it green.
_NOT_PENDING_CODE = 'SALES_CONFIRMATION_NOT_PENDING'
_NOT_PENDING_KEY = 'telegram.agent_order.not_pending'


def _error_code(response) -> str | None:
    """The structured `error_code` off a failed APIResponse, or None."""
    body = getattr(response, 'data', None)
    if not isinstance(body, dict):
        return None
    return body.get('error_code')


class AgentOrderHandlers(BaseHandler):
    """The store's answer to an agent-placed order proposal."""

    async def confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Confirm the proposed order."""
        await self._respond(update, context, 'confirm',
                            'telegram.agent_order.confirmed', 'agent_order_confirm')

    async def decline(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Decline the proposed order (the backend cancels it)."""
        await self._respond(update, context, 'decline',
                            'telegram.agent_order.declined', 'agent_order_decline')

    async def _respond(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                       action: str, ack_key: str, operation: str):
        """One POST, one toast, one retired proposal — the only difference
        between the two buttons is the action word and the acknowledgement, so
        they share this body rather than existing as two copies that can drift
        apart."""
        query = update.callback_query
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # The order id comes off the callback data — the only place that
            # survives a redeploy. The registered pattern guarantees the
            # trailing digits, and this re-validates them anyway.
            order_id = int(query.data.rsplit('_', 1)[1])

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    # A toast, not a screen — on this path especially. The shared
                    # `_handle_auth_error` shim EDITS the tapped message, which here
                    # is the proposal: its text is replaced and the inline keyboard
                    # goes with it, so a customer who re-authenticates has no buttons
                    # left to answer with and the order rides the confirmation TTL
                    # into an auto-confirm. Leave the proposal standing.
                    await self._ack(query, i18n.get('telegram.error.auth_failed', language))
                    return

                response = await client.respond_agent_order(user_token, order_id, action)

                if response.success:
                    note = i18n.get(ack_key, language)
                    await self._ack(query, note)
                    await self._settle(query, note)
                    return

                if _error_code(response) == _NOT_PENDING_CODE:
                    # Already answered — elsewhere, or on a message whose edit
                    # Telegram refused. Retire it here too, for the same reason.
                    note = i18n.get(_NOT_PENDING_KEY, language)
                    await self._ack(query, note)
                    await self._settle(query, note)
                    return

                # `_ack`, not `_handle_api_error`: the shim answers the query
                # bare, so a query Telegram has already expired raises here,
                # lands in the blanket `except` below, and costs a second doomed
                # answerCallbackQuery carrying generic copy. The three paths
                # above already go through the guard; this is the fourth.
                # The proposal keeps its buttons: nothing was answered, and the
                # store still has to answer it.
                await self._ack(query, f'❌ {response.error}')

        except Exception as exc:
            await self._handle_error(update, exc=exc, operation=operation)

    async def _settle(self, query, note: str) -> None:
        """Retire an answered proposal: drop its buttons, append the answer.

        Editing the text with NO `reply_markup` does both in one call —
        Telegram removes the inline keyboard of a message edited without one —
        and keeps the itemised proposal on screen as the record of what was
        agreed. `note` is our own seeded copy, so it needs no escaping under
        the `parse_mode='HTML'` the proposal was sent with.

        Never lets tidy-up cost the answer, in the family of `_ack` and
        `_delete_callback_message` (handlers/base.py): the POST is already
        committed on the backend by the time this runs, and Telegram refuses
        `editMessageText` routinely — past the 48h window, on a message the
        customer deleted, or with "message is not modified" for an answer that
        raced another. Deliberately NOT `_edit_or_replace_callback_message`,
        whose fallback deletes the bubble and re-sends: the proposal is the
        record, and a refused edit is no reason to destroy it.
        """
        message = getattr(query, 'message', None)
        body = getattr(message, 'text_html', None) if message is not None else None
        try:
            await query.edit_message_text(
                text=f'{body}\n\n{note}' if body else note,
                parse_mode='HTML',
            )
        except Exception as exc:
            logger.info("Could not retire the agent-order proposal (cosmetic): %s", exc)


agent_order_handlers = AgentOrderHandlers()
