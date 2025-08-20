"""
Subscription management handlers
"""
import logging
from telegram import Update
from telegram.ext import ContextTypes

from i18n import i18n
from keyboards import SubscriptionKeyboards, MenuKeyboards
from api_client import api_client
from utils import user_middleware, authenticate_telegram_user

logger = logging.getLogger('handlers')


class SubscriptionHandlers:
    """Subscription-related handlers"""
    
    async def subscriptions_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show user subscriptions"""
        try:
            user = await user_middleware(update)
            if not user:
                return
            
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            
            async with api_client as client:
                user_token = await authenticate_telegram_user(update, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return
                
                response = await client.get_user_subscriptions(user_token)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return
                
                subscriptions = response.data.get('subscriptions', [])
            
            subs_text = f"{i18n.get('subscription_title', language)}\n\n"
            if subscriptions:
                active_count = len([s for s in subscriptions if s.get('status') == 'active'])
                subs_text += f"{i18n.get('subscription_active', language, count=active_count)}\n"
            else:
                subs_text += "You have no active subscriptions."
            
            keyboard = SubscriptionKeyboards.subscription_list(subscriptions, language)
            
            if update.callback_query:
                await update.callback_query.edit_message_text(text=subs_text, reply_markup=keyboard)
                await update.callback_query.answer()
            else:
                await update.message.reply_text(text=subs_text, reply_markup=keyboard)
            
        except Exception as e:
            logger.error(f"Error in subscriptions menu: {e}")
            await self._handle_error(update)
    
    async def subscription_details(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show subscription details"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            
            subscription_id = int(query.data.split('_')[1])
            
            # Mock subscription data for demo
            subscription = {
                'id': subscription_id,
                'name': 'Weekly Water Delivery',
                'status': 'active',
                'frequency': 'weekly',
                'next_delivery': '2024-01-15'
            }
            
            details_text = f"🔄 {subscription['name']}\n\n"
            details_text += f"Status: {'✅ Active' if subscription['status'] == 'active' else '⏸️ Paused'}\n"
            details_text += f"Frequency: {subscription['frequency'].title()}\n"
            details_text += f"Next Delivery: {subscription.get('next_delivery', 'TBD')}"
            
            keyboard = SubscriptionKeyboards.subscription_actions(subscription_id, subscription['status'], language)
            
            await query.edit_message_text(text=details_text, reply_markup=keyboard)
            await query.answer()
            
        except Exception as e:
            logger.error(f"Error in subscription details: {e}")
            await self._handle_error(update)
    
    async def create_subscription(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle subscription creation"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            
            freq_text = f"{i18n.get('subscription_frequency', language)}"
            keyboard = SubscriptionKeyboards.subscription_frequency(language)
            
            await query.edit_message_text(text=freq_text, reply_markup=keyboard)
            await query.answer()
            
        except Exception as e:
            logger.error(f"Error creating subscription: {e}")
            await self._handle_error(update)
    
    async def subscription_actions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle subscription actions (pause/resume/cancel)"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            
            action_data = query.data.split('_')
            action = action_data[0]  # pause, resume, cancel
            sub_id = int(action_data[2])
            
            if action == 'pause':
                result_msg = "⏸️ Subscription paused successfully!"
            elif action == 'resume':
                result_msg = "▶️ Subscription resumed successfully!"
            elif action == 'cancel':
                result_msg = "❌ Subscription cancelled successfully!"
            else:
                result_msg = "✅ Action completed!"
            
            await query.answer(result_msg)
            
            # Return to subscriptions menu
            await self.subscriptions_menu(update, context)
            
        except Exception as e:
            logger.error(f"Error in subscription actions: {e}")
            await self._handle_error(update)
    
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
subscription_handlers = SubscriptionHandlers()