"""Standalone COD debt collection flow for delivery drivers."""

import logging
from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

from shared import business_config
from staff_bot.api_client import api_client
from staff_bot.handlers.base import BaseHandler
from staff_bot.i18n import i18n
from staff_bot.keyboards.common import CommonKeyboards
from staff_bot.keyboards.delivery import DeliveryKeyboards
from staff_bot.permissions import require_auth, require_delivery_driver
from staff_bot.utils import flow_state
from staff_bot.utils.formatters import escape_html, format_currency

logger = logging.getLogger(__name__)

COLLECTION_AMOUNT_INPUT = 104
COLLECTION_NOTE_INPUT = 105
COLLECTION_OVERPAYMENT_CONFIRM = 106

COD_DEBTORS_PER_PAGE = 10


class CashCollectionHandler(BaseHandler):
    """Handle standalone COD debt collection outside delivery completion."""

    @staticmethod
    async def _clear_flow(
        context: ContextTypes.DEFAULT_TYPE,
        update: Update = None,
    ):
        """Clear the standalone-COD flow flag plus the Redis mirror, and
        deliver any pool-insertion suggestions deferred while the driver
        was mid-collection. See `flow_state.clear_and_drain` for the queue
        protocol; `update` is optional so legacy call sites keep working
        with degraded (in-memory-only) behaviour."""
        context.user_data.pop('pending_cod_collection_flow', None)
        if update and update.effective_user:
            language = context.user_data.get('language') if context else None
            await flow_state.clear_and_drain(
                update.effective_user.id, context.bot, language=language
            )

    @staticmethod
    def _debtor_name(data: dict) -> str:
        """Best-effort display name from a statement/debtor dict."""
        first = (data.get('first_name') or '').strip()
        last = (data.get('last_name') or '').strip()
        return f"{first} {last}".strip()

    @staticmethod
    def _debtor_header(name: str, phone: str) -> str:
        """One-line debtor identity banner prepended to every collection screen
        so the driver always sees who they are collecting from before money
        changes hands. Returns '' when no identity is known."""
        parts = []
        if name:
            parts.append(f"👤 {escape_html(name)}")
        if phone:
            parts.append(f"📞 {escape_html(phone)}")
        return ' · '.join(parts)

    @classmethod
    def _flow_header(cls, flow: dict) -> str:
        """Debtor identity banner built from the stored collection flow."""
        return cls._debtor_header(flow.get('customer_name') or '', flow.get('customer_phone') or '')

    @staticmethod
    def _with_header(header: str, body: str) -> str:
        """Stack the debtor header above a screen body (no-op when header empty)."""
        return f"{header}\n\n{body}" if header else body

    @staticmethod
    def _int_or_default(value, default: int = 0) -> int:
        """Tolerant int cast for payload counters (None/'' → ``default``)."""
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _resolve_scope_address_id(statement: dict) -> Optional[int]:
        """Delivery address that gives an order-less collection PLACE scope (spec 8).

        A standalone collection carries no order, so ``delivery_address_id`` is
        the ONLY input that lets the 2b scope engine settle a coworker's debt at
        the same workplace. There is exactly one rule left:

        * the customer's single grouped place, when they have exactly one;
        * ``None`` otherwise — ambiguity must NOT be guessed. With two places,
          picking one arbitrarily would spread the cash across the wrong
          workplace, so we fall back to today's cluster/personal scope.

        OWNER RULING A7 REMOVED THE THIRD INPUT. There used to be a first rule —
        "the place the driver actually tapped" (``pending_place_group_id``, set
        by the place-statement screen). A7 deletes the place row and its screen
        from the driver's debtor list ("the debtors list only shows the users,
        and the office debt is included in each coworker's debt"), so nothing
        writes that key any more and no tap can name a place. Every standalone
        collection now starts from a PERSON row, and the person's own statement
        is the only place resolver.

        ⚠️ CONSEQUENCE, RECORDED DELIBERATELY: a cluster owning two or more
        grouped places resolves to ``None`` and is never widened. Its
        coworkers' debt is reachable only through a member whose own cluster
        owns exactly one place. ``StaffService.paginate_cod_debtors_for_staff``
        applies the same guard to the row so the list can never advertise a
        total this method refuses.
        """
        places = statement.get('places') or []
        if len(places) == 1:
            return places[0].get('address_id')
        return None

    @classmethod
    def _resolved_place(cls, statement: dict) -> tuple:
        """``(candidate_address_id, place_open_cod_debt_total)`` — WHICH PLACE, and
        what that place alone owes. **Nominates; does not decide.**

        ⚠️ THE ADDRESS THIS RETURNS IS A CANDIDATE, NOT A POSTABLE SCOPE. Never
        store it on a flow and never post it. It becomes a scope only by coming
        back out of :meth:`_scoped_ceiling` paired with that address's own
        published ceiling — see the P0-degraded note there. This method answers
        "which place did the driver mean"; that one answers "may we post against
        it, and for how much", and both halves of the second answer are produced
        together on purpose.

        The customer statement screen reads the TOTAL half for its Collect gate;
        the collection flows read the ADDRESS half and immediately price it
        through :meth:`_scoped_ceiling`, so the figure the driver is offered and
        the place the collection will actually post against can never be two
        different places (plan E4).

        Returns a ``0.0`` total — "nothing to widen" — in three cases:

        * the gate ``PLACE_COD_COLLECTION_ENABLED`` is off (ships dark);
        * ``_resolve_scope_address_id`` refused to guess between two places, so
          the collection would post CLUSTER/PERSONAL scope and any surplus would
          land as this payer's prepaid credit rather than settling the place;
        * the resolved place carries no open delivered COD debt.

        The address half is returned unconditionally with the gate OFF because it
        is only a candidate and the gate-off *display* has no use for a refusal.
        Nothing posts it: with the gate off :meth:`_scoped_ceiling` drops it, so
        the rollback path posts no ``delivery_address_id`` at all — which is what
        Plan D did (``git show HEAD`` of this file has no such key).

        ``place_open_cod_debt_total`` is DELIVERED open COD at the place, i.e.
        the same debt definition ring 1 allocates against — so it is a
        collectible ceiling, not a headline figure.

        PLAN E20 IS GONE WITH THE PLACE SCREEN (owner ruling A7). E20 refused a
        TAPPED place that was not one of this customer's own — the Q5 shape,
        where an order was delivered to an address its orderer does not own. It
        was reachable only through ``pending_place_group_id``, which only the
        deleted place-statement screen ever wrote. With no tap there is no
        cross-place to refuse: every address this method can return is read out
        of THIS customer's own ``places``, so the guard's precondition can no
        longer occur. (Task 7's ownership check on ``create_phone_order`` still
        closes the Q5 state at its source.)
        """
        address_id = cls._resolve_scope_address_id(statement)
        if not business_config.PLACE_COD_COLLECTION_ENABLED or address_id is None:
            return address_id, 0.0
        return address_id, cls._place_total_for_address(statement, address_id)

    @classmethod
    def _place_total_for_address(cls, statement: dict, address_id) -> float:
        """Open delivered COD debt at ONE place, named by an ALREADY-CHOSEN address.

        The pricing half of :meth:`_resolved_place`, split out so a step that
        has already committed to a place can price THAT place instead of
        resolving a fresh one.

        WHY THAT MATTERS (plan E4 / invariant 1). ``start_full_collection``
        resolves and posts in a single instant, so one ``_resolved_place`` call
        is enough there. The CUSTOM path does not: ``start_custom_collection``
        stores ``delivery_address_id`` and ``receive_collection_amount`` prices
        the overpayment threshold one or more Telegram updates later. Nothing
        holds those two moments together: ``receive_collection_amount`` is
        dispatched by the catch-all text router (``staff_bot/bot.py:1092-1100``)
        rather than by a ConversationHandler state, so an unrelated update can
        land between them and the customer's grouped addresses can be re-grouped
        by an admin in the meantime. Pricing the STORED address — rather than
        re-resolving a fresh one from a fresh statement — makes the offer and
        the post provably one place.

        Returns ``0.0`` — "nothing to widen" — when the gate is off (C0), when
        no address was resolved at all (E7's ambiguity refusal arrives here as
        ``None``), or when the address names no place carrying open delivered
        COD debt.
        """
        if not business_config.PLACE_COD_COLLECTION_ENABLED or address_id is None:
            return 0.0
        for place in (statement.get('places') or []):
            if place.get('address_id') == address_id:
                try:
                    return max(0.0, float(place.get('place_open_cod_debt_total') or 0))
                except (TypeError, ValueError):
                    return 0.0
        return 0.0

    @classmethod
    def _scoped_ceiling(cls, statement: dict, address_id) -> tuple:
        """``(delivery_address_id_to_post, ceiling)`` — **ONE decision, never two.**

        🔴 THE OFFER AND THE SETTLEMENT SCOPE ARE DECIDED HERE, TOGETHER, AND
        NOWHERE ELSE. Do not split them apart again and do not re-derive either
        half at a call site. Splitting them is the ONLY bug this seam has ever
        had, and it has now been shipped twice:

        * P0 — the debtor row was a UNION while the ceiling was
          ``max(own, cluster, place)``. ``max(25k, 25k, 35k)`` is 35 000 where
          the row and the settlement are both 45 000, so the list advertised a
          total the flow refused and the surplus copy fired over a window that
          was still paying a coworker's debt.
        * P0-degraded — the ceiling fell back to the cluster-only figure when no
          place ceiling was published, **but the caller still posted
          ``delivery_address_id``**. The post stayed PLACE-scoped and still
          settled ring 1 ∪ ring 2, so a 25 000 ceiling sat over a 45 000
          settlement: the driver was promised 20 000 of prepayment and the
          measured ``unapplied_amount`` was ``0.00`` while the coworker's debt
          went to zero. "Under-offering is the safe direction" was FALSE — a
          ceiling BELOW the settlement set is exactly what makes the shipped
          overpayment copy untrue.

        Hence the invariant this method exists to enforce, in one line:

            **an address is returned ONLY together with that address's own
            published ceiling; every degradation drops the address too.**

        Cluster-scoped post ⇒ the engine settles the cluster's own delivered COD
        debt only (``resolve_allocation_scope`` falls through to CLUSTER/PERSONAL
        when ``delivery_address_id is None`` — ``cash_collection_service.py:603``,
        ``:629-631``), which is exactly ``cluster_delivered_outstanding_amount``.
        So in the degraded shape the offer and the settlement agree again and the
        surplus copy is true — the price being that the coworkers' debt is simply
        not collected, which is Plan D's behaviour, not a lie.

        A6/R-B — THE CEILING IS READ, NOT RECOMPOSED.
        ``StaffService.get_customer_cod_statement_for_staff`` composes it with the
        same :func:`collectible_cod_total` the debtor row is composed with and
        publishes it as ``places[].place_collect_ceiling_amount``; this method
        only *selects* which place applies. There is deliberately no ``max`` and
        no addition anywhere in it.

        Degrades to ``(None, own cluster debt)`` — the un-widened row, i.e.
        exactly what Plan D offered and posted — in every case where no place
        ceiling applies:

        * the gate ``PLACE_COD_COLLECTION_ENABLED`` is off (C0 rollback path:
          Plan D posted no ``delivery_address_id`` at all, so dropping it here is
          what makes the rollback a real rollback for the money and not only for
          the payload);
        * no place resolved (E7 ambiguity arrives here as ``address_id is None``);
        * the address names none of this customer's places;
        * the payload carries no ceiling for it, which is what a business_app
          older than this bot sends. The bot then collects cluster-scoped: less
          money settled, nothing mis-stated.

        DELIVERED-ONLY, CLUSTER-WIDE, in every branch. ``cluster_delivered_
        outstanding_amount`` is the only base used, never the per-account
        ``total_outstanding_amount``: the allocation engine's candidate rings
        select DELIVERED orders only (``cash_collection_service.py:183-196``,
        ``:245-259``), so cash offered against a pending order settles nothing and
        silently becomes prepayment. ``start_full_collection`` and
        ``receive_collection_amount`` used to disagree on that point — one summed
        DELIVERED items, the other took the per-account headline — which is a
        third desynchronisation of the same family.
        """
        base = float(statement.get('cluster_delivered_outstanding_amount') or 0)
        if not business_config.PLACE_COD_COLLECTION_ENABLED or address_id is None:
            return None, base
        for place in (statement.get('places') or []):
            if place.get('address_id') != address_id:
                continue
            ceiling = place.get('place_collect_ceiling_amount')
            if ceiling is None:
                return None, base
            try:
                return address_id, float(ceiling)
            except (TypeError, ValueError):
                return None, base
        return None, base

    @classmethod
    def _collect_offer(cls, statement: dict) -> tuple:
        """``(delivery_address_id_to_post, amount)`` — THE offer, for the screen
        AND for the flow, from one call.

        🔴 FIFTH INSTANCE OF THE SHOW-VS-SETTLE SPLIT. This method exists so the
        driver's statement screen and the collection it starts can never again be
        two independent expressions of "how much".

        What was shipped: :meth:`_format_statement` printed
        ``statement['total_outstanding_amount']`` (per-account, PENDING-inclusive)
        and ``places[].place_open_cod_debt_total`` (the place's whole debt,
        including the part this cluster already owns) straight off the raw engine
        payload, while "💸 Collect full" priced itself through
        :meth:`_scoped_ceiling`. Measured on the canonical A6 rows the screen read
        *Total outstanding 25 000* over *🏢 office 35 000* and then offered
        **45 000** — a figure that appeared nowhere on it, and the one the debtor
        list that got the driver there had advertised. Adding a single PENDING
        order widened the headline to 95 000 against the same 45 000 offer. That
        is verbatim the defect ``resolve_collect_scope``
        (``business_app/services/cod_collect_ceiling.py``) exists to make
        impossible for the ADMIN modal; the driver's copy of it was never fixed.

        It was money-safe — the engine's rings are DELIVERED-only, so the surplus
        landed as this customer's prepayment and no coworker was charged — but
        ``staff_service.py:2414`` states the invariant in so many words: *"never
        advertise a total the collect flow refuses"*, and this screen sat between
        the row and the offer showing neither.

        This is deliberately a two-line composition rather than new arithmetic:
        :meth:`_resolved_place` nominates WHICH place, :meth:`_scoped_ceiling`
        decides whether it may be posted and for how much. Adding a third
        expression of the ceiling is what caused every instance of this defect —
        so do not inline either half at a call site, and do not compute a
        "display total" beside it.

        NOT used by ``receive_collection_amount``: that step must price the place
        ``start_custom_collection`` already COMMITTED to (see
        :meth:`_place_total_for_address`), never a place freshly re-resolved from
        a statement fetched updates later. It calls :meth:`_scoped_ceiling`
        directly with the stored address for exactly that reason.
        """
        candidate_address_id, _place_total = cls._resolved_place(statement)
        return cls._scoped_ceiling(statement, candidate_address_id)

    @classmethod
    def _format_statement(cls, statement: dict, language: str) -> str:
        """The screen the driver reads immediately before pressing Collect.

        🔴 EVERY MONEY FIGURE HERE IS EITHER THE OFFER OR A LABELLED COMPONENT OF
        IT. The headline is :meth:`_collect_offer` — the same call
        ``start_full_collection`` arms its flow from — so the number the driver
        sees is the number the flow will act on. The lines below it are named
        parts (this cluster's own share, the workplace's own debt, per-order
        rows); none of them is a collectible total and none may be presented as
        one. The raw per-account ``total_outstanding_amount`` is deliberately NOT
        rendered: it counts PENDING orders the allocation engine cannot settle,
        which is what made this screen's old headline diverge from the offer by
        50 000 on the canonical rows.
        """
        items = statement.get('items') or []
        lines = [f"📜 <b>{i18n.get('staff.delivery.cod_statement_title', language)}</b>"]
        header = cls._debtor_header(cls._debtor_name(statement), statement.get('phone') or '')
        if header:
            lines.append(header)
        cluster_debt_count = cls._int_or_default(statement.get('active_cod_debt_count'))
        _scope_address_id, collectible = cls._collect_offer(statement)
        lines.extend([
            f"💳 {i18n.get('staff.delivery.active_cod_debts', language)}: {statement.get('active_cod_debt_count', 0)}",
            f"💰 <b>{i18n.get('staff.delivery.collectible_now', language)}: "
            f"{format_currency(collectible, language=language)}</b>",
        ])

        # `active_cod_debt_count` is CLUSTER-wide while `items` and
        # `total_outstanding_amount` stay PER-ACCOUNT, so a linked sibling with
        # no debts of their own would read "2 active debts" over an empty list.
        # State the per-account half instead of leaving the driver to guess it.
        account_debt_count = statement.get('account_active_cod_debt_count')
        if account_debt_count is not None and cls._int_or_default(account_debt_count) != cluster_debt_count:
            lines.append(
                f"👤 {i18n.get('staff.delivery.account_cod_debts', language)}: "
                f"{cls._int_or_default(account_debt_count)}"
            )

        if cls._int_or_default(statement.get('cluster_member_count'), 1) > 1:
            lines.append(
                f"👥 {i18n.get('staff.delivery.cluster_debt_total', language)}: "
                f"{format_currency(statement.get('cluster_delivered_outstanding_amount', 0), language=language)}"
                f" ({i18n.get('staff.delivery.cluster_members', language)}: "
                f"{statement.get('cluster_member_count')})"
            )

        # A COMPONENT, not a collectible total. `place_open_cod_debt_total` is the
        # workplace's OWN debt — it excludes this person's private debts elsewhere
        # and double-counts nothing back, so it is neither the offer nor a share
        # of it. Rendered bare ("🏢 Acme office: 35,000") it read as a headline
        # figure, which is precisely why owner ruling A7 deleted the place screen
        # whose header showed it. It now carries the SSOT workplace label
        # (`staff.delivery.place_cod_total`, the same one the at-door prompt uses)
        # so the place name can never be mistaken for the line's subject.
        for place in (statement.get('places') or []):
            place_label = place.get('label') or f"#{place.get('place_group_id')}"
            lines.append(
                f"🏢 {i18n.get('staff.delivery.place_cod_total', language)} "
                f"({escape_html(place_label)}): "
                f"{format_currency(place.get('place_open_cod_debt_total') or 0, language=language)}"
            )

        if not items:
            lines.append(i18n.get('staff.delivery.no_cod_debt', language))
            return '\n'.join(lines)

        # FILTER FIRST, THEN SLICE. Slicing first silently dropped real debt
        # lines whenever five non-collectible rows happened to sort ahead of
        # them — and the statement now lists every payment rail in full (owner
        # ruling 2026-08-08), so settled card orders are exactly the rows that
        # would crowd the driver's screen.
        #
        # `is_collectible_target` is the server's own answer to "may a collection
        # settle this", computed with the same predicate the collect endpoint
        # validates against, so these lines can never advertise a debt the flow
        # refuses. The `outstanding_amount` fallback keeps this bot working
        # against a business_app older than that field.
        collectible = [
            item
            for item in items
            if (
                item.get('is_collectible_target')
                if item.get('is_collectible_target') is not None
                else float(item.get('outstanding_amount') or 0) > 0
            )
        ]
        if not collectible:
            lines.append(i18n.get('staff.delivery.no_cod_debt', language))
            return '\n'.join(lines)

        lines.append('')
        for item in collectible[:5]:
            lines.append(
                f"• {escape_html(item.get('order_number') or i18n.get('staff.order.unknown', language))}: "
                f"{format_currency(item.get('outstanding_amount') or 0, language=language)}"
            )
        return '\n'.join(lines)

    @require_auth
    @require_delivery_driver
    async def show_debtor_list(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = None
    ):
        """List customers with outstanding COD debt, paginated.

        OWNER RULING A7 — USER ROWS ONLY. There is no 🏢 place row and no place
        screen: "the debtors list only shows the users, and the office debt is
        included in each coworker's debt". Each person row already carries the
        whole grouped place's debt (A6/R-A, composed by
        ``StaffService.paginate_cod_debtors_for_staff``), so the office is
        collectible THROUGH A PERSON and never through a place.
        """
        language = await self._get_language(update, context)
        # Browsing the list awaits no text input — clear any pending flow so
        # the catch-all text router doesn't swallow menu taps. Done before the
        # token lookup so the flag is cleared even when that bails out below.
        await self._clear_flow(context, update)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        if page is None:
            page = 1
        context.user_data['cod_list_page'] = page

        try:
            async with api_client as client:
                response = await client.get_cod_debtors(
                    token, page=page, per_page=COD_DEBTORS_PER_PAGE
                )

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return

            data = response.data or {}
            items = data.get('items', [])
            pagination = data.get('pagination', {})

            if not items and page > 1:
                # Stale page (debts collected since render) — restart at page 1.
                await self.show_debtor_list(update, context, page=1)
                return

            if not items:
                text = f"✅ {i18n.get('staff.delivery.no_cod_debtors', language)}"
                keyboard = CommonKeyboards.back_button(language, callback_data='staff_cash_hub')
            else:
                total = pagination.get('total', len(items))
                text = (
                    f"💳 <b>{i18n.get('staff.delivery.cod_debtors_title', language)}</b> ({total})\n"
                    f"{i18n.get('staff.delivery.cod_debtors_hint', language)}"
                )
                keyboard = DeliveryKeyboards.cod_debtor_list(
                    language, items, page, pagination.get('pages', 1)
                )

            if update.callback_query:
                await update.callback_query.answer()
                await update.callback_query.edit_message_text(
                    text, reply_markup=keyboard, parse_mode='HTML'
                )
            else:
                await update.message.reply_text(text, reply_markup=keyboard, parse_mode='HTML')
        except Exception as exc:
            logger.error("Error showing COD debtor list: %s", exc, exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def paginate_debtor_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle ◀️/▶️ page flips on the COD debtor list."""
        query = update.callback_query
        try:
            page = int(query.data.split('_')[-1])
        except (ValueError, IndexError):
            page = 1
        await self.show_debtor_list(update, context, page=page)

    @require_auth
    @require_delivery_driver
    async def show_customer_statement(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show COD debt statement for a selected customer."""
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
                response = await client.get_customer_cod_statement(token, customer_id)

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return

            statement = response.data or {}
            # NB: do NOT arm pending_cod_collection_flow here. This screen only
            # offers inline buttons (Collect full / Collect custom / Back); the
            # routing flag is armed in start_full_collection / start_custom_collection
            # once the driver actually commits to a collection. Arming on mere
            # display turned every reply-keyboard menu tap into "Invalid cash
            # amount" and let a stray typed number start an unintended collection.
            # Plan E R1: `active_cod_debt_count` is a per-person (cluster) count,
            # so on its own it hides exactly the coworker who is holding the
            # office's cash. The place total is consulted only for the place this
            # screen would actually post against (E4/E7).
            _, place_total = self._resolved_place(statement)
            can_collect = (statement.get('active_cod_debt_count', 0) > 0) or place_total > 0
            await query.edit_message_text(
                self._format_statement(statement, language),
                reply_markup=DeliveryKeyboards.cod_statement_actions(
                    language,
                    customer_id,
                    can_collect=can_collect,
                    back_callback=f"staff_cod_list_page_{context.user_data.get('cod_list_page', 1)}",
                ),
                parse_mode='HTML',
            )
        except Exception as exc:
            logger.error("Error showing customer COD statement: %s", exc, exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def start_full_collection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Prepare a full outstanding-balance standalone collection."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        customer_id = int(query.data.split('_')[-1])
        async with api_client as client:
            response = await client.get_customer_cod_statement(token, customer_id)

        if not response.success:
            await self._handle_api_response_error(update, response, language)
            return

        statement = response.data or {}
        # A6/R-B: "Collect all" offers EXACTLY the figure the debtor list
        # advertised for this person — one calculation, read from the payload.
        # ONE DECISION (P0-degraded): the amount we offer and the address we post
        # come back from the SAME call, so a degraded ceiling can never be paired
        # with a place-scoped post.
        # ONE DECISION (fifth instance): `_collect_offer` is also what
        # `_format_statement` printed on the screen the driver just tapped
        # through, so the offer below cannot be a number they have never seen.
        scope_address_id, total_outstanding = self._collect_offer(statement)
        if total_outstanding <= 0:
            await query.answer(i18n.get('staff.delivery.no_cod_debt', language), show_alert=True)
            return

        flow = {
            'customer_id': customer_id,
            'amount': total_outstanding,
            'total_outstanding_amount': total_outstanding,
            'customer_name': self._debtor_name(statement),
            'customer_phone': statement.get('phone') or '',
            # Spec 8: scope address for an order-less standalone collection.
            'delivery_address_id': scope_address_id,
        }
        context.user_data['pending_cod_collection_flow'] = flow
        # C-2: mirror the flow into Redis so the webhook server can defer
        # pool-insertion suggestions until this collection completes.
        await flow_state.mark_active(
            update.effective_user.id, 'pending_cod_collection_flow'
        )
        await query.edit_message_text(
            self._with_header(
                self._flow_header(flow),
                i18n.get(
                    'staff.delivery.cod_collection_note_prompt',
                    language,
                    amount=format_currency(total_outstanding, language=language),
                ),
            ),
            reply_markup=CommonKeyboards.flow_cancel(language),
            parse_mode='HTML',
        )
        return COLLECTION_NOTE_INPUT

    @require_auth
    @require_delivery_driver
    async def start_custom_collection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Prompt the driver to enter a custom standalone collection amount."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        customer_id = int(query.data.split('_')[-1])
        # Re-fetch the statement so the debtor identity (name/phone) is fresh and
        # always belongs to the tapped customer — symmetric with
        # start_full_collection, and never relies on a flow armed by the (now
        # display-only) statement screen. The routing flag is armed only here,
        # at the moment the driver commits to entering an amount.
        async with api_client as client:
            response = await client.get_customer_cod_statement(token, customer_id)
        if not response.success:
            await self._handle_api_response_error(update, response, language)
            return

        statement = response.data or {}
        # ONE DECISION (P0-degraded). This step stores the scope the post at
        # `receive_collection_note` will use, but the ceiling that prices the
        # overpayment copy is only read one or more Telegram updates later in
        # `receive_collection_amount`. Storing a raw resolved address here would
        # commit to PLACE scope before anything had established that a ceiling
        # for it exists — the split-decision shape again, just stretched over two
        # updates. So the address is stored only if it comes back WITH its own
        # ceiling; `receive_collection_amount` re-applies the same rule against a
        # fresh statement and overwrites this key if the ceiling has since gone.
        scope_address_id, _ceiling = self._collect_offer(statement)
        flow = {
            'customer_id': customer_id,
            'total_outstanding_amount': float(statement.get('total_outstanding_amount') or 0),
            'customer_name': self._debtor_name(statement),
            'customer_phone': statement.get('phone') or '',
            # Spec 8: scope address for an order-less standalone collection.
            'delivery_address_id': scope_address_id,
        }
        context.user_data['pending_cod_collection_flow'] = flow
        await flow_state.mark_active(
            update.effective_user.id, 'pending_cod_collection_flow'
        )
        await query.edit_message_text(
            self._with_header(
                self._flow_header(flow),
                i18n.get('staff.delivery.cod_collection_amount_prompt', language),
            ),
            reply_markup=CommonKeyboards.flow_cancel(language),
            parse_mode='HTML',
        )
        return COLLECTION_AMOUNT_INPUT

    @require_auth
    @require_delivery_driver
    async def receive_collection_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Receive custom amount for standalone collection."""
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END

        flow = context.user_data.get('pending_cod_collection_flow') or {}
        customer_id = flow.get('customer_id')
        if not customer_id:
            await update.message.reply_text(i18n.get('staff.error_occurred', language))
            return ConversationHandler.END

        try:
            amount = float(update.message.text.strip().replace(',', '').replace(' ', ''))
            if amount <= 0:
                raise ValueError("non-positive")
        except ValueError:
            await update.message.reply_text(i18n.get('staff.delivery.invalid_cash_amount', language))
            return COLLECTION_AMOUNT_INPUT

        async with api_client as client:
            response = await client.get_customer_cod_statement(token, customer_id)

        if response.success:
            statement = response.data or {}
            # Plan E E4: price the place this flow will ACTUALLY post against —
            # the address `start_custom_collection` already resolved and stored
            # in the flow — never a fresh resolution, which an admin re-grouping
            # the customer's addresses between the two steps could move. Fresh
            # statement, committed place.
            #
            # WHY THE OVERPAYMENT COPY IS TRUE ABOVE THIS THRESHOLD (A6/R-D).
            # Not because the number is "raised" — the previous comment argued
            # that from `max(own, cluster, place)`, and a max is not the set the
            # money is spent on. A PLACE-scoped post settles ring 1 ∪ ring 2
            # (`cash_collection_service.py:3503-3511`): every open delivered COD
            # debt at the place, ANY owner, plus every open delivered COD debt
            # of this customer's own cluster. `place_collect_ceiling_amount` is
            # the sum of exactly that union, so a cent above it has no candidate
            # payment left to allocate against and lands as this customer's
            # prepaid credit — the collection event is keyed to the customer who
            # posted it (`cash_collection_service.py:2611-2624`), which is R-D's
            # "surplus is per-user". Under the old `max` the window between the
            # max and the union was still settling a coworker's debt while the
            # confirmation told the driver it was becoming prepayment.
            #
            # ONE DECISION (P0-degraded): `_scoped_ceiling` hands back the scope
            # AND the ceiling from the same call, and the scope is WRITTEN BACK
            # onto the flow so `receive_collection_note` posts exactly the scope
            # this threshold was priced for. If the fresh statement no longer
            # publishes a ceiling for the committed address, the address is
            # dropped here and the collection posts cluster-scoped — the offer
            # and the settlement move together or not at all.
            scope_address_id, total_outstanding = self._scoped_ceiling(
                statement, flow.get('delivery_address_id')
            )
            flow['delivery_address_id'] = scope_address_id
            flow['total_outstanding_amount'] = total_outstanding
            if total_outstanding > 0 and amount > total_outstanding:
                overpayment = amount - total_outstanding
                flow['pending_overpayment_amount'] = amount
                flow.pop('amount', None)
                context.user_data['pending_cod_collection_flow'] = flow
                await update.message.reply_text(
                    self._with_header(
                        self._flow_header(flow),
                        i18n.get(
                            'staff.delivery.cod_collection_overpayment_confirm',
                            language,
                            amount=format_currency(amount, language=language),
                            outstanding=format_currency(total_outstanding, language=language),
                            overpayment=format_currency(overpayment, language=language),
                        ),
                    ),
                    reply_markup=CommonKeyboards.yes_no(
                        language,
                        'staff_cod_confirm_overpay_yes',
                        'staff_cod_confirm_overpay_no',
                    ),
                    parse_mode='HTML',
                )
                return COLLECTION_OVERPAYMENT_CONFIRM

        flow.pop('pending_overpayment_amount', None)
        flow['amount'] = amount
        context.user_data['pending_cod_collection_flow'] = flow
        await update.message.reply_text(
            self._with_header(
                self._flow_header(flow),
                i18n.get(
                    'staff.delivery.cod_collection_note_prompt',
                    language,
                    amount=format_currency(amount, language=language),
                ),
            ),
            reply_markup=CommonKeyboards.flow_cancel(language),
            parse_mode='HTML',
        )
        return COLLECTION_NOTE_INPUT

    @require_auth
    @require_delivery_driver
    async def confirm_overpayment_collection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Driver confirmed they really mean to overpay; surplus → customer prepayment."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)

        flow = context.user_data.get('pending_cod_collection_flow') or {}
        pending_amount = flow.get('pending_overpayment_amount')
        if not flow.get('customer_id') or pending_amount is None:
            await query.edit_message_text(i18n.get('staff.error_occurred', language))
            return ConversationHandler.END

        flow['amount'] = pending_amount
        flow.pop('pending_overpayment_amount', None)
        context.user_data['pending_cod_collection_flow'] = flow

        await query.edit_message_text(
            self._with_header(
                self._flow_header(flow),
                i18n.get(
                    'staff.delivery.cod_collection_note_prompt',
                    language,
                    amount=format_currency(pending_amount, language=language),
                ),
            ),
            reply_markup=CommonKeyboards.flow_cancel(language),
            parse_mode='HTML',
        )
        return COLLECTION_NOTE_INPUT

    @require_auth
    @require_delivery_driver
    async def cancel_overpayment_collection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Driver said no to overpayment confirmation; reset amount and re-prompt."""
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)

        # The flow has to EXIST to be reset. Falling back to `{}` re-armed an
        # empty one and drew the amount prompt with no debtor banner, inviting
        # the driver to type a cash figure against no order and no customer.
        # `pending_cod_collection_flow` is in `flow_state.PENDING_FLOW_USER_DATA_KEYS`,
        # so any menu tap clears it — no deploy required.
        # tests/staff_bot/test_driver_flows_after_state_loss.py
        flow = await self._require_flow(update, context, 'pending_cod_collection_flow')
        if flow is None:
            return
        flow.pop('pending_overpayment_amount', None)
        flow.pop('amount', None)
        context.user_data['pending_cod_collection_flow'] = flow

        await query.edit_message_text(
            self._with_header(
                self._flow_header(flow),
                i18n.get('staff.delivery.cod_collection_amount_prompt', language),
            ),
            reply_markup=CommonKeyboards.flow_cancel(language),
            parse_mode='HTML',
        )
        return COLLECTION_AMOUNT_INPUT

    @require_auth
    @require_delivery_driver
    async def receive_collection_note(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Finalize standalone COD collection after receiving notes."""
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            await self._clear_flow(context, update)
            return ConversationHandler.END

        flow = context.user_data.get('pending_cod_collection_flow') or {}
        customer_id = flow.get('customer_id')
        amount = flow.get('amount')
        notes = update.message.text.strip()
        if not customer_id or amount is None:
            await update.message.reply_text(i18n.get('staff.error_occurred', language))
            await self._clear_flow(context, update)
            return ConversationHandler.END
        if not notes:
            await update.message.reply_text(i18n.get('staff.delivery.collection_notes_required', language))
            return COLLECTION_NOTE_INPUT

        # Capture the debtor banner before _clear_flow wipes the flow so the
        # receipt still names who the cash was collected from.
        header = self._flow_header(flow)

        try:
            async with api_client as client:
                response = await client.record_cash_collection(
                    token,
                    {
                        'customer_id': customer_id,
                        'amount': amount,
                        'source': 'standalone_meeting',
                        'notes': notes,
                        # Spec 8: the only scope input an order-less collection
                        # has. None ⇒ today's cluster/personal scope.
                        'delivery_address_id': flow.get('delivery_address_id'),
                        'proof_data': {'channel': 'staff_bot', 'flow': 'standalone_cod_collection'},
                    },
                )

            if not response.success:
                await self._handle_api_response_error(update, response, language)
                # Drop the flow so the next typed text isn't re-submitted as
                # another collection note for the same (failed) collection.
                await self._clear_flow(context, update)
                return ConversationHandler.END

            async with api_client as client:
                statement_response = await client.get_customer_cod_statement(token, customer_id)

            # 🔴 SIXTH INSTANCE OF THE SHOW-VS-SETTLE SPLIT. The receipt states
            # what is STILL COLLECTIBLE, so it must be the same one decision the
            # next collect flow will act on — `_collect_offer`, exactly as
            # `_format_statement` and `start_full_collection` use it.
            #
            # It used to read the raw per-account `total_outstanding_amount`,
            # the one key `_format_statement`'s docstring says is "deliberately
            # NOT rendered: it counts PENDING orders the allocation engine
            # cannot settle". Measured on the canonical A6 rows plus a 70 000
            # PENDING order, after a full 45 000 collection: the receipt said
            # remaining = 70 000 where the next offer is 0 — money the driver is
            # told to chase and no flow will take
            # (tests/unit/test_cod_receipt_remaining_matches_offer.py).
            remaining_collectible = 0.0
            if statement_response.success:
                _scope_address_id, remaining_collectible = self._collect_offer(
                    statement_response.data or {}
                )

            await self._clear_flow(context, update)
            await update.message.reply_text(
                self._with_header(
                    header,
                    i18n.get(
                        'staff.delivery.cod_collection_recorded',
                        language,
                        amount=format_currency(amount, language=language),
                        remaining=format_currency(remaining_collectible, language=language),
                    ),
                ),
                reply_markup=CommonKeyboards.back_button(language),
                parse_mode='HTML',
            )
            return ConversationHandler.END
        except Exception as exc:
            logger.error("Error recording standalone COD collection: %s", exc, exc_info=True)
            await self._handle_error(update, context)
            await self._clear_flow(context, update)
            return ConversationHandler.END
