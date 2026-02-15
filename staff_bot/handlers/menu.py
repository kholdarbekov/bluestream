"""
Main menu handler for Staff Bot - Role-aware menu display
"""
import logging
from telegram import Update
from telegram.ext import ContextTypes

from handlers.base import BaseHandler
from i18n import i18n
from keyboards.menu import MenuKeyboards
from permissions import is_delivery_driver, is_operator

logger = logging.getLogger(__name__)

_handler = BaseHandler()


async def main_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show the main menu based on user's staff roles"""
    if not context.user_data.get('authenticated'):
        language = context.user_data.get('language', 'en')
        if update.callback_query:
            await update.callback_query.answer(
                i18n.get('staff.session_expired', language), show_alert=True
            )
        elif update.message:
            await update.message.reply_text(
                i18n.get('staff.session_expired', language)
            )
        return

    language = await _handler._get_language(update, context)
    staff_roles = context.user_data.get('staff_roles', [])

    menu_text = i18n.get('staff.menu.title', language)

    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            menu_text,
            reply_markup=MenuKeyboards.main_menu_inline(language, staff_roles)
        )
    elif update.message:
        await update.message.reply_text(
            menu_text,
            reply_markup=MenuKeyboards.main_menu(language, staff_roles)
        )


async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/menu command handler"""
    await main_menu_handler(update, context)
