"""
Loyalty program handlers
"""
import logging
from telegram import Update
from telegram.ext import ContextTypes

from i18n import i18n
from keyboards import MenuKeyboards
from api_client import api_client
from utils import user_middleware, format_price, get_auth_token

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
                user_token = await get_auth_token(update, context, client)
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
            loyalty_text = f"{i18n.get('telegram.menu.loyalty', language)}\n\n"
            loyalty_text += f"🏆 {i18n.get('telegram.loyalty.current_balance', language)}: {current_points} points\n"
            loyalty_text += f"📈 {i18n.get('telegram.loyalty.lifetime_earned', language)}: {lifetime_points} points\n\n"

            if rewards:
                loyalty_text += f"🎁 {i18n.get('telegram.loyalty.available_rewards', language)} ({len(rewards)}):\n"
                for reward in rewards[:3]:  # Show first 3 rewards
                    loyalty_text += f"• {reward.get('name', 'Reward')} - {reward.get('points_cost', 0)} points\n"

                if len(rewards) > 3:
                    loyalty_text += i18n.get('telegram.loyalty.and_more', language, count=len(rewards) - 3)
            else:
                loyalty_text += i18n.get('telegram.loyalty.no_rewards_available', language)
            
            # Create simple keyboard
            keyboard_buttons = [
                [
                    {'text': i18n.get('telegram.loyalty.points_history', language), 'callback_data': 'loyalty_history'},
                    {'text': i18n.get('telegram.loyalty.view_rewards', language), 'callback_data': 'loyalty_rewards'}
                ],
                [
                    {'text': i18n.get('telegram.loyalty.refer_friends', language), 'callback_data': 'loyalty_referral'},
                ],
                [
                    {'text': i18n.get('telegram.back', language), 'callback_data': 'back_to_main'}
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
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return
                
                response = await client.get_loyalty_history(user_token)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return
                
                history = response.data.get('history', [])
            
            if not history:
                history_text = i18n.get('telegram.loyalty.points_history', language) + "\n\n" + i18n.get('telegram.loyalty.no_history', language)
            else:
                history_text = i18n.get('telegram.loyalty.points_history', language) + "\n\n"
                for transaction in history[:10]:  # Show last 10 transactions
                    date = transaction.get('created_at', '')[:10]
                    points = transaction.get('points', 0)
                    transaction_type = transaction.get('transaction_type', 'unknown')

                    if transaction_type == 'earned':
                        icon = "🟢"
                        sign = "+"
                        type_label = i18n.get('telegram.loyalty.transaction_earned', language)
                    elif transaction_type == 'redeemed':
                        icon = "🔴"
                        sign = "-"
                        type_label = i18n.get('telegram.loyalty.transaction_redeemed', language)
                    else:
                        icon = "🟡"
                        sign = ""
                        type_label = transaction_type.title()

                    history_text += f"{icon} {sign}{points} points - {type_label}\n"
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
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return
                
                response = await client.redeem_reward(user_token, reward_id)
                if response.success:
                    success_msg = i18n.get('telegram.loyalty.redeem_success', language)
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
        error_msg = i18n.get('telegram.error.auth_failed', language)
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
            error_msg = i18n.get('telegram.error_occurred', language)
        except:
            error_msg = i18n.get('telegram.error_occurred', 'en')

        if update.callback_query:
            await update.callback_query.answer(error_msg)
        else:
            await update.message.reply_text(error_msg)


# Global handler instance
loyalty_handlers = LoyaltyHandlers()