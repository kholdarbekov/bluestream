"""
Search Client Handler for Staff Bot
Allows operators to search for existing clients by phone or name.
"""
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from handlers.base import BaseHandler
from api_client import api_client
from keyboards.operator import OperatorKeyboards
from keyboards.common import CommonKeyboards
from utils.formatters import format_user_card
from permissions import require_auth, require_operator
from i18n import i18n

logger = logging.getLogger(__name__)

# Conversation state
SEARCH_INPUT = 20


class SearchUserHandler(BaseHandler):
    """Handle client search flow"""

    @require_auth
    @require_operator
    async def start_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start the client search"""
        language = await self._get_language(update, context)

        text = i18n.get('staff.operator.search_prompt', language)

        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(text, parse_mode='HTML')
        else:
            await update.message.reply_text(text, parse_mode='HTML')

        return SEARCH_INPUT

    async def receive_search_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Receive search query and show results"""
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END

        query_text = update.message.text.strip()

        if len(query_text) < 2:
            await update.message.reply_text(
                i18n.get('staff.operator.search_too_short', language),
                parse_mode='HTML'
            )
            return SEARCH_INPUT

        try:
            async with api_client as client:
                response = await client.search_clients(token, query_text)

            if not response.success:
                if response.status_code == 401:
                    await self._handle_auth_error(update, language)
                    return ConversationHandler.END
                await self._handle_api_error(update, response.error, language)
                return SEARCH_INPUT

            clients = response.data if isinstance(response.data, list) else response.data.get('items', [])

            if not clients:
                text = i18n.get('staff.operator.no_results', language, query=query_text)
                keyboard = OperatorKeyboards.user_not_found(language)
                await update.message.reply_text(
                    text, reply_markup=keyboard, parse_mode='HTML'
                )
                return ConversationHandler.END

            if len(clients) == 1:
                # Single result - show details directly
                client_user = clients[0]
                card = format_user_card(client_user, language)
                keyboard = OperatorKeyboards.user_found(language, client_user['id'])
                await update.message.reply_text(
                    card, reply_markup=keyboard, parse_mode='HTML'
                )
                return ConversationHandler.END

            # Multiple results - show list
            header = i18n.get('staff.operator.search_results', language, count=len(clients))
            await update.message.reply_text(header, parse_mode='HTML')

            for client_user in clients[:10]:  # Limit to 10 results
                card = format_user_card(client_user, language)
                keyboard = OperatorKeyboards.user_found(language, client_user['id'])
                await update.message.reply_text(
                    card, reply_markup=keyboard, parse_mode='HTML'
                )

            return ConversationHandler.END

        except Exception as e:
            logger.error(f"Error searching clients: {e}", exc_info=True)
            await self._handle_error(update, context)
            return ConversationHandler.END

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel search"""
        language = await self._get_language(update, context)
        if update.callback_query:
            await update.callback_query.answer()
        await (update.callback_query or update).message.reply_text(
            i18n.get('staff.cancelled', language),
            reply_markup=CommonKeyboards.back_button(language)
        )
        return ConversationHandler.END
