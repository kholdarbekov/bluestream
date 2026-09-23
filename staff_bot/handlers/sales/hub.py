"""My outlets: lists, the outlet card and requesting activation."""
import logging
from typing import Dict

from telegram import Update
from telegram.ext import ContextTypes

from staff_bot.api_client import api_client
from staff_bot.handlers.base import BaseHandler
from staff_bot.i18n import i18n
from staff_bot.keyboards.menu import MenuKeyboards
from staff_bot.keyboards.sales import SalesKeyboards, stage_label
from staff_bot.permissions import require_auth, require_sales_agent
from staff_bot.utils.flow_state import (
    SALES_NEARBY_FLOW_KEY,
    SALES_TRYOUT_FLOW_KEY,
    SALES_VISIT_FLOW_KEY as VISIT_FLOW_KEY,
)
from staff_bot.utils.formatters import escape_html, format_currency, format_local_date

logger = logging.getLogger(__name__)


def format_outlet_card(outlet: Dict, language: str) -> str:
    """The outlet card text. Each money/bottle/visit line appears only when the
    backend published a figure for it.

    Reading the field IS the whole rule. `OutletService.card` answers NULL for a
    figure that does not apply — no customer account means no wallet, no address
    row means no bottle ledger, a store nobody has visited has no rate — and the
    activation POST answers with a plain `serialize_outlet` that carries none of
    these keys. So `.get(...) is not None` covers both shapes, and the bot never
    re-derives applicability from `user_id`: that rule lives server-side, in one
    place, or the two ends of it drift apart. The figures are answered
    independently — a half-activated outlet has a wallet and no ledger — so each
    line gates on its OWN field. A 0 printed here reads as a settled bill and an
    empty crate on a store that has never ordered.

    D25 adds the account: an account may own several outlets, one per address,
    so the receivable is the ACCOUNT's figure while the bottle ledger is the
    BRANCH's. Which of the two labels is printed follows `is_branch` — the
    answer the backend published, and the money label also checks the
    `open_receivable_scope` it published beside it. The bot never counts an
    account's outlets and never reads `user_id` to guess at one.

    The same rule governs the phase-2a visit block. `overdue_days` is computed
    by the backend (never by subtracting dates here), and `rate_per_day` /
    `suggested_now` distinguish NULL — "not computable yet", no second stock
    check and no delivery history — from 0.0, which is the real and useful
    answer "this store is not moving stock". The visit OUTCOME goes through its
    translation family: shipping the raw `order_placed` enum is the English-leak
    class this codebase keeps rediscovering.
    """
    type_label = i18n.get(f"staff.sales.type.{outlet.get('outlet_type', 'grocery_store')}", language)
    # D25: branch mode is the BACKEND's answer, published by `OutletService.card`
    # as `is_branch` (= `OutletService.is_branch`). Read into ONE local by the
    # three lines below -- the account line, the money label and the bottle
    # label -- so the card cannot say "branch" in one place and "account" in
    # another, and so the rule is not re-derived here from a count (the admin
    # drawer reads the same field for the same reason). A payload that carries
    # no `is_branch` is not a branch: the operator's review card renders a plain
    # `serialize_outlet` row, which publishes none of these fields (R9: no
    # sibling count per row on a list).
    is_branch = outlet.get('is_branch') is True
    lines = [
        f"🏪 <b>{escape_html(outlet.get('name', ''))}</b> · {type_label}",
        f"{i18n.get('staff.sales.card.stage', language)}: {stage_label(outlet.get('stage', 'prospect'), language)}",
    ]
    if outlet.get('class'):
        lines.append(f"{i18n.get('staff.sales.card.class', language)}: {outlet['class']}")
    contacts = outlet.get('contacts') or []
    if contacts:
        primary = next((c for c in contacts if c.get('is_primary')), contacts[0])
        # Built from the non-empty parts: a contact with a phone and no name
        # (the common field case) would otherwise render a double space.
        who = ' '.join(escape_html(part) for part in (primary.get('name'), primary.get('phone')) if part)
        if who:
            lines.append(f"📞 {i18n.get('staff.sales.card.contact', language)}: {who}")
    if outlet.get('address_text'):
        lines.append(f"📍 {i18n.get('staff.sales.card.address', language)}: {escape_html(outlet['address_text'])}")
    if is_branch and outlet.get('account_name'):
        # Under the address, because the address is what makes a branch a
        # branch and this line says which account it belongs to.
        account_line = i18n.get(
            'staff.sales.card.account', language,
            account=escape_html(outlet['account_name']), count=outlet.get('branch_count'),
        )
        lines.append(f"🏢 {account_line}")
    last_visit = outlet.get('last_visit') or {}
    visited_on = format_local_date(last_visit.get('ended_at'))
    if visited_on:
        outcome = last_visit.get('outcome') or ''
        outcome_label = i18n.get(f'staff.sales.visit.outcome.{outcome}', language) if outcome else ''
        summary = f"{visited_on} · {outcome_label}" if outcome_label else visited_on
        lines.append(f"🕘 {i18n.get('staff.sales.card.last_visit', language)}: {summary}")
        visit_notes = last_visit.get('notes')
        if visit_notes:
            lines.append(f"    💬 {escape_html(visit_notes)}")
    due_on = format_local_date(outlet.get('next_visit_due_at'))
    if due_on:
        due_line = f"📅 {i18n.get('staff.sales.card.next_due', language)}: {due_on}"
        overdue_days = outlet.get('overdue_days')
        if overdue_days:
            due_line += f" · {i18n.get('staff.sales.card.overdue', language, days=overdue_days)}"
        lines.append(due_line)
    if outlet.get('rate_per_day') is not None:
        lines.append(
            f"📈 {i18n.get('staff.sales.card.rate', language)}: "
            f"{float(outlet['rate_per_day']):.1f}"
        )
    if outlet.get('suggested_now') is not None:
        lines.append(
            f"🧮 {i18n.get('staff.sales.card.suggested', language)}: "
            f"{int(outlet['suggested_now'])}"
        )
    if outlet.get('open_receivable') is not None:
        # Both keys are written as LITERALS in their own `i18n.get` call: the
        # required-key extractor (`Translation._extract_literal_staff_keys`)
        # only sees a literal staff key passed directly as that call's first
        # argument, so a key chosen into a variable drops out of /health's
        # required set and an unseeded one reaches an agent's phone as a
        # humanised key tail.
        # The qualifier is printed only when there is a distinction to draw AND
        # the backend says the figure is the account's: `open_receivable_scope`
        # is what the money line MEANS, so if it ever stopped being account-wide
        # this label would stop claiming otherwise instead of lying.
        account_money = is_branch and outlet.get('open_receivable_scope') == 'account'
        receivable_label = (
            i18n.get('staff.sales.card.receivable_account', language) if account_money
            else i18n.get('staff.sales.card.receivable', language)
        )
        lines.append(
            f"💰 {receivable_label}: "
            f"{format_currency(outlet['open_receivable'], language=language)}"
        )
    if outlet.get('bottle_balance') is not None:
        bottles_label = (
            i18n.get('staff.sales.card.bottles_branch', language) if is_branch
            else i18n.get('staff.sales.card.bottles', language)
        )
        lines.append(
            f"🧴 {bottles_label}: "
            f"{float(outlet['bottle_balance']):,.0f}"
        )
    if outlet.get('last_orders'):
        lines.append(f"🧾 {i18n.get('staff.sales.card.last_orders', language)}:")
        for order in outlet['last_orders'][:3]:
            status = order.get('status', '')
            # No `or status` fallback: `i18n.get` never returns falsy — it
            # humanises the key tail for a family it has no row for.
            status_label = i18n.get(f'staff.order.status.{status}', language) if status else ''
            lines.append(
                f"  • {escape_html(order.get('order_number', ''))} — "
                f"{format_currency(order.get('total_amount') or 0, language=language)} "
                f"({escape_html(status_label)})"
            )
    if outlet.get('notes'):
        lines.append(f"📝 {i18n.get('staff.sales.card.notes', language)}: {escape_html(outlet['notes'])}")
    return "\n".join(lines)


