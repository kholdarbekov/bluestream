"""
Customer-facing bottle balance handler.
Shows the customer their returnable bottle balance per address.
"""
import logging

from telegram import Update
from telegram.ext import ContextTypes

from api_client import api_client
from handlers.base import BaseHandler
from i18n import i18n
from keyboards import MenuKeyboards
from utils import user_middleware, get_auth_token

logger = logging.getLogger('handlers')


class BottleBalanceHandler(BaseHandler):
    """Show customer their bottle balances and recent ledger."""

    async def show_bottle_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Display bottle balances across all addresses."""
        query = update.callback_query
        if query:
            await query.answer()

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

                response = await client.get_my_bottle_balances(user_token)

            if not response.success:
                await self._handle_api_error(
                    update,
                    i18n.get('telegram.bottles.load_error', language),
                    language,
                )
                return

            payload = response.data
            if isinstance(payload, dict):
                balances = payload.get('data', []) or []
            elif isinstance(payload, list):
                balances = payload
            else:
                balances = []

            if not balances:
                text = (
                    f"📦 <b>{i18n.get('telegram.bottles.title', language)}</b>\n\n"
                    f"{i18n.get('telegram.bottles.no_balance', language)}"
                )
            else:
                lines = [f"📦 <b>{i18n.get('telegram.bottles.title', language)}</b>\n"]
                total = 0
                for b in balances:
                    balance = int(float(b.get('balance', 0)))
                    if balance <= 0:
                        continue
                    total += balance
                    title = b.get('address_title') or b.get('address_label') or f"Address #{b.get('address_id')}"
                    lines.append(f"• {title}: <b>{balance}</b>")

                if total == 0:
                    text = (
                        f"📦 <b>{i18n.get('telegram.bottles.title', language)}</b>\n\n"
                        f"{i18n.get('telegram.bottles.no_balance', language)}"
                    )
                else:
                    lines.insert(1, f"{i18n.get('telegram.bottles.total', language)}: <b>{total}</b>\n")
                    text = '\n'.join(lines)

            keyboard = MenuKeyboards.back_button(language)
            if query:
                await self._edit_or_replace_callback_message(
                    query, text, reply_markup=keyboard, parse_mode='HTML'
                )
            else:
                await update.message.reply_text(
                    text, reply_markup=keyboard, parse_mode='HTML'
                )

        except Exception as exc:
            logger.error("Error showing bottle balance: %s", exc, exc_info=True)
            await self._handle_error(update, context)
