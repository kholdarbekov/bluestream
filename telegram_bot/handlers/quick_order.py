"""
Quick Order handlers — one-tap re-ordering for habitual customers.

Two entry points lead here:
  * "🔁 Repeat last" / "⭐ Your usual" buttons rendered at the top of the
    Products menu by `ProductHandlers.products_menu`.
  * "🔁 Reorder" button rendered on delivered orders in order history.

The flow is intentionally non-destructive: both reorder entry points refill the
cart through the backend's fill-from-order (the backend decides which lines a
reorder copies) and route the user through the existing checkout flow (address
+ payment + final confirmation). Checkout opens as a NEW message; the tapped
message is never edited or deleted. We never call `POST /orders/repeat/{id}`
directly — that endpoint creates a finalized order with no confirmation
step, which would bypass the user's "yes, place this order" tap.
"""
import logging
from typing import Dict, List, Optional, Tuple

from telegram import Update
from telegram.ext import ContextTypes

from i18n import i18n
from api_client import api_client
from keyboards import ReorderKeyboards
from utils import user_middleware, get_auth_token, order_status_label
from handlers.base import BaseHandler

logger = logging.getLogger('handlers')


class QuickOrderHandlers(BaseHandler):
    """Handlers for Quick Order buttons."""

    # --- Suggestion building (called by ProductHandlers.products_menu) -----

    async def build_quick_suggestions(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        client,
        user_token: str,
    ) -> List[Dict]:
        """Return up to 2 suggestion dicts for the Products-menu Quick Order section.

        Each dict has shape:
            {
                'kind': 'repeat_last' | 'usual',
                'callback_data': 'quick_repeat_last' | 'quick_usual',
                'label_key': str,                # i18n key for the button label
                'label_args': dict,              # format args
                'product_id': int | None,        # for dedup only
                'quantity': int | None,
            }

        Returns an empty list when no past orders / no suggestions exist.
        Quick Order is supplementary — any failure (missing endpoint, network
        error, malformed payload) silently degrades to no suggestions rather
        than breaking the Products menu.
        """
        suggestions: List[Dict] = []

        try:
            last = await self._get_last_order_summary(client, user_token)
        except Exception as e:
            logger.warning(f"Quick Order: failed to build 'repeat last' suggestion: {e}")
            last = None
        try:
            usual = await self._get_usual_suggestion(client, user_token)
        except Exception as e:
            logger.warning(f"Quick Order: failed to build 'usual' suggestion: {e}")
            usual = None

        if last:
            suggestions.append(last)

        # Dedupe: hide "Your usual" if it points to the same product+quantity
        # as "Repeat last".
        if usual and not self._is_same_target(last, usual):
            suggestions.append(usual)

        return suggestions

    # --- Callback handlers --------------------------------------------------

    async def handle_repeat_last(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle the 'Repeat last' button from the Products menu."""
        # Every render and error goes to `screen`, so the tapped message is never edited.
        screen = self.as_message_update(update)
        try:
            await self._ack(update.callback_query)
            user = await user_middleware(screen)
            if not user:
                return

            language = await i18n.get_user_language(update.effective_user.id)

            async with api_client as client:
                user_token = await get_auth_token(screen, context, client)
                if not user_token:
                    await self._handle_auth_error(screen, language)
                    return

                info = await self._reorder_info(client, user_token)
                if info is None:
                    await self._reply_error(screen, i18n.get('telegram.error.generic', language))
                    return
                candidate = info.get('candidate')
                if not candidate:
                    await self._reply_error(screen, i18n.get('telegram.quick_order.no_history', language))
                    return

                await self._fill_and_open_checkout(
                    screen, context, client, user_token,
                    candidate['order_id'], self._address_id(candidate), language,
                )

        except Exception as e:
            await self._handle_error(screen, exc=e, operation="handle_repeat_last")

    async def handle_usual(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle the 'Your usual' button from the Products menu."""
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

                suggestion = await self._get_usual_suggestion(client, user_token)
                if not suggestion or not suggestion.get('product_id'):
                    await self._reply_error(
                        update,
                        i18n.get('telegram.quick_order.unavailable', language),
                    )
                    return

                items = [{
                    'product_id': suggestion['product_id'],
                    'quantity': suggestion['quantity'],
                }]
                ok = await self._replace_cart_with_items(client, user_token, items, update, language)
                if not ok:
                    return

                # "Usual" address = user's default address, falling back to the
                # most-recent order's address. Same skip-or-confirm semantics
                # as Repeat last (see checkout_handler).
                address_id = await self._resolve_usual_address_id(client, user_token)

            self._mark_quick_order_flow(context, address_id)
            await self._proceed_to_checkout(update, context)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="handle_usual")

    async def order_water(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """The bottom-menu "Order water" row: an open order first (R9), then the cart, then a refill."""
        await self._order_water(update, context, fresh_order=False)

    async def order_water_continue(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """The open-order notice's "Order again" button (`order_water_go`): a FRESH order, never a repeat.

        The cart if it holds something, else Products, where the customer picks product, quantity and
        (at checkout) address. No open-order check, no reorder-info read, no refill.
        """
        await self._order_water(update, context, fresh_order=True)

    async def _order_water(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, *, fresh_order: bool
    ) -> None:
        """Order water: notice -> cart -> Products -> refill-and-checkout (spec §3, R9).

        `fresh_order` is the notice's "Order again" tap (amended 2026-10-22): skip the notice and the
        refill, so the flow is only cart -> Products and the customer chooses everything.

        A cart that cannot be read stops the flow: nothing is ever filled over a cart we could
        not see. The refill replaces the cart, so only an empty one (`cart_is_empty`) is refilled.
        """
        screen = self.as_message_update(update)
        try:
            await self._ack(update.callback_query)
            user = await user_middleware(screen)
            if not user:
                return

            language = await i18n.get_user_language(update.effective_user.id)

            async with api_client as client:
                user_token = await get_auth_token(screen, context, client)
                if not user_token:
                    await self._handle_auth_error(screen, language)
                    return

                cart_response = await client.get_cart(user_token)
                if not cart_response.success:
                    logger.warning("Order water: cart read failed: %s", cart_response.error)
                    await self._reply_error(screen, i18n.get('telegram.error.generic', language))
                    return
                cart = ((cart_response.data or {}).get('data') or {}).get('cart') or {}

                info = {} if fresh_order else await self._reorder_info(client, user_token)
                if info is None:
                    await self._reply_error(screen, i18n.get('telegram.error.generic', language))
                    return

                open_order = info.get('open_order')
                if open_order:
                    await self._send_open_order_notice(screen, open_order, 'order_water_go', language)
                    return

                # Local imports: products.py imports this module at load time.
                from handlers import product_handlers
                from handlers.products import cart_is_empty

                if not cart_is_empty(cart):
                    await product_handlers.show_cart(screen, context)
                    return

                candidate = info.get('candidate')
                if not candidate:
                    await product_handlers.products_menu(screen, context)
                    return

                await self._fill_and_open_checkout(
                    screen, context, client, user_token,
                    candidate['order_id'], self._address_id(candidate), language,
                )

        except Exception as e:
            await self._handle_error(screen, exc=e, operation="order_water")

    async def handle_reorder_from_history(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle the 'Order again' / 'Reorder' button (reminder, order history).

        Callback format: `reorder_{order_id}`.
        """
        await self._reorder(update, context, check_open=True)

    async def reorder_continue(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """The open-order notice's continue button. Callback format: `reorder_go_{order_id}`."""
        await self._reorder(update, context, check_open=False)

    async def _reorder(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, *, check_open: bool
    ) -> None:
        """Order again: an explicit "this order again", so the cart is replaced, never consulted.

        `rsplit('_', 1)` reads the id from both `reorder_<id>` and `reorder_go_<id>`.
        """
        screen = self.as_message_update(update)
        try:
            await self._ack(update.callback_query)
            user = await user_middleware(screen)
            if not user:
                return

            language = await i18n.get_user_language(update.effective_user.id)

            try:
                order_id = int(update.callback_query.data.rsplit('_', 1)[1])
            except (IndexError, ValueError):
                await self._reply_error(screen, i18n.get('telegram.quick_order.unavailable', language))
                return

            async with api_client as client:
                user_token = await get_auth_token(screen, context, client)
                if not user_token:
                    await self._handle_auth_error(screen, language)
                    return

                info = await self._reorder_info(client, user_token)
                if info is None:
                    await self._reply_error(screen, i18n.get('telegram.error.generic', language))
                    return

                open_order = info.get('open_order')
                if check_open and open_order:
                    await self._send_open_order_notice(screen, open_order, f'reorder_go_{order_id}', language)
                    return

                # The saved address rides along only for the order the backend names as the
                # candidate; any other order gets the normal checkout.
                candidate = info.get('candidate')
                address_id = (
                    self._address_id(candidate)
                    if candidate and candidate.get('order_id') == order_id
                    else None
                )
                await self._fill_and_open_checkout(
                    screen, context, client, user_token, order_id, address_id, language,
                )

        except Exception as e:
            await self._handle_error(screen, exc=e, operation="reorder")

    @staticmethod
    async def _send_open_order_notice(
        screen, open_order: Dict, continue_callback: str, language: str
    ) -> None:
        """Say an order is already in progress, with View order and a way to carry on (R9).

        No parse_mode: order numbers contain underscores.
        """
        await screen.message.reply_text(
            i18n.get(
                'telegram.orders.open_order_notice', language,
                order_number=open_order['order_number'],
                status=order_status_label(open_order['status'], language),
            ),
            reply_markup=ReorderKeyboards.open_order_notice(language, open_order['id'], continue_callback),
        )

    async def _reorder_info(self, client, user_token: str) -> Optional[Dict]:
        """The backend's reorder-info payload; `{}` = no candidate and no open order; None = the call failed."""
        response = await client.get_reorder_info(user_token)
        if not response.success:
            logger.warning("Quick order: reorder-info failed: %s", response.error)
            return None
        return (response.data or {}).get('data') or {}

    @staticmethod
    def _address_id(candidate: Optional[Dict]) -> Optional[int]:
        """The candidate's saved address id; None when the backend says it is no longer saved."""
        return ((candidate or {}).get('address') or {}).get('id')

    async def _fill_and_open_checkout(
        self,
        screen,
        context: ContextTypes.DEFAULT_TYPE,
        client,
        user_token: str,
        order_id: int,
        address_id: Optional[int],
        language: str,
    ) -> None:
        """Fill the cart from the order through the backend, then open the right next screen."""
        response = await client.fill_cart_from_order(user_token, order_id)
        if not response.success:
            logger.warning("Quick order: fill-from-order %s failed: %s", order_id, response.error)
            await self._reply_error(screen, i18n.get('telegram.error.generic', language))
            return

        result = (response.data or {}).get('data') or {}
        added = result.get('added') or []
        dropped = result.get('dropped') or []

        # Local import: products.py imports this module at load time.
        from handlers import product_handlers

        if not added:
            await screen.message.reply_text(i18n.get('telegram.quick_order.unavailable', language))
            await product_handlers.products_menu(screen, context)
            return
        if dropped:
            await screen.message.reply_text(i18n.get('telegram.quick_order.unavailable', language))
            await product_handlers.show_cart(screen, context)
            return

        self._mark_quick_order_flow(context, address_id, source='reorder')
        await self._proceed_to_checkout(screen, context)

    @staticmethod
    def _mark_quick_order_flow(
        context: ContextTypes.DEFAULT_TYPE,
        address_id: Optional[int],
        *,
        source: str = 'quick_order',
    ) -> None:
        """Tag the checkout context so downstream handlers can branch.

        ``source='reorder'`` (Order water, Repeat last, Order again) goes
        straight to the payment picker on a saved address, whatever the address
        count. ``'quick_order'`` (Your usual) keeps the "Delivering to" card,
        whose Back button opens the products menu instead of the cart.
        """
        context.user_data['checkout_source'] = source
        if address_id is not None:
            context.user_data['quick_order_address_id'] = int(address_id)

    async def _resolve_usual_address_id(
        self,
        client,
        user_token: str,
    ) -> Optional[int]:
        """Pick the user's 'usual' delivery address.

        Heuristic: prefer the address flagged is_default=True; otherwise the
        most-recently-used (first in the list returned by the addresses
        endpoint, which already sorts by is_default DESC + recency). Returns
        None when the user has no addresses — checkout_handler will then
        show the 'add address' prompt as normal.
        """
        response = await client.get_user_addresses(user_token)
        if not response.success:
            return None
        addresses = (response.data.get('data', {}) or {}).get('addresses') or []
        if not addresses:
            return None
        default = next((a for a in addresses if a.get('is_default')), None)
        chosen = default or addresses[0]
        return chosen.get('id')

    # --- Internal helpers ---------------------------------------------------

    async def _get_last_order_summary(self, client, user_token: str) -> Optional[Dict]:
        """Return a Quick Order suggestion for the backend's reorder candidate.

        Returns None when there is no candidate (or the call failed).
        """
        info = await self._reorder_info(client, user_token)
        items = ((info or {}).get('candidate') or {}).get('items') or []
        if not items:
            return None

        # Single-item label: "Repeat last: 5× Product"
        # Multi-item label:  "Repeat last order (3 items)"
        if len(items) == 1:
            item = items[0]
            return {
                'kind': 'repeat_last',
                'callback_data': 'quick_repeat_last',
                'label_key': 'telegram.quick_order.repeat_last',
                'label_args': {
                    'qty': item.get('quantity', 1),
                    'product': item.get('product_name') or '',
                },
                'product_id': item.get('product_id'),
                'quantity': item.get('quantity'),
            }

        return {
            'kind': 'repeat_last',
            'callback_data': 'quick_repeat_last',
            'label_key': 'telegram.quick_order.repeat_last_multi',
            'label_args': {'n': len(items) or 1},
            'product_id': None,
            'quantity': None,
        }

    async def _get_usual_suggestion(self, client, user_token: str) -> Optional[Dict]:
        """Return the top 'your usual' suggestion from /orders/quick-reorder."""
        # The bot's FakeAPIClientContext used in unit tests may not expose this
        # method; handle the missing-method case along with regular API failure.
        get_suggestions = getattr(client, 'get_quick_reorder_suggestions', None)
        if get_suggestions is None:
            return None
        response = await get_suggestions(user_token, limit=1)
        if not response or not getattr(response, 'success', False):
            return None

        items = (response.data.get('data', {}) or {}).get('quick_reorder_suggestions') or []
        if not items:
            return None

        top = items[0]
        if not top.get('in_stock', True):
            return None

        return {
            'kind': 'usual',
            'callback_data': 'quick_usual',
            'label_key': 'telegram.quick_order.usual',
            'label_args': {
                'qty': top.get('suggested_quantity', 1),
                'product': top.get('product_name') or '',
            },
            'product_id': top.get('product_id'),
            'quantity': top.get('suggested_quantity'),
        }

    @staticmethod
    def _is_same_target(a: Optional[Dict], b: Optional[Dict]) -> bool:
        """Two suggestions are 'same' when both point at the same product and quantity."""
        if not a or not b:
            return False
        if a.get('product_id') is None or b.get('product_id') is None:
            return False
        return (a['product_id'] == b['product_id']) and (a.get('quantity') == b.get('quantity'))

    async def _replace_cart_with_items(
        self,
        client,
        user_token: str,
        items: List[Dict],
        update: Update,
        language: str,
    ) -> bool:
        """Clear cart and add the given items. Returns True on success.

        If any add fails, surfaces the API error and returns False. We replace
        rather than merge so the user sees exactly what they tapped on.
        """
        clear_resp = await client.clear_cart(user_token)
        if not clear_resp.success:
            await self._handle_api_error(update, clear_resp.error, language)
            return False

        added_any = False
        for item in items:
            resp = await client.add_to_cart(user_token, item['product_id'], item['quantity'])
            if not resp.success:
                if not added_any:
                    await self._handle_api_error(update, resp.error, language)
                    return False
                # Some items succeeded; log the failure and keep going so the
                # user sees a partial cart rather than nothing.
                logger.warning(
                    "Quick order: failed to add product_id=%s qty=%s: %s",
                    item['product_id'], item['quantity'], resp.error,
                )
                continue
            added_any = True

        return added_any

    async def _proceed_to_checkout(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Route into the existing checkout flow (address → payment → confirm)."""
        # Local import avoids circular imports at module load.
        from handlers.orders import order_handlers
        await order_handlers.checkout_handler(update, context)


quick_order_handlers = QuickOrderHandlers()
