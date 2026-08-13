"""
Product browsing and shopping cart handlers
"""
import logging
import json
from typing import Dict, Any, List, Optional
from urllib.parse import urlparse, urlunparse
from io import BytesIO
import os
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, constants
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown


from i18n import i18n
from keyboards import ProductKeyboards, MenuKeyboards, OrderKeyboards
from api_client import api_client
from database import db_manager, BotUserRepository
from utils import user_middleware, format_price, get_auth_token
from handlers.base import BaseHandler
from shared.business_config import MIN_ORDER_AMOUNT
from handlers.quick_order import quick_order_handlers
from config import config

logger = logging.getLogger('handlers')


def min_order_shortfall(subtotal: float) -> float:
    """How far a cart subtotal sits below the shared checkout floor.

    ONE expression decides BOTH halves of the minimum-order gate: whether the
    checkout button is unlocked, and what the "add N UZS more" line says. They
    used to be two (`subtotal < MIN_ORDER_AMOUNT` and `MIN_ORDER_AMOUNT -
    subtotal`) sitting next to each other, which is the same shape as every
    show-vs-settle defect in this codebase, only smaller.

    `subtotal` must be the SERVER's `cart['subtotal']` — never a client sum.
    Nothing is posted from the returned figure; it is gate + copy only.
    """
    return max(0.0, float(MIN_ORDER_AMOUNT) - float(subtotal))


