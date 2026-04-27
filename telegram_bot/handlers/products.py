"""
Product browsing and shopping cart handlers
"""
import logging
import json
from typing import Dict, Any, List
from urllib.parse import urlparse, urlunparse
from io import BytesIO
import os
import httpx
from telegram import Update, constants
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown


from i18n import i18n
from keyboards import ProductKeyboards, MenuKeyboards, OrderKeyboards
from api_client import api_client
from database import db_manager, BotUserRepository
from utils import user_middleware, format_price, get_auth_token
from handlers.base import BaseHandler
from config import config

logger = logging.getLogger('handlers')


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
        """Show product categories"""
        try:
            logger.info("=== PRODUCTS MENU HANDLER CALLED ===")
            # Skip user middleware for now - authentication handled by API
            # user = await user_middleware(update)
            # if not user:
            #     return

            user_id = update.effective_user.id
            logger.info(f"Products menu requested by user {user_id}")
            language = await i18n.get_user_language(user_id)

            # Get user token for API calls (uses TokenManager for caching)
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                # Get product categories
                response = await client.get_product_categories(user_token, language=language)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return

                logger.info(f"API Response data type: {type(response.data)}")
                logger.info(f"API Response data: {response.data}")

                # Handle nested data structure
                if isinstance(response.data, dict) and 'data' in response.data:
                    categories = response.data['data'].get('categories', [])
                elif isinstance(response.data, dict):
                    categories = response.data.get('categories', [])
                else:
                    logger.error(f"Unexpected response.data structure: {response.data}")
                    categories = []

            logger.info(f"Retrieved {len(categories)} categories from API")
            logger.info(f"Categories data: {categories}")

            # Show categories
            menu_text = i18n.get('telegram.menu.products', language)
            keyboard = ProductKeyboards.product_categories(categories, language)

            logger.info(f"Menu text: {menu_text}")
            logger.info(f"Keyboard created with {len(keyboard.inline_keyboard)} rows")

            if update.callback_query:
                logger.info("Editing message via callback query...")
                try:
                    # Check if previous message had a photo (cannot edit text of photo message directly to text-only easily)
                    if update.callback_query.message.photo:
                         await update.callback_query.message.delete()
                         await update.callback_query.message.reply_text(
                            text=menu_text,
                            reply_markup=keyboard
                        )
                    else:
                        await update.callback_query.edit_message_text(
                            text=menu_text,
                            reply_markup=keyboard
                        )
                    logger.info("Message updated successfully")
                    await update.callback_query.answer()
                    logger.info("Callback query answered")
                except Exception as edit_error:
                    logger.error(f"Error editing message: {edit_error}")
                    # Fallback
                    try:
                        await update.callback_query.message.delete()
                        await update.callback_query.message.reply_text(
                            text=menu_text,
                            reply_markup=keyboard
                        )
                    except Exception as e:
                        logger.warning(f"Failed to send fallback product menu message: {e}")
            else:
                logger.info("Sending new message...")
                await update.message.reply_text(
                    text=menu_text,
                    reply_markup=keyboard
                )
                logger.info("New message sent")

            logger.info(f"Product categories shown to user {user_id}")

        except Exception as e:
            logger.error(f"Error in products menu: {e}")
            await self._handle_error(update)

    async def category_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle category selection and show products"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Extract category ID
            category_id = query.data.split('_')[1]
            page = int(context.user_data.get('current_page', 1))

            # Get user token
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                # Get products in category
                response = await client.get_products(
                    user_token,
                    category=category_id,
                    page=page,
                    per_page=6,
                    language=language
                )

                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return

                # Handle nested data structure
                if isinstance(response.data, dict) and 'data' in response.data:
                    products = response.data['data'].get('items', [])
                    total_pages = response.data.get('meta', {}).get('pages', 1)
                else:
                    # Fallback to old structure
                    products = response.data.get('products', [])
                    total_pages = response.data.get('total_pages', 1)

                # Fetch category details for image
                category_img_url = None
                try:
                    cat_response = await client.get_category(user_token, int(category_id), language=language)
                    if cat_response.success and cat_response.data and 'category' in cat_response.data.get('data', {}):
                        category_data = cat_response.data['data']['category']
                        # Try image_url first, then icon_url
                        category_img_url = category_data.get('image_url') or category_data.get('icon_url')
                except Exception as cat_error:
                    logger.warning(f"Failed to fetch category details: {cat_error}")

            # Store category for pagination
            context.user_data['current_category'] = category_id
            context.user_data['current_page'] = page

            if not products:
                # If we have an image, show it even if empty? Likely no, just show text message
                text = i18n.get('telegram.products.category_empty', language)
                if query.message.photo:
                    await query.message.delete()
                    await query.message.reply_text(text, reply_markup=MenuKeyboards.back_button(language))
                else:
                    await query.edit_message_text(
                        text=text,
                        reply_markup=MenuKeyboards.back_button(language)
                    )
                await query.answer()
                return

            # Format product list text
            products_text = self._format_products_list(products, language)

            # Create inline keyboard with product buttons
            keyboard = ProductKeyboards.product_list(products, page, total_pages, language)

            # Check if we should send a photo
            if category_img_url:
                try:
                    await query.message.delete()
                    await context.bot.send_photo(
                        chat_id=user_id,
                        photo=category_img_url,
                        caption=products_text,
                        reply_markup=keyboard,
                        parse_mode=constants.ParseMode.MARKDOWN_V2
                    )
                except Exception as img_error:
                    logger.error(f"Failed to send category image: {img_error}")
                    # Fallback to text
                    try:
                        # If message was deleted effectively, we sent nothing.
                        # We must send text message
                        await context.bot.send_message(
                            chat_id=user_id,
                            text=products_text,
                            reply_markup=keyboard,
                            parse_mode=constants.ParseMode.MARKDOWN_V2
                        )
                    except Exception as e:
                        logger.warning(f"Failed to send fallback category text message: {e}")
            else:
                # No image, use text
                if query.message.photo:
                    # Previous was photo, so delete and send text
                    await query.message.delete()
                    await query.message.reply_text(
                        text=products_text,
                        reply_markup=keyboard,
                        parse_mode=constants.ParseMode.MARKDOWN_V2
                    )
                else:
                    # Previous was text, edit it
                    await query.edit_message_text(
                        text=products_text,
                        reply_markup=keyboard,
                        parse_mode=constants.ParseMode.MARKDOWN_V2
                    )

            await query.answer()

            logger.info(f"Products in category {category_id} shown to user {user_id}")

        except Exception as e:
            logger.error(f"Error in category handler: {e}")
            await self._handle_error(update)

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
                    if downloaded_image:
                        try:
                            await query.message.delete()
                        except Exception:
                            pass
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
            logger.error(f"Error in product details: {e}")
            await self._handle_error(update)

    async def add_to_cart(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show quantity selector for adding to cart"""
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

                # Add product to cart via API
                add_response = await client.add_to_cart(
                    user_token,
                    product_id,
                    quantity=1
                )
                if not add_response.success:
                    await self._handle_api_error(update, add_response.error, language)
                    return

                # Get actual quantity from cart response
                current_qty = 1
                try:
                    cart_data = add_response.data.get('data', {}).get('cart', {})
                    for item in cart_data.get('cart_items', []):
                        if item.get('product_id') == product_id:
                            current_qty = item.get('quantity', 1)
                            break
                except Exception as e:
                    logger.error(f"Error parsing cart response: {e}")

            # Show quantity selector
            unit_price = self._get_effective_unit_price(product)
            quantity_text = (
                f"🛒 {product['name']}\n\n"
                f"{i18n.get('telegram.quantity', language)}: {current_qty}\n"
                f"{i18n.get('telegram.price', language)}: {format_price(unit_price * current_qty)} UZS"
            )
            keyboard = ProductKeyboards.quantity_selector(product_id, current_qty, language)

            await self._edit_or_replace_callback_message(
                query,
                quantity_text,
                reply_markup=keyboard,
            )
            await query.answer()

        except Exception as e:
            logger.error(f"Error in add to cart: {e}")
            await self._handle_error(update)

    async def quantity_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle quantity increase/decrease"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Parse callback data: qty_{inc/dec}_{product_id}_{current_qty}
            parts = query.data.split('_')
            action = parts[1]  # inc or dec
            product_id = int(parts[2])
            current_qty = int(parts[3])

            if action == 'inc':
                new_qty = min(current_qty + 1, 99)  # Max 99 items
            elif action == 'dec':
                new_qty = max(current_qty - 1, 1)   # Min 1 item
            else:
                await query.answer(i18n.get('telegram.products.invalid_action', language))
                return

            # Get product for price calculation
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                response = await client.get_product(user_token, product_id, language=language)
                if response.success:
                    product = response.data['data']['product']
                    total_price = self._get_effective_unit_price(product) * new_qty

                    # Update quantity display
                    quantity_text = f"🛒 {product['name']}\n\n{i18n.get('telegram.quantity', language)}: {new_qty}\n{i18n.get('telegram.total', language)}: {format_price(total_price)} UZS"
                    keyboard = ProductKeyboards.quantity_selector(product_id, new_qty, language)

                    # Update cart via API
                    update_response = await client.update_cart_item(
                        user_token,
                        product_id,
                        quantity=new_qty
                    )
                    if not update_response.success:
                        await self._handle_api_error(update, update_response.error, language)
                        return

                    await self._edit_or_replace_callback_message(
                        query,
                        quantity_text,
                        reply_markup=keyboard,
                    )

            await query.answer()

        except Exception as e:
            logger.error(f"Error in quantity handler: {e}")
            await self._handle_error(update)

    async def cart_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle cart actions"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            action = query.data.split('_')[1]  # cart_{action}

            if action == 'view':
                await self.show_cart(update, context)
            elif action == 'clear':
                await self._clear_cart(update, context)
            elif action == 'checkout':
                # Redirect to order handler
                from .orders import order_handlers
                await order_handlers.checkout_handler(update, context)

        except Exception as e:
            logger.error(f"Error in cart handler: {e}")
            await self._handle_error(update)

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
                f"   💰 {price_str} UZS \| 📦 {escape_markdown(str(product['specifications'].get('volume', 'N/A')), version=2)}{escape_markdown(product['specifications'].get('volume_unit', ''), version=2)}"
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

        if product.get('description'):
            details.append(f"📝 {escape_markdown(product['description'], version=2)}")

        if product.get('category'):
            details.append(
                f"📂 {i18n.get('telegram.products.category_label', language)}: "
                f"{escape_markdown(product['category'].get('name', 'N/A'), version=2)}"
            )

        return "\n\n".join(details)

    async def show_cart(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show shopping cart contents"""
        # This loads cart from database
        user_id = update.effective_user.id
        language = await i18n.get_user_language(user_id)

        # Minimum order amount (should match backend config)
        MIN_ORDER_AMOUNT = 20000  # TODO: Consider fetching from API config endpoint

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
            total_amount = 0
            for item in cart_items:
                product = item['product']
                quantity = item['quantity']
                price = product['current_price']
                line_total = price * quantity
                total_amount += line_total

                lines.append(
                    f"🛒 {product['name']} x {quantity} = {format_price(line_total)} UZS"
                )
            cart_is_empty = total_amount <= 0
            lines.append(f"\n💰 {i18n.get('telegram.cart_total', language)}: {format_price(total_amount)} UZS")

            cod_prepayment = cart.get('cod_prepayment') or {}
            available_balance = float(cod_prepayment.get('available_balance') or 0)
            potential_applied = float(cod_prepayment.get('potential_applied_amount') or 0)
            payable_after = float(cod_prepayment.get('estimated_payable_after_prepayment') or total_amount)
            if available_balance > 0:
                lines.append("")
                lines.append(f"💳 COD prepaid balance: {format_price(available_balance)} UZS")
                lines.append(f"🔁 Auto-applied on next COD order: {format_price(potential_applied)} UZS")
                lines.append(f"🧾 Estimated COD payable after prepaid: {format_price(payable_after)} UZS")

            # Add minimum order warning if needed
            if total_amount < MIN_ORDER_AMOUNT:
                meets_minimum = False
                remaining = MIN_ORDER_AMOUNT - total_amount
                lines.append("")
                lines.append("⚠️ " + i18n.get('telegram.cart_min_order_warning', language,
                    min_amount=format_price(MIN_ORDER_AMOUNT),
                    remaining=format_price(remaining)))
            else:
                lines.append("")
                lines.append("✅ " + i18n.get('telegram.cart_ready_checkout', language))

            cart_text = "\n".join(lines)

        keyboard = OrderKeyboards.cart_actions(language, cart_is_empty, meets_minimum)

        await self._edit_or_replace_callback_message(
            update.callback_query,
            cart_text,
            reply_markup=keyboard,
        )
        await update.callback_query.answer()

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
