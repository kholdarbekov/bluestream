"""Later / Stop on a reorder reminder (spec 2026-10-09 §7.3 "Callbacks").

Armed by the `/internal/reorder-reminder` push (telegram_bot/webhook_server.py), whose buttons
`keyboards.ReorderReminderKeyboards.reminder` builds: `rr_later_<episode_id>` on reminder 1
only, `rr_stop_<episode_id>` on both.

Same shape as handlers/agent_orders.py: one POST, one answered tap, one retired message. The
reminder's text stays; its buttons go and a one-line confirmation is appended, so it cannot be
answered twice by accident. A Later the backend refuses (R2 went out, the customer ordered)
retires the message with the `expired` line instead. Stop never refuses for episode state, so
only a failed call keeps its buttons, with the localized generic error as its toast
(`BaseHandler._ack_failed_call`).

The two HTTP routes are business_app/api/reorder_reminders.py; the client methods are
`BusinessAPIClient.snooze_reorder_reminder` and `stop_reorder_reminders`.
"""

from telegram import Update
from telegram.ext import ContextTypes

from api_client import api_client
from handlers.base import BaseHandler
from i18n import i18n
from shared import reorder_reminder_copy
from utils import get_auth_token

# The one code these buttons branch on (top level of the body; see
# BaseHandler._response_error_code). Only Later can be refused with it.
_NOT_ACTIVE_CODE = 'REORDER_REMINDER_NOT_ACTIVE'


class ReorderReminderHandlers(BaseHandler):
    """The customer's answer to a reorder reminder."""

    async def later(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Remind me later: reminder 2 moves out by the snooze length."""
        await self._respond(update, context, 'later')

    async def stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Stop these reminders: the customer-off setting (spec §8)."""
        await self._respond(update, context, 'stop')

    async def _respond(self, update: Update, context: ContextTypes.DEFAULT_TYPE, action: str):
        """One POST, one answered tap, one retired reminder; the buttons differ only in the call
        and the confirmation, so they share this body rather than existing as two copies."""
        query = update.callback_query
        try:
            language = await i18n.get_user_language(update.effective_user.id)
            render = i18n.renderer(language)
            # The registered patterns guarantee the trailing digits; parsed here anyway.
            episode_id = int(query.data.rsplit('_', 1)[1])

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    # A toast, not a screen: `_handle_auth_error` would EDIT the reminder and
                    # take its buttons with it.
                    await self._ack(query, i18n.get('telegram.error.auth_failed', language))
                    return

                if action == 'later':
                    response = await client.snooze_reorder_reminder(user_token, episode_id)
                else:
                    response = await client.stop_reorder_reminders(user_token, episode_id)

                if response.success:
                    if action == 'later':
                        # `success_response` wraps the payload: {"success": true, "data": {...}}.
                        snooze_days = int(response.data['data']['snooze_days'])
                        note = reorder_reminder_copy.later_ok(render, language, snooze_days=snooze_days)
                    else:
                        note = reorder_reminder_copy.stop_ok(render)
                    await self._ack(query)
                    await self._settle_callback_message(query, note)
                    return

                if action == 'later' and self._response_error_code(response) == _NOT_ACTIVE_CODE:
                    await self._ack(query)
                    await self._settle_callback_message(query, reorder_reminder_copy.expired(render))
                    return

                # Nothing was answered, so the reminder keeps its buttons; the customer reads the
                # localized generic error, never the transport or backend text (logged).
                await self._ack_failed_call(query, response, language, operation=f'reorder_reminder_{action}')

        except Exception as exc:
            await self._handle_error(update, exc=exc, operation=f'reorder_reminder_{action}')


reorder_reminder_handlers = ReorderReminderHandlers()