class ProductHandlers(BaseHandler):
    """Product-related handlers"""

    @staticmethod
    def _get_effective_unit_price(product: Dict[str, Any]) -> float:
        """Resolve the best available display price with safe schema fallbacks."""
        pricing = product.get('pricing') or {}
        candidates = (
            pricing.get('current_price'),
            product.get('current_price'),
            pricing.get('base_price'),
            product.get('base_price'),
        )
        for candidate in candidates:
            if candidate is None:
                continue
            try:
                return float(candidate)
            except (TypeError, ValueError):
                continue
        return 0.0

    @staticmethod
    def _quantity_in_cart_payload(response: Any, product_id: int) -> Optional[int]:
        """Return product_id's quantity from an API cart payload, or None if absent.

        Works for either a GET /cart response or an add/update response — both
        wrap the cart at data -> data -> cart -> cart_items.
        """
        try:
            cart = (getattr(response, "data", None) or {}).get("data", {}).get("cart") or {}
            for item in cart.get("cart_items", []):
                if item.get("product_id") == product_id:
                    return int(item.get("quantity", 0))
        except Exception as e:
            logger.error(f"Error parsing cart response: {e}")
        return None

    @staticmethod
    def _extract_product_image_url(product: Dict[str, Any]) -> str | None:
        """Extract primary product image URL across API schema variants."""
        media = product.get('media') or {}
        candidates = [
            (media.get('images') or [None])[0],
            (media.get('image_urls') or [None])[0],
            (product.get('images') or [None])[0],
            product.get('image_url'),
            media.get('thumbnail_url'),
        ]
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return None

    @staticmethod
    def _is_private_image_url(url: str) -> bool:
        """Return True when Telegram likely cannot fetch this URL directly."""
        if not url:
            return True
        if url.startswith('/'):
            return True
        parsed = urlparse(url)
        host = (parsed.hostname or '').lower()
        return host in {'localhost', '127.0.0.1', '0.0.0.0', 'business_app'}

    def _build_internal_fetch_url(self, url: str) -> str | None:
        """
        Build a bot-reachable URL for downloading image bytes.
        This supports relative URLs and localhost URLs by remapping them to BUSINESS_APP_URL host.
        """
        if not url:
            return None

        base_url = (config.business_api.base_url or '').rstrip('/')
        if not base_url:
            return None

        if url.startswith('//'):
            return f"https:{url}"

        if url.startswith('/'):
            return f"{base_url}{url}"

        parsed = urlparse(url)
        if parsed.scheme in {'http', 'https'}:
            if self._is_private_image_url(url):
                base = urlparse(base_url)
                return urlunparse((
                    base.scheme,
                    base.netloc,
                    parsed.path,
                    parsed.params,
                    parsed.query,
                    parsed.fragment
                ))
            return url

        return f"{base_url}/{url.lstrip('/')}"

    async def _download_image_bytes(self, url: str) -> BytesIO | None:
        """Download image as bytes for Telegram upload."""
        if not url:
            return None

        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                response = await client.get(url)
                response.raise_for_status()

                content_type = response.headers.get('content-type', '')
                if content_type and not content_type.lower().startswith('image/'):
                    logger.warning(f"Downloaded non-image content-type for product image: {content_type}")
                    return None

                image_bytes = BytesIO(response.content)
                extension = os.path.splitext(urlparse(url).path)[1] or '.jpg'
                image_bytes.name = f"product{extension}"
                image_bytes.seek(0)
                return image_bytes
        except Exception as e:
            logger.warning(f"Failed to download product image from {url}: {e}")
            return None

    async def products_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Entry point for the Products menu.

        Behavior:
          * Fetches active categories + Quick Order suggestions.
          * 0 categories → existing empty state (handled by the helper).
          * 1 category → skip the category picker and render that category's
            products directly, with a single_category flag so "Back" goes to
            the main menu instead of an empty category list.
          * 2+ categories → render the category picker, prefixed by Quick
            Order suggestions when available.
        """
        try:
            logger.info("=== PRODUCTS MENU HANDLER CALLED ===")
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                response = await client.get_product_categories(user_token, language=language)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return

                if isinstance(response.data, dict) and 'data' in response.data:
                    categories = response.data['data'].get('categories', [])
                elif isinstance(response.data, dict):
                    categories = response.data.get('categories', [])
                else:
                    logger.error(f"Unexpected response.data structure: {response.data}")
                    categories = []

                # Build Quick Order suggestions inside the same client session.
                quick_suggestions = await quick_order_handlers.build_quick_suggestions(
                    update, context, client, user_token,
                )

            logger.info(
                "Products menu for user %s: %s categories, %s quick suggestions",
                user_id, len(categories), len(quick_suggestions),
            )

            # Single-category short-circuit
            if len(categories) == 1:
                context.user_data['single_category'] = True
                await self._render_products_in_category(
                    update, context,
                    category_id=str(categories[0]['id']),
                    single_category=True,
                    quick_suggestions=quick_suggestions,
                )
                return

            # 0 or 2+ categories → show the (possibly empty) category list with
            # Quick Order section on top.
            context.user_data['single_category'] = False
            menu_text = i18n.get('telegram.menu.products', language)
            keyboard = ProductKeyboards.product_categories(
                categories, language, quick_suggestions=quick_suggestions,
            )

            # Breadcrumb back to the cart edit screen when the user reached the
            # product menu via the order-confirmation 'Edit -> Add product' path
            # (Deliverable B). 'edit_order' re-enters edit mode so 'Done' still
            # routes back to the confirmation screen.
            if context.user_data.get('cart_edit_return'):
                rows = list(keyboard.inline_keyboard) + [
                    [
                        InlineKeyboardButton(
                            i18n.get('telegram.cart.back_to_cart', language),
                            callback_data='edit_order',
                        )
                    ]
                ]
                keyboard = InlineKeyboardMarkup(rows)

            if update.callback_query:
                try:
                    if update.callback_query.message.photo:
                        await update.callback_query.message.delete()
                        await update.callback_query.message.reply_text(
                            text=menu_text, reply_markup=keyboard,
                        )
                    else:
                        await update.callback_query.edit_message_text(
                            text=menu_text, reply_markup=keyboard,
                        )
                    await update.callback_query.answer()
                except Exception as edit_error:
                    logger.error(f"Error editing message: {edit_error}")
                    try:
                        await update.callback_query.message.delete()
                        await update.callback_query.message.reply_text(
                            text=menu_text, reply_markup=keyboard,
                        )
                    except Exception as e:
                        logger.warning(f"Failed to send fallback product menu message: {e}")
            else:
                await update.message.reply_text(text=menu_text, reply_markup=keyboard)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="products_menu")

    async def _render_products_in_category(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        category_id: str,
        single_category: bool = False,
        quick_suggestions: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Fetch and render the product list for a category.

        Shared by `category_handler` (user tapped a category) and `products_menu`
        (single-category short-circuit). `single_category` controls the "Back"
        target on the product list keyboard. `quick_suggestions` is only
        rendered in the single-category case (otherwise the suggestions live
        above the category picker).
        """
        user_id = update.effective_user.id
        language = await i18n.get_user_language(user_id)
        page = int(context.user_data.get('current_page', 1))

        async with api_client as client:
            user_token = await get_auth_token(update, context, client)
            if not user_token:
                await self._handle_auth_error(update, language)
                return

            response = await client.get_products(
                user_token, category=category_id, page=page, per_page=6, language=language,
            )
            if not response.success:
                await self._handle_api_error(update, response.error, language)
                return

            if isinstance(response.data, dict) and 'data' in response.data:
                products = response.data['data'].get('items', [])
                total_pages = response.data.get('meta', {}).get('pages', 1)
            else:
                products = response.data.get('products', [])
                total_pages = response.data.get('total_pages', 1)

            category_img_url = None
            try:
                cat_response = await client.get_category(user_token, int(category_id), language=language)
                if cat_response.success and cat_response.data and 'category' in cat_response.data.get('data', {}):
                    category_data = cat_response.data['data']['category']
                    category_img_url = category_data.get('image_url') or category_data.get('icon_url')
            except Exception as cat_error:
                logger.warning(f"Failed to fetch category details: {cat_error}")

        context.user_data['current_category'] = category_id
        context.user_data['current_page'] = page

        # Empty-category fallback
        if not products:
            text = i18n.get('telegram.products.category_empty', language)
            query = update.callback_query
            if query and query.message.photo:
                await query.message.delete()
                await query.message.reply_text(text, reply_markup=MenuKeyboards.back_button(language))
            elif query:
                await query.edit_message_text(text=text, reply_markup=MenuKeyboards.back_button(language))
            else:
                await update.message.reply_text(text=text, reply_markup=MenuKeyboards.back_button(language))
            if query:
                await query.answer()
            return

        products_text = self._format_products_list(products, language)
        keyboard = ProductKeyboards.product_list(
            products, page, total_pages, language,
            quick_suggestions=quick_suggestions if single_category else None,
            single_category=single_category,
        )

        query = update.callback_query
        if category_img_url and query:
            try:
                await query.message.delete()
                await context.bot.send_photo(
                    chat_id=user_id,
                    photo=category_img_url,
                    caption=products_text,
                    reply_markup=keyboard,
                    parse_mode=constants.ParseMode.MARKDOWN_V2,
                )
            except Exception as img_error:
                logger.error(f"Failed to send category image: {img_error}")
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=products_text,
                        reply_markup=keyboard,
                        parse_mode=constants.ParseMode.MARKDOWN_V2,
                    )
                except Exception as e:
                    logger.warning(f"Failed to send fallback category text message: {e}")
        elif query:
            if query.message.photo:
                await query.message.delete()
                await query.message.reply_text(
                    text=products_text,
                    reply_markup=keyboard,
                    parse_mode=constants.ParseMode.MARKDOWN_V2,
                )
            else:
                await query.edit_message_text(
                    text=products_text,
                    reply_markup=keyboard,
                    parse_mode=constants.ParseMode.MARKDOWN_V2,
                )
        else:
            await update.message.reply_text(
                text=products_text,
                reply_markup=keyboard,
                parse_mode=constants.ParseMode.MARKDOWN_V2,
            )

        if query:
            await query.answer()

    async def category_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle a category tap from the category picker."""
        try:
            query = update.callback_query
            category_id = query.data.split('_')[1]
            # Reaching this handler means the user tapped a category; we're not
            # in single-category short-circuit mode.
            context.user_data['single_category'] = False
            await self._render_products_in_category(
                update, context,
                category_id=category_id,
                single_category=False,
                quick_suggestions=None,
            )
        except Exception as e:
            await self._handle_error(update, exc=e, operation="category_handler")

    async def product_details(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show product details"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Extract product ID
            if 'back_to_product_' in query.data:
                product_id = int(query.data.split('_')[3])  # back_to_product_{id}
            else:
                product_id = int(query.data.split('_')[1])  # product_{id}

            # Get user token
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                # Get product details
                response = await client.get_product(user_token, product_id, language=language)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return

                product = response.data['data']['product']

            # Get category ID for back button
            category_id = product.get('category', {}).get('id')

            # Format product details
            details_text = self._format_product_details(product, language)
            keyboard = ProductKeyboards.product_details(product_id, category_id, language)

            # Get product image (supports media.images, media.image_urls, images, image_url)
            image_url = self._extract_product_image_url(product)

            if image_url:
                should_send_direct_url = not self._is_private_image_url(image_url)
                if should_send_direct_url:
                    try:
                        await query.message.delete()
                        await context.bot.send_photo(
                            chat_id=user_id,
                            photo=image_url,
                            caption=details_text,
                            reply_markup=keyboard,
                            parse_mode=constants.ParseMode.MARKDOWN_V2
                        )
                    except Exception as img_error:
                        logger.warning(f"Failed to send product image by URL ({image_url}): {img_error}")
                        should_send_direct_url = False

                if not should_send_direct_url:
                    # Download image via backend-accessible URL and upload bytes.
                    # This covers localhost/internal/relative URLs that Telegram cannot fetch directly.
                    fetch_url = self._build_internal_fetch_url(image_url)
                    downloaded_image = await self._download_image_bytes(fetch_url) if fetch_url else None

                    # Always replace the previous message to keep navigation tidy:
                    # whether we send a photo or fall back to a text message, delete
                    # the message that hosted the button the user just clicked first.
                    try:
                        await query.message.delete()
                    except Exception:
                        pass

                    if downloaded_image:
                        try:
                            await context.bot.send_photo(
                                chat_id=user_id,
                                photo=downloaded_image,
                                caption=details_text,
                                reply_markup=keyboard,
                                parse_mode=constants.ParseMode.MARKDOWN_V2
                            )
                        except Exception as upload_error:
                            logger.error(f"Failed to upload downloaded product image: {upload_error}")
                            try:
                                await context.bot.send_message(
                                    chat_id=user_id,
                                    text=details_text,
                                    reply_markup=keyboard,
                                    parse_mode=constants.ParseMode.MARKDOWN_V2
                                )
                            except Exception as e:
                                logger.warning(f"Failed to send fallback product detail message: {e}")
                    else:
                        try:
                            await context.bot.send_message(
                                chat_id=user_id,
                                text=details_text,
                                reply_markup=keyboard,
                                parse_mode=constants.ParseMode.MARKDOWN_V2
                            )
                        except Exception as e:
                            logger.warning(f"Failed to send fallback product detail message: {e}")
            else:
                if query.message.photo:
                    await query.message.delete()
                    await query.message.reply_text(
                        text=details_text,
                        reply_markup=keyboard,
                        parse_mode=constants.ParseMode.MARKDOWN_V2
                    )
                else:
                    await query.edit_message_text(
                        text=details_text,
                        reply_markup=keyboard,
                        parse_mode=constants.ParseMode.MARKDOWN_V2
                    )

            await query.answer()

            logger.info(f"Product {product_id} details shown to user {user_id}")

        except Exception as e:
            await self._handle_error(update, exc=e, operation="product_details")

    def _format_quantity_step_text(
        self, product: Dict[str, Any], quantity: int, language: str,
    ) -> str:
        """Build the body text shown above the quantity selector.

        Used as either an inline message body or as a photo caption depending on
        whether we have a product image available.
        """
        unit_price = self._get_effective_unit_price(product)
        min_order_qty = int((product.get('inventory') or {}).get('min_order_quantity', 1) or 1)
        text = (
            f"🛒 {product['name']}\n\n"
            f"{i18n.get('telegram.quantity', language)}: {quantity}\n"
            f"{i18n.get('telegram.total', language)}: {format_price(unit_price * quantity)} UZS"
        )
        if min_order_qty > 1:
            text += (
                f"\nℹ️ {i18n.get('telegram.products.min_order_quantity_label', language, min_qty=min_order_qty)}"
            )
        return text

    async def _render_quantity_step(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        product_id: int,
        product: Dict[str, Any],
        quantity: int,
        language: str,
    ) -> None:
        """Render the quantity selector with the product image shown.

        Strategy:
          * If the current callback bubble is already a photo, edit its caption
            and keyboard in place (cheap, no re-upload).
          * If it's a text bubble and a product image is available, delete and
            re-send as a photo so the user sees what they're ordering.
          * Otherwise fall back to editing text in place.

        The product image is fetched via the same helpers used by
        `product_details` so private/internal URLs are downloaded server-side
        before being uploaded to Telegram. ``product_id`` is passed explicitly
        rather than read from the product dict so callers (which parse it from
        the callback) can stay the single source of truth.
        """
        inventory = product.get('inventory') or {}
        min_order_qty = int(inventory.get('min_order_quantity', 1) or 1)
        stock_quantity = inventory.get('stock_quantity')

        text = self._format_quantity_step_text(product, quantity, language)
        keyboard = ProductKeyboards.quantity_selector(
            product_id, quantity, language,
            min_order_qty=min_order_qty,
            stock_quantity=stock_quantity,
        )

        query = update.callback_query
        if not query:
            # Defensive: shouldn't happen from real callback flow.
            await update.message.reply_text(text=text, reply_markup=keyboard)
            return

        message = query.message
        is_photo_message = bool(getattr(message, 'photo', None))

        # Fast path: already a photo bubble — edit caption only.
        if is_photo_message:
            try:
                await query.edit_message_caption(caption=text, reply_markup=keyboard)
                return
            except Exception as e:
                logger.warning(f"edit_message_caption failed, falling back to resend: {e}")

        image_url = self._extract_product_image_url(product)
        user_id = update.effective_user.id

        # Helper to send the photo using the same private/public logic as product_details.
        async def _send_with_photo(photo: Any) -> bool:
            try:
                await context.bot.send_photo(
                    chat_id=user_id,
                    photo=photo,
                    caption=text,
                    reply_markup=keyboard,
                )
                return True
            except Exception as send_err:
                logger.warning(f"send_photo failed in quantity step: {send_err}")
                return False

        if image_url:
            try:
                await message.delete()
            except Exception:
                pass

            sent = False
            if not self._is_private_image_url(image_url):
                sent = await _send_with_photo(image_url)
            if not sent:
                fetch_url = self._build_internal_fetch_url(image_url)
                downloaded = await self._download_image_bytes(fetch_url) if fetch_url else None
                if downloaded:
                    sent = await _send_with_photo(downloaded)
            if sent:
                return
            # Fall through to plain text as last resort.
            try:
                await context.bot.send_message(chat_id=user_id, text=text, reply_markup=keyboard)
            except Exception as e:
                logger.warning(f"Quantity step text fallback failed: {e}")
            return

        # No image available — edit text in place (or replace as needed).
        await self._edit_or_replace_callback_message(query, text, reply_markup=keyboard)

    async def add_to_cart(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show quantity selector (with product image) for adding to cart."""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Extract product ID
            product_id = int(query.data.split('_')[3])  # add_to_cart_{product_id}

            # Get product details for quantity selector
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                response = await client.get_product(user_token, product_id, language=language)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return

                product = response.data['data']['product']

                # Default add-to-cart quantity to the product's minimum so users
                # don't immediately fall foul of the per-product purchase rule.
                min_order_qty = int((product.get('inventory') or {}).get('min_order_quantity', 1) or 1)

                # Idempotent entry point. POST /cart/items is an INCREMENT on the
                # backend (cart_item.quantity += quantity), so tapping "Add to
                # cart" repeatedly (e.g. add -> back to product -> add again)
                # used to silently pile on min_order_qty each time and inflate the
                # order total. Only add when the product isn't in the cart yet;
                # otherwise just re-open the selector at the existing quantity and
                # let the +/- and preset buttons (which SET) adjust from there.
                cart_response = await client.get_cart(user_token)
                existing_qty = (
                    self._quantity_in_cart_payload(cart_response, product_id)
                    if (cart_response and cart_response.success)
                    else None
                )

                if existing_qty:
                    current_qty = existing_qty
                else:
                    add_response = await client.add_to_cart(
                        user_token,
                        product_id,
                        quantity=min_order_qty,
                    )
                    if not add_response.success:
                        await self._handle_api_error(update, add_response.error, language)
                        return
                    current_qty = (
                        self._quantity_in_cart_payload(add_response, product_id)
                        or min_order_qty
                    )

            await self._render_quantity_step(update, context, product_id, product, current_qty, language)
            await query.answer()

        except Exception as e:
            await self._handle_error(update, exc=e, operation="add_to_cart")

    async def quantity_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle quantity adjustments: +1/-1 fine-tune and preset jumps."""
        try:
            query = update.callback_query
            # The quantity display button ('qty_current') also routes here via
            # the broad '^qty_' pattern and is a deliberate no-op.
            if query.data == 'qty_current':
                await query.answer()
                return

            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Parse callback data:
            #   qty_inc_{product_id}_{current_qty}
            #   qty_dec_{product_id}_{current_qty}
            #   qty_set_{product_id}_{target_qty}
            # Validate shape up-front so a malformed callback short-circuits
            # before we hit the API.
            parts = query.data.split('_')
            if len(parts) != 4 or parts[1] not in ('inc', 'dec', 'set'):
                await query.answer(i18n.get('telegram.products.invalid_action', language))
                return
            action = parts[1]
            try:
                product_id = int(parts[2])
                payload_qty = int(parts[3])
            except ValueError:
                await query.answer(i18n.get('telegram.products.invalid_action', language))
                return

            # Get product for price calculation and the per-product purchase minimum.
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                response = await client.get_product(user_token, product_id, language=language)
                if response.success:
                    product = response.data['data']['product']
                    inventory = product.get('inventory') or {}
                    min_order_qty = int(inventory.get('min_order_quantity', 1) or 1)
                    stock_quantity = inventory.get('stock_quantity')
                    upper = ProductKeyboards.MAX_QUANTITY
                    if isinstance(stock_quantity, int) and stock_quantity > 0:
                        upper = min(upper, stock_quantity)

                    if action == 'inc':
                        new_qty = min(payload_qty + 1, upper)
                    elif action == 'dec':
                        new_qty = max(payload_qty - 1, min_order_qty)
                    else:  # 'set' — preset jump; clamp to [min, upper]
                        new_qty = max(min_order_qty, min(payload_qty, upper))

                    # Update cart via API
                    update_response = await client.update_cart_item(
                        user_token,
                        product_id,
                        quantity=new_qty,
                    )
                    if not update_response.success:
                        await self._handle_api_error(update, update_response.error, language)
                        return

                    await self._render_quantity_step(update, context, product_id, product, new_qty, language)

            await query.answer()

        except Exception as e:
            await self._handle_error(update, exc=e, operation="quantity_handler")

    async def cart_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle cart actions"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            action = query.data.split('_')[1]  # cart_{action}

            if action == 'view':
                # Edit-mode-aware: if the user is in the cart editing flow, stay
                # in edit mode so a stray tap on a display cell doesn't eject them.
                edit_mode = bool(context.user_data.get('cart_edit_return'))
                await self.show_cart(update, context, edit_mode=edit_mode)
            elif action == 'clear':
                await self._clear_cart(update, context)
            elif action in ('inc', 'dec', 'rm'):
                # Edit-mode per-item controls (Deliverable B):
                #   cart_inc_{product_id} / cart_dec_{product_id} / cart_rm_{product_id}
                await self._handle_cart_item_action(update, context, action, language)
            elif action == 'checkout':
                # Cart-driven checkout — clear any lingering Quick Order
                # flags so the cart flow starts from a clean state and
                # doesn't accidentally inherit a stale quick_order_address_id.
                context.user_data.pop('checkout_source', None)
                context.user_data.pop('quick_order_address_id', None)
                from .orders import order_handlers
                await order_handlers.checkout_handler(update, context)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="cart_handler")

    async def _handle_cart_item_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                                       action: str, language: str):
        """Mutate a single cart line then re-render the edit-mode cart.

        action is one of 'inc' | 'dec' | 'rm'. Callback shape is
        cart_{action}_{product_id}. The +/- clamp mirrors quantity_handler:
        new_qty is bounded to [min_order_qty, min(MAX_QUANTITY, stock)].
        """
        query = update.callback_query
        parts = query.data.split('_')  # ['cart', action, product_id]
        try:
            product_id = int(parts[2])
        except (IndexError, ValueError):
            await query.answer(i18n.get('telegram.products.invalid_action', language))
            return

        async with api_client as client:
            user_token = await get_auth_token(update, context, client)
            if not user_token:
                await self._handle_auth_error(update, language)
                return

            if action == 'rm':
                remove_response = await client.remove_cart_item(user_token, product_id)
                if not remove_response.success:
                    await self._handle_api_error(update, remove_response.error, language)
                    return
            else:
                # Read the current cart quantity for this product, and the
                # product's purchase bounds, to clamp exactly like quantity_handler.
                cart_response = await client.get_cart(user_token)
                current_qty = (
                    self._quantity_in_cart_payload(cart_response, product_id)
                    if (cart_response and cart_response.success)
                    else None
                ) or 0

                product_response = await client.get_product(user_token, product_id, language=language)
                if not product_response.success:
                    await self._handle_api_error(update, product_response.error, language)
                    return
                product = product_response.data['data']['product']
                inventory = product.get('inventory') or {}
                min_order_qty = int(inventory.get('min_order_quantity', 1) or 1)
                stock_quantity = inventory.get('stock_quantity')
                upper = ProductKeyboards.MAX_QUANTITY
                if isinstance(stock_quantity, int) and stock_quantity > 0:
                    upper = min(upper, stock_quantity)

                if action == 'inc':
                    new_qty = min(current_qty + 1, upper)
                else:  # 'dec'
                    new_qty = max(current_qty - 1, min_order_qty)

                update_response = await client.update_cart_item(
                    user_token,
                    product_id,
                    quantity=new_qty,
                )
                if not update_response.success:
                    await self._handle_api_error(update, update_response.error, language)
                    return

        # Re-render in edit mode so controls + warnings refresh in place.
        await self.show_cart(update, context, edit_mode=True)
        await query.answer()

    async def search_products(self, update: Update, context: ContextTypes.DEFAULT_TYPE, search_term: str):
        """Handle product search"""
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Get user token
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                # Search products
                response = await client.get_products(
                    user_token,
                    search=search_term,
                    page=1,
                    language=language
                )

                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return

                products_data = response.data
                products = products_data.get('products', [])

            if not products:
                await update.message.reply_text(
                    i18n.get('telegram.products.no_results_for_search', language, search_term=search_term)
                )
                return

            # Show search results
            search_title = i18n.get(
                'telegram.products.search_results_for',
                language,
                search_term=search_term
            )
            search_text = f"{search_title}\n\n{self._format_products_list(products, language)}"
            keyboard = ProductKeyboards.product_list(products, 1, 1, language)

            await update.message.reply_text(
                text=search_text,
                reply_markup=keyboard
            )

            # Clear search state
            await self.user_repo.update_user_state(user_id, {})

        except Exception as e:
            logger.error(f"Error in product search: {e}")
            language = await i18n.get_user_language(update.effective_user.id)
            await update.message.reply_text(i18n.get('telegram.error.product_error', language))

    def _format_products_list(self, products: List[Dict], language: str) -> str:
        """Format products list for display"""
        if not products:
            return i18n.get('telegram.products.no_products_found', language)

        formatted_lines = []
        for product in products:
            price_str = escape_markdown(format_price(self._get_effective_unit_price(product)), version=2)
            stock_indicator = "✅" if product['inventory'].get('stock_quantity', 0) > 0 else "❌"

            formatted_lines.append(
                f"{stock_indicator} *{escape_markdown(product['name'], version=2)}*\n"
                f"   💰 {price_str} UZS \\| 📦 {escape_markdown(str(product['specifications'].get('volume', 'N/A')), version=2)}{escape_markdown(product['specifications'].get('volume_unit', ''), version=2)}"
            )

        return "\n\n".join(formatted_lines)

    def _format_product_details(self, product: Dict, language: str) -> str:
        """Format single product details"""
        price_str = escape_markdown(format_price(self._get_effective_unit_price(product)), version=2)
        stock = product['inventory'].get('stock_quantity', 0)
        stock_status = i18n.get('telegram.products.in_stock', language) if stock > 0 else i18n.get('telegram.products.out_of_stock', language)

        details = [
            f"🏷️ *{escape_markdown(product['name'], version=2)}*",
            f"💰 {i18n.get('telegram.price', language)}: {price_str} UZS",
            f"📦 {i18n.get('telegram.products.volume_label', language)}: {escape_markdown(str(product['specifications'].get('volume', 'N/A')), version=2)}{escape_markdown(product['specifications'].get('volume_unit', ''), version=2)}",
            f"📊 {i18n.get('telegram.products.stock_label', language)}: {stock_status}",
        ]

        min_order_qty = int(product['inventory'].get('min_order_quantity', 1) or 1)
        if min_order_qty > 1:
            details.append(
                f"📐 {i18n.get('telegram.products.min_order_quantity_label', language, min_qty=min_order_qty)}"
            )

        if product.get('description'):
            details.append(f"📝 {escape_markdown(product['description'], version=2)}")

        if product.get('category'):
            details.append(
                f"📂 {i18n.get('telegram.products.category_label', language)}: "
                f"{escape_markdown(product['category'].get('name', 'N/A'), version=2)}"
            )

        return "\n\n".join(details)

    async def show_cart(self, update: Update, context: ContextTypes.DEFAULT_TYPE, edit_mode: bool = False):
        """Show shopping cart contents.

        edit_mode=True (Deliverable B) renders per-item +/- and remove controls
        plus 'Add product' and 'Done' rows instead of the normal cart actions.
        """
        # This loads cart from database
        user_id = update.effective_user.id
        language = await i18n.get_user_language(user_id)


        async with api_client as client:
            user_token = await get_auth_token(update, context, client)
            if not user_token:
                await self._handle_auth_error(update, language)
                return

            response = await client.get_cart(user_token)
            if not response.success:
                await self._handle_api_error(update, response.error, language)
                return

            cart_data = response.data
            cart = cart_data.get('data', {}).get('cart') or {}
            cart_items = cart.get('cart_items', [])

        cart_is_empty = None
        meets_minimum = True

        if not cart_items:
            cart_text = i18n.get('telegram.cart_empty', language)
            cart_is_empty = True
        else:
            lines = [i18n.get('telegram.cart_title', language) + ":\n"]
            min_qty_violations = []

            # 🔴 THE FIGURE SHOWN AND THE FIGURE CHARGED ARE ONE DECISION, AND
            # THAT DECISION IS THE SERVER'S. Do not reintroduce arithmetic here.
            #
            # This screen used to re-derive the money:
            #     price       = product['current_price']
            #     line_total  = price * quantity
            #     total_amount += line_total
            # `current_price` is baked by `CartItem.to_dict()` through
            # `Product.calculate_price`, which IGNORES its `user` argument
            # (business_app/models/product.py:140-143) — contract-blind and
            # price-rule-blind. `CartService.get_cart_details` patches it from
            # `get_cart_summary` afterwards, but ONLY for the lines that summary
            # kept: `get_cart_summary` SKIPS inactive products entirely
            # (cart_service.py:635-637), so a dropped line keeps its raw
            # `current_price` and this screen kept adding it into a total the
            # server's `subtotal` excludes and the order will never contain.
            # That same total also drove the MIN_ORDER_AMOUNT gate below, so it
            # could unlock or block checkout against a figure the server
            # disagrees with.
            #
            # `cart_items[].total_price` and `cart['subtotal']` are both composed
            # by `CartService.get_cart_summary` — one calculation, the same one
            # `_show_order_confirmation` reads one screen later and the same one
            # order creation is built from. Read them; never re-multiply. A
            # server-dropped line has no `total_price`, so it renders 0 — which
            # is exactly what it contributes to the order.
            # tests/integration/test_cart_screen_total_is_server_authoritative.py
            subtotal = float(cart.get('subtotal') or 0)

            for item in cart_items:
                product = item['product']
                quantity = item['quantity']
                line_total = float(item.get('total_price') or 0)

                lines.append(
                    f"🛒 {product['name']} x {quantity} = {format_price(line_total)} UZS"
                )

                # Per-product purchase minimum (mirrors backend rule).
                inventory = product.get('inventory') or {}
                min_qty = int(inventory.get('min_order_quantity', 1) or 1)
                if quantity < min_qty:
                    min_qty_violations.append({
                        'name': product['name'],
                        'min_qty': min_qty,
                        'remaining': min_qty - quantity,
                    })
            cart_is_empty = subtotal <= 0
            lines.append(f"\n💰 {i18n.get('telegram.cart_total', language)}: {format_price(subtotal)} UZS")

            cod_prepayment = cart.get('cod_prepayment') or {}
            available_balance = float(cod_prepayment.get('available_balance') or 0)
            potential_applied = float(cod_prepayment.get('potential_applied_amount') or 0)
            payable_after = float(cod_prepayment.get('estimated_payable_after_prepayment') or subtotal)
            if available_balance > 0:
                lines.append("")
                lines.append(i18n.get(
                    'telegram.cart.cod_prepaid_balance',
                    language,
                    available_balance=format_price(available_balance),
                ))
                lines.append(i18n.get(
                    'telegram.cart.cod_prepaid_auto_applied_next',
                    language,
                    potential_applied=format_price(potential_applied),
                ))
                lines.append(i18n.get(
                    'telegram.cart.cod_estimated_payable',
                    language,
                    payable_after=format_price(payable_after),
                ))

            # Minimum-order gate. `min_order_shortfall` is the ONE expression
            # behind both the gate and the "add N more" copy, and it is fed the
            # server's subtotal — so checkout can no longer be unlocked (or
            # blocked) against a total the order will not have.
            shortfall = min_order_shortfall(subtotal)
            if shortfall > 0:
                meets_minimum = False
                lines.append("")
                lines.append("⚠️ " + i18n.get('telegram.cart_min_order_warning', language,
                    min_amount=format_price(MIN_ORDER_AMOUNT),
                    remaining=format_price(shortfall)))

            # Per-product minimum order quantity warnings.
            if min_qty_violations:
                meets_minimum = False
                lines.append("")
                for v in min_qty_violations:
                    lines.append("⚠️ " + i18n.get(
                        'telegram.cart_min_qty_warning', language,
                        product_name=v['name'],
                        min_qty=v['min_qty'],
                        remaining=v['remaining'],
                    ))

            if meets_minimum:
                lines.append("")
                lines.append("✅ " + i18n.get('telegram.cart_ready_checkout', language))

            cart_text = "\n".join(lines)

        keyboard = OrderKeyboards.cart_actions(
            language,
            cart_is_empty,
            meets_minimum,
            edit_mode=edit_mode,
            cart_items=cart_items,
            edit_return=context.user_data.get('cart_edit_return'),
        )

        if update.callback_query:
            await self._edit_or_replace_callback_message(
                update.callback_query,
                cart_text,
                reply_markup=keyboard,
            )
            await update.callback_query.answer()
        else:
            # Message-only caller (e.g. cancel_address_text from zero-address
            # checkout): there is no callback message to edit, so send the
            # cart as a fresh reply. `_edit_or_replace_callback_message`
            # requires a `query` and re-raises when one isn't there — never
            # call it from here.
            await update.message.reply_text(cart_text, reply_markup=keyboard)

    async def _clear_cart(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Clear shopping cart"""
        user_id = update.effective_user.id
        language = await i18n.get_user_language(user_id)

        # Clear cart
        async with api_client as client:
            user_token = await get_auth_token(update, context, client)
            if not user_token:
                await self._handle_auth_error(update, language)
                return

            response = await client.clear_cart(user_token)
            if not response.success:
                await self._handle_api_error(update, response.error, language)
                return

        await update.callback_query.answer(i18n.get('telegram.products.cart_cleared', language))
        await self.show_cart(update, context)



# Global handler instance
product_handlers = ProductHandlers()
