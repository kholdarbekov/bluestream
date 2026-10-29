"""
Subscription management handlers with complete implementation
"""
import re

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from i18n import i18n
from keyboards import (
    SubscriptionKeyboards, MenuKeyboards, ProductKeyboards,
    address_confirm_callback, address_method_callback, items_save_callback, parse_address_confirm_callback,
    parse_address_method_callback, parse_items_save_callback, parse_payment_save_callback, parse_schedule_callback,
    payment_save_callback, schedule_callback,
)
from api_client import api_client
from flow_exit import ITEM_MANAGEMENT_KEYS, SUBSCRIPTION_CREATION_KEYS, UPDATE_ITEM_KEYS
from menu_keyboard import send_menu_keyboard

from utils import format_date, user_middleware, get_auth_token
from shared.constants import SUBSCRIPTION_STATUS_ICONS
# `BaseHandler._ack` is why every ack below goes through `self._ack`: answering
# a callback is cosmetic and must never abort the step. Every handler here works
# inside one `try` that returns ConversationHandler.END, so a late "query is too
# old" used to end the customer's subscription flow AFTER the change had already
# been sent to the backend.
from handlers.base import BaseHandler
from handlers.products import ProductHandlers


# Conversation states for subscription creation. SELECT_ADDRESS is the state that waits for the
# FREQUENCY tap (its handler, `choose_frequency`, used to show the address list straight away); SELECT_DAY
# and SELECT_SLOT are the delivery day and time-slot steps between the frequency and the address.
(SELECT_PRODUCTS, SELECT_QUANTITY, SELECT_FREQUENCY, SELECT_ADDRESS,
 SELECT_START_DATE, SELECT_PAYMENT, CONFIRM_SUBSCRIPTION) = range(7)

# Conversation states for item management
(ITEM_ACTION, ITEM_SELECT_PRODUCT, ITEM_SELECT_QUANTITY) = range(7, 10)

(SELECT_DAY, SELECT_SLOT) = range(10, 12)

# The frequencies whose delivery day is a weekday (0 = Monday); monthly's is a day of the month.
_WEEKDAY_FREQUENCIES = ('weekly', 'biweekly')


def _upcoming_text(upcoming, language: str) -> str | None:
    """"29.10.2026, 10:00–12:00" from the backend's `upcoming_delivery`: its date and its published
    `time_label` (a window, "Any time" or "Time to be confirmed", all finished words); ``None`` when there is
    nothing upcoming. The bot works no dates or times out (spec 2026-10-23 §9). The date reads dd.mm.yyyy, by
    the bot's one date rule (`utils.format_date`), as in the order notices."""
    if not upcoming or not upcoming.get('date'):
        return None
    day = format_date(upcoming['date']) or upcoming['date']
    time_label = upcoming.get('time_label')
    return f"{day}, {time_label}" if time_label else day


def _item_line(item, language: str, *, priced: bool) -> str:
    """One subscription item, "  • Aqua 19L x2", as the backend published it. An item the backend marks
    ``available: False`` (a product no longer sold, or deleted) is left out of every order (spec 2026-10-26
    §8.5), so it says "Not included" instead of a price; otherwise the items screen (``priced``) adds the
    line's published label (the one price rule, B10)."""
    line = f"  • {SubscriptionKeyboards.item_name(item, language)} x{item.get('quantity', 1)}"
    if item.get('available') is False:
        return f"{line} - {i18n.get('telegram.subscription.item_not_included', language)}"
    if priced and item.get('line_total_label'):
        return f"{line} - {item['line_total_label']}"
    return line


def _availability_note(subscription, language: str) -> str | None:
    """Why some or all items will not come, from the backend's ``items_availability`` (§8.5): ``some``, the rest
    is delivered without them; ``none``, nothing can be ordered until the items change. ``None`` for ``all``."""
    availability = (subscription or {}).get('items_availability')
    if availability == 'some':
        return i18n.get('telegram.subscription.some_unavailable_note', language)
    if availability == 'none':
        return i18n.get('telegram.subscription.none_available_note', language)
    return None


# The ids and tokens ride the callbacks, and a state token can be all digits, so `_subscription_id`'s scan from the
# right would read it as the id. The shapes that carry one are parsed whole.
_SKIP_CALLBACK = re.compile(
    r'^skip_sub_(\d+)(?:_(\d{4}-\d{2}-\d{2})(?:_([a-z_]+))?)?$'
)
_STATE_CALLBACK = re.compile(r'^confirm(pause|cancel)_sub_(\d+)(?:_([0-9a-f]{16}))?$')
_HISTORY_CALLBACK = re.compile(r'^(?:subhist_(\d+)_(\d+)|view_logs_(\d+))$')


def _published(subscription, action: str) -> dict | None:
    """The card's published ``allowed_actions.<action>`` when it is allowed, else ``None``."""
    published = ((subscription or {}).get('allowed_actions') or {}).get(action)
    return published if isinstance(published, dict) and published.get('allowed') else None


def _confirm_text(published: dict, *, coins: bool = False) -> str:
    """The consequence the backend published for a pause or a cancel, one sentence per line:
    ``confirm_text``, what stays, what is cancelled, and for a cancel the AquaCoins line. Finished words."""
    lines = [published.get('confirm_text'), *(published.get('kept_lines') or []), *(published.get('cancel_lines') or [])]
    if coins:
        lines.append(published.get('coins_line'))
    return "\n".join(line for line in lines if line)


def _subscription_id(query_data) -> int | None:
    """The subscription id trailing a subscription callback; ``None`` when absent.

    ONE parser for every shape these screens emit — ``subscription_7``,
    ``edit_sub_7``, ``skip_sub_7_2026-11-06``, ``confirmcancel_sub_7``, ``change_payment_7``,
    ``change_slot_7``, ``subslot_5_7``, ``manage_items_7``, ``view_logs_7``,
    ``subfreq_weekly_7``, ``subpay_business_account_7`` — the same way
    ``handlers/orders.py`` keeps exactly one ``_cancelling_order_id``.

    It exists because the hand-written indices drifted. ``billing_history``,
    ``change_payment`` and ``retry_billing`` all read ``split('_')[3]`` from a
    THREE-segment callback and raised ``IndexError`` on every tap, while their
    six siblings correctly used ``[2]``. Worse, ``retry_billing`` and
    ``skip_sub`` finish by delegating to :meth:`subscription_details`, which
    re-parsed the very same string at ``[1]`` — so even the corrected index left
    ``int('billing')`` behind it. One question, one answer.

    Scans from the RIGHT: the id is always last, and the parts before it may
    themselves hold underscores (``business_account``). ``0`` is treated as
    absent — it is not a real subscription id.
    """
    for part in reversed(str(query_data or '').split('_')):
        if part.isdigit() and int(part):
            return int(part)
    return None


