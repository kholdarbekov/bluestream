"""
Telegram Bot Handlers
All conversation handlers and command processors
"""

from handlers.start import start_handler
from handlers.menu import main_menu_handler
from handlers.language import language_handler
from handlers.products import product_handlers
from handlers.orders import order_handlers
from handlers.subscriptions import subscription_handlers
from handlers.profile import profile_handlers
from handlers.loyalty import loyalty_handlers
from handlers.payments import payment_handlers

# Simple handlers for remaining modules
class SimpleHandlers:
    async def admin_panel(self, update, context):
        """Admin panel - access controlled by backend API"""
        await update.message.reply_text("🔧 Admin panel functionality coming soon!")
    
    async def help_handler(self, update, context):
        await update.message.reply_text("🆘 Help: Use /menu to see available options.")
    
    async def support_menu(self, update, context):
        if update.callback_query:
            await update.callback_query.edit_message_text("🆘 Support menu coming soon!")
            await update.callback_query.answer()
        else:
            await update.message.reply_text("🆘 Support menu coming soon!")
    
    async def faq_handler(self, update, context):
        if update.callback_query:
            await update.callback_query.edit_message_text("❓ FAQ coming soon!")
            await update.callback_query.answer()
        else:
            await update.message.reply_text("❓ FAQ coming soon!")
    
    async def contact_support(self, update, context):
        if update.callback_query:
            await update.callback_query.edit_message_text("📞 Contact support: @bluestreamwater")
            await update.callback_query.answer()
        else:
            await update.message.reply_text("📞 Contact support: @bluestreamwater")
    
    async def handle_support_message(self, update, context, text):
        await update.message.reply_text(f"Support message received: {text}\nOur team will get back to you soon!")
    
    async def admin_orders(self, update, context):
        """Admin orders - access controlled by backend API"""
        if update.callback_query:
            await update.callback_query.edit_message_text("📊 Admin orders panel coming soon!")
            await update.callback_query.answer()
        else:
            await update.message.reply_text("📊 Admin orders panel coming soon!")
    
    async def admin_analytics(self, update, context):
        """Admin analytics - access controlled by backend API"""
        if update.callback_query:
            await update.callback_query.edit_message_text("📈 Admin analytics coming soon!")
            await update.callback_query.answer()
        else:
            await update.message.reply_text("📈 Admin analytics coming soon!")

admin_handlers = SimpleHandlers()
support_handlers = SimpleHandlers()
# payment_handlers is now imported from handlers.payments module

__all__ = [
    'start_handler',
    'main_menu_handler',
    'language_handler',
    'product_handlers',
    'order_handlers',
    'subscription_handlers',
    'profile_handlers',
    'loyalty_handlers',
    'admin_handlers',
    'support_handlers',
    'payment_handlers'
]