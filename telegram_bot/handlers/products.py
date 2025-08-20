"""
Product browsing and shopping cart handlers
"""
import logging
import json
from typing import Dict, Any, List
from telegram import Update
from telegram.ext import ContextTypes

from i18n import i18n
from keyboards import ProductKeyboards, MenuKeyboards
from api_client import api_client
from database import db_manager, BotUserRepository
from utils import user_middleware, format_price, authenticate_telegram_user

logger = logging.getLogger('handlers')


class ProductHandlers:
    """Product-related handlers"""
    
    def __init__(self):
        self.user_repo = BotUserRepository(db_manager)
    
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
            
            # Get user token for API calls
            async with api_client as client:
                user_token = await authenticate_telegram_user(update, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return
                
                # Get product categories
                response = await client.get_product_categories(user_token)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return
                
                categories = response.data.get('categories', [])
            
            # Show categories
            menu_text = i18n.get('products_title', language)
            keyboard = ProductKeyboards.product_categories(categories, language)
            
            if update.callback_query:
                await update.callback_query.edit_message_text(
                    text=menu_text,
                    reply_markup=keyboard
                )
                await update.callback_query.answer()
            else:
                await update.message.reply_text(
                    text=menu_text,
                    reply_markup=keyboard
                )
            
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
                user_token = await authenticate_telegram_user(update, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return
                
                # Get products in category
                response = await client.get_products(
                    user_token,
                    category=category_id,
                    page=page
                )
                
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return
                
                products_data = response.data
                products = products_data.get('products', [])
                total_pages = products_data.get('total_pages', 1)
            
            # Store category for pagination
            context.user_data['current_category'] = category_id
            context.user_data['current_page'] = page
            
            if not products:
                await query.edit_message_text(
                    text="No products found in this category.",
                    reply_markup=MenuKeyboards.back_button(language)
                )
                await query.answer()
                return
            
            # Format products list
            products_text = self._format_products_list(products, language)
            keyboard = ProductKeyboards.product_list(products, page, total_pages, language)
            
            await query.edit_message_text(
                text=products_text,
                reply_markup=keyboard
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
            product_id = int(query.data.split('_')[1])
            
            # Get user token
            async with api_client as client:
                user_token = await authenticate_telegram_user(update, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return
                
                # Get product details
                response = await client.get_product(user_token, product_id)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return
                
                product = response.data['product']
            
            # Format product details
            details_text = self._format_product_details(product, language)
            keyboard = ProductKeyboards.product_details(product_id, language)
            
            await query.edit_message_text(
                text=details_text,
                reply_markup=keyboard
            )
            await query.answer()
            
            # Send product image if available
            if product.get('images') and len(product['images']) > 0:
                try:
                    image_url = product['images'][0]
                    await context.bot.send_photo(
                        chat_id=user_id,
                        photo=image_url,
                        caption=f"📸 {product['name']}"
                    )
                except Exception as img_error:
                    logger.warning(f"Could not send product image: {img_error}")
            
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
                user_token = await authenticate_telegram_user(update, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return
                
                response = await client.get_product(user_token, product_id)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return
                
                product = response.data['product']
            
            # Show quantity selector
            quantity_text = f"🛒 {product['name']}\n\n{i18n.get('quantity', language)} 1\n{i18n.get('price', language)} {format_price(product['base_price'])} UZS"
            keyboard = ProductKeyboards.quantity_selector(product_id, 1, language)
            
            await query.edit_message_text(
                text=quantity_text,
                reply_markup=keyboard
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
                await query.answer("❌ Invalid action")
                return
            
            # Get product for price calculation
            async with api_client as client:
                user_token = await authenticate_telegram_user(update, client)
                response = await client.get_product(user_token, product_id)
                if response.success:
                    product = response.data['product']
                    total_price = product['base_price'] * new_qty
                    
                    # Update quantity display
                    quantity_text = f"🛒 {product['name']}\n\n{i18n.get('quantity', language)} {new_qty}\n{i18n.get('total', language)} {format_price(total_price)} UZS"
                    keyboard = ProductKeyboards.quantity_selector(product_id, new_qty, language)
                    
                    await query.edit_message_text(
                        text=quantity_text,
                        reply_markup=keyboard
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
                await self._show_cart(update, context)
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
                user_token = await authenticate_telegram_user(update, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return
                
                # Search products
                response = await client.get_products(
                    user_token,
                    search=search_term,
                    page=1
                )
                
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return
                
                products_data = response.data
                products = products_data.get('products', [])
            
            if not products:
                await update.message.reply_text(
                    f"🔍 No products found for '{search_term}'"
                )
                return
            
            # Show search results
            search_text = f"🔍 Search results for '{search_term}':\n\n{self._format_products_list(products, language)}"
            keyboard = ProductKeyboards.product_list(products, 1, 1, language)
            
            await update.message.reply_text(
                text=search_text,
                reply_markup=keyboard
            )
            
            # Clear search state
            await self.user_repo.update_user_state(user_id, {})
            
        except Exception as e:
            logger.error(f"Error in product search: {e}")
            await update.message.reply_text("❌ Search failed. Please try again.")
    
    def _format_products_list(self, products: List[Dict], language: str) -> str:
        """Format products list for display"""
        if not products:
            return "No products available."
        
        formatted_lines = []
        for product in products:
            price_str = format_price(product['base_price'])
            stock_indicator = "✅" if product.get('stock_quantity', 0) > 0 else "❌"
            
            formatted_lines.append(
                f"{stock_indicator} **{product['name']}**\n"
                f"   💰 {price_str} UZS | 📦 {product.get('volume', 'N/A')}{product.get('volume_unit', '')}"
            )
        
        return "\n\n".join(formatted_lines)
    
    def _format_product_details(self, product: Dict, language: str) -> str:
        """Format single product details"""
        price_str = format_price(product['base_price'])
        stock = product.get('stock_quantity', 0)
        stock_status = "✅ In Stock" if stock > 0 else "❌ Out of Stock"
        
        details = [
            f"🏷️ **{product['name']}**",
            f"💰 {i18n.get('price', language)}: {price_str} UZS",
            f"📦 Volume: {product.get('volume', 'N/A')}{product.get('volume_unit', '')}",
            f"📊 Stock: {stock_status}",
        ]
        
        if product.get('description'):
            details.append(f"📝 {product['description']}")
        
        if product.get('category'):
            details.append(f"📂 Category: {product['category']}")
        
        return "\n\n".join(details)
    
    async def _show_cart(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show shopping cart contents"""
        # This would typically load cart from Redis or database
        # For now, show empty cart message
        user_id = update.effective_user.id
        language = await i18n.get_user_language(user_id)
        
        cart_text = i18n.get('cart_empty', language)
        keyboard = MenuKeyboards.back_button(language)
        
        await update.callback_query.edit_message_text(
            text=cart_text,
            reply_markup=keyboard
        )
        await update.callback_query.answer()
    
    async def _clear_cart(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Clear shopping cart"""
        user_id = update.effective_user.id
        language = await i18n.get_user_language(user_id)
        
        # Clear cart logic here
        await update.callback_query.answer("🗑️ Cart cleared!")
        await self._show_cart(update, context)
    
    async def _handle_auth_error(self, update: Update, language: str):
        """Handle authentication error"""
        error_msg = "❌ Authentication failed. Please restart the bot with /start"
        
        if update.callback_query:
            await update.callback_query.edit_message_text(error_msg)
            await update.callback_query.answer()
        else:
            await update.message.reply_text(error_msg)
    
    async def _handle_api_error(self, update: Update, error: str, language: str):
        """Handle API error"""
        error_msg = f"❌ {error}"
        
        if update.callback_query:
            await update.callback_query.answer(error_msg)
        else:
            await update.message.reply_text(error_msg)
    
    async def _handle_error(self, update: Update):
        """Handle general error"""
        try:
            language = await i18n.get_user_language(update.effective_user.id)
            error_msg = i18n.get('error_occurred', language)
        except:
            error_msg = "❌ An error occurred. Please try again."
        
        if update.callback_query:
            await update.callback_query.answer(error_msg)
        else:
            await update.message.reply_text(error_msg)


# Global handler instance
product_handlers = ProductHandlers()