class SubscriptionHandlers(BaseHandler):
    """Subscription-related handlers with full functionality"""

    async def subscriptions_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show user subscriptions"""
        try:
            user = await user_middleware(update)
            if not user:
                return

            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                response = await client.get_user_subscriptions(user_token)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return

                subscriptions = response.data.get("data", {}).get('items', [])

            subs_text = f"{i18n.get('telegram.subscription.title', language)}\n\n"
            if subscriptions:
                active_count = len([s for s in subscriptions if s.get('status') == 'active'])
                paused_count = len([s for s in subscriptions if s.get('status') == 'paused'])
                subs_text += f"✅ {i18n.get('telegram.subscription.active', language)}: {active_count}\n"
                if paused_count > 0:
                    subs_text += f"⏸️ {i18n.get('telegram.subscription.paused', language)}: {paused_count}\n"
            else:
                subs_text += i18n.get('telegram.subscription.no_subscriptions', language)

            keyboard = SubscriptionKeyboards.subscription_list(subscriptions, language)

            if update.callback_query:
                await self._edit_or_replace_callback_message(
                    update.callback_query, text=subs_text, reply_markup=keyboard
                )
                await self._ack(update.callback_query)
            else:
                await update.message.reply_text(text=subs_text, reply_markup=keyboard)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="subscriptions_menu")

    async def subscription_details(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, subscription_id: int | None = None,
        *, deep_link: bool = False,
    ):
        """Show subscription details with real API data.

        ``subscription_id`` may be passed explicitly (an order's skip lands here, and so does a
        `/start sub_<id>` link); otherwise it is parsed from the callback data. ``deep_link``
        sends the screen as a new message, and answers a subscription this account does not own
        (404) "not found" with the main menu (spec 2026-10-27 §16 M5).
        """
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            if subscription_id is None:
                subscription_id = _subscription_id(query.data)

            response = await self._read_card(update, context, subscription_id, language)
            if response is None:
                return
            if not response.success:
                if deep_link and response.status_code == 404:
                    await send_menu_keyboard(
                        context.bot, update.effective_chat.id, user_id,
                        i18n.get('telegram.deep_link.not_found', language), language,
                    )
                    return
                await self._handle_api_error(update, response.error, language)
                return

            subscription = response.data.get('data', {}).get("subscription", {})

            # The buttons are the actions the backend published as allowed; the bot checks no status itself.
            keyboard = SubscriptionKeyboards.subscription_actions(
                subscription_id, subscription.get('allowed_actions'), language
            )

            await self._show(update, self._card_text(subscription, language), reply_markup=keyboard)
            await self._ack(query)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="subscription_details")

    async def _read_card(self, update, context, subscription_id: int, language: str):
        """One read of the subscription (`GET /subscriptions/<id>`): the card, with its published
        `allowed_actions` and the `expected_state` they carry. ``None`` when the customer could not be signed in
        (the error is already shown); otherwise the response, for the caller to read or refuse."""
        async with api_client as client:
            user_token = await get_auth_token(update, context, client)
            if not user_token:
                await self._handle_auth_error(update, language)
                return None
            return await client.get_subscription(user_token, subscription_id)

    @staticmethod
    def _card_text(subscription: dict, language: str) -> str:
        """The subscription card, in the backend's published words (spec 2026-10-27 §5.3): the schedule sentence,
        the next delivery with its time and payment note, the next dates (a skipped one marked), the items, the
        per-delivery and per-month figures, the payment method. The bot adds only its icons and labels."""
        get = lambda key: i18n.get(f'telegram.subscription.{key}', language)  # noqa: E731
        status = subscription.get('status')
        lines = [
            f"🔄 {get('details_title')}",
            "",
            f"{SUBSCRIPTION_STATUS_ICONS.get(status, '❓')} {get('status')}: "
            f"{i18n.get(f'telegram.subscription.status_{status}', language)}",
        ]
        if subscription.get('schedule_sentence'):
            lines.append(f"📅 {subscription['schedule_sentence']}")

        upcoming = subscription.get('upcoming_delivery') or {}
        delivery = _upcoming_text(upcoming, language)
        if delivery:
            lines.append(f"🚚 {get('next_delivery')}: {delivery}")
        # The payment note, what Pay settles when it is another delivered order (the note under the Pay the card
        # offers), and what a paused or cancelled card still delivers; each said once. The Pay note stands on its
        # own: Pay can settle a delivered order on a card with no delivery coming (paused, cancelled).
        pay = _published(subscription, 'pay') or {}
        notes = []
        for note in (upcoming.get('payment_note') if delivery else None, pay.get('note'),
                     upcoming.get('kept_line') if delivery else None):
            if note and note not in notes:
                notes.append(note)
        lines.extend(f"   {note}" for note in notes)

        # The website's rule (my-subscriptions.js `waterline`): when Resume is offered, the one date is what a
        # resume today would give, not a delivery that is coming.
        dates_label = get('if_resume') if _published(subscription, 'resume') else get('next_dates')
        dates = [
            f"{d['label_short']} ({get('date_skipped')})" if d.get('state') == 'skipped' else d['label_short']
            for d in subscription.get('upcoming_dates') or []
        ]
        if dates:
            lines.append(f"🗓 {dates_label}: {' · '.join(dates)}")

        items = subscription.get('subscription_items') or []
        if items:
            lines.append("")
            lines.append(f"📦 {get('items')}:")
            lines.extend(_item_line(item, language, priced=False) for item in items)

        # The backend's one estimate (spec 2026-10-26 §12, B10): the items at their current customer price, the
        # figure the website shows. Never `billing_amount`, a stored figure nobody is charged. It leaves out what
        # can no longer be delivered (§8.5), and the note says so.
        money = []
        if subscription.get('estimated_per_delivery_label'):
            money.append(f"{get('per_delivery')}: {subscription['estimated_per_delivery_label']}")
        if subscription.get('estimated_per_month_label'):
            money.append(f"{get('per_month')}: {subscription['estimated_per_month_label']}")
        if money:
            lines.append("")
            lines.append(f"💰 {' · '.join(money)}")
        if subscription.get('payment_method_label'):
            lines.append(f"💳 {get('payment')}: {subscription['payment_method_label']}")
        if subscription.get('payment_method_warning'):
            lines.append(f"⚠️ {subscription['payment_method_warning']}")
        note = _availability_note(subscription, language)
        if note:
            lines.append("")
            lines.append(f"⚠️ {note}")
        return "\n".join(lines) + "\n"

    # ========== SUBSCRIPTION CREATION FLOW ==========

    async def create_subscription_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start subscription creation flow"""
        try:
            language = await i18n.get_user_language(update.effective_user.id)

            # Initialize context
            context.user_data['subscription_creation'] = {
                'items': [],
                'frequency': None,
                'address_id': None,
                'start_date': None,
                'payment_method': None
            }

            return await self.select_products(update, context)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="create_subscription_start")
            return ConversationHandler.END

    async def select_products(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Select products for subscription"""
        try:
            language = await i18n.get_user_language(update.effective_user.id)
            products = await self._fetch_and_display_products(
                update, context, language,
                header_key='telegram.subscription.select_products'
            )
            if products is None:
                return ConversationHandler.END

            return SELECT_QUANTITY

        except Exception as e:
            await self._handle_error(update, exc=e, operation="select_products")
            return ConversationHandler.END

    async def select_quantity(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Select quantity for chosen product"""
        try:
            language = await i18n.get_user_language(update.effective_user.id)
            product_id = int(update.callback_query.data.split('_')[2])
            context.user_data['current_product_id'] = product_id

            min_order_qty = await self._product_min_order_qty(update, context, product_id, language)
            if min_order_qty is None:
                return SELECT_QUANTITY

            await self._show_quantity_selector(
                update, language, 'telegram.subscription.select_quantity', min_order_qty=min_order_qty
            )
            return SELECT_FREQUENCY

        except Exception as e:
            await self._handle_error(update, exc=e, operation="select_quantity")
            return ConversationHandler.END

    async def add_item_with_quantity(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Add item with selected quantity to subscription and ask for more"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Save item with quantity
            quantity = int(query.data.split('_')[2])
            product_id = context.user_data.get('current_product_id')

            context.user_data['subscription_creation']['items'].append({
                'product_id': product_id,
                'quantity': quantity
            })

            # Show confirmation and options to add more or continue
            items_count = len(context.user_data['subscription_creation']['items'])
            text = f"✅ {i18n.get('telegram.subscription.item_added', language)}\n\n"
            text += f"{i18n.get('telegram.subscription.total_items', language)}: {items_count}\n\n"
            text += i18n.get('telegram.subscription.add_more_or_continue', language)

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"➕ {i18n.get('telegram.subscription.add_more_items', language)}",
                    callback_data='sub_add_more_items'
                )],
                [InlineKeyboardButton(
                    f"✅ {i18n.get('telegram.continue', language)}",
                    callback_data='sub_items_done'
                )],
                [InlineKeyboardButton(
                    i18n.get('telegram.cancel', language),
                    callback_data='cancel_subscription_creation'
                )]
            ])

            await self._edit_or_replace_callback_message(query, text=text, reply_markup=keyboard)
            await self._ack(query)

            return SELECT_FREQUENCY  # Reusing this state for the intermediate step

        except Exception as e:
            await self._handle_error(update, exc=e, operation="add_item_with_quantity")
            return ConversationHandler.END

    async def choose_frequency(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """The frequency tap (`subscription_freq_<frequency>`): remember ONE frequency, forget any day
        and slot chosen for a previous one, and ask the delivery day (weekly, biweekly, monthly) or go
        straight to the time slot (daily, whose days are the slot's)."""
        try:
            language = await i18n.get_user_language(update.effective_user.id)
            creation = context.user_data['subscription_creation']
            creation['frequency'] = update.callback_query.data.split('_')[2]
            for stale in ('delivery_day', 'delivery_time_slot_id', 'delivery_time_range', 'slot_choices'):
                creation.pop(stale, None)

            if creation['frequency'] == 'daily':
                return await self._show_slot_selection(update, context, language)
            return await self._show_day_selection(update, context, language)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="choose_frequency")
            return ConversationHandler.END

    async def _show_frequency_selection(self, update: Update, language: str):
        """The frequency screen: after the items, and again from either Back below."""
        query = update.callback_query
        await self._edit_or_replace_callback_message(
            query,
            text=i18n.get('telegram.subscription.select_frequency', language),
            reply_markup=SubscriptionKeyboards.subscription_frequency(language),
        )
        await self._ack(query)
        return SELECT_ADDRESS

    async def _delivery_weekdays(self, update: Update, context: ContextTypes.DEFAULT_TYPE, frequency: str):
        """The weekdays the backend delivers on (`delivery_weekdays` on the time-slots answer, spec 2026-10-27
        §6.4), for a weekly or biweekly day picker; ``None`` for monthly, which has no weekday. A failed call or a
        missing field is ``None`` too, and the picker then offers all seven days: the schedule write checks the
        day again, so the picker is a convenience, never the rule."""
        if frequency not in _WEEKDAY_FREQUENCIES:
            return None
        async with api_client as client:
            user_token = await get_auth_token(update, context, client)
            if not user_token:
                return None
            response = await client.get_subscription_time_slots(user_token, 'any', frequency)
        days = (response.data or {}).get('delivery_weekdays') if response.success else None
        return days if isinstance(days, list) and days else None

    async def _show_day_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE, language: str):
        """The delivery-day step of creation. Returns the next state."""
        frequency = context.user_data['subscription_creation']['frequency']
        key = 'select_day_of_week' if frequency in _WEEKDAY_FREQUENCIES else 'select_day_of_month'
        weekdays = await self._delivery_weekdays(update, context, frequency)
        query = update.callback_query
        await self._edit_or_replace_callback_message(
            query,
            text=i18n.get(f'telegram.subscription.{key}', language),
            reply_markup=SubscriptionKeyboards.day_picker(
                frequency, language, delivery_weekdays=weekdays if weekdays else range(7),
            ),
        )
        await self._ack(query)
        return SELECT_DAY

    async def select_day(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """The day tap (`subscription_day_<n>`): a weekday (0 = Monday) or a day of the month."""
        try:
            language = await i18n.get_user_language(update.effective_user.id)
            creation = context.user_data['subscription_creation']
            creation['delivery_day'] = int(update.callback_query.data.rsplit('_', 1)[1])
            for stale in ('delivery_time_slot_id', 'delivery_time_range', 'slot_choices'):
                creation.pop(stale, None)

            return await self._show_slot_selection(update, context, language)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="select_day")
            return ConversationHandler.END

    async def _show_slot_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE, language: str):
        """The time-slot step of creation: the slots the backend offers on the chosen weekday (every
        active slot when the day is not a weekday). No slot offered means no screen: the order is dated
        and its time confirmed later. Returns the next state."""
        query = update.callback_query
        creation = context.user_data['subscription_creation']
        weekday = creation['delivery_day'] if creation['frequency'] in _WEEKDAY_FREQUENCIES else 'any'

        async with api_client as client:
            user_token = await get_auth_token(update, context, client)
            if not user_token:
                await self._handle_auth_error(update, language)
                return ConversationHandler.END

            response = await client.get_subscription_time_slots(user_token, weekday, creation['frequency'])
            if not response.success:
                await self._handle_api_error(update, response.error, language)
                return ConversationHandler.END

        slots = (response.data or {}).get('time_slots', [])
        if not slots:
            # Nothing to choose: the order is dated and its time confirmed later, and the confirm says so in the
            # same words as the Any time tile.
            creation.pop('delivery_time_slot_id', None)
            creation['delivery_time_range'] = i18n.get('telegram.subscription.any_time', language)
            return await self._show_address_selection(update, context, language)

        creation['slot_choices'] = {str(slot['id']): slot.get('time_range') for slot in slots}
        await self._edit_or_replace_callback_message(
            query,
            text=i18n.get('telegram.subscription.select_time_slot', language),
            reply_markup=SubscriptionKeyboards.time_slot_picker(slots, language),
        )
        await self._ack(query)
        return SELECT_SLOT

    async def select_time_slot(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """The slot tap (`subscription_slot_<id>` or `subscription_slot_any`)."""
        try:
            language = await i18n.get_user_language(update.effective_user.id)
            creation = context.user_data['subscription_creation']
            choice = update.callback_query.data.rsplit('_', 1)[1]
            if choice == 'any':
                creation.pop('delivery_time_slot_id', None)
                creation['delivery_time_range'] = i18n.get('telegram.subscription.any_time', language)
            else:
                creation['delivery_time_slot_id'] = int(choice)
                creation['delivery_time_range'] = creation.get('slot_choices', {}).get(choice)

            return await self._show_address_selection(update, context, language)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="select_time_slot")
            return ConversationHandler.END

    async def back_to_frequency_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Back from the day picker: the frequency screen again."""
        try:
            language = await i18n.get_user_language(update.effective_user.id)
            return await self._show_frequency_selection(update, language)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="back_to_frequency_selection")
            return ConversationHandler.END

    async def back_to_day_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Back from the slot picker: the day picker, or the frequency screen for a daily subscription
        (it has no day to go back to)."""
        try:
            language = await i18n.get_user_language(update.effective_user.id)
            if context.user_data['subscription_creation']['frequency'] == 'daily':
                return await self._show_frequency_selection(update, language)
            return await self._show_day_selection(update, context, language)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="back_to_day_selection")
            return ConversationHandler.END

    async def back_to_address_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Back from the payment-method screen of subscription creation.

        `SubscriptionKeyboards.payment_methods` has always rendered this
        button; nothing claimed it, so a customer part way through subscription
        checkout tapped Back and stayed on the payment screen. The frequency
        they already chose is still in `subscription_creation`, so only the
        address step is re-shown.
        """
        try:
            language = await i18n.get_user_language(update.effective_user.id)
            return await self._show_address_selection(update, context, language)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="back_to_address_selection")
            return ConversationHandler.END

    async def _show_address_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                                      language: str):
        """Render the address step of subscription creation. Returns the next state.

        Shared by the forward step (`select_address`) and the Back button
        (`back_to_address_selection`) so the screen a customer returns to is
        the screen they left, by construction.
        """
        try:
            query = update.callback_query

            # Get user addresses
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return ConversationHandler.END

                response = await client.get_user_addresses(user_token)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return ConversationHandler.END

                addresses = response.data.get("data", {}).get('addresses', [])

            if not addresses:
                text = i18n.get('telegram.subscription.no_addresses', language)
                keyboard = [[InlineKeyboardButton(
                    i18n.get('telegram.subscription.add_address', language),
                    callback_data='add_new_address'
                )]]
                await self._edit_or_replace_callback_message(query, text=text, reply_markup=InlineKeyboardMarkup(keyboard))
                await self._ack(query)
                return ConversationHandler.END

            text = i18n.get('telegram.subscription.select_address', language)
            keyboard = self._build_address_keyboard(addresses, language)

            await self._edit_or_replace_callback_message(query, text=text, reply_markup=keyboard)
            await self._ack(query)

            return SELECT_PAYMENT

        except Exception as e:
            await self._handle_error(update, exc=e, operation="show_address_selection")
            return ConversationHandler.END

    async def select_payment(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Select payment method"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Save address
            address_id = int(query.data.split('_')[1])
            context.user_data['subscription_creation']['address_id'] = address_id

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return ConversationHandler.END

                methods_response = await client.get_payment_methods(
                    user_token, context="subscription", delivery_address_id=address_id
                )
                if not methods_response.success:
                    await self._handle_api_error(update, methods_response.error, language)
                    return ConversationHandler.END

            available_methods = (methods_response.data or {}).get('data', {}).get('available_methods', [])

            text = i18n.get('telegram.subscription.select_payment', language)
            keyboard = SubscriptionKeyboards.payment_methods(available_methods, language)

            await self._edit_or_replace_callback_message(query, text=text, reply_markup=keyboard)
            await self._ack(query)

            return CONFIRM_SUBSCRIPTION

        except Exception as e:
            await self._handle_error(update, exc=e, operation="select_payment")
            return ConversationHandler.END

    async def confirm_subscription(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show preview and confirm subscription creation"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Save payment method
            payment_method = query.data.split('_', 2)[2]
            context.user_data['subscription_creation']['payment_method'] = payment_method

            # Get preview from API
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return ConversationHandler.END

                # Prepare preview data - only send fields required by SubscriptionPreviewRequest
                subscription_data = context.user_data['subscription_creation']
                preview_data = {
                    'frequency': subscription_data.get('frequency'),
                    'items': subscription_data.get('items', [])
                }
                response = await client.preview_subscription(user_token, preview_data, language)

                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return ConversationHandler.END

                preview = response.data.get("data", {}).get('preview', {})

            # Build confirmation message
            # What the quote publishes, in the customer's language (re-review admin-bot-backend-4): each line's
            # translated name and label, and the per-delivery label, as the picker two screens back showed them.
            text = f"{i18n.get('telegram.subscription.confirm_title', language)}\n\n"
            text += f"📦 {i18n.get('telegram.subscription.items', language)}:\n"
            for item in preview.get('items', []):
                text += f"{_item_line(item, language, priced=True)}\n"
            text += f"\n📅 {i18n.get('telegram.subscription.frequency', language)}: "
            text += f"{SubscriptionKeyboards.frequency_label(subscription_data.get('frequency'), language)}\n"
            if subscription_data.get('delivery_day') is not None:
                day = subscription_data['delivery_day']
                day_text = (SubscriptionKeyboards.weekday_label(day, language)
                            if subscription_data['frequency'] in _WEEKDAY_FREQUENCIES else str(day))
                text += f"🗓 {i18n.get('telegram.subscription.delivery_day', language)}: {day_text}\n"
            if subscription_data.get('delivery_time_range'):
                text += (f"🕘 {i18n.get('telegram.subscription.delivery_time', language)}: "
                         f"{subscription_data['delivery_time_range']}\n")
            text += f"💰 {i18n.get('telegram.subscription.total', language)}: {preview.get('total_amount_label', '')}\n"

            keyboard = [[
                InlineKeyboardButton(
                    i18n.get('telegram.confirm', language),
                    callback_data='confirm_create_subscription'
                ),
                InlineKeyboardButton(
                    i18n.get('telegram.cancel', language),
                    callback_data='cancel_subscription_creation'
                )
            ]]

            await self._edit_or_replace_callback_message(query, text=text, reply_markup=InlineKeyboardMarkup(keyboard))
            await self._ack(query)

            return CONFIRM_SUBSCRIPTION

        except Exception as e:
            await self._handle_error(update, exc=e, operation="confirm_subscription")
            return ConversationHandler.END

    async def create_subscription_confirmed(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Actually create the subscription"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return ConversationHandler.END

                # Prepare subscription data according to CreateSubscriptionRequest schema
                context_data = context.user_data['subscription_creation']

                # Generate subscription name from first product or use default
                frequency = context_data.get('frequency')
                subscription_name = f"Subscription - {(frequency or 'weekly').title()}"

                subscription_data = {
                    'name': subscription_name,
                    'frequency': frequency,
                    'delivery_address_id': context_data.get('address_id'),  # Map address_id to delivery_address_id
                    'payment_method': context_data.get('payment_method'),
                    'items': context_data.get('items', []),
                    'auto_renew': True,
                }
                # The day and the slot are the customer's choices (spec §2.9): a weekday (0 = Monday) for
                # weekly and biweekly, a day of the month for monthly, an optional slot for every frequency.
                if context_data.get('delivery_day') is not None:
                    day_field = 'delivery_day_of_week' if frequency in _WEEKDAY_FREQUENCIES else 'delivery_day_of_month'
                    subscription_data[day_field] = context_data['delivery_day']
                if context_data.get('delivery_time_slot_id'):
                    subscription_data['delivery_time_slot_id'] = context_data['delivery_time_slot_id']

                # Add optional fields if present
                if context_data.get('start_date'):
                    subscription_data['start_date'] = context_data['start_date']
                if context_data.get('description'):
                    subscription_data['description'] = context_data['description']

                response = await client.create_subscription(user_token, subscription_data)

                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return ConversationHandler.END

                subscription = (response.data or {}).get('data', {}).get('subscription', {})

            text = f"✅ {i18n.get('telegram.subscription.created_success', language)}\n\n"
            text += f"🆔 {i18n.get('telegram.subscription.id', language)}: {subscription.get('id')}"
            upcoming_text = _upcoming_text(subscription.get('upcoming_delivery'), language)
            if upcoming_text:
                text += f"\n🚚 {i18n.get('telegram.subscription.next_delivery', language)}: {upcoming_text}"

            keyboard = [[
                InlineKeyboardButton(
                    i18n.get('telegram.subscription.view', language),
                    callback_data=f'subscription_{subscription.get("id")}'
                )
            ]]

            await self._edit_or_replace_callback_message(query, text=text, reply_markup=InlineKeyboardMarkup(keyboard))
            await self._ack(query, i18n.get('telegram.subscription.created_success', language))

            # Clear context
            for name in SUBSCRIPTION_CREATION_KEYS:
                context.user_data.pop(name, None)

            return ConversationHandler.END

        except Exception as e:
            await self._handle_error(update, exc=e, operation="create_subscription_confirmed")
            return ConversationHandler.END

    async def add_more_items(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle 'Add More Items' button during subscription creation"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Go back to product selection
            return await self.select_products(update, context)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="add_more_items")
            return ConversationHandler.END

    async def items_selection_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle 'Done' button after selecting items"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Check if at least one item was added
            items = context.user_data.get('subscription_creation', {}).get('items', [])
            if not items:
                await self._ack(query, i18n.get('telegram.subscription.select_at_least_one_item', language), show_alert=True)
                return SELECT_QUANTITY

            # Proceed to frequency selection
            return await self._show_frequency_selection(update, language)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="items_selection_done")
            return ConversationHandler.END

    async def cancel_subscription_creation(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel subscription creation"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Clear context
            for name in SUBSCRIPTION_CREATION_KEYS:
                context.user_data.pop(name, None)

            text = i18n.get('telegram.subscription.creation_cancelled', language)

            # An edit carries no menu: the bottom keyboard is the way on.
            await self._edit_or_replace_callback_message(query, text=text)
            await self._ack(query)

            return ConversationHandler.END

        except Exception as e:
            await self._handle_error(update, exc=e, operation="cancel_subscription_creation")
            return ConversationHandler.END

    # ========== SUBSCRIPTION ACTIONS ==========

    async def subscription_actions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """`pause_sub_<id>` / `resume_sub_<id>`.

        Pause reads the card afresh. When the backend says the pause cancels or keeps an order
        (`pause.needs_confirm`) it draws that consequence and waits for `confirmpause_sub_<id>_<state>`; otherwise
        it pauses at once with the state it just read, as the website does. Resume has no consequence to ask about.
        """
        try:
            query = update.callback_query
            language = await i18n.get_user_language(update.effective_user.id)

            action = query.data.split('_')[0]  # pause, resume
            sub_id = _subscription_id(query.data)

            if action == 'resume':
                async with api_client as client:
                    user_token = await get_auth_token(update, context, client)
                    if not user_token:
                        await self._handle_auth_error(update, language)
                        return
                    response = await client.resume_subscription(user_token, sub_id)
                await self._finish_write(
                    update, context, response, sub_id, language, 'telegram.subscription.resumed_success',
                )
                return

            card = await self._read_card(update, context, sub_id, language)
            if card is None:
                return
            if not card.success:
                await self._handle_api_error(update, card.error, language)
                return
            subscription = card.data.get('data', {}).get('subscription', {})
            pause = _published(subscription, 'pause')
            if pause is None:
                await self._stale(update, context, sub_id, language)
                return

            allowed = subscription['allowed_actions']
            if pause.get('needs_confirm'):
                await self._show(
                    update, _confirm_text(pause),
                    reply_markup=SubscriptionKeyboards.action_confirmation(sub_id, 'pause', allowed, language),
                )
                await self._ack(query)
                return
            await self._write_pause(update, context, sub_id, allowed['expected_state'], language)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="subscription_actions")

    async def pause_subscription_confirmed(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """`confirmpause_sub_<id>_<state>`: the customer saw the consequence and said yes. The state the card
        showed rides the callback, so the backend refuses (409) a pause the world has moved past."""
        try:
            language = await i18n.get_user_language(update.effective_user.id)
            match = _STATE_CALLBACK.match(update.callback_query.data)
            await self._write_pause(update, context, int(match.group(2)), match.group(3), language)
        except Exception as e:
            await self._handle_error(update, exc=e, operation="pause_subscription_confirmed")

    async def _write_pause(self, update, context, sub_id: int, expected_state: str, language: str):
        async with api_client as client:
            user_token = await get_auth_token(update, context, client)
            if not user_token:
                await self._handle_auth_error(update, language)
                return
            response = await client.pause_subscription(user_token, sub_id, expected_state=expected_state)
        await self._finish_write(update, context, response, sub_id, language, 'telegram.subscription.paused_success')

    async def _finish_write(self, update, context, response, sub_id: int, language: str, success_key: str,
                            then_items: bool = False):
        """The end of every subscription write. 200: the success toast, then the card as it stands now.
        409 ("This changed"): the backend's sentence as an alert, then the fresh card. Any other refusal: its
        own message. `then_items`: the screen shown afterwards is the items screen, not the card."""
        query = update.callback_query
        if response.success:
            await self._ack(query, i18n.get(success_key, language))
        elif response.status_code == 409:
            await self._ack(query, response.error, show_alert=True)
        else:
            await self._handle_api_error(update, response.error, language)
            return
        if then_items:
            await self._show_items(update, context, sub_id, language)
        else:
            await self.subscription_details(update, context, subscription_id=sub_id)

    async def _stale(self, update, context, sub_id: int, language: str):
        """The card the customer tapped is out of date (the action is no longer offered): say so, show the card
        as it is now."""
        await self._ack(update.callback_query, i18n.get('telegram.subscription.skip_stale', language), show_alert=True)
        await self.subscription_details(update, context, subscription_id=sub_id)

    async def cancel_subscription_prompt(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """`cancel_sub_<id>`, and an old `confirmcancel_sub_<id>` that carried no state: ask before cancelling,
        with what the backend says the cancel will cancel and keep. Nothing is sent by this tap."""
        try:
            query = update.callback_query
            language = await i18n.get_user_language(update.effective_user.id)
            sub_id = _subscription_id(query.data)

            card = await self._read_card(update, context, sub_id, language)
            if card is None:
                return
            if not card.success:
                await self._handle_api_error(update, card.error, language)
                return
            subscription = card.data.get('data', {}).get('subscription', {})
            cancel = _published(subscription, 'cancel')
            if cancel is None:
                await self._stale(update, context, sub_id, language)
                return

            await self._show(
                update, _confirm_text(cancel, coins=True),
                reply_markup=SubscriptionKeyboards.action_confirmation(
                    sub_id, 'cancel', subscription['allowed_actions'], language,
                ),
            )
            await self._ack(query)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="cancel_subscription_prompt")

    async def cancel_subscription_confirmed(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """`confirmcancel_sub_<id>_<state>`: the customer said yes to the consequence they were shown. A button
        from before the state rode the callback has nothing to send, so it asks again."""
        try:
            query = update.callback_query
            language = await i18n.get_user_language(update.effective_user.id)
            match = _STATE_CALLBACK.match(query.data)
            if match is None or match.group(3) is None:
                await self.cancel_subscription_prompt(update, context)
                return
            sub_id, state = int(match.group(2)), match.group(3)

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return
                response = await client.cancel_subscription(user_token, sub_id, expected_state=state)
            await self._finish_write(update, context, response, sub_id, language, 'telegram.subscription.cancelled_success')

        except Exception as e:
            await self._handle_error(update, exc=e, operation="cancel_subscription_confirmed")

    async def skip_delivery(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Skip the delivery the button named, in three taps' worth of shapes (`skip_sub_<id>_<yyyy-mm-dd>[_<reason>]`).

        * dated: read the card, draw the backend's `skip.confirm_text` with its reasons;
        * dated with a reason (or `none`): write it. The date rides the callback and is sent as
          ``expected_date``: if the delivery changed meanwhile (paid, created, already skipped) the backend
          answers 409 and skips nothing;
        * dateless (a button from before the date rode the callback): nothing to send, so it is stale.
        Either way the card is shown again, fresh."""
        try:
            query = update.callback_query
            language = await i18n.get_user_language(update.effective_user.id)

            match = _SKIP_CALLBACK.match(query.data or '')
            if match is None:
                await self._ack(query, i18n.get('telegram.unknown_action', language))
                return
            sub_id, expected_date, code = int(match.group(1)), match.group(2), match.group(3)

            if expected_date is None:
                await self._stale(update, context, sub_id, language)
                return

            if code is None:
                card = await self._read_card(update, context, sub_id, language)
                if card is None:
                    return
                if not card.success:
                    await self._handle_api_error(update, card.error, language)
                    return
                subscription = card.data.get('data', {}).get('subscription', {})
                skip = _published(subscription, 'skip')
                if skip is None or skip.get('date') != expected_date:
                    await self._stale(update, context, sub_id, language)
                    return
                await self._show(
                    update, skip['confirm_text'],
                    reply_markup=SubscriptionKeyboards.action_confirmation(
                        sub_id, 'skip', subscription['allowed_actions'], language,
                    ),
                )
                await self._ack(query)
                return

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return
                response = await client.skip_next_delivery(
                    user_token, sub_id, expected_date, reason_code=None if code == 'none' else code,
                )
            await self._finish_write(update, context, response, sub_id, language, 'telegram.subscription.skip_success')

        except Exception as e:
            await self._handle_error(update, exc=e, operation="skip_delivery")

    # ========== ITEM MANAGEMENT ==========

    async def manage_subscription_items(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show item management menu for subscription (`manage_items_<id>`)."""
        try:
            language = await i18n.get_user_language(update.effective_user.id)
            await self._show_items(update, context, _subscription_id(update.callback_query.data), language)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="manage_subscription_items")

    async def _show_items(self, update: Update, context: ContextTypes.DEFAULT_TYPE, sub_id: int, language: str):
        """The item-management screen of ONE named subscription. Its buttons follow the ``allowed_actions``
        the backend publishes on the subscription. Callers pass the id: the remove-item follow-up used to
        re-parse its own callback and read the ITEM id as the subscription's."""
        query = update.callback_query

        async with api_client as client:
            user_token = await get_auth_token(update, context, client)
            if not user_token:
                await self._handle_auth_error(update, language)
                return

            sub_response = await client.get_subscription(user_token, sub_id)
            if not sub_response.success:
                await self._handle_api_error(update, sub_response.error, language)
                return
            subscription = sub_response.data.get('data', {}).get('subscription', {})
            allowed_actions = subscription.get('allowed_actions')
            items = subscription.get('subscription_items') or []

        text = f"{i18n.get('telegram.subscription.manage_items', language)}\n\n"

        if items:
            text += f"{i18n.get('telegram.subscription.current_items', language)}:\n"
            for item in items:
                # Each line at the backend's label (the one price rule, B10), or "Not included" (§8.5).
                text += f"{_item_line(item, language, priced=True)}\n"
            note = _availability_note(subscription, language)
            if note:
                text += f"\n⚠️ {note}\n"
        else:
            text += i18n.get('telegram.subscription.no_items', language)

        keyboard = SubscriptionKeyboards.item_management_menu(sub_id, items, allowed_actions, language)

        await self._edit_or_replace_callback_message(query, text=text, reply_markup=keyboard)
        await self._ack(query)

    async def add_item_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start adding item to subscription"""
        try:
            context.user_data['editing_subscription_id'] = int(
                update.callback_query.data.split('_')[2]
            )
            return await self.add_item_back_to_products(update, context)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="add_item_start")
            return ConversationHandler.END

    async def add_item_back_to_products(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show the add-an-item product list — the first step, and the one the
        quantity screen's Back button returns to.

        That Back used to emit `back_to_product_selection` with nothing in this
        conversation claiming it, so it fell through to the group-0
        `^back_to_product_` handler and died inside `int('selection')`.
        """
        try:
            language = await i18n.get_user_language(update.effective_user.id)
            sub_id = context.user_data.get('editing_subscription_id')
            if not sub_id:
                # The flow's SUBJECT is gone. Reading on would post to
                # `/api/v1/subscriptions/None/items` — a URL the backend can
                # only refuse, shown to the customer as though their
                # subscription were at fault — or mint a `manage_items_None`
                # Back button onto the screen they are looking at, which no
                # pattern claims. Say the flow expired instead; the copy is
                # already seeded for this conversation's own timeout.
                # tests/telegram_bot/test_subscription_item_flows_after_state_loss.py
                await self._ack(
                    update.callback_query,
                    i18n.get('telegram.subscription.flow_timed_out', language),
                    show_alert=True,
                )
                return ConversationHandler.END

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return ConversationHandler.END
                subscription = await self._read_subscription(client, user_token, sub_id, update, language)
            if subscription is None:
                return ConversationHandler.END

            # A product already in the subscription is changed from the item list, not added a second time.
            products = await self._fetch_and_display_products(
                update, context, language,
                header_key='telegram.subscription.select_product_to_add',
                back_callback=f'manage_items_{sub_id}',
                exclude_product_ids={item.get('product_id') for item in subscription.get('subscription_items') or []},
            )
            if products is None:
                return ConversationHandler.END

            return ITEM_SELECT_PRODUCT

        except Exception as e:
            await self._handle_error(update, exc=e, operation="add_item_back_to_products")
            return ConversationHandler.END

    async def add_item_select_quantity(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Select quantity for new item"""
        try:
            language = await i18n.get_user_language(update.effective_user.id)
            product_id = int(update.callback_query.data.split('_')[2])
            context.user_data['adding_product_id'] = product_id

            min_order_qty = await self._product_min_order_qty(update, context, product_id, language)
            if min_order_qty is None:
                return ITEM_SELECT_PRODUCT

            await self._show_quantity_selector(
                update, language, 'telegram.subscription.select_quantity_for_item', min_order_qty=min_order_qty
            )
            return ITEM_SELECT_QUANTITY

        except Exception as e:
            await self._handle_error(update, exc=e, operation="add_item_select_quantity")
            return ConversationHandler.END

    async def add_item_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Confirm and add item to subscription"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            quantity = int(query.data.split('_')[2])
            sub_id = context.user_data.get('editing_subscription_id')
            if not sub_id or not context.user_data.get('adding_product_id'):
                # The flow's SUBJECT is gone. Reading on would post to
                # `/api/v1/subscriptions/None/items` — a URL the backend can
                # only refuse, shown to the customer as though their
                # subscription were at fault — or mint a `manage_items_None`
                # Back button onto the screen they are looking at, which no
                # pattern claims. Say the flow expired instead; the copy is
                # already seeded for this conversation's own timeout.
                # tests/telegram_bot/test_subscription_item_flows_after_state_loss.py
                await self._ack(
                    update.callback_query,
                    i18n.get('telegram.subscription.flow_timed_out', language),
                    show_alert=True,
                )
                return ConversationHandler.END
            product_id = context.user_data.get('adding_product_id')

            await self._preview_items_change(update, context, sub_id, 'add', product_id, quantity, language)

            # Clear context: the save carries everything it needs on its own callback.
            for name in ITEM_MANAGEMENT_KEYS:
                context.user_data.pop(name, None)

            return ConversationHandler.END

        except Exception as e:
            await self._handle_error(update, exc=e, operation="add_item_confirm")
            return ConversationHandler.END

    async def update_item_quantity(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Update quantity for existing item"""
        try:
            language = await i18n.get_user_language(update.effective_user.id)

            # Parse: update_item_{sub_id}_{item_id}
            parts = update.callback_query.data.split('_')
            sub_id = int(parts[2])
            item_id = int(parts[3])

            context.user_data['editing_subscription_id'] = sub_id
            context.user_data['editing_item_id'] = item_id

            product_id = await self._item_product_id(update, context, sub_id, item_id, language)
            if product_id is None:
                return ConversationHandler.END
            min_order_qty = await self._product_min_order_qty(update, context, product_id, language)
            if min_order_qty is None:
                return ConversationHandler.END

            # This flow never showed a product list — the customer picked an
            # existing item off the item-management menu, so that is where Back
            # belongs. The shared default (`back_to_product_selection`) would
            # offer a step that does not exist here.
            await self._show_quantity_selector(
                update, language, 'telegram.subscription.select_new_quantity',
                back_callback=f'manage_items_{sub_id}',
                min_order_qty=min_order_qty,
            )
            return ITEM_SELECT_QUANTITY

        except Exception as e:
            await self._handle_error(update, exc=e, operation="update_item_quantity")
            return ConversationHandler.END

    async def update_item_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Confirm item quantity update"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            quantity = int(query.data.split('_')[2])
            sub_id = context.user_data.get('editing_subscription_id')
            if not sub_id:
                # The flow's SUBJECT is gone. Reading on would post to
                # `/api/v1/subscriptions/None/items` — a URL the backend can
                # only refuse, shown to the customer as though their
                # subscription were at fault — or mint a `manage_items_None`
                # Back button onto the screen they are looking at, which no
                # pattern claims. Say the flow expired instead; the copy is
                # already seeded for this conversation's own timeout.
                # tests/telegram_bot/test_subscription_item_flows_after_state_loss.py
                await self._ack(
                    update.callback_query,
                    i18n.get('telegram.subscription.flow_timed_out', language),
                    show_alert=True,
                )
                return ConversationHandler.END
            item_id = context.user_data.get('editing_item_id')

            product_id = await self._item_product_id(update, context, sub_id, item_id, language)
            if product_id is not None:
                await self._preview_items_change(update, context, sub_id, 'set', product_id, quantity, language)

            # Clear context: the save carries everything it needs on its own callback.
            for name in UPDATE_ITEM_KEYS:
                context.user_data.pop(name, None)

            return ConversationHandler.END

        except Exception as e:
            await self._handle_error(update, exc=e, operation="update_item_confirm")
            return ConversationHandler.END

    async def remove_item_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """`remove_item_<sub>_<item>`: preview the list without that item. Nothing is removed by this tap."""
        try:
            language = await i18n.get_user_language(update.effective_user.id)

            # Parse: remove_item_{sub_id}_{item_id}
            parts = update.callback_query.data.split('_')
            sub_id = int(parts[2])
            item_id = int(parts[3])

            product_id = await self._item_product_id(update, context, sub_id, item_id, language)
            if product_id is not None:
                await self._preview_items_change(update, context, sub_id, 'del', product_id, 0, language)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="remove_item_confirm")

    @staticmethod
    def _items_after(subscription: dict, op: str, product_id: int, quantity: int) -> list[dict]:
        """The whole item list as it would stand after one change: the one builder for the preview and the save, so
        what the customer confirmed is what is written. ``add`` appends the product (or sets its quantity when it
        is already there), ``set`` changes the quantity of a product that is there, ``del`` drops it. Every other
        line, an unavailable one included, is kept as the card published it."""
        items = [
            {'product_id': item['product_id'], 'quantity': item['quantity']}
            for item in subscription.get('subscription_items') or []
        ]
        if op == 'del':
            return [item for item in items if item['product_id'] != product_id]
        for item in items:
            if item['product_id'] == product_id:
                item['quantity'] = quantity
                return items
        if op == 'add':
            items.append({'product_id': product_id, 'quantity': quantity})
        return items

    async def _preview_items_change(self, update, context, sub_id: int, op: str, product_id: int, quantity: int,
                                    language: str):
        """Read the card, ask the backend what the list after this change would do, and show its words with a Save
        carrying the preview's `expected_state`. Nothing is saved here. A refusal (the last item, a product below its
        minimum) is the backend's own sentence and leaves the items as they are."""
        async with api_client as client:
            user_token = await get_auth_token(update, context, client)
            if not user_token:
                await self._handle_auth_error(update, language)
                return
            subscription = await self._read_subscription(client, user_token, sub_id, update, language)
            if subscription is None:
                return
            response = await client.preview_subscription_change(
                user_token, sub_id, {'items': self._items_after(subscription, op, product_id, quantity)},
            )
        if not response.success:
            await self._handle_api_error(update, response.error, language)
            return

        preview = (response.data or {}).get('data', {}).get('preview') or {}
        lines = [i18n.get('telegram.subscription.preview_title', language), ""]
        lines.extend(_item_line(item, language, priced=True) for item in preview.get('items') or [])
        if preview.get('estimated_per_delivery_label'):
            lines.append("")
            lines.append(
                f"{i18n.get('telegram.subscription.per_delivery', language)}: {preview['estimated_per_delivery_label']}"
            )
        if preview.get('kept_order_note'):
            lines.append(preview['kept_order_note'])
        await self._show(
            update, "\n".join(lines),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    i18n.get('telegram.subscription.save_change', language),
                    callback_data=items_save_callback(op, sub_id, product_id, quantity, preview.get('expected_state')),
                )],
                [InlineKeyboardButton(
                    i18n.get('telegram.back', language), callback_data=f'manage_items_{sub_id}',
                )],
            ]),
        )
        await self._ack(update.callback_query)

    async def items_save_confirmed(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """`subitems_<op>_<sub>_<product>_<qty>_<state>`: save the list. The card is read again first and the one
        change re-applied to it, so a line changed elsewhere is not overwritten from a stale copy; the backend still
        answers 409 ("This changed") when the state the preview carried no longer holds. Either way the item list
        is shown as it stands now."""
        try:
            query = update.callback_query
            language = await i18n.get_user_language(update.effective_user.id)
            parsed = parse_items_save_callback(query.data)
            if parsed is None:
                await self._ack(query, i18n.get('telegram.unknown_action', language))
                return
            op, sub_id, product_id, quantity, state = parsed

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return
                subscription = await self._read_subscription(client, user_token, sub_id, update, language)
                if subscription is None:
                    return
                response = await client.replace_subscription_items(
                    user_token, sub_id, self._items_after(subscription, op, product_id, quantity),
                    expected_state=state,
                )
            await self._finish_write(
                update, context, response, sub_id, language,
                'telegram.subscription.items_updated_successfully', then_items=True,
            )

        except Exception as e:
            await self._handle_error(update, exc=e, operation="items_save_confirmed")

    # ========== SUBSCRIPTION EDITING ==========

    async def edit_subscription_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show subscription edit options"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            sub_id = _subscription_id(query.data)

            text = i18n.get('telegram.subscription.edit_menu', language)
            keyboard = SubscriptionKeyboards.edit_subscription_menu(sub_id, language)

            await self._edit_or_replace_callback_message(query, text=text, reply_markup=keyboard)
            await self._ack(query)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="edit_subscription_menu")

    async def change_frequency(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Change subscription frequency"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            sub_id = _subscription_id(query.data)
            context.user_data['editing_subscription_id'] = sub_id

            text = i18n.get('telegram.subscription.select_new_frequency', language)
            keyboard = SubscriptionKeyboards.subscription_frequency(
                language, subscription_id=sub_id
            )

            await self._edit_or_replace_callback_message(query, text=text, reply_markup=keyboard)
            await self._ack(query)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="change_frequency")

    async def update_frequency_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """A new frequency was tapped (`subfreq_<frequency>_<sub_id>`). Nothing is written. Daily has no day, so the
        slot is asked next; weekly, biweekly and monthly ask the delivery day first (a changed cadence with no day
        would deliver on whatever day the schedule finds first), weekly and biweekly offering only the weekdays the
        backend delivers on."""
        try:
            query = update.callback_query
            language = await i18n.get_user_language(update.effective_user.id)

            # `subfreq_<frequency>_<sub_id>`. The subject rides the callback, not
            # `editing_subscription_id`: the Application has no `persistence`, so
            # a deploy empties user_data while this card stays tappable forever,
            # and a single user_data slot meant two open cards edited whichever
            # subscription was stored last.
            frequency = query.data.split('_')[1]
            sub_id = _subscription_id(query.data)

            if frequency == 'daily':
                await self._show_edit_slots(update, context, sub_id, frequency, None)
                return

            key = 'select_day_of_week' if frequency in _WEEKDAY_FREQUENCIES else 'select_day_of_month'
            weekdays = await self._delivery_weekdays(update, context, frequency)
            await self._edit_or_replace_callback_message(
                query,
                text=i18n.get(f'telegram.subscription.{key}', language),
                reply_markup=SubscriptionKeyboards.day_picker(
                    frequency, language, subscription_id=sub_id,
                    delivery_weekdays=weekdays if weekdays else range(7),
                ),
            )
            await self._ack(query)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="update_frequency_confirm")

    async def update_frequency_day_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """The day for a changed frequency (`subday_<frequency>_<day>_<sub_id>`). Nothing is written: the slot is
        asked next, for that day."""
        try:
            _, frequency, day, _sub = update.callback_query.data.split('_')
            await self._show_edit_slots(
                update, context, _subscription_id(update.callback_query.data), frequency, int(day),
            )

        except Exception as e:
            await self._handle_error(update, exc=e, operation="update_frequency_day_confirm")

    async def _show_edit_slots(self, update: Update, context: ContextTypes.DEFAULT_TYPE, sub_id: int,
                               frequency: str, day: int | None):
        """The slot step of a schedule edit: the slots the backend offers that schedule, then "Any time". The
        weekday and the frequency go to the backend, which filters by its own rule, the very rule the save checks.
        A day with no slot offers Any time alone instead of a dead end. Each button carries the whole schedule
        (`subsched_<frequency>_<day>_<slot|any>_<sub>`) and asks for the preview; nothing is saved here."""
        query = update.callback_query
        language = await i18n.get_user_language(update.effective_user.id)

        async with api_client as client:
            user_token = await get_auth_token(update, context, client)
            if not user_token:
                await self._handle_auth_error(update, language)
                return
            weekday = day if frequency in _WEEKDAY_FREQUENCIES and day is not None else 'any'
            response = await client.get_subscription_time_slots(user_token, weekday, frequency)
            if not response.success:
                await self._handle_api_error(update, response.error, language)
                return

        slots = (response.data or {}).get('time_slots', [])
        await self._edit_or_replace_callback_message(
            query,
            text=i18n.get('telegram.subscription.select_new_time_slot', language),
            reply_markup=SubscriptionKeyboards.time_slot_picker(
                slots, language, subscription_id=sub_id, frequency=frequency, day=day,
            ),
        )
        await self._ack(query)

    @staticmethod
    def _schedule_changes(choice) -> dict:
        """The body of both the preview and the save of a schedule change. The day field is left out for daily, and
        Any time is an explicit ``None`` slot: the backend reads a missing slot as "keep the old one"."""
        changes = {'frequency': choice.frequency}
        if choice.frequency in _WEEKDAY_FREQUENCIES:
            changes['delivery_day_of_week'] = choice.day
        elif choice.frequency == 'monthly':
            changes['delivery_day_of_month'] = choice.day
        changes['delivery_time_slot_id'] = choice.slot_id
        return changes

    async def schedule_preview(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """`subsched_<frequency>_<day|x>_<slot|any>_<sub>`: ask the backend what this schedule would do and show its
        words, with a Save that carries the preview's `expected_state`. Nothing is saved by this tap. A refusal
        (a slot switched off meanwhile, a monthly day not chosen) is shown as the backend's own sentence."""
        try:
            query = update.callback_query
            language = await i18n.get_user_language(update.effective_user.id)
            choice = parse_schedule_callback(query.data)
            if choice is None or choice.state is not None:
                await self._ack(query, i18n.get('telegram.unknown_action', language))
                return

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return
                response = await client.preview_subscription_change(
                    user_token, choice.subscription_id, self._schedule_changes(choice),
                )
            if not response.success:
                await self._handle_api_error(update, response.error, language)
                return

            preview = (response.data or {}).get('data', {}).get('preview') or {}
            lines = [i18n.get('telegram.subscription.preview_title', language), ""]
            lines.extend(line for line in (
                f"📅 {preview['schedule_sentence']}" if preview.get('schedule_sentence') else None,
                preview.get('sentence'),
            ) if line)
            dates = " · ".join(d['label_short'] for d in preview.get('upcoming_dates') or [])
            if dates:
                lines.append(f"🗓 {i18n.get('telegram.subscription.next_dates', language)}: {dates}")
            await self._show(
                update, "\n".join(lines),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        i18n.get('telegram.subscription.save_change', language),
                        callback_data=schedule_callback(
                            choice.frequency, choice.day, choice.slot_id, choice.subscription_id,
                            state=preview.get('expected_state'),
                        ),
                    )],
                    [InlineKeyboardButton(
                        i18n.get('telegram.back', language), callback_data=f'edit_sub_{choice.subscription_id}',
                    )],
                ]),
            )
            await self._ack(query)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="schedule_preview")

    async def schedule_save_confirmed(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """`subschedok_<frequency>_<day|x>_<slot|any>_<sub>_<state>`: save exactly what the preview showed. The
        backend re-checks the schedule and answers 409 ("This changed") when the state moved; either way the card
        is shown as it stands now."""
        try:
            query = update.callback_query
            language = await i18n.get_user_language(update.effective_user.id)
            choice = parse_schedule_callback(query.data)
            if choice is None or choice.state is None:
                await self._ack(query, i18n.get('telegram.unknown_action', language))
                return

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return
                response = await client.update_subscription(
                    user_token, choice.subscription_id, self._schedule_changes(choice), expected_state=choice.state,
                )
            await self._finish_write(
                update, context, response, choice.subscription_id, language,
                'telegram.subscription.schedule_updated_successfully',
            )

        except Exception as e:
            await self._handle_error(update, exc=e, operation="schedule_save_confirmed")

    async def change_payment_method_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show payment method selection"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            sub_id = _subscription_id(query.data)
            context.user_data['editing_subscription_id'] = sub_id

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                # The menu depends on the delivery address (the place arm of the cash cap), and the backend
                # refuses a method the menu would not have offered: ask it the way the write will judge it.
                subscription = await self._read_subscription(client, user_token, sub_id, update, language)
                if subscription is None:
                    return

                methods_response = await client.get_payment_methods(
                    user_token, context="subscription", delivery_address_id=subscription.get('delivery_address_id')
                )
                if not methods_response.success:
                    await self._handle_api_error(update, methods_response.error, language)
                    return

            available_methods = (methods_response.data or {}).get('data', {}).get('available_methods', [])

            text = i18n.get('telegram.subscription.select_new_payment_method', language)
            if subscription.get('payment_method_warning'):
                text += f"\n\n⚠️ {subscription['payment_method_warning']}"
            # Reached from the subscription's edit menu, not from the creation
            # flow's address step, so Back goes back there.
            keyboard = SubscriptionKeyboards.payment_methods(
                available_methods, language, back_callback=f'edit_sub_{sub_id}',
                subscription_id=sub_id,
            )

            await self._edit_or_replace_callback_message(query, text=text, reply_markup=keyboard)
            await self._ack(query)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="change_payment_method_menu")

    async def change_payment_method_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """`subpay_<method>_<sub>`: the customer picked a method. Nothing is written: the card is read again and
        the consequence shown (the kept order stays as it is), with a Save that carries the card's `expected_state`."""
        try:
            query = update.callback_query
            language = await i18n.get_user_language(update.effective_user.id)

            # The rail is everything BETWEEN the prefix and the trailing id, because `business_account` contains
            # an underscore of its own.
            payment_method = '_'.join(query.data.split('_')[1:-1])
            sub_id = _subscription_id(query.data)

            card = await self._read_card(update, context, sub_id, language)
            if card is None:
                return
            if not card.success:
                await self._handle_api_error(update, card.error, language)
                return
            subscription = card.data.get('data', {}).get('subscription', {})
            if _published(subscription, 'edit') is None:
                await self._stale(update, context, sub_id, language)
                return

            lines = [f"{i18n.get('telegram.subscription.new_payment_method', language)}: "
                     f"{i18n.get(f'telegram.payment_{payment_method}', language)}"]
            if subscription.get('kept_order_note'):
                lines.append(subscription['kept_order_note'])
            await self._show(
                update, "\n".join(lines),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        i18n.get('telegram.subscription.save_change', language),
                        callback_data=payment_save_callback(
                            payment_method, sub_id, subscription['allowed_actions']['expected_state'],
                        ),
                    )],
                    [InlineKeyboardButton(i18n.get('telegram.back', language), callback_data=f'change_payment_{sub_id}')],
                ]),
            )
            await self._ack(query)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="change_payment_method_confirm")

    async def change_payment_method_saved(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """`subpayok_<method>_<sub>_<state>`: save the method the consequence screen named. The backend answers
        409 ("This changed") when the state no longer holds; either way the card is shown as it stands now."""
        try:
            query = update.callback_query
            language = await i18n.get_user_language(update.effective_user.id)
            parsed = parse_payment_save_callback(query.data)
            if parsed is None:
                await self._ack(query, i18n.get('telegram.unknown_action', language))
                return
            payment_method, sub_id, state = parsed

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return
                response = await client.change_payment_method(
                    user_token, sub_id, {'payment_method': payment_method}, expected_state=state,
                )
            await self._finish_write(
                update, context, response, sub_id, language,
                'telegram.subscription.payment_method_updated_successfully',
            )

        except Exception as e:
            await self._handle_error(update, exc=e, operation="change_payment_method_saved")

    # ========== CHANGE ADDRESS ==========

    async def change_address_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """`change_address_<sub>`: the customer's saved addresses, the current one ticked. No address is created
        here: adding one stays in the address screens (spec 2026-10-27 §7)."""
        try:
            query = update.callback_query
            language = await i18n.get_user_language(update.effective_user.id)
            sub_id = _subscription_id(query.data)

            card = await self._read_card(update, context, sub_id, language)
            if card is None:
                return
            if not card.success:
                await self._handle_api_error(update, card.error, language)
                return
            subscription = card.data.get('data', {}).get('subscription', {})
            if _published(subscription, 'edit') is None:
                await self._stale(update, context, sub_id, language)
                return

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return
                addresses = await client.get_user_addresses(user_token)
            if not addresses.success:
                await self._handle_api_error(update, addresses.error, language)
                return

            current = subscription.get('delivery_address_id')
            rows = [
                [InlineKeyboardButton(
                    ("✓ " if address['id'] == current else "")
                    + (address.get('title') or address.get('full_address') or i18n.get('telegram.common.address', language)),
                    callback_data=f"subaddr_{address['id']}_{sub_id}",
                )]
                for address in (addresses.data or {}).get('data', {}).get('addresses') or []
            ]
            rows.append([InlineKeyboardButton(i18n.get('telegram.back', language), callback_data=f'edit_sub_{sub_id}')])
            await self._show(
                update, i18n.get('telegram.subscription.select_new_address', language),
                reply_markup=InlineKeyboardMarkup(rows),
            )
            await self._ack(query)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="change_address_menu")

    async def change_address_preview(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """`subaddr_<address>_<sub>`: ask the backend what moving to this address does, and show its words. A
        method the new address does not offer is asked for here, from the preview's own menu (B8), before saving."""
        await self._address_preview(update, context, *self._address_ids(update.callback_query.data), method=None)

    async def change_address_payment(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """`subaddrpay_<method>_<address>_<sub>`: the method picked for the new address; previewed again with it."""
        parsed = parse_address_method_callback(update.callback_query.data)
        if parsed is None:
            await self._ack(update.callback_query)
            return
        method, address_id, sub_id = parsed
        await self._address_preview(update, context, address_id, sub_id, method=method)

    @staticmethod
    def _address_ids(data: str) -> tuple[int, int]:
        _, address_id, sub_id = data.split('_')
        return int(address_id), int(sub_id)

    @staticmethod
    def _address_changes(address_id: int, method: str | None) -> dict:
        changes = {'delivery_address_id': address_id}
        if method is not None:
            changes['payment_method'] = method
        return changes

    async def _address_preview(self, update, context, address_id: int, sub_id: int, *, method: str | None):
        try:
            language = await i18n.get_user_language(update.effective_user.id)
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return
                response = await client.preview_subscription_change(
                    user_token, sub_id, self._address_changes(address_id, method),
                )
            if not response.success:
                await self._handle_api_error(update, response.error, language)
                return

            preview = (response.data or {}).get('data', {}).get('preview') or {}
            change = preview.get('address_change') or {}
            if not change.get('payment_method_offered'):
                await self._show(
                    update, i18n.get('telegram.subscription.address_choose_method', language),
                    reply_markup=SubscriptionKeyboards.payment_methods(
                        change.get('payment_methods') or [], language, back_callback=f'change_address_{sub_id}',
                        callback=lambda published: address_method_callback(published, address_id, sub_id),
                    ),
                )
            else:
                lines = [i18n.get('telegram.subscription.preview_title', language), "", *(preview.get('consequences') or [])]
                await self._show(
                    update, "\n".join(lines),
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(
                            i18n.get('telegram.subscription.save_change', language),
                            callback_data=address_confirm_callback(address_id, method, sub_id, preview['expected_state']),
                        )],
                        [InlineKeyboardButton(i18n.get('telegram.back', language), callback_data=f'change_address_{sub_id}')],
                    ]),
                )
            await self._ack(update.callback_query)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="change_address_preview")

    async def change_address_confirmed(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """`subaddrok_<address>_<method|keep>_<sub>_<state>`: save exactly what the preview showed (the address, and
        the method asked for when the old one was not offered there). 409 ("This changed") shows the fresh card."""
        try:
            query = update.callback_query
            language = await i18n.get_user_language(update.effective_user.id)
            parsed = parse_address_confirm_callback(query.data)
            if parsed is None:
                await self._ack(query, i18n.get('telegram.unknown_action', language))
                return
            address_id, method, sub_id, state = parsed

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return
                response = await client.update_subscription(
                    user_token, sub_id, self._address_changes(address_id, method), expected_state=state,
                )
            await self._finish_write(
                update, context, response, sub_id, language, 'telegram.subscription.address_updated_successfully',
            )

        except Exception as e:
            await self._handle_error(update, exc=e, operation="change_address_confirmed")

    async def change_time_slot_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """`change_slot_<id>`, and an old `subslot_<slot>_<id>` button (it named a slot but not the schedule, so it
        writes nothing): the slot picker for THIS subscription's current schedule, the same screen a changed
        frequency or day ends on."""
        try:
            query = update.callback_query
            language = await i18n.get_user_language(update.effective_user.id)
            sub_id = _subscription_id(query.data)

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return
                subscription = await self._read_subscription(client, user_token, sub_id, update, language)
            if subscription is None:
                return

            frequency = subscription.get('frequency')
            day_field = {'monthly': 'delivery_day_of_month'}.get(frequency, 'delivery_day_of_week')
            await self._show_edit_slots(
                update, context, sub_id, frequency,
                None if frequency == 'daily' else subscription.get(day_field),
            )

        except Exception as e:
            await self._handle_error(update, exc=e, operation="change_time_slot_menu")

    # ========== ORDER HISTORY ==========

    async def subscription_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """`subhist_<id>_<page>`: the orders this subscription placed, five to a page, as the website lists them
        (spec 2026-10-27 §7 D19). An old `view_logs_<id>` button lands on page 1. Each row prints the backend's
        date, status, amount and reason; an order that publishes `customer_actions` gets its Pay and its removal."""
        try:
            query = update.callback_query
            language = await i18n.get_user_language(update.effective_user.id)

            match = _HISTORY_CALLBACK.match(query.data or '')
            sub_id = int(match.group(1) or match.group(3))
            page = max(int(match.group(2) or 1), 1)

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return
                response = await client.get_subscription_history(user_token, sub_id, page)
            if not response.success:
                await self._handle_api_error(update, response.error, language)
                return

            body = response.data or {}
            rows = (body.get('data') or {}).get('items') or []
            meta = body.get('meta') or {}

            lines = [i18n.get('telegram.subscription.history_title', language), ""]
            if not rows:
                lines.append(i18n.get('telegram.subscription.history_empty', language))
            for row in rows:
                line = f"• {row.get('date_label')} · {(row.get('status') or {}).get('label')}"
                if row.get('amount_label'):
                    line += f" · {row['amount_label']}"
                if row.get('order_number'):
                    line += f" · {row['order_number']}"
                if row.get('reason_label'):
                    line += f"\n   {row['reason_label']}"
                lines.append(line)

            await self._show(
                update, "\n".join(lines),
                reply_markup=SubscriptionKeyboards.history(
                    sub_id, rows, page, bool(meta.get('has_prev')), bool(meta.get('has_next')), language,
                ),
            )
            await self._ack(query)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="subscription_history")

    # ========== HELPER METHODS ==========

    async def _fetch_and_display_products(self, update, context, language, header_key, back_callback=None,
                                          exclude_product_ids=frozenset()):
        """Shared helper (create and Add item): the subscription catalog and its selection keyboard.

        The list is the backend's subscription catalog (`GET /subscriptions/products`, spec 2026-10-26 B10):
        every product a subscription can take, each at the price label the subscription will use. Never the
        products API's checkout card, whose price is another rule. Every product is listed: none is cut off.

        `exclude_product_ids` leaves out products the subscription already holds (the Add item picker).

        Returns list of products on success, or None on failure (error already shown).
        """
        query = update.callback_query

        async with api_client as client:
            user_token = await get_auth_token(update, context, client)
            if not user_token:
                await self._handle_auth_error(update, language)
                return None

            response = await client.get_subscription_products(user_token, language=language)
            if not response.success:
                await self._handle_api_error(update, response.error, language)
                return None

            products = [
                product for product in response.data.get('data', {}).get('items', [])
                if product.get('id') not in exclude_product_ids
            ]

        # Build text
        products_text = i18n.get(header_key, language) + "\n\n"
        for idx, product in enumerate(products, 1):
            products_text += f"{idx}. {product.get('product_name')} - {product.get('unit_price_label')}\n"

        # Build keyboard
        buttons = []
        for product in products:
            buttons.append([InlineKeyboardButton(
                f"➕ {product.get('product_name')}",
                callback_data=f"sub_product_{product['id']}"
            )])

        if back_callback:
            buttons.append([InlineKeyboardButton(
                i18n.get('telegram.back', language),
                callback_data=back_callback
            )])
        else:
            buttons.append([InlineKeyboardButton(
                i18n.get('telegram.cancel', language),
                callback_data='cancel_subscription_creation'
            )])

        keyboard = InlineKeyboardMarkup(buttons)

        if query:
            await self._edit_or_replace_callback_message(query, text=products_text, reply_markup=keyboard)
            await self._ack(query)
        else:
            await context.bot.send_message(
                chat_id=update.effective_user.id,
                text=products_text,
                reply_markup=keyboard
            )

        return products

    async def _show_quantity_selector(self, update, language, text_key, back_callback=None, *, min_order_qty):
        """Shared helper: display quantity selection keyboard.

        `back_callback` names the screen this one was reached from; the builder
        default (`back_to_product_selection`) is correct for the two flows that
        came from a product list.
        """
        query = update.callback_query
        text = i18n.get(text_key, language)
        keyboard = SubscriptionKeyboards.quantity_selector(
            language, back_callback=back_callback, min_order_qty=min_order_qty
        )
        await self._edit_or_replace_callback_message(query, text=text, reply_markup=keyboard)
        await self._ack(query)

    async def _product_min_order_qty(self, update, context, product_id, language):
        """The product's minimum order quantity: the first button the quantity screen offers.

        Read through `ProductHandlers._purchase_bounds`, the resolver every cart surface
        uses. Its stock ceiling is ignored: a subscription repeats, so today's shelf does
        not bound it. Returns None, with the customer already told, when the product
        cannot be read. Guessing 1 is how a below-minimum subscription got saved.
        """
        async with api_client as client:
            user_token = await get_auth_token(update, context, client)
            if not user_token:
                await self._handle_auth_error(update, language)
                return None
            response = await client.get_product(user_token, product_id, language=language)
            if not response.success:
                await self._handle_api_error(update, response.error, language)
                return None

        product = (response.data or {}).get('data', {}).get('product')
        if not product:
            await self._ack(
                update.callback_query, i18n.get('telegram.error.product_error', language), show_alert=True
            )
            return None
        min_order_qty, _ceiling = ProductHandlers._purchase_bounds(product)
        return min_order_qty

    async def _read_subscription(self, client, user_token: str, sub_id: int, update: Update, language: str):
        """One subscription as the backend publishes it, or ``None`` with the customer already told."""
        response = await client.get_subscription(user_token, sub_id)
        if not response.success:
            await self._handle_api_error(update, response.error, language)
            return None
        return (response.data or {}).get('data', {}).get('subscription') or {}

    async def _item_product_id(self, update, context, sub_id, item_id, language):
        """The product of one subscription line, read off the card's `subscription_items`, or None with the
        customer already told."""
        async with api_client as client:
            user_token = await get_auth_token(update, context, client)
            if not user_token:
                await self._handle_auth_error(update, language)
                return None
            subscription = await self._read_subscription(client, user_token, sub_id, update, language)
        if subscription is None:
            return None

        product_id = next(
            (item.get('product_id') for item in subscription.get('subscription_items') or [] if item.get('id') == item_id),
            None,
        )
        if product_id is None:
            await self._ack(
                update.callback_query, i18n.get('telegram.error.product_error', language), show_alert=True
            )
        return product_id

    def _build_address_keyboard(self, addresses, language):
        """Build keyboard for address selection"""
        keyboard = []
        address_fallback = i18n.get('telegram.common.address', language)
        for addr in addresses:
            button_text = f"{addr.get('address_line1') or address_fallback} - {addr.get('city', '')}"
            if addr.get('is_default'):
                button_text = f"⭐ {button_text}"
            keyboard.append([InlineKeyboardButton(
                button_text,
                callback_data=f'addr_{addr.get("id")}'
            )])

        keyboard.append([InlineKeyboardButton(
            i18n.get('telegram.subscription.add_new_address', language),
            callback_data='add_new_address'
        )])

        return InlineKeyboardMarkup(keyboard)



# Global handler instance
subscription_handlers = SubscriptionHandlers()

subscription_handlers.SELECT_PRODUCTS = SELECT_PRODUCTS
subscription_handlers.SELECT_QUANTITY = SELECT_QUANTITY
subscription_handlers.SELECT_FREQUENCY = SELECT_FREQUENCY
subscription_handlers.SELECT_ADDRESS = SELECT_ADDRESS
subscription_handlers.SELECT_START_DATE = SELECT_START_DATE
subscription_handlers.SELECT_PAYMENT = SELECT_PAYMENT
subscription_handlers.CONFIRM_SUBSCRIPTION = CONFIRM_SUBSCRIPTION
subscription_handlers.ITEM_ACTION = ITEM_ACTION
subscription_handlers.ITEM_SELECT_PRODUCT = ITEM_SELECT_PRODUCT
subscription_handlers.ITEM_SELECT_QUANTITY = ITEM_SELECT_QUANTITY
subscription_handlers.SELECT_DAY = SELECT_DAY
subscription_handlers.SELECT_SLOT = SELECT_SLOT
