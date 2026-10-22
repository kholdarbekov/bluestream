"""
Telegram Bot Handlers
All conversation handlers and command processors
"""

from i18n import i18n
from utils import user_middleware

from handlers.menu import main_menu_handler
from handlers.language import language_handler
from handlers.products import product_handlers
from handlers.orders import order_handlers
from handlers.quick_order import quick_order_handlers
from handlers.subscriptions import subscription_handlers
from handlers.profile import profile_handlers
from handlers.loyalty import loyalty_handlers
from handlers.payments import payment_handlers
from handlers.bottles import BottleBalanceHandler
from handlers.support import support_flow_handlers
from handlers.agent_orders import agent_order_handlers
from handlers.reorder_reminders import reorder_reminder_handlers

bottle_handlers = BottleBalanceHandler()

# Simple handlers for remaining modules
class SimpleHandlers:
    async def _get_language(self, update) -> str:
        """Resolve user language with safe fallback."""
        try:
            if update.effective_user:
                return await i18n.get_user_language(update.effective_user.id)
        except Exception:
            pass
        return 'en'

    async def _send_response(self, update, text: str, reply_markup=None):
        """Send/edit response depending on update type.

        ``reply_markup`` is optional so callers that need navigation (e.g. a
        back button) can attach an inline keyboard; existing callers pass
        nothing and behave exactly as before.
        """
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
            await update.callback_query.answer()
        else:
            await update.message.reply_text(text, reply_markup=reply_markup)

    async def admin_panel(self, update, context):
        """Admin panel - access controlled by backend API"""
        language = await self._get_language(update)
        await self._send_response(update, i18n.get('telegram.admin.panel_coming_soon', language))

    async def support_menu(self, update, context):
        """The one Help screen: /help and the Help button. Tell the customer to write here, or use the buttons."""
        if not await user_middleware(update):
            return
        language = await self._get_language(update)
        await self._send_response(
            update,
            f"{i18n.get('telegram.support.menu_intro', language)}\n\n"
            f"{i18n.get('telegram.help.command_hint', language)}",
        )

    async def faq_handler(self, update, context):
        language = await self._get_language(update)
        await self._send_response(update, i18n.get('telegram.support.faq_coming_soon', language))

    async def contact_support(self, update, context):
        language = await self._get_language(update)
        await self._send_response(update, i18n.get('telegram.support.contact_message', language))

    async def admin_orders(self, update, context):
        """Admin orders - access controlled by backend API"""
        language = await self._get_language(update)
        await self._send_response(update, i18n.get('telegram.admin.orders_panel_coming_soon', language))

    async def admin_analytics(self, update, context):
        """Admin analytics - access controlled by backend API"""
        language = await self._get_language(update)
        await self._send_response(update, i18n.get('telegram.admin.analytics_coming_soon', language))

admin_handlers = SimpleHandlers()
support_handlers = SimpleHandlers()
# payment_handlers is now imported from handlers.payments module

__all__ = [
    'main_menu_handler',
    'language_handler',
    'product_handlers',
    'order_handlers',
    'subscription_handlers',
    'profile_handlers',
    'loyalty_handlers',
    'admin_handlers',
    'support_handlers',
    'support_flow_handlers',
    'payment_handlers',
    'bottle_handlers',
    'agent_order_handlers',
    'reorder_reminder_handlers',
]
