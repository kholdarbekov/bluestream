"""Operator side of outlet onboarding: review activation requests, approve or reject."""
import logging

from telegram import Update
from telegram.ext import ContextTypes

from staff_bot.api_client import api_client
from staff_bot.handlers.base import BaseHandler
from staff_bot.handlers.sales.hub import format_outlet_card
from staff_bot.i18n import i18n
from staff_bot.keyboards.sales import REJECT_REASONS, SalesKeyboards
from staff_bot.permissions import require_auth, require_operator

logger = logging.getLogger(__name__)


class SalesApprovalsHandler(BaseHandler):
    """The approver's queue. Reached from the profile hub and from the push
    notification the backend sends when an agent requests activation.

    Guarded with `@require_operator`, matching the three backend routes it
    calls (`@require_staff_roles("operator")`). That also decides where the
    review card's content comes from: the agent-scoped `GET /outlets/<id>` is
    sales-agent-only AND assignment-checked, so an operator cannot fetch it.
    The card is rendered from the LIST item instead — a plain
    `serialize_outlet`, which is why `format_outlet_card` prints no money lines
    here (it needs `open_receivable`, which only the agent's GET carries).
    """

    async def _render(self, update: Update, text: str, keyboard) -> None:
        """Callback-only: every screen here is reached by tapping a button.

        No message branch, unlike the sales agent's hub — `staff_sales_approvals`
        has no reply-keyboard label and no text-router entry, so a `None`
        callback_query would be a wiring bug, not a shape to fall back on.

        The acknowledgement goes through `_safe_callback_answer`, never a bare
        `answer()`: a stale tap ("query is too old or invalid") would otherwise
        raise BEFORE the edit below and leave the operator on the old screen.
        The EDIT is the part that matters.
        """
        query = update.callback_query
        await self._safe_callback_answer(query, None, show_alert=False)
        await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')

    async def _pending(self, update, context, language):
        """The activation queue, or None when the caller has already been told why not."""
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return None
        async with api_client as client:
            response = await client.sales_list_activation_requests(token)
        if not response.success:
            await self._handle_api_response_error(update, response, language)
            return None
        return (response.data or {}).get('items', [])

    @require_auth
    @require_operator
    async def show_requests(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        try:
            items = await self._pending(update, context, language)
            if items is None:
                return
            title = f"⏳ <b>{i18n.get('staff.sales.approvals.title', language)}</b>"
            text = title if items else f"{title}\n{i18n.get('staff.sales.approvals.empty', language)}"
            await self._render(update, text, SalesKeyboards.approvals_list(language, items))
        except Exception as e:
            logger.error(f"Error listing activation requests: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_operator
    async def review(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        outlet_id = int(update.callback_query.data.split('_')[-1])
        try:
            items = await self._pending(update, context, language)
            if items is None:
                return
            outlet = next((o for o in items if o.get('id') == outlet_id), None)
            if outlet is None:
                # Somebody else approved it while this operator was reading the
                # list. Say so rather than editing the message: the queue on
                # screen is stale, and the alert is what tells them why.
                await self._notify_user(
                    update, i18n.get('staff.sales.error.stage_invalid', language), show_alert=True
                )
                return
            await self._render(
                update, format_outlet_card(outlet, language),
                SalesKeyboards.approval_actions(language, outlet_id),
            )
        except Exception as e:
            logger.error(f"Error reviewing outlet {outlet_id}: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_operator
    async def approve(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        outlet_id = int(update.callback_query.data.split('_')[-1])
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return
        try:
            async with api_client as client:
                response = await client.sales_approve_outlet(token, outlet_id)
            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return
            outlet = (response.data or {}).get('outlet') or {}
            text = (
                f"✅ {i18n.get('staff.sales.approvals.approved', language)}\n\n"
                f"{format_outlet_card(outlet, language)}"
            )
            await self._render(update, text, SalesKeyboards.approval_result(language))
        except Exception as e:
            logger.error(f"Error approving outlet {outlet_id}: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_operator
    async def reject(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        query = update.callback_query
        parts = query.data.split('_')  # staff_sales_reject_<id>_<reason>
        outlet_id, reason = int(parts[3]), '_'.join(parts[4:])
        # The registered pattern is `^staff_sales_reject_\d+_\w+$` rather than
        # the four-way alternation the keyboard draws, because an alternation is
        # a shape the static collision check cannot sample (see bot.py). So the
        # REASON is re-validated here, the same way `show_list` re-checks its
        # scope: anything the keyboard never offered is answered so the button
        # stops spinning, and the review card it was tapped from stays exactly
        # where it is as the re-prompt. Deliberately not re-editing that message
        # -- the copy would be identical and Telegram answers "message is not
        # modified". Never widened to "send it anyway": `reason` is stored
        # verbatim on the outlet and read back in the admin UI.
        if reason not in REJECT_REASONS:
            logger.warning("Unroutable sales reject callback: %s", query.data)
            await self._safe_callback_answer(query, None, show_alert=False)
            return
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return
        try:
            async with api_client as client:
                response = await client.sales_reject_outlet(token, outlet_id, REJECT_REASONS[reason])
            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return
            outlet = (response.data or {}).get('outlet') or {}
            text = (
                f"❌ {i18n.get('staff.sales.approvals.rejected', language)}\n\n"
                f"{format_outlet_card(outlet, language)}"
            )
            await self._render(update, text, SalesKeyboards.approval_result(language))
        except Exception as e:
            logger.error(f"Error rejecting outlet {outlet_id}: {e}", exc_info=True)
            await self._handle_error(update, context)
