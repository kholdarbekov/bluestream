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
from telegram import Update
from telegram.ext import ContextTypes

from api_client import api_client
from handlers.base import BaseHandler
from i18n import i18n
from utils import get_auth_token

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
# `BaseHandler._response_error_code` reads that top-level shape.
_NOT_PENDING_CODE = 'SALES_CONFIRMATION_NOT_PENDING'
_NOT_PENDING_KEY = 'telegram.agent_order.not_pending'


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
                    await self._settle_callback_message(query, note)
                    return

                if self._response_error_code(response) == _NOT_PENDING_CODE:
                    # Already answered — elsewhere, or on a message whose edit
                    # Telegram refused. Retire it here too, for the same reason.
                    note = i18n.get(_NOT_PENDING_KEY, language)
                    await self._ack(query, note)
                    await self._settle_callback_message(query, note)
                    return

                # `_ack`, not `_handle_api_error`: the shim answers the query
                # bare, so a query Telegram has already expired raises here,
                # lands in the blanket `except` below, and costs a second doomed
                # answerCallbackQuery carrying generic copy. The three paths
                # above already go through the guard; this is the fourth.
                # The proposal keeps its buttons: nothing was answered, and the
                # store still has to answer it. The toast is the localized
                # generic error; the backend's text is logged (final review E2).
                await self._ack_failed_call(query, response, language, operation=operation)

        except Exception as exc:
            await self._handle_error(update, exc=exc, operation=operation)


agent_order_handlers = AgentOrderHandlers()
