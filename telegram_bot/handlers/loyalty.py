"""
Loyalty program handlers
"""
import logging
from telegram import Update
from telegram.ext import ContextTypes

from i18n import i18n
from keyboards import MenuKeyboards
from api_client import api_client
from utils import user_middleware, format_price, authenticate_telegram_user

logger = logging.getLogger('handlers')


class LoyaltyHandlers:
    """Loyalty program handlers"""
    
    async def loyalty_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show loyalty points and rewards"""
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
                
                # Get loyalty points
                points_response = await client.get_loyalty_points(user_token)
                rewards_response = await client.get_loyalty_rewards(user_token)
                
                if points_response.success:
                    points_data = points_response.data
                    current_points = points_data.get('current_balance', 0)
                    lifetime_points = points_data.get('lifetime_earned', 0)
                else:
                    current_points = lifetime_points = 0
                
                if rewards_response.success:
                    rewards = rewards_response.data.get('rewards', [])
                else:
                    rewards = []
            
            # Build loyalty message
            loyalty_text = f"{i18n.get('loyalty_title', language)}\n\n"
            loyalty_text += f"🏆 {i18n.get('loyalty_balance', language, current_points)}\n"
            loyalty_text += f"📈 Lifetime Earned: {lifetime_points} points\n\n"
            
            if rewards:
                loyalty_text += f"🎁 Available Rewards ({len(rewards)}):\n"
                for reward in rewards[:3]:  # Show first 3 rewards
                    loyalty_text += f"• {reward.get('name', 'Reward')} - {reward.get('points_cost', 0)} points\n"
                
                if len(rewards) > 3:
                    loyalty_text += f"...and {len(rewards) - 3} more rewards"
            else:
                loyalty_text += "🎁 No rewards available at the moment."
            
            # Create simple keyboard
            keyboard_buttons = [
                [
                    {'text': '📊 Points History', 'callback_data': 'loyalty_history'},
                    {'text': '🎁 View Rewards', 'callback_data': 'loyalty_rewards'}
                ],
                [
                    {'text': '👥 Refer Friends', 'callback_data': 'loyalty_referral'},
                ],
                [
                    {'text': i18n.get('back', language), 'callback_data': 'back_to_main'}
                ]
            ]
            
            from keyboards import KeyboardBuilder
            keyboard = KeyboardBuilder.build_inline_keyboard(keyboard_buttons)
            
            if update.callback_query:
                await update.callback_query.edit_message_text(text=loyalty_text, reply_markup=keyboard)
                await update.callback_query.answer()
            else:
                await update.message.reply_text(text=loyalty_text, reply_markup=keyboard)
            
        except Exception as e:
            logger.error(f"Error in loyalty menu: {e}")
            await self._handle_error(update)
    
    async def loyalty_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show loyalty points history"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            
            async with api_client as client:
                user_token = await authenticate_telegram_user(update, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return
                
                response = await client.get_loyalty_history(user_token)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return
                
                history = response.data.get('history', [])
            
            if not history:
                history_text = "📊 Points History\n\nNo points transactions yet."
            else:
                history_text = "📊 Points History\n\n"
                for transaction in history[:10]:  # Show last 10 transactions
                    date = transaction.get('created_at', '')[:10]
                    points = transaction.get('points', 0)
                    transaction_type = transaction.get('transaction_type', 'unknown')
                    
                    if transaction_type == 'earned':
                        icon = "🟢"
                        sign = "+"
                    elif transaction_type == 'redeemed':
                        icon = "🔴"
                        sign = "-"
                    else:
                        icon = "🟡"
                        sign = ""
                    
                    history_text += f"{icon} {sign}{points} points - {transaction_type.title()}\n"
                    history_text += f"   {date}\n\n"
            
            keyboard = MenuKeyboards.back_button(language)
            
            await query.edit_message_text(text=history_text, reply_markup=keyboard)
            await query.answer()
            
        except Exception as e:
            logger.error(f"Error in loyalty history: {e}")
            await self._handle_error(update)
    
    async def redeem_reward(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle reward redemption"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)
            
            reward_id = int(query.data.split('_')[1])
            
            async with api_client as client:
                user_token = await authenticate_telegram_user(update, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return
                
                response = await client.redeem_reward(user_token, reward_id)
                if response.success:
                    success_msg = "🎉 Reward redeemed successfully!"
                    await query.answer(success_msg)
                    
                    # Return to loyalty menu
                    await self.loyalty_menu(update, context)
                else:
                    await self._handle_api_error(update, response.error, language)
            
        except Exception as e:
            logger.error(f"Error redeeming reward: {e}")
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
loyalty_handlers = LoyaltyHandlers()