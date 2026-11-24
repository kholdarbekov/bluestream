"""Standalone bottle collection and fine creation flow for delivery drivers."""

import logging
import uuid
from decimal import Decimal
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

# Both quantity bounds are enforced twice -- here, where the driver can still be
# told what was wrong with what they typed, and at the HTTP boundary by
# `DriverBottleSessionOpenRequest` / `DriverBottleSessionCloseRequest` -- so the
# numbers themselves live in exactly one place. They are deliberately DIFFERENT
# numbers: the load-out is a business ceiling, the return is a storage bound
# (over-returning is legitimate). See shared/staff_constants.py for both.
from shared.staff_constants import BOTTLE_RETURN_COLUMN_CEILING, MAX_BOTTLES_PER_SESSION
from staff_bot.api_client import TRANSPORT_AMBIGUOUS_ERROR_CODE, api_client
from staff_bot.handlers.base import BaseHandler
from staff_bot.i18n import i18n
from staff_bot.keyboards.common import CommonKeyboards
from staff_bot.keyboards.delivery import DeliveryKeyboards
from staff_bot.permissions import require_auth, require_delivery_driver
from staff_bot.utils import flow_state
from staff_bot.utils.money_input import parse_typed_money
from staff_bot.utils.formatters import (
    escape_html,
    format_currency,
    format_quantity,
    format_session_unit_lines,
    format_transfer_lines,
    format_truck_bottle_lines,
    format_user_card,
)
from staff_bot.utils.search import detect_search_type

logger = logging.getLogger(__name__)

BOTTLE_COLLECTION_SEARCH_INPUT = 107
BOTTLE_COLLECTION_QTY_INPUT = 108
BOTTLE_COLLECTION_NOTE_INPUT = 109
BOTTLE_FINE_QTY_INPUT = 110
BOTTLE_FINE_AMOUNT_INPUT = 111
BOTTLE_FINE_NOTE_INPUT = 112
BOTTLES_LOADED_INPUT = 113
BOTTLES_RETURNED_WH_INPUT = 114
BOTTLE_SESSION_LOADED_QTY_INPUT = 120
BOTTLE_SESSION_RETURNED_QTY_INPUT = 121
BOTTLE_TRANSFER_DRIVER_SELECT = 122
BOTTLE_TRANSFER_FULL_QTY_INPUT = 123
BOTTLE_TRANSFER_CONFIRM_FULL_QTY_INPUT = 124
BOTTLE_TRANSFER_SEND_CHOICE = 125
BOTTLE_TRANSFER_EMPTY_QTY_INPUT = 126
BOTTLE_TRANSFER_CONFIRM_EMPTY_QTY_INPUT = 127
BOTTLE_TRANSFER_UNIT_QTY_INPUT = 128
BOTTLE_TRANSFER_CONFIRM_UNIT_QTY_INPUT = 129