class SalesHubHandler(BaseHandler):
    """Entry points: main-menu label 'My outlets' (router) and the staff_sales_* callbacks."""

    def _main_menu(self, context, language: str):
        """The agent's persistent reply keyboard, rebuilt from their CURRENT roles.

        Every sales flow that asks for a pin replaces this keyboard with a
        one-shot `request_location` prompt, so every one of them has to hand
        it back. Defined once, here, because the three flows that do
        (`new_outlet`, `visit`, `nearby`) all subclass this handler: three
        copies is three places for the roles argument to go stale, and a menu
        rebuilt from the wrong roles is a control surface with the wrong
        buttons on it.
        """
        return MenuKeyboards.main_menu(language, context.user_data.get('staff_roles', []))

    async def _refused_by_open_visit(self, update: Update, context) -> bool:
        """Is this tap a stale button arriving on top of a LIVE visit?

        The sales conversations that register their own callbacks as ENTRY
        POINTS (so a tap that outlived a bot restart can re-arm them) pay for
        it: an entry point also fires for a tap the ACTIVE conversation does
        not claim, and the visit conversation claims only its own
        `staff_sales_v_*`. Every other sales literal left on an older screen —
        a Nearby list, the hub message above it — would therefore open a new
        flow ON TOP of a visit the agent is standing inside, replacing their
        keyboard and filing the next pin they share as something other than
        their arrival.

        The visit's own draft is the signal, the same key `_require_flow` and
        the menu escape read: no fresh screen can be reached without clearing
        it. The tap is answered with the SENTENCE the backend's own
        `SALES_VISIT_ALREADY_OPEN` gets (`staff.sales.error.visit_open`): a
        button that answers nothing at all reads as a bot that has stopped, and
        the agent goes on tapping a button that cannot work until the visit is
        closed.
        """
        query = update.callback_query
        if not query or not isinstance(context.user_data.get(VISIT_FLOW_KEY), dict):
            return False
        language = await self._get_language(update, context)
        await self._safe_callback_answer(
            query, i18n.get('staff.sales.error.visit_open', language), show_alert=True
        )
        return True

    def _drop_other_sales_drafts(self, context, own_key: str) -> None:
        """Entering one sales conversation leaves the other two holding nothing.

        PTB checks an INACTIVE conversation's entry points on every update, so a
        stale 🧭 Nearby or 🧪 Try-out tap arms its conversation on top of whatever
        the agent is really doing — and nothing ends the one that loses. The
        loser keeps its draft, its five-minute timer keeps running, and
        `_refused_by_open_visit` then reads a draft no live conversation owns.

        So the entry point that WINS drops the others' drafts. The visit's is not
        dropped here: it is guarded instead (a live visit refuses the tap
        outright), because the visit is the one flow with a row open on the
        server behind it.
        """
        for key in (SALES_NEARBY_FLOW_KEY, SALES_TRYOUT_FLOW_KEY):
            if key != own_key:
                context.user_data.pop(key, None)

    async def _render(self, update: Update, text: str, keyboard) -> None:
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
        else:
            await update.message.reply_text(
                text, reply_markup=keyboard, parse_mode='HTML', disable_notification=True
            )

    @require_auth
    @require_sales_agent
    async def show_hub(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        try:
            text = (
                f"📍 <b>{i18n.get('staff.sales.hub.title', language)}</b>\n"
                f"{i18n.get('staff.sales.hub.hint', language)}"
            )
            await self._render(update, text, SalesKeyboards.hub(language))
        except Exception as e:
            logger.error(f"Error showing sales hub: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_sales_agent
    async def show_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        language = await self._get_language(update, context)
        # The registered pattern is `^staff_sales_list_\w+$` (see bot.py for
        # why it cannot be narrower), so the SHAPE is checked here instead:
        # anything but a scope this hub draws is answered, not raised. Raising
        # would happen outside the try below and leave the tap spinning.
        # The allowlist is the set of scopes `OutletService.list_for_agent`
        # answers AND this hub has a `list.title_<scope>` row for — widening
        # one without the other ships a humanised key as a screen title.
        parts = query.data.split('_')  # staff_sales_list_<scope>[_<page>]
        scope = parts[3] if len(parts) > 3 else ''
        try:
            page = int(parts[4]) if len(parts) > 4 else 1
        except ValueError:
            page = 0
        if scope not in ('due', 'prospects', 'all') or page < 1:
            logger.warning("Unroutable sales list callback: %s", query.data)
            await query.answer()
            return
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return
        try:
            async with api_client as client:
                response = await client.sales_list_outlets(token, scope=scope, page=page)
            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return
            items = (response.data or {}).get('items', [])
            title = i18n.get(f'staff.sales.list.title_{scope}', language)
            text = f"<b>{title}</b>" if items else f"<b>{title}</b>\n{i18n.get('staff.sales.list.empty', language)}"
            await self._render(update, text, SalesKeyboards.outlet_list(language, items, scope, page))
        except Exception as e:
            logger.error(f"Error listing outlets: {e}", exc_info=True)
            await self._handle_error(update, context)

    async def _show_card(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                         outlet_id: int, language: str) -> None:
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return
        async with api_client as client:
            response = await client.sales_get_outlet(token, outlet_id)
        if not response.success:
            await self._handle_api_response_error(update, response, language)
            return
        outlet = (response.data or {}).get('outlet') or {}
        await self._render(
            update, format_outlet_card(outlet, language), SalesKeyboards.outlet_card(language, outlet)
        )

    @require_auth
    @require_sales_agent
    async def show_outlet(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        outlet_id = int(update.callback_query.data.split('_')[-1])
        try:
            await self._show_card(update, context, outlet_id, language)
        except Exception as e:
            logger.error(f"Error showing outlet {outlet_id}: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_sales_agent
    async def request_activation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        outlet_id = int(update.callback_query.data.split('_')[-1])
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return
        try:
            async with api_client as client:
                response = await client.sales_request_activation(token, outlet_id)
            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return
            outlet = (response.data or {}).get('outlet') or {}
            await update.callback_query.answer()
            text = (
                f"⏳ {i18n.get('staff.sales.card.activation_requested', language)}\n\n"
                f"{format_outlet_card(outlet, language)}"
            )
            await update.callback_query.edit_message_text(
                text, reply_markup=SalesKeyboards.outlet_card(language, outlet), parse_mode='HTML'
            )
        except Exception as e:
            logger.error(f"Error requesting activation for outlet {outlet_id}: {e}", exc_info=True)
            await self._handle_error(update, context)
