"""
Quick Order handlers — one-tap re-ordering for habitual customers.

Two entry points lead here:
  * "🔁 Repeat last" / "⭐ Your usual" buttons rendered at the top of the
    Products menu by `ProductHandlers.products_menu`.
  * "🔁 Reorder" button rendered on delivered orders in order history.

The flow is intentionally non-destructive: items are placed in the cart and
the user is routed through the existing checkout flow (address +
payment + final confirmation). We never call `POST /orders/repeat/{id}`
directly — that endpoint creates a finalized order with no confirmation
step, which would bypass the user's "yes, place this order" tap.
"""
import logging
from typing import Dict, List, Optional, Tuple

from telegram import Update
from telegram.ext import ContextTypes

from i18n import i18n
from api_client import api_client
from utils import user_middleware, get_auth_token
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

                last_order = await self._fetch_last_order_with_items(client, user_token)
                if not last_order:
                    await self._reply_error(
                        update,
                        i18n.get('telegram.quick_order.no_history', language),
                    )
                    return

                items = self._extract_cart_items(last_order)
                if not items:
                    await self._reply_error(
                        update,
                        i18n.get('telegram.quick_order.unavailable', language),
                    )
                    return

                ok = await self._replace_cart_with_items(client, user_token, items, update, language)
                if not ok:
                    return

                # Address auto-selection: "Repeat last" means same address as
                # last order. Picked here so checkout_handler can skip the
                # picker / show only confirmation depending on address count.
                address_id = self._extract_address_id(last_order)

            self._mark_quick_order_flow(context, address_id)
            await self._proceed_to_checkout(update, context)

        except Exception as e:
            logger.exception(f"Error in handle_repeat_last: {e}")
            await self._handle_error(update)

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
            logger.exception(f"Error in handle_usual: {e}")
            await self._handle_error(update)

    async def handle_reorder_from_history(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle the 'Reorder' button from an order's history entry.

        Callback format: `reorder_{order_id}`.
        """
        try:
            user = await user_middleware(update)
            if not user:
                return

            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            query = update.callback_query
            try:
                order_id = int(query.data.split('_', 1)[1])
            except (IndexError, ValueError):
                await self._reply_error(
                    update,
                    i18n.get('telegram.quick_order.unavailable', language),
                )
                return

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                response = await client.get_order(user_token, order_id)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return

                order = response.data.get('data', {}).get('order') or {}
                items = self._extract_cart_items(order)
                if not items:
                    await self._reply_error(
                        update,
                        i18n.get('telegram.quick_order.unavailable', language),
                    )
                    return

                ok = await self._replace_cart_with_items(client, user_token, items, update, language)
                if not ok:
                    return

                address_id = self._extract_address_id(order)

            self._mark_quick_order_flow(context, address_id)
            await self._proceed_to_checkout(update, context)

        except Exception as e:
            logger.exception(f"Error in handle_reorder_from_history: {e}")
            await self._handle_error(update)

    @staticmethod
    def _mark_quick_order_flow(
        context: ContextTypes.DEFAULT_TYPE,
        address_id: Optional[int],
    ) -> None:
        """Tag the checkout context so downstream handlers can branch.

        ``checkout_source='quick_order'`` flips checkout_handler into the
        skip-address-when-possible mode and routes the address-confirmation
        Back button to the products menu instead of the cart.
        """
        context.user_data['checkout_source'] = 'quick_order'
        if address_id is not None:
            context.user_data['quick_order_address_id'] = int(address_id)

    @staticmethod
    def _extract_address_id(order: Dict) -> Optional[int]:
        """Pull the delivery_address_id out of an order payload (best-effort)."""
        if not order:
            return None
        address = order.get('delivery_address') or {}
        if isinstance(address, dict) and address.get('id'):
            return int(address['id'])
        # Some serializers may expose the flat field instead.
        flat = order.get('delivery_address_id')
        if flat:
            return int(flat)
        return None

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
        """Return a Quick Order suggestion for the user's most recent delivered order.

        Returns None when the user has no qualifying past orders. Only the order
        summary is needed (not full items) to render the button label.
        """
        response = await client.get_user_orders(user_token, status='delivered')
        if not response or not getattr(response, 'success', False):
            return None

        orders = response.data.get('data', {}).get('orders') or []
        if not orders:
            return None

        last = orders[0]  # API returns newest-first
        items = last.get('order_items') or last.get('items') or []

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

    @staticmethod
    def _extract_cart_items(order: Dict) -> List[Dict]:
        """Pull (product_id, quantity) tuples out of an order payload."""
        raw_items = order.get('order_items') or order.get('items') or []
        items: List[Dict] = []
        for raw in raw_items:
            product_id = raw.get('product_id')
            quantity = raw.get('quantity')
            if product_id and quantity:
                items.append({'product_id': int(product_id), 'quantity': int(quantity)})
        return items

    async def _fetch_last_order_with_items(self, client, user_token: str) -> Optional[Dict]:
        """Fetch the user's most recent delivered order, with full item details."""
        list_resp = await client.get_user_orders(user_token, status='delivered')
        if not list_resp.success:
            return None

        orders = list_resp.data.get('data', {}).get('orders') or []
        if not orders:
            return None

        order_id = orders[0].get('id')
        if not order_id:
            return None

        detail = await client.get_order(user_token, int(order_id))
        if not detail.success:
            return None

        return detail.data.get('data', {}).get('order') or {}

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