class BottleCollectionHandler(BaseHandler):
    """Handle standalone bottle collection and fine creation outside delivery flow."""

    @staticmethod
    async def _clear_flow(
        context: ContextTypes.DEFAULT_TYPE,
        update: Update = None,
    ):
        """Clear the standalone-bottle flow flag plus the Redis mirror, and
        deliver any pool-insertion suggestions deferred while the driver was
        mid-collection. See `flow_state.clear_and_drain` for the queue
        protocol; `update` is optional for legacy callers."""
        context.user_data.pop('pending_bottle_collection_flow', None)
        if update and update.effective_user:
            language = context.user_data.get('language') if context else None
            await flow_state.clear_and_drain(
                update.effective_user.id, context.bot, language=language
            )

    @staticmethod
    def _begin_flow(
        context: ContextTypes.DEFAULT_TYPE,
        *,
        customer_id: int,
        address_id: int,
        action: str = None,
    ) -> dict:
        """Start a FRESH standalone-bottle flow at (customer, address).

        CLEAR-ON-ENTRY, the counterpart to `_finalize_collection`'s
        clear-in-finally. The flow dict is the only record of what the driver is
        doing and the global text router (staff_bot/bot.py) dispatches purely on
        which keys it carries: with `action == 'collect'` and `quantity` set, ANY
        typed text finalises a collection. Mutating the previous dict in place —
        which is what `flow.setdefault`-style entry did — meant an abandoned
        pick left `quantity` armed, so re-entering Collect (possibly at a
        DIFFERENT door) turned the driver's next message into a completed
        collection of a quantity they never picked.

        Only the two cached lookup maps survive: `place_balances` (the fine
        prompt's grouped-place hint) and `picker_place_balances` (the picker's
        can-collect decision). Both are read-only views of the statement that was
        on screen, never step state.

        Deliberately NOT `flow_state.clear_pending_flows`: that SSOT is for
        LEAVING a flow (it drains the deferred pool-suggestion queue, which would
        push an Accept keyboard at a driver in the middle of entering one) and it
        drops the delivery/cash flows this handler has no business touching.
        """
        previous = context.user_data.get('pending_bottle_collection_flow') or {}
        flow = {'customer_id': customer_id, 'address_id': address_id}
        if action:
            flow['action'] = action
        for cached in ('place_balances', 'picker_place_balances'):
            if cached in previous:
                flow[cached] = previous[cached]
        context.user_data['pending_bottle_collection_flow'] = flow
        return flow

    @staticmethod
    def _new_intent_token() -> str:
        """A retry token for ONE decision, reused by every transmission of it.

        Minted when the driver reaches the confirm step — `pick_collection_qty`
        for a collection, `receive_fine_amount` for a fine (a fine has no
        confirm button; the note message IS the confirm) — stored in the flow
        dict, and sent on every submit of that intent.

        It is a SERVER-SIDE FENCE, not a patch for any one transport: the
        backend cannot otherwise tell a duplicate delivery of one POST from a
        second real collection, and duplicates can arrive from a retrying
        client, a proxy, a replayed request from outside this bot, or a future
        client with its own retry loop. (`StaffAPIClient` no longer re-POSTs an
        ambiguous failure — see `RETRY_SAFE_METHODS` in staff_bot/api_client.py
        — so do NOT justify this token by citing that loop.)

        Minted at the CONFIRM step and nowhere else:
          * not at submit time — a token minted per attempt buys exactly
            nothing, and it would also break the token-less body an older flow
            dict must still be able to post;
          * not in `_begin_flow`, where the intent does not exist yet because no
            quantity/amount has been chosen.

        It dies with the flow: `_clear_flow` pops the whole dict from BOTH
        submit paths' `finally`, `_begin_flow` replaces the dict on every
        re-entry (carrying over only the two read-only balance maps — do NOT add
        this key to that allow-list), and `flow_state.clear_pending_flows` drops
        the key outright. A token that OUTLIVED its intent would be worse than a
        duplicate: the backend would swallow the driver's next genuine
        collection at HTTP 200 with no ledger row and no session-tally bump —
        an invisible loss instead of a visible double.

        `uuid4().hex` is 32 lowercase hex chars, which satisfies the backend's
        `\\A[A-Za-z0-9_-]{8,64}\\Z` fullmatch; the server prepends the namespace
        and the authenticated actor id, so the client never controls the whole
        stored key.

        `context.user_data` is in-memory only (staff_bot/bot.py builds the
        Application with no `.persistence(...)`), so a bot restart mid-flow
        loses the token. That is safe rather than lucky: with the flow gone the
        submit paths fail their "do we still know what this is?" guard and never
        POST at all.
        """
        return uuid.uuid4().hex

    async def _handle_submit_failure(self, update: Update, response, language: str):
        """Render a failed collection/fine submit, warning the driver when the
        write MAY already have landed.

        `TRANSPORT_AMBIGUOUS` is stamped by `StaffAPIClient._make_request` on
        the terminal give-up response when the failure happened in a phase where
        the request may already have reached the backend (read/write timeout,
        read/write error, server closing mid-exchange). Everything else — a
        connect-phase exhaustion, a named 4xx, a 5xx — keeps today's copy.

        Why this exists, and why it is not optional: a driver who redoes the
        flow BY HAND mints a NEW token, so the per-intent token cannot dedup
        that path. RULING 1's verb-only retry policy makes that path MORE likely
        (the transport now gives up after ONE ambiguous send instead of three),
        which is exactly why the warning is required. See
        `.superpowers/sdd/2026-08-03-retry-safety/RULINGS.md` RULING 2.

        Scoped to the ambiguous phase ALONE. A connect-phase failure provably
        never reached the backend, so "this may already have been recorded"
        would be a lie there — and a warning that cries wolf is one the driver
        stops reading. Note the default copy is actively the wrong advice here:
        `BaseHandler.API_ERROR_MESSAGE_KEY_MAP` maps the transport's
        "Request failed after retries" to `staff.error.api.service_unavailable`
        ("please try later"), i.e. "do it again" — the one instruction that
        turns a possible duplicate into a certain one.
        """
        error_code = getattr(response, 'error_code', None)
        if error_code == TRANSPORT_AMBIGUOUS_ERROR_CODE:
            logger.warning(
                "Ambiguous transport failure on a bottle write; the driver was "
                "warned it may already be recorded: user=%s error=%s",
                getattr(update.effective_user, 'id', None),
                getattr(response, 'error', None),
            )
            await self._notify_user(
                update,
                f"⚠️ {i18n.get('staff.error.api.maybe_recorded', language)}",
                show_alert=True,
            )
            return
        await self._handle_api_response_error(update, response, language)

    @staticmethod
    def _parse_positive_amount(raw_text: str) -> float:
        """A driver-typed money amount, read by the one money-typing rule.

        Raises ``ValueError`` for anything unreadable or <= 0.
        """
        value = float(parse_typed_money(raw_text))
        if value <= 0:
            raise ValueError("non-positive amount")
        return value

    @staticmethod
    def _place_key(addr: dict) -> tuple:
        """Scope identity of a payload row: the address GROUP when the row is
        grouped, else the address itself.

        ``get_customer_summary``'s ``addresses[]`` rows name the group
        ``address_group_id``; ``get_customer_place_rows`` names it
        ``place_group_id``. Both are the same ``AddressGroup`` — one PLACE, one
        pool of empties.
        """
        if addr.get('is_grouped'):
            group_id = addr.get('address_group_id')
            if group_id is None:
                group_id = addr.get('place_group_id')
            if group_id is not None:
                return ('g', group_id)
        return ('a', addr.get('address_id'))

    @staticmethod
    def _place_balances(summary: dict) -> dict:
        """``{address_id: place_balance}`` for the customer's GROUPED addresses.

        ``place_balance`` IS the empties standing at that door, whoever's
        account they sit on, so there is nothing left to union. Ungrouped
        addresses are omitted entirely — an empty map means "nothing to say".

        Zero and NEGATIVE (over-returned) places are deliberately kept: the
        caller decides what to say about them.
        """
        return {
            addr['address_id']: float(addr.get('place_balance') or 0)
            for addr in (summary.get('addresses') or [])
            if addr.get('is_grouped') and addr.get('address_id') is not None
        }

    @staticmethod
    def _actionable_places(summary: dict) -> list:
        """The DISTINCT places the driver can still act on — one row per place.

        Accepts anything shaped like ``{'addresses': [<place-ish row>, …]}``:
        both ``get_customer_summary()['addresses']`` and the
        ``get_customer_place_rows()`` list qualify.

        Deduped by scope first, because ``addresses`` is keyed by the addresses
        the customer OWNS — two owned addresses in one group are the same
        physical place twice (``get_customer_summary`` warns about this in its
        own docstring).

        Filtered on ``!= 0``, not ``> 0``: an over-returned place has nothing
        left to collect, but a fine is still issuable there, so it must stay
        reachable from the statement screen.
        """
        rows = []
        seen = set()
        for addr in (summary.get('addresses') or []):
            if addr.get('address_id') is None:
                continue
            key = BottleCollectionHandler._place_key(addr)
            if key in seen:
                continue
            seen.add(key)
            if float(addr.get('place_balance') or 0) != 0:
                rows.append(addr)
        return rows

    @staticmethod
    def _build_fine_payload(flow: dict, quantity, fine_amount, notes) -> dict:
        """POST body for ``/api/v1/staff/bottles/fine``.

        A fine is keyed by ADDRESS: ``BottleFine`` carries ``address_id`` plus a
        frozen ``address_group_id`` and has no ``bottle_balance_id`` at all
        (migration ``a3e7d1f9c204`` dropped the column), and the route requires
        ``customer_id``, ``address_id``, ``quantity`` and ``fine_amount``.
        """
        payload = {
            'customer_id': flow.get('customer_id'),
            'address_id': flow.get('address_id'),
            'quantity': quantity,
            'fine_amount': fine_amount,
            'notes': notes,
        }
        # CONDITIONAL for the same reason as the collection body: a flow without
        # a token posts byte-identically to today, so nothing about the un-keyed
        # backend path changes.
        retry_token = flow.get('idempotency_key')
        if retry_token:
            payload['idempotency_key'] = retry_token
        return payload

    @staticmethod
    def _over_returned_line(language: str, value) -> str:
        """Driver-facing "over-returned by N" copy for a NEGATIVE balance.

        One helper for all three call sites (statement total, statement body,
        quantity guard) so the magnitude convention cannot drift at one of them:
        the copy supplies the direction, so what crosses is always
        ``abs(value)`` — never a minus sign the driver has to interpret at the
        door. Callers branch on the sign; this only renders.

        ``format_quantity`` rather than ``int()``: int() truncates toward zero,
        so a place at -0.5 survives the ``!= 0`` actionable filter and would
        otherwise announce "over-returned by 0".
        """
        return i18n.get(
            'staff.delivery.place_over_returned', language,
            count=format_quantity(abs(value)),
        )

    @staticmethod
    def _format_bottle_statement(summary: dict, language: str) -> str:
        """The driver-facing bottle statement, one line per distinct PLACE.

        ``get_customer_summary`` returns no scalar total by design — summing
        ``place_balance`` across ``addresses`` reports a place once per owned
        address. ``cluster_scopes`` is the backend's own one-row-per-place list,
        and its rows are keyed ``balance``, NOT ``place_balance``.

        The total is a SIGNED sum, so it goes negative once the over-returned
        places outweigh the rest. It is named the same way the body lines are —
        a bare minus sign on the header of a driver's screen reads as a bug.
        """
        scopes = summary.get('cluster_scopes') or []
        # Summed as Decimal, not float: 1.1 + 2.2 is 3.3000000000000003 in
        # binary floating point, and that noise would land on the header of a
        # driver's screen. Decimal(str(x)) keeps each scope's decimal value
        # exact, so only real fractions survive.
        total = sum(
            (Decimal(str(scope.get('balance') or 0)) for scope in scopes),
            Decimal('0'),
        )
        fines = summary.get('active_fines_count', 0)
        fine_amount = summary.get('total_fine_amount', 0)

        if total < 0:
            # Deliberately the SAME key as the per-place body line below: both
            # say "over-returned by N" about a signed bottle figure, and the
            # only difference is scope. If you re-word this for one caller, it
            # re-words for the other — add a second key instead.
            total_text = BottleCollectionHandler._over_returned_line(language, total)
        else:
            total_text = format_quantity(total)

        lines = [
            f"📊 <b>{i18n.get('staff.delivery.bottle_statement_title', language)}</b>",
            f"📦 {i18n.get('staff.delivery.total_bottles', language)}: {total_text}",
        ]
        if fines > 0:
            lines.append(
                f"⚠️ {i18n.get('staff.delivery.active_fines', language)}: {fines} "
                f"({format_currency(fine_amount, language=language)})"
            )

        body = []
        seen = set()
        for addr in (summary.get('addresses') or []):
            key = BottleCollectionHandler._place_key(addr)
            if key in seen:
                continue
            seen.add(key)
            balance = float(addr.get('place_balance') or 0)
            if balance == 0:
                continue
            title = addr.get('address_title') or addr.get('full_address', '')[:30]
            marker = ' 👥' if addr.get('is_grouped') else ''
            # An over-returned place is a real record, not a data error — name
            # the state instead of printing a bare "-3" the driver has to
            # interpret at the door.
            if balance < 0:
                detail = BottleCollectionHandler._over_returned_line(language, balance)
            else:
                detail = format_quantity(balance)
            body.append(f"• {escape_html(title)}{marker}: {detail}")

        # The empty state must key off what was RENDERED, not off owning zero
        # addresses: a customer with addresses whose places are all at zero used
        # to get a header and nothing else.
        if not body:
            lines.append(i18n.get('staff.delivery.no_bottle_balance', language))
            return '\n'.join(lines)

        lines.append('')
        lines.extend(body)
        return '\n'.join(lines)

    # ------------------------------------------------------------------
    # Standalone bottle collection flow
    # ------------------------------------------------------------------

    @require_auth
    @require_delivery_driver
    async def start_collection_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Prompt the driver to search for a customer with bottles."""
        language = await self._get_language(update, context)
        await self._clear_flow(context, update)

        text = i18n.get('staff.delivery.bottle_collection_search_prompt', language)
        cancel_keyboard = CommonKeyboards.back_button(language, "staff_cash_hub")
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(
                text, reply_markup=cancel_keyboard, parse_mode='HTML'
            )
        else:
            await update.message.reply_text(
                text, reply_markup=cancel_keyboard, parse_mode='HTML'
            )
        return BOTTLE_COLLECTION_SEARCH_INPUT

    @require_auth
    @require_delivery_driver
    async def receive_collection_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Search customers with bottle balance > 0."""
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END

        query_text = update.message.text.strip()
        if len(query_text) < 2:
            await update.message.reply_text(
                i18n.get('staff.operator.search_too_short', language),
                parse_mode='HTML',
            )
            return BOTTLE_COLLECTION_SEARCH_INPUT

        try:
            search_type = detect_search_type(query_text)
            async with api_client as client:
                # Bottle collection: search all customers (don't filter by COD).
                # The downstream per-customer summary already handles "no bottles"
                # gracefully via 'staff.delivery.no_bottle_balance'.
                response = await client.search_customers(
                    token, query_text,
                    search_type=search_type,
                    only_with_open_cod=False,
                )

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return BOTTLE_COLLECTION_SEARCH_INPUT

            customers = response.data if isinstance(response.data, list) else response.data.get('items', [])
            if not customers:
                await update.message.reply_text(
                    i18n.get('staff.delivery.no_customer_bottle_results', language, query=escape_html(query_text)),
                    reply_markup=CommonKeyboards.back_button(language),
                    parse_mode='HTML',
                )
                return ConversationHandler.END

            # Single message + paginated inline keyboard instead of the old
            # one-message-per-result approach (driver no longer scrolls past
            # 10 separate cards just to tap one).
            top = customers[:10]
            title = i18n.get(
                'staff.delivery.bottle_search_results_title', language,
                count=len(top),
            )
            await update.message.reply_text(
                title,
                reply_markup=DeliveryKeyboards.bottle_search_results(language, top),
                parse_mode='HTML',
            )

            return ConversationHandler.END
        except Exception as exc:
            logger.error("Error searching customers for bottle collection: %s", exc, exc_info=True)
            await self._handle_error(update, context)
            return ConversationHandler.END

    @require_auth
    @require_delivery_driver
    async def show_customer_bottle_statement(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show bottle balance for selected customer + their addresses."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            customer_id = int(query.data.split('_')[-1])
            async with api_client as client:
                response = await client.get_customer_bottle_summary(token, customer_id)

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return

            summary = response.data or {}
            flow = {'customer_id': customer_id}
            # Capture each grouped place's balance while the summary is on
            # screen: the fine prompt then states the empties physically at the
            # place without an extra round trip. Ungrouped addresses are absent
            # from the map, so nothing changes for them.
            flow['place_balances'] = self._place_balances(summary)
            context.user_data['pending_bottle_collection_flow'] = flow

            text = self._format_bottle_statement(summary, language)

            # The buttons and the qty cap must read the SAME endpoint (D7).
            # `/summary` lists one row per address the customer OWNS, while
            # `/addresses` lists one row per PLACE — a group is represented by
            # the lowest-id owned address. Building the picker from `/summary`
            # therefore offered addresses the cap lookup could never match, and
            # tapping them dead-ended on a place that has empties.
            async with api_client as client:
                addr_response = await client.get_customer_bottle_addresses(token, customer_id)

            # "The call failed" and "the call succeeded with nothing actionable"
            # are different screens and must stay different. Swallowing a
            # timeout / 500 / expired token into an empty list would print the
            # balance above a bare Back button — the exact unexplained dead end
            # this handler exists to eliminate.
            if not addr_response.success:
                await self._handle_api_response_error(update, addr_response, language)
                return

            actionable = self._actionable_places({'addresses': addr_response.data or []})

            # Remember each OFFERED place's balance so `select_address` can
            # decide whether Collect is meaningful without a second round trip.
            # Distinct from `place_balances` above, which is the fine prompt's
            # grouped-only map: this one covers every place in the picker,
            # grouped or not.
            flow['picker_place_balances'] = {
                row.get('address_id'): float(row.get('place_balance') or 0)
                for row in actionable
            }
            context.user_data['pending_bottle_collection_flow'] = flow

            if len(actionable) == 1:
                # Auto-skip the address picker: pre-select the single place with
                # a non-zero balance and jump straight to the action picker
                # (driver still chooses Collect vs. Fine).
                only_addr = actionable[0]
                flow['address_id'] = only_addr.get('address_id')
                context.user_data['pending_bottle_collection_flow'] = flow
                keyboard = DeliveryKeyboards.bottle_statement_actions(
                    language, customer_id, only_addr.get('address_id'),
                    # Nothing to collect at an over-returned place, but a fine
                    # is still issuable — the screen must stay actionable.
                    can_collect=float(only_addr.get('place_balance') or 0) > 0,
                )
            elif actionable:
                keyboard = DeliveryKeyboards.bottle_address_selection(
                    language, customer_id, actionable
                )
            else:
                keyboard = CommonKeyboards.back_button(language)

            await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
        except Exception as exc:
            logger.error("Error showing customer bottle statement: %s", exc, exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def select_address(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Driver selects which place they're collecting from.

        The multi-place path lands here; the single-place shortcut in
        :meth:`show_customer_bottle_statement` skips it. Both must apply the
        same rule: an over-returned place has nothing left to collect, so
        offering Collect only dead-ends the driver on the quantity guard.
        """
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)

        try:
            # Parse: staff_bottle_addr_{customer_id}_{address_id}
            parts = query.data.split('_')
            customer_id = int(parts[3])
            address_id = int(parts[4])

            flow = context.user_data.get('pending_bottle_collection_flow') or {}
            flow['customer_id'] = customer_id
            flow['address_id'] = address_id
            context.user_data['pending_bottle_collection_flow'] = flow

            # Fail OPEN when the map is missing (cleared user_data, restarted
            # bot): hiding Collect on a place that actually has empties is the
            # worse failure, and `start_collection` re-reads the live balance.
            known_balance = (flow.get('picker_place_balances') or {}).get(address_id)
            keyboard = DeliveryKeyboards.bottle_statement_actions(
                language, customer_id, address_id,
                can_collect=True if known_balance is None else float(known_balance) > 0,
            )
            await query.edit_message_text(
                i18n.get('staff.delivery.bottle_address_selected', language),
                reply_markup=keyboard,
                parse_mode='HTML',
            )
        except Exception as exc:
            logger.error("Error selecting bottle address: %s", exc, exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def start_collection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show the quantity picker for bottle collection at this address.

        The driver no longer types a quantity — they pick from a numeric
        inline keyboard capped at the address's current bottle balance. This
        eliminates typo errors at the customer's door. Picking a number fires
        :meth:`pick_collection_qty` (callback ``staff_bottle_qty_*``).
        """
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            # Parse: staff_bottle_collect_{customer_id}_{address_id}
            parts = query.data.split('_')
            customer_id = int(parts[3])
            address_id = int(parts[4])

            # CLEAR ON ENTRY, before anything can go wrong or the driver can
            # type: a stale `quantity` from an abandoned pick would otherwise
            # still be armed while this screen — including both dead-end arms
            # below — is on display, and the text router would finalise it.
            flow = self._begin_flow(
                context, customer_id=customer_id, address_id=address_id
            )

            # Look up the PLACE's bottle balance so we can size the picker.
            # One place, one pool — grouped or not — so `place_balance` is the
            # only number there is: the empties standing at this door, whichever
            # member's account they sit on (spec 8).
            async with api_client as client:
                addr_response = await client.get_customer_bottle_addresses(token, customer_id)
            if not addr_response.success:
                # A failed read is not "no bottles": saying so sends the driver
                # away from a door that owes empties.
                await self._clear_flow(context, update)
                await self._handle_api_response_error(update, addr_response, language)
                return
            place_balance = 0.0
            for addr in addr_response.data or []:
                if addr.get('address_id') == address_id:
                    place_balance = float(addr.get('place_balance') or 0)
                    break

            # Over-returned and empty are DIFFERENT states and must be branched
            # apart here — before `bottle_collection_qty_picker`, whose
            # `max(0, int(balance))` clamp would render a picker with nothing on
            # it but Cancel and no word of explanation.
            if place_balance < 0:
                # Nothing to collect, but a fine is still issuable: keep the
                # actions on screen instead of dead-ending on a Back button.
                await query.edit_message_text(
                    self._over_returned_line(language, place_balance),
                    reply_markup=DeliveryKeyboards.bottle_statement_actions(
                        language, customer_id, address_id, can_collect=False,
                    ),
                    parse_mode='HTML',
                )
                return

            balance = int(place_balance)
            if balance <= 0:
                # Distinct copy from the negative arm, but the SAME actions: a
                # fine is issuable at a place with no empties too. This arm also
                # catches 0 < balance < 1 — the picker can label a place "(0.5)"
                # yet int() truncates it to 0 here — so without the actions the
                # positive fractional case would dead-end where its negative
                # mirror does not.
                await query.edit_message_text(
                    i18n.get('staff.delivery.no_bottle_balance', language),
                    reply_markup=DeliveryKeyboards.bottle_statement_actions(
                        language, customer_id, address_id, can_collect=False,
                    ),
                    parse_mode='HTML',
                )
                return

            flow['action'] = 'collect'
            flow['balance'] = balance
            context.user_data['pending_bottle_collection_flow'] = flow
            # Mirror the flow flag in Redis so any webhook-driven prompts get
            # queued instead of interrupting the picker / note step. Notes are
            # the only step that still accepts text input.
            await flow_state.mark_active(
                update.effective_user.id, 'pending_bottle_collection_flow'
            )

            await query.edit_message_text(
                i18n.get('staff.delivery.enter_bottle_collection_qty', language),
                reply_markup=DeliveryKeyboards.bottle_collection_qty_picker(
                    language, customer_id, address_id, balance
                ),
                parse_mode='HTML',
            )
        except Exception as exc:
            logger.error("Error starting bottle collection: %s", exc, exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def pick_collection_qty(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Store the qty selected from the inline picker and prompt for note.

        Called via callback ``staff_bottle_qty_<customer_id>_<address_id>_<qty>``.
        Once ``flow['quantity']`` is set, the global text router routes any
        typed message to :meth:`receive_collection_note`; tapping
        "💾 Save without note" instead invokes :meth:`save_collection_no_note`.
        """
        query = update.callback_query
        language = await self._get_language(update, context)

        flow = context.user_data.get('pending_bottle_collection_flow') or {}
        if flow.get('action') != 'collect':
            # The picker is still on screen but its flow is gone — the driver
            # tapped a main-menu button, or already finished this collection.
            # Answer WITH text (see `_refuse_stale_tap`); a bare answer() here
            # stopped the spinner and told the driver nothing at all.
            await self._refuse_stale_tap(
                update, language,
                reply_markup=CommonKeyboards.back_button(language, "staff_cash_hub"),
            )
            return
        await query.answer()

        try:
            # Parse: staff_bottle_qty_{customer_id}_{address_id}_{qty}
            parts = query.data.split('_')
            customer_id = int(parts[3])
            address_id = int(parts[4])
            qty = int(parts[5])
        except (ValueError, IndexError):
            await self._handle_error(update, context)
            return

        if qty <= 0:
            return

        flow['customer_id'] = customer_id
        flow['address_id'] = address_id
        flow['quantity'] = qty
        # THE CONFIRM STEP: the decision now exists, and the note prompt below
        # is what the driver confirms it from. One token for this decision,
        # reused by every transmission of it — see `_new_intent_token`.
        flow['idempotency_key'] = self._new_intent_token()
        context.user_data['pending_bottle_collection_flow'] = flow

        await query.edit_message_text(
            i18n.get('staff.delivery.enter_bottle_collection_note', language),
            reply_markup=DeliveryKeyboards.bottle_collection_note_prompt(language),
            parse_mode='HTML',
        )

    async def _finalize_collection(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        language: str,
        notes: str,
    ):
        """Submit the standalone bottle collection with the given (possibly empty) notes.

        Shared by :meth:`receive_collection_note` (typed note) and
        :meth:`save_collection_no_note` (button). Renders the success message
        on whichever update channel triggered it (``message`` vs. ``callback_query``).

        CLEAR IN FINALLY, the counterpart to `_begin_flow`'s clear-on-entry.
        This used to clear the flow only on SUCCESS, so after a failed POST the
        flow still carried ``action='collect'`` + ``quantity`` and the global
        text router (staff_bot/bot.py) finalised a collection for the driver's
        NEXT message — a silent re-post of a collection nobody confirmed. A
        collection that did not land must cost the driver one re-pick, never a
        phantom debit at the customer's door.
        """
        flow = context.user_data.get('pending_bottle_collection_flow') or {}
        customer_id = flow.get('customer_id')
        address_id = flow.get('address_id')
        quantity = flow.get('quantity')

        # Pick a sensible reply target for either update kind.
        async def _say(text: str, reply_markup=None):
            if update.callback_query:
                await update.callback_query.edit_message_text(
                    text, reply_markup=reply_markup, parse_mode='HTML'
                )
            else:
                await update.message.reply_text(
                    text, reply_markup=reply_markup, parse_mode='HTML'
                )

        try:
            # Inside the try so an expired session ends the flow too: leaving it
            # armed would post the collection on whatever the driver types after
            # re-authenticating.
            token = await self._get_auth_token(update, context)
            if not token:
                await self._handle_auth_error(update, language)
                return

            if not all([customer_id, address_id, quantity]):
                await _say(i18n.get('staff.error_occurred', language))
                return

            payload = {
                'customer_id': customer_id,
                'address_id': address_id,
                'quantity': quantity,
                'notes': notes,
            }
            # CONDITIONAL, and that is load-bearing: a flow dict without a token
            # — one minted by an older bot process, or by any caller that does
            # not mint — must post the exact four route keys it posts today, so
            # the backend takes its un-keyed path unchanged.
            retry_token = flow.get('idempotency_key')
            if retry_token:
                payload['idempotency_key'] = retry_token

            async with api_client as client:
                response = await client.record_bottle_collection(token, payload)

            if not response.success:
                await self._handle_submit_failure(update, response, language)
                return

            result = response.data or {}
            # `remaining_balance` is the PLACE's balance and is NOT clamped
            # (business_app/api/staff.py), so a collection can leave the place
            # over-returned. Handing a driver "Remaining balance: -3" reads as
            # an error; name the state and pass the magnitude.
            remaining = float(result.get('remaining_balance', 0) or 0)
            if remaining < 0:
                receipt = i18n.get(
                    'staff.delivery.bottle_collection_recorded_over_returned', language,
                    quantity=quantity,
                    remaining=format_quantity(abs(remaining)),
                )
            else:
                receipt = i18n.get(
                    'staff.delivery.bottle_collection_recorded', language,
                    quantity=quantity,
                    remaining=format_quantity(remaining),
                )
            await _say(receipt, reply_markup=CommonKeyboards.back_button(language))
        except Exception as exc:
            logger.error("Error recording bottle collection: %s", exc, exc_info=True)
            await self._handle_error(update, context)
        finally:
            # Success, refusal, backend failure or crash — the collect flow is
            # over either way and must never be left armed for the next text.
            await self._clear_flow(context, update)

    @require_auth
    @require_delivery_driver
    async def receive_collection_note(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Finalize standalone bottle collection from a typed note."""
        language = await self._get_language(update, context)
        notes = update.message.text.strip()
        await self._finalize_collection(update, context, language, notes)
        return ConversationHandler.END

    @require_auth
    @require_delivery_driver
    async def save_collection_no_note(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Finalize collection via the 'Save without note' inline button (notes='')."""
        language = await self._get_language(update, context)
        await self._finalize_collection(update, context, language, '')

    # ------------------------------------------------------------------
    # Manual fine creation flow
    # ------------------------------------------------------------------

    @require_auth
    @require_delivery_driver
    async def start_fine(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start fine creation from customer statement."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)

        try:
            # Parse: staff_bottle_fine_{customer_id}_{address_id}
            parts = query.data.split('_')
            customer_id = int(parts[3])
            address_id = int(parts[4])

            # Clear on entry: `start_collection` and `start_fine` share ONE flow
            # dict and the text router dispatches on which keys are set, so a
            # quantity left by an abandoned pick must not survive into the fine
            # steps (and vice versa).
            flow = self._begin_flow(
                context, customer_id=customer_id, address_id=address_id, action='fine'
            )
            await flow_state.mark_active(
                update.effective_user.id, 'pending_bottle_collection_flow'
            )

            prompt = i18n.get('staff.delivery.enter_fine_bottle_qty', language)
            # Fining at a grouped place: the empties are pooled across the
            # members, so the driver fines against the PLACE's balance, not one
            # account's slice (spec 8). A zero place says nothing — there is no
            # figure to quote — but an over-returned one has plenty to say, and
            # going silent there was the whole reason a driver could not tell a
            # negative place from a missing one. Both branches keep the `{union}`
            # kwarg name: renaming it would make `str.format` raise, and
            # staff_bot/i18n.py catches that and prints the RAW template.
            place_balance = (flow.get('place_balances') or {}).get(address_id)
            if place_balance and place_balance < 0:
                prompt += "\n" + i18n.get(
                    'staff.delivery.fine_place_over_returned_hint', language,
                    union=format_quantity(abs(place_balance)),
                )
            elif place_balance and place_balance > 0:
                prompt += "\n" + i18n.get(
                    'staff.delivery.fine_place_union_hint', language,
                    union=format_quantity(place_balance),
                )

            await query.edit_message_text(
                prompt,
                reply_markup=CommonKeyboards.flow_cancel(language),
                parse_mode='HTML',
            )
            return BOTTLE_FINE_QTY_INPUT
        except Exception as exc:
            logger.error("Error starting bottle fine: %s", exc, exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def receive_fine_bottle_qty(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Receive how many bottles to fine for."""
        language = await self._get_language(update, context)
        flow = context.user_data.get('pending_bottle_collection_flow') or {}
        if flow.get('action') != 'fine':
            return ConversationHandler.END

        try:
            qty = int(update.message.text.strip())
            if qty <= 0:
                raise ValueError
        except (TypeError, ValueError):
            await update.message.reply_text(
                i18n.get('staff.delivery.invalid_bottle_count', language)
            )
            return BOTTLE_FINE_QTY_INPUT

        flow['fine_quantity'] = qty
        context.user_data['pending_bottle_collection_flow'] = flow
        await update.message.reply_text(
            i18n.get('staff.delivery.enter_fine_amount', language),
            reply_markup=CommonKeyboards.flow_cancel(language),
            parse_mode='HTML',
        )
        return BOTTLE_FINE_AMOUNT_INPUT

    @require_auth
    @require_delivery_driver
    async def receive_fine_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Receive monetary fine amount."""
        language = await self._get_language(update, context)
        flow = context.user_data.get('pending_bottle_collection_flow') or {}
        if flow.get('action') != 'fine':
            return ConversationHandler.END

        try:
            # `_parse_positive_amount` fences non-finite input by name; a bare
            # `float()` + `<= 0` check lets NaN and Infinity straight through.
            amount = self._parse_positive_amount(update.message.text)
        except (TypeError, ValueError):
            await update.message.reply_text(
                i18n.get('staff.delivery.invalid_amount', language)
            )
            return BOTTLE_FINE_AMOUNT_INPUT

        flow['fine_amount'] = amount
        # THE CONFIRM STEP for a fine: there is no confirm BUTTON — the driver
        # types qty, then amount, then a note, and the note message IS the
        # confirm — so this is the last state before the money-carrying POST.
        # Minting in `receive_fine_note` would mint a fresh token per typed
        # message. See `_new_intent_token`.
        flow['idempotency_key'] = self._new_intent_token()
        context.user_data['pending_bottle_collection_flow'] = flow
        await update.message.reply_text(
            i18n.get('staff.delivery.enter_fine_note', language),
            reply_markup=CommonKeyboards.flow_cancel(language),
            parse_mode='HTML',
        )
        return BOTTLE_FINE_NOTE_INPUT

    @require_auth
    @require_delivery_driver
    async def receive_fine_note(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Submit fine creation.

        Clears the flow in a ``finally`` for the same reason
        :meth:`_finalize_collection` does: the text router sends the driver's
        next message straight back here once ``fine_quantity`` and
        ``fine_amount`` are both set, so a flow left armed by a failed POST
        re-issues a real, money-carrying fine on the next thing they type.
        """
        language = await self._get_language(update, context)
        flow = context.user_data.get('pending_bottle_collection_flow') or {}
        notes = update.message.text.strip()

        try:
            # Inside the try so an expired session ends the fine flow too.
            token = await self._get_auth_token(update, context)
            if not token:
                await self._handle_auth_error(update, language)
                return ConversationHandler.END

            # The fine is keyed by ADDRESS, and `start_fine` already put one in
            # the flow — no round trip needed. (This used to re-fetch `/summary`
            # purely to look up a `bottle_balance_id`, a column dropped by
            # migration a3e7d1f9c204; the lookup always failed, so every
            # driver-issued fine bailed to a generic error.)
            customer_id = flow.get('customer_id')
            address_id = flow.get('address_id')

            if not customer_id or not address_id:
                await update.message.reply_text(i18n.get('staff.error_occurred', language))
                return ConversationHandler.END

            async with api_client as client:
                response = await client.create_bottle_fine(
                    token,
                    self._build_fine_payload(
                        flow,
                        flow.get('fine_quantity'),
                        flow.get('fine_amount'),
                        notes,
                    ),
                )

            if not response.success:
                await self._handle_submit_failure(update, response, language)
                return ConversationHandler.END

            await update.message.reply_text(
                i18n.get(
                    'staff.delivery.bottle_fine_created', language,
                    quantity=flow.get('fine_quantity'),
                    amount=format_currency(flow.get('fine_amount'), language=language),
                ),
                reply_markup=CommonKeyboards.back_button(language),
                parse_mode='HTML',
            )
            return ConversationHandler.END
        except Exception as exc:
            logger.error("Error creating bottle fine: %s", exc, exc_info=True)
            await self._handle_error(update, context)
            return ConversationHandler.END
        finally:
            # `flow` is a local reference, so the receipt above still reads the
            # submitted figures after the flow itself is gone.
            await self._clear_flow(context, update)

    # ------------------------------------------------------------------
    # Session formatting helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_session(session: dict, language: str) -> str:
        """Format a DriverBottleSession as an HTML summary block.

        Every number is a field the backend publishes (spec 2026-11-09 §4.4):
        the transfers split into full and empties, the truck's full bottles,
        empties and free full bottles (U1), and `current_inventory`, the total
        the warehouse expects back (the return screen shows this block). The
        bot adds nothing up.
        """
        status = session.get('status', 'open')
        loaded = session.get('bottles_loaded', 0)
        delivered = session.get('bottles_delivered', 0)
        collected = session.get('bottles_collected_from_customers', 0)
        full_out = session.get('bottles_transferred_out', 0)
        empties_out = session.get('empties_transferred_out', 0)
        full_in = session.get('bottles_transferred_in', 0)
        empties_in = session.get('empties_transferred_in', 0)
        current = session.get('current_inventory', 0)
        returned = session.get('bottles_returned_to_warehouse')
        discrepancy = session.get('discrepancy')
        started_at = session.get('started_at', '')[:16].replace('T', ' ')
        ref = (session.get('session_ref') or '')[:8]

        session_label = i18n.get('staff.delivery.session_ref_label', language)
        started_label = i18n.get('staff.delivery.session_started_label', language)
        loaded_label = i18n.get('staff.delivery.bottles_loaded_label', language)
        delivered_label = i18n.get('staff.delivery.bottles_delivered_label', language)
        collected_label = i18n.get('staff.delivery.bottles_collected_label', language)
        on_truck_label = i18n.get('staff.delivery.bottles_on_truck_label', language)
        returned_wh_label = i18n.get('staff.delivery.bottles_returned_wh_label', language)
        discrepancy_label = i18n.get('staff.delivery.discrepancy_label', language)

        # `status.upper()` printed the raw enum value ("[OPEN]", "[FORCE_CLOSED]")
        # in every language; route it through the translated status family.
        status_label = i18n.get(f'staff.delivery.bottle_session_status.{status}', language)

        lines = [
            f"🚚 <b>{session_label} #{escape_html(ref)}</b>  [{escape_html(status_label)}]",
            f"⏱ {started_label}: {escape_html(started_at)}",
            "",
            f"📦 {loaded_label}:               <b>{loaded}</b>",
            f"🚚 {delivered_label}:            <b>{delivered}</b>",
            f"♻️ {collected_label}:            <b>{collected}</b>",
        ]
        if full_out or empties_out or full_in or empties_in:
            lines += [
                "📤 " + i18n.get(
                    'staff.delivery.bottles_transferred_out_split', language,
                    full=f"<b>{full_out}</b>", empty=f"<b>{empties_out}</b>",
                ),
                "📥 " + i18n.get(
                    'staff.delivery.bottles_transferred_in_split', language,
                    full=f"<b>{full_in}</b>", empty=f"<b>{empties_in}</b>",
                ),
            ]
        lines.append("─" * 30)
        lines += format_truck_bottle_lines(
            language,
            full=session.get('full_bottles_on_board', 0),
            empties=session.get('empties_on_board', 0),
            free_full=session.get('free_full_bottles', 0),
            units=session.get('units'),
        )
        lines.append(f"🚚 {on_truck_label}:         <b>{current}</b>")
        if returned is not None:
            lines.append(f"🏢 {returned_wh_label}:       <b>{returned}</b>")
        if discrepancy is not None:
            if discrepancy == 0:
                lines.append(i18n.get('staff.delivery.discrepancy_zero', language))
            else:
                lines.append(
                    f"⚠️ {discrepancy_label}:          <b>{discrepancy}</b>"
                )
        return "\n".join(lines)

    @require_auth
    @require_delivery_driver
    async def show_my_accountability(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show driver's current session or most recent closed session."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            async with api_client as client:
                response = await client.get_current_bottle_session(token)

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return

            session = response.data
            if session:
                text = self._format_session(session, language)
            else:
                text = i18n.get('staff.delivery.bottle_accountability_no_data', language)

            await query.edit_message_text(
                text,
                reply_markup=self._session_menu_with_codriver_actions(language, has_own_session=bool(session)),
                parse_mode='HTML',
            )
        except Exception as exc:
            logger.error("Error showing bottle session: %s", exc, exc_info=True)
            await self._handle_error(update, context)

    @staticmethod
    def _session_menu_plus(language: str, extra_rows: list) -> InlineKeyboardMarkup:
        """``DeliveryKeyboards.bottle_session_menu`` with ``extra_rows`` above its Back row, which stays last."""
        rows = [list(row) for row in DeliveryKeyboards.bottle_session_menu(language).inline_keyboard]
        return InlineKeyboardMarkup(rows[:-1] + extra_rows + rows[-1:])

    @staticmethod
    def _membership_status_row(language: str) -> list:
        """The way to the co-driver membership screen, whose Leave is the only way off a colleague's trip."""
        return [InlineKeyboardButton(
            f"🤝 {i18n.get('staff.bottles.current_membership_title', language)}",
            callback_data='bottles_membership_status',
        )]

    @classmethod
    def _session_menu_with_codriver_actions(cls, language: str, *, has_own_session: bool) -> InlineKeyboardMarkup:
        """The session menu plus the two co-driver entry points.

        ``bottles_membership_status`` and ``bottles_invite_driver`` are both
        registered in ``staff_bot/bot.py`` and were both emitted by NOTHING, so
        the handlers behind them — including the ONLY keyboard in the bot that
        carries ``bottles_leave_session`` — could not be reached at all. A
        driver who joined a colleague's session had no way to leave it, and
        every bottle they moved kept landing on the colleague's ledger.

        Placed on "📊 My bottle accountability" because that is the one screen
        that answers "what am I holding, and under whose session?" — the
        question a driver is already asking at the moment they want out, and the
        one a session owner is looking at when they decide to let a colleague
        deliver against their load. (The other candidate, the
        BOTTLE_SESSION_REQUIRED prompt, is only ever shown after an order accept
        is refused — a place nobody navigates to on purpose.)

        Built by EXTENDING ``DeliveryKeyboards.bottle_session_menu`` rather than
        restating it: that keyboard is the single definition of "what can I do
        with my session" and is rendered from a dozen call sites here, so a row
        added there must keep appearing here too.

        A driver with no session of their own also gets "Join a colleague's
        session" here. It used to hang only off the BOTTLE_SESSION_REQUIRED
        prompt after a refused order accept, where nobody looked for it.
        """
        codriver_rows = [
            cls._membership_status_row(language),
            [InlineKeyboardButton(
                i18n.get('staff.bottles.invite_codriver', language),
                callback_data='bottles_invite_driver',
            )],
        ]
        if not has_own_session:
            codriver_rows.insert(0, [InlineKeyboardButton(
                i18n.get('staff.bottles.join_session', language),
                callback_data='bottles_join_session',
            )])
        return cls._session_menu_plus(language, codriver_rows)

    # ------------------------------------------------------------------
    # Session: Open (Load from Warehouse)
    # ------------------------------------------------------------------

    @require_auth
    @require_delivery_driver
    async def start_log_loaded(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start opening a trip: ONE read (the load screen) says what to ask for, or why he may not open one."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END

        try:
            async with api_client as client:
                response = await client.get_bottle_load_screen(token)
        except Exception as exc:
            logger.error("Error reading the load screen: %s", exc, exc_info=True)
            await self._handle_error(update, context)
            return ConversationHandler.END
        if not response.success:
            await self._handle_api_response_error(update, response, language)
            return ConversationHandler.END

        screen = response.data or {}
        refusal = screen.get('refusal')
        if refusal:
            text = self._load_refusal_text(refusal, screen.get('open_session') or {}, language)
            await query.edit_message_text(
                text, reply_markup=self._load_refusal_menu(refusal, language), parse_mode='HTML'
            )
            return ConversationHandler.END

        context.user_data[flow_state.LOAD_FLOW_KEY] = {
            'products': [
                {'product_id': product['product_id'], 'product_name': product['product_name']}
                for product in screen.get('products') or []
            ],
            'bottles': None,
            'units': [],
        }
        await query.edit_message_text(
            i18n.get('staff.delivery.enter_bottles_loaded_qty', language),
            reply_markup=CommonKeyboards.back_button(language, "staff_cash_hub"),
            parse_mode='HTML',
        )
        return BOTTLE_SESSION_LOADED_QTY_INPUT

    @classmethod
    def _load_refusal_menu(cls, refusal: dict, language: str) -> InlineKeyboardMarkup:
        """Under the refusal: the session menu, and for a co-driver, told to leave his colleague's trip first, the way
        to do it (the membership screen and its Leave)."""
        if refusal.get('error_code') == 'BOTTLE_SESSION_ALREADY_OPEN':
            return DeliveryKeyboards.bottle_session_menu(language)
        return cls._session_menu_plus(language, [cls._membership_status_row(language)])

    @staticmethod
    def _load_refusal_text(refusal: dict, open_session: dict, language: str) -> str:
        """The screen a driver who may not open a trip reads: his own trip, or the owner's trip he co-drives."""
        units = refusal.get('units')
        if refusal.get('error_code') == 'BOTTLE_SESSION_ALREADY_OPEN':
            raw_started = open_session.get('started_at')
            started = raw_started[:16].replace('T', ' ') if raw_started else i18n.get('staff.common.unknown_time', language)
            # The unit lines sit under the 18.9 L loaded figure; the instruction stays the last paragraph.
            return '\n'.join([
                i18n.get(
                    'staff.delivery.bottle_session_already_open', language,
                    started=escape_html(started), loaded=open_session.get('bottles_loaded', 0),
                ),
                *format_session_unit_lines(language, units, field='loaded'),
                '',
                i18n.get('staff.delivery.bottle_session_already_open_close_first', language),
            ])
        head = [
            i18n.get(
                'staff.error.api.membership_already_active_owner_detail', language,
                owner=escape_html(refusal.get('owner_name')),
            ),
            i18n.get('staff.delivery.session_bottles_line', language, n=escape_html(refusal.get('bottles_on_board'))),
        ]
        return '\n'.join(head + format_session_unit_lines(language, units, field='on_board'))

    @require_auth
    @require_delivery_driver
    async def receive_bottles_loaded(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Take the next load number: 18.9 L first, then one per product the load screen named; open once at the end."""
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END

        draft = context.user_data.get(flow_state.LOAD_FLOW_KEY)
        if not draft:
            await self._answer_gone_flow(update, context, flow_state.LOAD_FLOW_KEY)
            return ConversationHandler.END

        try:
            count = int(update.message.text.strip())
            # Zero is a real answer here (no 10 L today); an all-zero load is the backend's refusal.
            if count < 0 or count > MAX_BOTTLES_PER_SESSION:
                raise ValueError("outside a plausible truck load-out")
        except (TypeError, ValueError):
            await update.message.reply_text(
                i18n.get('staff.delivery.invalid_count_zero_ok', language)
            )
            return BOTTLE_SESSION_LOADED_QTY_INPUT

        self._record_typed_count(draft, count)
        if len(draft['units']) < len(draft['products']):
            await self._ask_unit_qty(update, language, draft, 'staff.delivery.enter_unit_loaded_qty')
            return BOTTLE_SESSION_LOADED_QTY_INPUT
        return await self._submit_load(update, context, language, token, draft)

    @staticmethod
    def _record_typed_count(draft: dict, count: int) -> None:
        """Store one typed number on the Load / Return draft: the 18.9 L number first, then one per trip line."""
        if draft['bottles'] is None:
            draft['bottles'] = count
        else:
            draft['units'].append({'product_id': draft['products'][len(draft['units'])]['product_id'], 'quantity': count})

    @staticmethod
    async def _ask_unit_qty(update: Update, language: str, draft: dict, prompt_key: str):
        """Ask for the next trip line's number (the line after those already typed)."""
        product = draft['products'][len(draft['units'])]
        await update.message.reply_text(
            i18n.get(prompt_key, language, product=escape_html(product['product_name'])),
            reply_markup=CommonKeyboards.back_button(language, "staff_cash_hub"),
            parse_mode='HTML',
        )

    @staticmethod
    async def _reask_from_bottles(update: Update, language: str, draft: dict, prompt_key: str, **draft_changes):
        """Restart the draft at the 18.9 L number (the backend refused the whole set) and ask for it again."""
        draft.update(bottles=None, units=[], **draft_changes)
        await update.message.reply_text(
            i18n.get(prompt_key, language),
            reply_markup=CommonKeyboards.back_button(language, "staff_cash_hub"),
            parse_mode='HTML',
        )

    async def _submit_load(self, update: Update, context: ContextTypes.DEFAULT_TYPE, language: str, token: str, draft: dict):
        """The one open call, with every number typed. An all-zero load is the backend's to refuse; the driver is then
        asked again from the 18.9 L number."""
        try:
            async with api_client as client:
                response = await client.open_bottle_session(
                    token, draft['bottles'], units=draft['units'] if draft['products'] else None
                )

            if not response.success:
                error_code = getattr(response, 'error_code', None)
                if error_code == 'BOTTLE_SESSION_EMPTY_LOAD':
                    await self._handle_api_response_error(update, response, language)
                    await self._reask_from_bottles(update, language, draft, 'staff.delivery.enter_bottles_loaded_qty')
                    return BOTTLE_SESSION_LOADED_QTY_INPUT
                context.user_data.pop(flow_state.LOAD_FLOW_KEY, None)
                if error_code == 'BOTTLE_SESSION_ALREADY_OPEN':
                    await update.message.reply_text(
                        i18n.get('staff.delivery.bottle_session_already_open_short', language),
                        reply_markup=DeliveryKeyboards.bottle_session_menu(language),
                        parse_mode='HTML',
                    )
                    return ConversationHandler.END
                await self._handle_api_response_error(update, response, language)
                return ConversationHandler.END

            context.user_data.pop(flow_state.LOAD_FLOW_KEY, None)
            session = response.data or {}
            ref = (session.get('session_ref') or '')[:8]
            lines = [
                i18n.get('staff.delivery.bottle_session_opened_header', language, ref=escape_html(ref)),
                i18n.get('staff.delivery.session_bottles_line', language, n=escape_html(session.get('bottles_loaded'))),
                *format_session_unit_lines(language, session.get('units'), field='loaded'),
            ]
            await update.message.reply_text(
                '\n'.join(lines),
                reply_markup=DeliveryKeyboards.bottle_session_menu(language),
                parse_mode='HTML',
            )
            return ConversationHandler.END
        except Exception as exc:
            logger.error("Error opening bottle session: %s", exc, exc_info=True)
            context.user_data.pop(flow_state.LOAD_FLOW_KEY, None)
            await self._handle_error(update, context)
            return ConversationHandler.END

    # ------------------------------------------------------------------
    # Session: Close (Return to Warehouse)
    # ------------------------------------------------------------------

    @require_auth
    @require_delivery_driver
    async def start_return_to_warehouse(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show session summary and prompt driver for return count."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END

        # Fetch open session for context display
        context_text = ""
        products = []
        try:
            async with api_client as client:
                response = await client.get_current_bottle_session(token)
            if response.success and response.data:
                # The lines the close must answer come first: a summary that fails to draw costs the summary,
                # never a line (a close without one is refused as incomplete, and the driver types it all again).
                products = self._return_products(response.data)
                context_text = self._format_session(response.data, language) + "\n\n"
            elif response.success and not response.data:
                await query.edit_message_text(
                    i18n.get('staff.delivery.no_active_bottle_session', language),
                    reply_markup=DeliveryKeyboards.bottle_session_menu(language),
                    parse_mode='HTML',
                )
                return ConversationHandler.END
        except Exception as exc:
            # The question is still asked; a close missing a line is refused and asked again over the trip's lines.
            logger.warning("Return screen: the trip read or its summary failed: %s", exc, exc_info=True)

        context.user_data[flow_state.RETURN_FLOW_KEY] = {'products': products, 'bottles': None, 'units': []}
        prompt = i18n.get('staff.delivery.enter_bottles_returned_qty', language)
        await query.edit_message_text(
            context_text + prompt,
            reply_markup=CommonKeyboards.back_button(language, "staff_cash_hub"),
            parse_mode='HTML',
        )
        return BOTTLE_SESSION_RETURNED_QTY_INPUT

    @staticmethod
    def _return_products(session: dict) -> list:
        """The lines a close asks a number for: every one-way unit line on the trip, as the session publishes them."""
        return [
            {'product_id': unit['product_id'], 'product_name': unit.get('product_name')}
            for unit in session.get('units') or []
        ]

    @require_auth
    @require_delivery_driver
    async def receive_bottles_returned(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Take the next return number: 18.9 L first, then one per line on the trip; close once at the end."""
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END

        draft = context.user_data.get(flow_state.RETURN_FLOW_KEY)
        if not draft:
            await self._answer_gone_flow(update, context, flow_state.RETURN_FLOW_KEY)
            return ConversationHandler.END

        try:
            count = int(update.message.text.strip())
            # A driver may hand back MORE than they took out -- the load plus
            # every empty collected at a door -- so there is no plausible upper
            # count to argue with. The only refusal is a number the ledger
            # column cannot hold, which used to arrive as a generic 500.
            if count < 0 or count > BOTTLE_RETURN_COLUMN_CEILING:
                raise ValueError("negative, or past what the ledger column can hold")
        except (TypeError, ValueError):
            await update.message.reply_text(
                i18n.get('staff.delivery.invalid_count_zero_ok', language)
            )
            return BOTTLE_SESSION_RETURNED_QTY_INPUT

        self._record_typed_count(draft, count)
        if len(draft['units']) < len(draft['products']):
            await self._ask_unit_qty(update, language, draft, 'staff.delivery.enter_unit_returned_qty')
            return BOTTLE_SESSION_RETURNED_QTY_INPUT
        return await self._submit_return(update, context, language, token, draft)

    async def _submit_return(self, update: Update, context: ContextTypes.DEFAULT_TYPE, language: str, token: str, draft: dict):
        """The one close call, with every number typed. A close that left a line out is the backend's to refuse; the
        driver is then asked again from the 18.9 L number, over the lines the trip has now."""
        try:
            async with api_client as client:
                response = await client.close_bottle_session(
                    token, draft['bottles'], units_returned=draft['units'] if draft['products'] else None
                )
                if not response.success and getattr(response, 'error_code', None) == 'BOTTLE_SESSION_RETURN_INCOMPLETE':
                    current = await client.get_current_bottle_session(token)
                    if current.success and current.data:
                        await self._handle_api_response_error(update, response, language)
                        await self._reask_from_bottles(
                            update, language, draft, 'staff.delivery.enter_bottles_returned_qty',
                            products=self._return_products(current.data),
                        )
                        return BOTTLE_SESSION_RETURNED_QTY_INPUT

            context.user_data.pop(flow_state.RETURN_FLOW_KEY, None)
            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return ConversationHandler.END

            session = response.data or {}
            discrepancy = session.get('discrepancy', 0)
            ref = (session.get('session_ref') or '')[:8]

            disc_line = (
                i18n.get('staff.delivery.discrepancy_zero', language)
                if discrepancy == 0
                else i18n.get('staff.delivery.discrepancy_nonzero', language, discrepancy=discrepancy)
            )
            text = i18n.get(
                'staff.delivery.bottle_session_closed', language,
                count=draft['bottles'], disc_line=disc_line, ref=escape_html(ref),
            )
            unit_lines = [
                i18n.get('staff.delivery.discrepancy_unit_zero', language, product=escape_html(unit.get('product_name')))
                if unit.get('discrepancy') == 0
                else i18n.get(
                    'staff.delivery.discrepancy_unit_line', language,
                    product=escape_html(unit.get('product_name')), discrepancy=escape_html(unit.get('discrepancy')),
                )
                for unit in session.get('units') or []
                if unit.get('discrepancy') is not None
            ]
            await update.message.reply_text(
                '\n'.join([text, *unit_lines]),
                reply_markup=DeliveryKeyboards.bottle_session_menu(language),
                parse_mode='HTML',
            )
            return ConversationHandler.END
        except Exception as exc:
            logger.error("Error closing bottle session: %s", exc, exc_info=True)
            context.user_data.pop(flow_state.RETURN_FLOW_KEY, None)
            await self._handle_error(update, context)
            return ConversationHandler.END

    # ------------------------------------------------------------------
    # Transfer: Sender side
    # ------------------------------------------------------------------
    # A transfer moves full bottles, empties and each one-way unit line (spec 2026-11-09 T1-T3, 2026-11-18 §6.7):
    # the sender picks a colleague, then sends everything on the truck or chooses the numbers, one per line (full,
    # empties, then each unit line). `pending_transfer_flow` keeps the flow alive. It is stamped
    # when the picker opens with the backend's transferable numbers (what the
    # send screen shows) and registered with `flow_state`, so walking away drops
    # it and every button left in the chat is refused out loud. It caps nothing:
    # the backend caps each number, and its refusal says which one was too high.

    # What the transfer itself drops when it starts over or ends; every key is also in
    # `flow_state.PENDING_FLOW_USER_DATA_KEYS`, so leaving by a menu tap drops them too.
    # The typed counts are a draft: the full count, then the empties, then one count per one-way unit line.
    TRANSFER_FLOW_KEYS = (
        'pending_transfer_flow', 'pending_transfer_receiver_id', 'pending_transfer_full_qty',
        'pending_transfer_empty_qty', 'pending_transfer_units',
    )
    # The receiver's "Enter other numbers" draft: the transfer, the full count and the empties typed so far, and
    # `pending_confirm_transfer_units` = {'lines': the transfer's lines as the inbox published them, 'units': the
    # one-way unit counts typed so far}.
    CONFIRM_FLOW_KEYS = (
        'pending_confirm_transfer_id', 'pending_confirm_transfer_full', 'pending_confirm_transfer_empty',
        'pending_confirm_transfer_units',
    )
    # Both conversations sit in handler group 0 with the send registered first, so while both are armed a
    # typed number goes to the send whichever prompt is on screen. Entering either one drops the other's
    # draft; the displaced one then answers its next message as timed out (`_answer_gone_flow`) and ends,
    # and its timeout drops only its own keys (bot.py `_flow_timeout(own_keys=...)`).

    # The typed counts alone: what choosing amounts again, or a redrawn send screen, starts over.
    TRANSFER_DRAFT_KEYS = ('pending_transfer_full_qty', 'pending_transfer_empty_qty', 'pending_transfer_units')

    # Refusals of the amounts themselves. The colleague is still right, so the send screen is drawn again
    # under the refusal, with the numbers on the truck now.
    SEND_SCREEN_REFUSALS = frozenset({'BOTTLE_TRANSFER_EXCEEDS_INVENTORY', 'BOTTLE_TRANSFER_NOTHING_TO_SEND'})

    @staticmethod
    def _drop(context: ContextTypes.DEFAULT_TYPE, keys) -> None:
        for key in keys:
            context.user_data.pop(key, None)

    @staticmethod
    def _transfer_flow_from(session: dict) -> dict:
        """What the send screen shows: the backend's transferable numbers, as published, with its one decision on
        whether there is anything to send (`transferable_any`) and its one-way unit lines."""
        return {
            'transferable_full': session.get('transferable_full', 0),
            'transferable_empties': session.get('transferable_empties', 0),
            'transferable_any': bool(session.get('transferable_any')),
            'units': session.get('units') or [],
        }

    @staticmethod
    def _nothing_to_send(flow: dict) -> bool:
        """The one answer to "is there anything to hand over": the backend's `transferable_any`."""
        return not flow['transferable_any']

    @staticmethod
    def _flow_lines(flow: dict) -> list:
        """The flow's numbers in `transfer_lines`' shape, each line's `transferable` as published."""
        return [
            {'kind': 'full', 'transferable': flow['transferable_full']},
            {'kind': 'empty', 'transferable': flow['transferable_empties']},
            *(
                {'kind': 'product', 'product_name': unit.get('product_name'), 'transferable': unit.get('transferable')}
                for unit in flow['units']
            ),
        ]

    @staticmethod
    def _send_screen(language: str, flow: dict, *, refusal: Optional[str] = None):
        """The send screen's text and buttons: Send all, or Choose amounts. `refusal` goes above it."""
        full, empties = flow['transferable_full'], flow['transferable_empties']
        lines = [
            i18n.get('staff.delivery.transfer_send_prompt', language),
            *format_truck_bottle_lines(language, full=full, empties=empties),
            *format_session_unit_lines(language, flow['units'], field='transferable'),
        ]
        if refusal:
            lines = [refusal, "", *lines]
        return "\n".join(lines), DeliveryKeyboards.transfer_send_choice(language, BottleCollectionHandler._flow_lines(flow))

    @staticmethod
    def _parse_transfer_count(raw_text) -> Optional[int]:
        """A bottle count typed on the transfer screens, or None for a keypad slip.

        Zero is a real answer (a driver hands over only empties, or counted no
        full bottles). The only upper bound is what the ledger column can hold:
        how many bottles may move is the backend's call.
        """
        try:
            count = int((raw_text or '').strip())
        except ValueError:
            return None
        return count if 0 <= count <= BOTTLE_RETURN_COLUMN_CEILING else None

    @staticmethod
    async def _show(update: Update, text: str, reply_markup=None):
        """Put `text` on screen: edit the tapped message, or answer the typed one."""
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode='HTML')
        else:
            await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='HTML')

    async def _live_transfer_flow(self, update: Update, context: ContextTypes.DEFAULT_TYPE, language: str, *,
                                  receiver_required: bool) -> Optional[dict]:
        """The sender's flow if this tap still belongs to it; otherwise refuse the tap and return None.

        Gone means the driver left (`flow_state.clear_pending_flows` owns the
        keys) or the transfer was already sent, so the tap is a stale button in
        the scrollback. A send-screen button also needs the colleague picked in
        this flow: one from an earlier send screen, tapped while a new flow is
        still at its picker, names nobody.
        """
        flow = context.user_data.get('pending_transfer_flow')
        if flow is None or (receiver_required and context.user_data.get('pending_transfer_receiver_id') is None):
            await self._refuse_stale_tap(
                update, language,
                reply_markup=DeliveryKeyboards.bottle_session_menu(language),
            )
            return None
        return flow

    @require_auth
    @require_delivery_driver
    async def start_transfer_bottles(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start transfer flow: check open session, then show active driver list."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END

        # A fresh flow: nothing an earlier, abandoned one picked or typed carries
        # over, and its send screen stays dead even if this one stops early. A
        # "Enter other numbers" being typed is abandoned too: its prompt sits behind
        # this flow, and a number that reaches it must not count a transfer.
        self._drop(context, self.TRANSFER_FLOW_KEYS + self.CONFIRM_FLOW_KEYS)

        # Must have open session
        try:
            async with api_client as client:
                response = await client.get_current_bottle_session(token)
            if not response.success:
                # A failed read is not "no open session": the resolver says why.
                await self._handle_api_response_error(update, response, language)
                return ConversationHandler.END
            if not response.data:
                await query.edit_message_text(
                    i18n.get('staff.delivery.no_active_bottle_session', language),
                    reply_markup=DeliveryKeyboards.bottle_session_menu(language),
                    parse_mode='HTML',
                )
                return ConversationHandler.END

            flow = self._transfer_flow_from(response.data)
            if self._nothing_to_send(flow):
                await query.edit_message_text(
                    i18n.get('staff.delivery.no_bottles_to_transfer', language),
                    reply_markup=DeliveryKeyboards.bottle_session_menu(language),
                    parse_mode='HTML',
                )
                return ConversationHandler.END
        except Exception as exc:
            logger.error("Error checking session for transfer: %s", exc, exc_info=True)
            await self._handle_error(update, context)
            return ConversationHandler.END

        # Every active driver the backend says may receive from this sender, the
        # same rule the send is checked against. A failed read is not "no drivers
        # on shift": the resolver says why.
        async with api_client as client:
            drivers_response = await client.get_transfer_recipients(token)
        if not drivers_response.success:
            await self._handle_api_response_error(update, drivers_response, language)
            return ConversationHandler.END
        drivers = drivers_response.data or []

        if not drivers:
            await query.edit_message_text(
                i18n.get('staff.delivery.no_active_drivers', language),
                reply_markup=DeliveryKeyboards.bottle_session_menu(language),
                parse_mode='HTML',
            )
            return ConversationHandler.END

        context.user_data['pending_transfer_flow'] = flow
        await query.edit_message_text(
            i18n.get('staff.delivery.select_transfer_receiver', language),
            reply_markup=DeliveryKeyboards.driver_select_for_transfer(language, drivers),
            parse_mode='HTML',
        )
        return BOTTLE_TRANSFER_DRIVER_SELECT

    @require_auth
    @require_delivery_driver
    async def receive_transfer_driver_select(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Store the colleague and show the send screen: Send all, or Choose amounts."""
        query = update.callback_query
        language = await self._get_language(update, context)

        flow = await self._live_transfer_flow(update, context, language, receiver_required=False)
        if flow is None:
            return ConversationHandler.END
        await query.answer()

        data = query.data  # e.g. "staff_transfer_driver_42"
        try:
            receiver_id = int(data.split('_')[-1])
        except (ValueError, IndexError):
            await self._handle_error(update, context)
            return ConversationHandler.END

        context.user_data['pending_transfer_receiver_id'] = receiver_id
        await self._show(update, *self._send_screen(language, flow))
        return BOTTLE_TRANSFER_SEND_CHOICE

    @require_auth
    @require_delivery_driver
    async def send_all_bottles(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """"Send all": the backend sends whatever is on the truck when it books the transfer (T3)."""
        query = update.callback_query
        language = await self._get_language(update, context)
        if await self._live_transfer_flow(update, context, language, receiver_required=True) is None:
            return ConversationHandler.END
        await query.answer()
        return await self._submit_transfer(update, context, language, send_all=True)

    @require_auth
    @require_delivery_driver
    async def choose_transfer_amounts(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """"Choose amounts": ask full bottles first, then empties, then each one-way unit line, each with the
        backend's maximum."""
        query = update.callback_query
        language = await self._get_language(update, context)
        flow = await self._live_transfer_flow(update, context, language, receiver_required=True)
        if flow is None:
            return ConversationHandler.END
        await query.answer()

        self._drop(context, self.TRANSFER_DRAFT_KEYS)
        await query.edit_message_text(
            i18n.get('staff.delivery.enter_transfer_full_qty', language, max=flow['transferable_full']),
            parse_mode='HTML',
        )
        return BOTTLE_TRANSFER_FULL_QTY_INPUT

    @require_auth
    @require_delivery_driver
    async def receive_transfer_full_quantity(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Keep the full count, then ask for the empties."""
        language = await self._get_language(update, context)
        flow = context.user_data.get('pending_transfer_flow')
        if flow is None or context.user_data.get('pending_transfer_receiver_id') is None:
            await self._answer_gone_flow(update, context, 'pending_transfer_flow')
            return ConversationHandler.END

        full = self._parse_transfer_count(update.message.text)
        if full is None:
            await update.message.reply_text(i18n.get('staff.delivery.invalid_count_zero_ok', language))
            return BOTTLE_TRANSFER_FULL_QTY_INPUT

        context.user_data['pending_transfer_full_qty'] = full
        await update.message.reply_text(
            i18n.get('staff.delivery.enter_transfer_empty_qty', language, max=flow['transferable_empties']),
            parse_mode='HTML',
        )
        return BOTTLE_TRANSFER_EMPTY_QTY_INPUT

    @require_auth
    @require_delivery_driver
    async def receive_transfer_empty_quantity(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Keep the empties; send them with the full count, or ask the first one-way unit line when there is one."""
        language = await self._get_language(update, context)
        full = context.user_data.get('pending_transfer_full_qty')
        if full is None or context.user_data.get('pending_transfer_receiver_id') is None:
            await self._answer_gone_flow(update, context, 'pending_transfer_flow')
            return ConversationHandler.END

        empties = self._parse_transfer_count(update.message.text)
        if empties is None:
            await update.message.reply_text(i18n.get('staff.delivery.invalid_count_zero_ok', language))
            return BOTTLE_TRANSFER_EMPTY_QTY_INPUT

        flow = context.user_data.get('pending_transfer_flow')
        if flow is None:
            await self._answer_gone_flow(update, context, 'pending_transfer_flow')
            return ConversationHandler.END
        if not flow['units']:
            return await self._submit_transfer(
                update, context, language, full_quantity=full, empty_quantity=empties,
            )

        context.user_data['pending_transfer_empty_qty'] = empties
        context.user_data['pending_transfer_units'] = []
        await self._ask_transfer_unit(update, language, flow['units'][0])
        return BOTTLE_TRANSFER_UNIT_QTY_INPUT

    @staticmethod
    async def _ask_transfer_unit(update: Update, language: str, unit: dict) -> None:
        await update.message.reply_text(
            i18n.get(
                'staff.delivery.enter_transfer_unit_qty', language,
                product=escape_html(unit.get('product_name')), max=unit.get('transferable'),
            ),
            parse_mode='HTML',
        )

    @require_auth
    @require_delivery_driver
    async def receive_transfer_unit_quantity(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Keep one unit line's count and ask the next line; after the last, send everything typed."""
        language = await self._get_language(update, context)
        flow = context.user_data.get('pending_transfer_flow')
        drafted = context.user_data.get('pending_transfer_units')
        full = context.user_data.get('pending_transfer_full_qty')
        empties = context.user_data.get('pending_transfer_empty_qty')
        if (flow is None or drafted is None or full is None or empties is None
                or context.user_data.get('pending_transfer_receiver_id') is None):
            await self._answer_gone_flow(update, context, 'pending_transfer_flow')
            return ConversationHandler.END

        quantity = self._parse_transfer_count(update.message.text)
        if quantity is None:
            await update.message.reply_text(i18n.get('staff.delivery.invalid_count_zero_ok', language))
            return BOTTLE_TRANSFER_UNIT_QTY_INPUT

        unit = flow['units'][len(drafted)]
        drafted.append({'product_id': unit['product_id'], 'quantity': quantity})
        if len(drafted) < len(flow['units']):
            await self._ask_transfer_unit(update, language, flow['units'][len(drafted)])
            return BOTTLE_TRANSFER_UNIT_QTY_INPUT

        # A line the driver answered 0 for moves nothing: it is not sent.
        units = [line for line in drafted if line['quantity']]
        return await self._submit_transfer(
            update, context, language, full_quantity=full, empty_quantity=empties, units=units or None,
        )

    async def _submit_transfer(self, update: Update, context: ContextTypes.DEFAULT_TYPE, language: str,
                               **amounts) -> int:
        """POST the transfer. The receipt names what the backend recorded.

        A refusal of the amounts (`SEND_SCREEN_REFUSALS`) keeps the colleague and returns to the send screen,
        redrawn with the numbers on the truck now. Every other answer ends the flow.
        """
        receiver_id = context.user_data.get('pending_transfer_receiver_id')
        next_state = ConversationHandler.END
        try:
            token = await self._get_auth_token(update, context)
            if not token:
                await self._handle_auth_error(update, language)
                return next_state

            async with api_client as client:
                response = await client.initiate_bottle_transfer(token, receiver_id, **amounts)

            if not response.success:
                if response.error_code in self.SEND_SCREEN_REFUSALS:
                    next_state = await self._send_screen_after_refusal(update, context, language, token, response)
                if next_state == ConversationHandler.END:
                    await self._handle_api_response_error(update, response, language)
                return next_state

            transfer = response.data or {}
            ref = (transfer.get('transfer_ref') or '')[:8]
            await self._show(
                update,
                i18n.get(
                    'staff.delivery.bottle_transfer_sent_lines', language,
                    lines=format_transfer_lines(language, transfer.get('lines'), field='declared'),
                    ref=escape_html(ref),
                ),
                reply_markup=DeliveryKeyboards.bottle_session_menu(language),
            )
            return next_state
        except Exception as exc:
            logger.error("Error initiating bottle transfer: %s", exc, exc_info=True)
            await self._handle_error(update, context)
            next_state = ConversationHandler.END
            return next_state
        finally:
            if next_state == ConversationHandler.END:
                self._drop(context, self.TRANSFER_FLOW_KEYS)

    async def _send_screen_after_refusal(self, update: Update, context: ContextTypes.DEFAULT_TYPE, language: str,
                                         token: str, refusal) -> int:
        """Redraw the send screen under the backend's refusal of the amounts, with the truck as it is now.

        Returns BOTTLE_TRANSFER_SEND_CHOICE, or END when there is no screen to draw: the session cannot be
        read again, or nothing is left to send. The caller then shows the refusal with its remedy button.
        """
        async with api_client as client:
            response = await client.get_current_bottle_session(token)
        if not response.success or not response.data:
            return ConversationHandler.END
        flow = self._transfer_flow_from(response.data)
        if self._nothing_to_send(flow):
            return ConversationHandler.END

        context.user_data['pending_transfer_flow'] = flow
        self._drop(context, self.TRANSFER_DRAFT_KEYS)
        reason = f"❌ {self._resolve_response_error(language, refusal, html=True)}"
        await self._show(update, *self._send_screen(language, flow, refusal=reason))
        return BOTTLE_TRANSFER_SEND_CHOICE

    # ------------------------------------------------------------------
    # Transfer: Receiver confirmation
    # ------------------------------------------------------------------

    @require_auth
    @require_delivery_driver
    async def show_pending_transfers(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show pending incoming transfers for the driver: who sent what, every line, and three answers each."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            async with api_client as client:
                response = await client.get_pending_bottle_transfers(token)

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return

            transfers = response.data or []
            if not transfers:
                await query.edit_message_text(
                    i18n.get('staff.delivery.no_pending_transfers', language),
                    reply_markup=CommonKeyboards.back_button(language, callback_data="staff_cash_hub"),
                    parse_mode='HTML',
                )
                return

            rows = [i18n.get('staff.delivery.pending_transfers_title', language) + "\n"]
            for t in transfers:
                ref = (t.get('transfer_ref') or '')[:8]
                sender = t.get('sender_name') or i18n.get('staff.common.unknown_driver', language)
                rows.append(i18n.get(
                    'staff.delivery.pending_transfer_lines_line', language,
                    sender=escape_html(sender),
                    lines=format_transfer_lines(
                        language, DeliveryKeyboards.transfer_declared_counts(t), field='declared',
                    ),
                    ref=escape_html(ref),
                ))

            await query.edit_message_text(
                "\n".join(rows),
                reply_markup=DeliveryKeyboards.pending_transfer_list(language, transfers),
                parse_mode='HTML',
            )
        except Exception as exc:
            logger.error("Error fetching pending transfers: %s", exc, exc_info=True)
            await self._handle_error(update, context)

    @staticmethod
    def _tapped_transfer_id(query) -> Optional[int]:
        """The transfer id a receiver's button carries last (`staff_transfer_<action>_<id>`), or None."""
        try:
            return int(query.data.split('_')[-1])
        except (ValueError, IndexError, AttributeError):
            return None

    async def _pending_transfer(self, update: Update, context: ContextTypes.DEFAULT_TYPE, language: str,
                                transfer_id: int) -> Optional[dict]:
        """The receiver's pending transfer `transfer_id` as his inbox publishes it, or None, answered.

        The inbox is the backend's word on what still waits for him: a transfer settled since its push was drawn
        is not in it, and is refused the way the backend refuses it, before any question is asked about it.
        """
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return None
        async with api_client as client:
            response = await client.get_pending_bottle_transfers(token)
        if not response.success:
            await self._handle_api_response_error(update, response, language)
            return None
        rows = response.data if isinstance(response.data, list) else []
        transfer = next((row for row in rows if row.get('id') == transfer_id), None)
        if transfer is None:
            await self._refuse_transfer_answer(
                update, language, i18n.get('staff.error.api.transfer_already_handled', language),
            )
        return transfer

    async def _refuse_transfer_answer(self, update: Update, language: str, reason: str, error_code=None) -> None:
        """The receiver's Confirm, count or Reject was refused: the reason, never a fixed "failed" (already handled
        and sent to someone else each need a different next step). On a tap it replaces the tapped message, with
        the code's remedy above the session menu; a typed count is answered below it."""
        text = f"❌ {reason}"
        if update.callback_query:
            await update.callback_query.edit_message_text(
                text,
                reply_markup=self._with_remedy(language, error_code, DeliveryKeyboards.bottle_session_menu(language)),
            )
        else:
            await update.message.reply_text(text, reply_markup=self._with_remedy(language, error_code))

    @staticmethod
    def _declared(lines: list, kind: str) -> int:
        """What the sender declared on the transfer's full or empty line."""
        return next((line.get('declared') or 0 for line in lines if line.get('kind') == kind), 0)

    @staticmethod
    def _unit_lines(lines: list) -> list:
        """The transfer's one-way unit lines, in the order the backend published them."""
        return [line for line in lines if line.get('kind') == 'product']

    @staticmethod
    async def _ask_received_unit(update: Update, language: str, line: dict) -> None:
        """Ask how many of one unit line arrived, showing what the sender declared on it."""
        await update.message.reply_text(
            i18n.get(
                'staff.delivery.enter_received_unit_qty', language,
                product=escape_html(line.get('product_name')), n=line.get('declared') or 0,
            ),
            parse_mode='HTML',
        )

    @require_auth
    @require_delivery_driver
    async def start_transfer_custom_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Entry point: receiver taps 'Enter other numbers'. One number per line of the transfer, each prompt showing
        the sender's number: the full bottles, the empties, then each one-way unit line."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)

        # callback_data: staff_transfer_custom_<transfer_id>
        transfer_id = self._tapped_transfer_id(query)
        if transfer_id is None:
            await self._handle_error(update, context)
            return ConversationHandler.END

        try:
            transfer = await self._pending_transfer(update, context, language, transfer_id)
        except Exception as exc:
            logger.error("Error reading the transfer to count: %s", exc, exc_info=True)
            transfer = None
            await self._handle_error(update, context)
        if transfer is None:
            # This conversation ends here, so any count it was holding ends with it.
            self._drop(context, self.CONFIRM_FLOW_KEYS)
            return ConversationHandler.END

        # A send being chosen or typed is abandoned: its prompt sits ahead of this one, and a
        # number meant for this count must never reach it and post a transfer. A count of another
        # transfer started earlier is replaced by this one.
        self._drop(context, self.TRANSFER_FLOW_KEYS + self.CONFIRM_FLOW_KEYS)
        lines = DeliveryKeyboards.transfer_declared_counts(transfer)
        context.user_data['pending_confirm_transfer_id'] = transfer_id
        context.user_data['pending_confirm_transfer_units'] = {'lines': lines, 'units': []}
        await query.edit_message_text(
            i18n.get('staff.delivery.enter_received_full_qty', language, n=self._declared(lines, 'full')),
            parse_mode='HTML',
        )
        return BOTTLE_TRANSFER_CONFIRM_FULL_QTY_INPUT

    @require_auth
    @require_delivery_driver
    async def receive_transfer_confirm_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """The receiver's one tap: confirm the transfer as declared, every line.

        callback_data is `staff_transfer_confirm_<id>`, or `staff_transfer_confirm_<id>_<qty>` on a message sent
        before the full / empties split. Both confirm as declared: the backend takes the numbers off the row, never
        the old button's quantity.
        """
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)

        try:
            transfer_id = int(query.data.split('_')[3])
        except (ValueError, IndexError):
            await self._handle_error(update, context)
            return

        await self._do_confirm_transfer(update, context, transfer_id, language, confirm_as_declared=True)

    @require_auth
    @require_delivery_driver
    async def receive_transfer_custom_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Keep the full bottles the receiver counted, then ask for the empties."""
        language = await self._get_language(update, context)
        draft = context.user_data.get('pending_confirm_transfer_units')
        if not context.user_data.get('pending_confirm_transfer_id') or draft is None:
            await self._answer_gone_flow(update, context, 'pending_confirm_transfer_id')
            return ConversationHandler.END

        full = self._parse_transfer_count(update.message.text)
        if full is None:
            await update.message.reply_text(i18n.get('staff.delivery.invalid_count_zero_ok', language))
            return BOTTLE_TRANSFER_CONFIRM_FULL_QTY_INPUT

        context.user_data['pending_confirm_transfer_full'] = full
        await update.message.reply_text(
            i18n.get('staff.delivery.enter_received_empty_qty', language, n=self._declared(draft['lines'], 'empty')),
            parse_mode='HTML',
        )
        return BOTTLE_TRANSFER_CONFIRM_EMPTY_QTY_INPUT

    @require_auth
    @require_delivery_driver
    async def receive_transfer_custom_confirm_empties(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Keep the empties the receiver counted; ask the first unit line, or confirm when the transfer has none."""
        language = await self._get_language(update, context)
        transfer_id = context.user_data.get('pending_confirm_transfer_id')
        full = context.user_data.get('pending_confirm_transfer_full')
        draft = context.user_data.get('pending_confirm_transfer_units')
        if not transfer_id or full is None or draft is None:
            await self._answer_gone_flow(update, context, 'pending_confirm_transfer_id')
            return ConversationHandler.END

        empties = self._parse_transfer_count(update.message.text)
        if empties is None:
            await update.message.reply_text(i18n.get('staff.delivery.invalid_count_zero_ok', language))
            return BOTTLE_TRANSFER_CONFIRM_EMPTY_QTY_INPUT

        unit_lines = self._unit_lines(draft['lines'])
        if unit_lines:
            context.user_data['pending_confirm_transfer_empty'] = empties
            await self._ask_received_unit(update, language, unit_lines[0])
            return BOTTLE_TRANSFER_CONFIRM_UNIT_QTY_INPUT

        try:
            await self._do_confirm_transfer(
                update, context, transfer_id, language, confirmed_full=full, confirmed_empty=empties,
            )
        finally:
            # The count was this draft's; it is spent whatever the backend answered.
            self._drop(context, self.CONFIRM_FLOW_KEYS)
        return ConversationHandler.END

    @require_auth
    @require_delivery_driver
    async def receive_transfer_custom_confirm_unit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Keep one unit line's count; ask the next line, or confirm every line once the last is answered."""
        language = await self._get_language(update, context)
        transfer_id = context.user_data.get('pending_confirm_transfer_id')
        full = context.user_data.get('pending_confirm_transfer_full')
        empties = context.user_data.get('pending_confirm_transfer_empty')
        draft = context.user_data.get('pending_confirm_transfer_units')
        if not transfer_id or full is None or empties is None or draft is None:
            await self._answer_gone_flow(update, context, 'pending_confirm_transfer_id')
            return ConversationHandler.END

        count = self._parse_transfer_count(update.message.text)
        if count is None:
            await update.message.reply_text(i18n.get('staff.delivery.invalid_count_zero_ok', language))
            return BOTTLE_TRANSFER_CONFIRM_UNIT_QTY_INPUT

        unit_lines = self._unit_lines(draft['lines'])
        units = draft['units']
        units.append({'product_id': unit_lines[len(units)].get('product_id'), 'quantity': count})
        if len(units) < len(unit_lines):
            await self._ask_received_unit(update, language, unit_lines[len(units)])
            return BOTTLE_TRANSFER_CONFIRM_UNIT_QTY_INPUT

        try:
            await self._do_confirm_transfer(
                update, context, transfer_id, language, confirmed_full=full, confirmed_empty=empties, units=units,
            )
        finally:
            # The count was this draft's; it is spent whatever the backend answered.
            self._drop(context, self.CONFIRM_FLOW_KEYS)
        return ConversationHandler.END

    @require_auth
    @require_delivery_driver
    async def start_transfer_reject(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """The receiver taps Reject: ask once, naming whose trip the bottles go back to. Nothing is posted yet,
        and Back (the inbox) leaves the transfer pending. No reason is asked."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        transfer_id = self._tapped_transfer_id(query)
        if transfer_id is None:
            await self._handle_error(update, context)
            return

        try:
            transfer = await self._pending_transfer(update, context, language, transfer_id)
            if transfer is None:
                return
            sender = transfer.get('sender_name') or i18n.get('staff.common.unknown_driver', language)
            await query.edit_message_text(
                i18n.get('staff.delivery.transfer_reject_confirm', language, sender=escape_html(sender)),
                reply_markup=DeliveryKeyboards.transfer_reject_confirm(language, transfer_id),
                parse_mode='HTML',
            )
        except Exception as exc:
            logger.error("Error asking to reject a bottle transfer: %s", exc, exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def confirm_transfer_reject(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Yes: reject the transfer. Every line goes back to the sender's trip and the backend tells him; the reply
        names him. A refusal (already settled, another driver's) shows the backend's reason.

        A count of this transfer that he was typing ("Enter other numbers") is dropped, whatever the backend answers:
        he chose to reject it, and its next number would post a count of it. A count of another transfer goes on."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        transfer_id = self._tapped_transfer_id(query)
        if transfer_id is None:
            await self._handle_error(update, context)
            return
        if context.user_data.get('pending_confirm_transfer_id') == transfer_id:
            self._drop(context, self.CONFIRM_FLOW_KEYS)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            async with api_client as client:
                response = await client.reject_bottle_transfer(token, transfer_id)
            if not response.success:
                await self._refuse_transfer_answer(
                    update, language, self._resolve_response_error(language, response), response.error_code,
                )
                return
            rejected = response.data or {}
            sender = rejected.get('sender_name') or i18n.get('staff.common.unknown_driver', language)
            await self._show(
                update,
                i18n.get('staff.delivery.transfer_rejected_reply', language, sender=escape_html(sender)),
                reply_markup=DeliveryKeyboards.bottle_session_menu(language),
            )
        except Exception as exc:
            logger.error("Error rejecting a bottle transfer: %s", exc, exc_info=True)
            await self._handle_error(update, context)

    async def _do_confirm_transfer(self, update, context, transfer_id: int, language: str, **count):
        """API call to confirm/dispute a transfer and show result.

        `count` is `confirm_as_declared=True`, or the numbers the receiver typed (with `units` when the transfer
        has unit lines). Every number on the reply is the backend's: the answer's `lines`, as counted and as
        declared. A disputed reply says an admin was notified only when the backend reports one was
        (`admins_alerted`); otherwise it asks the driver to tell one (spec 2026-11-09 §4.4).

        It leaves the "Enter other numbers" draft alone: the typed count drops its own draft, and a one-tap Confirm
        on another transfer's push must not take the count the driver is typing for this one.
        """
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        try:
            async with api_client as client:
                response = await client.confirm_bottle_transfer(token, transfer_id, **count)

            if not response.success:
                await self._refuse_transfer_answer(
                    update, language, self._resolve_response_error(language, response), response.error_code,
                )
                return

            transfer = response.data or {}
            lines = transfer.get('lines')
            counted = format_transfer_lines(language, lines, field='confirmed')
            if transfer.get('status', 'confirmed') == 'confirmed':
                text = i18n.get('staff.delivery.transfer_confirmed_lines', language, lines=counted)
            else:
                declared = format_transfer_lines(language, lines, field='declared')
                if transfer.get('admins_alerted'):
                    text = i18n.get(
                        'staff.delivery.transfer_disputed_lines', language, declared=declared, counted=counted,
                    )
                else:
                    text = i18n.get(
                        'staff.delivery.transfer_disputed_lines_no_admin', language,
                        declared=declared, counted=counted,
                    )

            await self._show(update, text, reply_markup=DeliveryKeyboards.bottle_session_menu(language))
        except Exception as exc:
            logger.error("Error confirming bottle transfer: %s", exc, exc_info=True)
            await self._handle_error(update, context)
