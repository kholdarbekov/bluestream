"""
Loyalty program handlers
"""
import os
import re
from typing import Optional
from urllib.parse import urlparse

from telegram import Update
from telegram.ext import ContextTypes

from i18n import i18n
from keyboards import MenuKeyboards
from api_client import api_client
from utils import user_middleware, format_price, get_auth_token
from handlers.base import BaseHandler



class LoyaltyHandlers(BaseHandler):
    """Loyalty program handlers"""

    # Hosts Telegram rejects for inline-keyboard URL buttons ("wrong http url").
    _NON_PUBLIC_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}

    # AquaCoins history page size. Kept modest so a page fits comfortably in one
    # Telegram message; users page through the rest with the nav buttons.
    _HISTORY_PAGE_SIZE = 10

    @classmethod
    def _loyalty_guide_url(cls, language: str) -> Optional[str]:
        """Public loyalty-handbook URL for the given bot language.

        Built from COMPANY_WEBSITE (shared .env). The site keeps default-language
        (uz) URLs clean and marks other languages with ?lang=, matching the
        frontend's own URL convention.

        Returns None when COMPANY_WEBSITE is not a public http(s) URL (e.g. the
        dev default ``http://localhost:5000``). Telegram rejects inline-keyboard
        URL buttons that point at localhost/private hosts, and a single invalid
        button fails the whole menu render — so callers omit the button instead.
        """
        base = os.environ.get("COMPANY_WEBSITE", "https://aqua-element.uz").rstrip("/")
        path = "/loyalty-guide"
        if language and language != "uz":
            path = f"{path}?lang={language}"
        url = f"{base}{path}"
        return url if cls._is_public_http_url(url) else None

    @classmethod
    def _is_public_http_url(cls, url: str) -> bool:
        """True only for URLs Telegram accepts as inline-button targets.

        Requires an http/https scheme and a public-looking host: not
        localhost/loopback and containing a dot (rejects bare hostnames and
        docker service names like ``business_app``).
        """
        try:
            parsed = urlparse(url)
        except ValueError:
            return False
        host = (parsed.hostname or "").lower()
        return (
            parsed.scheme in ("http", "https")
            and bool(host)
            and host not in cls._NON_PUBLIC_HOSTS
            and "." in host
        )

    @staticmethod
    def _referral_deep_link(context: ContextTypes.DEFAULT_TYPE, code: str) -> Optional[str]:
        """Telegram deep link that opens the bot and carries the referral code.

        Tapping ``https://t.me/<bot>?start=ref_<code>`` opens the bot with
        ``/start ref_<code>``, which ``start_registration_new`` parses and applies
        on registration. Uses the bot's own username (runtime truth via getMe), so
        it works regardless of how COMPANY_WEBSITE/TELEGRAM_BOT_USERNAME are set.
        Returns None when the username isn't resolvable, so callers can fall back.
        """
        if not code:
            return None
        try:
            username = context.bot.username
        except Exception:
            username = None
        if not username:
            return None
        return f"https://t.me/{username}?start=ref_{code}"

    @staticmethod
    def _unwrap_response_data(response):
        """Support both bare payloads and success_response envelopes."""
        payload = getattr(response, "data", None) or {}
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            return payload["data"]
        return payload if isinstance(payload, dict) else {}

    @classmethod
    def _unwrap_paginated_items(cls, response):
        """Support both paginated_response envelopes and older flat payloads."""
        payload = cls._unwrap_response_data(response)
        return payload.get("items") or payload.get("history") or []

    @staticmethod
    def _signed_amount(points):
        """Render an AquaCoins delta as (icon, signed_text), coloured by SIGN.

        Mirrors the admin UI: a credit (>= 0) is green, a debit (< 0) is red. This
        is what makes refunds (positive adjustments) read as credits instead of the
        old yellow "other" bucket. The stored amount is already signed, so a debit
        keeps its leading minus and a credit gets an explicit plus.
        """
        try:
            pts = int(points)
        except (TypeError, ValueError):
            pts = 0
        if pts >= 0:
            return "🟢", f"+{pts}"
        return "🔴", str(pts)

    @staticmethod
    def _extract_reward_name(description):
        """Pull the reward name out of our own redeem description format
        ("Redeemed reward: <name>"). Returns None when it doesn't match, so the
        caller falls back to the generic localized label rather than guessing."""
        if not description:
            return None
        prefix = "Redeemed reward:"
        if description.startswith(prefix):
            return description[len(prefix):].strip() or None
        return None

    @classmethod
    def _transaction_category(cls, transaction):
        """Map a loyalty transaction to a stable display category + format args.

        Returns ``(category, fmt_kwargs)``; the caller resolves
        ``telegram.loyalty.txn.<category>`` via i18n. Keying off the granular
        ``action_type`` (then the coarse ``transaction_type``) keeps the label
        localized and free of the raw English description / internal user IDs.
        """
        action = (transaction.get("action_type") or "").lower()
        txn_type = (transaction.get("transaction_type") or "").lower()

        if action == "referral":
            return "referral", {}
        if action == "welcome_bonus":
            return "welcome", {}
        if action == "birthday_bonus":
            return "birthday", {}
        if action == "streak_bonus":
            return "streak", {}
        if action == "surprise_reward":
            return "surprise", {}
        if action == "purchase":
            return "order_earn", {}
        if action == "reward_refund":
            order_id = transaction.get("order_id")
            if order_id:
                return "refund_order", {"order_id": order_id}
            return "refund", {}
        if action in ("order_edit_reversal", "order_edit_award"):
            return "adjustment", {}

        # No / unknown action_type → fall back to the coarse transaction_type.
        if txn_type == "redeemed":
            name = cls._extract_reward_name(transaction.get("description"))
            if name:
                return "redeem_named", {"name": name}
            return "redeem", {}
        if txn_type == "earned":
            return "order_earn", {}
        if txn_type == "bonus":
            return "bonus", {}
        if txn_type == "expired":
            return "expired", {}
        if txn_type == "adjustment":
            return "adjustment", {}
        return "other", {}

    @staticmethod
    def _parse_history_page(callback_data):
        """Extract the 1-based page from a history callback.

        ``loyalty_history`` → 1, ``loyalty_history_page_<n>`` → n. The page lives
        in the callback_data (not shared user_data) so it can never collide with
        other paginated lists. Anything unexpected falls back to page 1.
        """
        if callback_data:
            match = re.match(r"^loyalty_history_page_(\d+)$", callback_data)
            if match:
                return max(1, int(match.group(1)))
        return 1

    @staticmethod
    def _history_nav_buttons(page, total_pages, language):
        """Build the nav keyboard rows: only the Prev/Next arrows that apply,
        then a Back button. Returns button-definition rows for KeyboardBuilder."""
        nav = []
        if page > 1:
            nav.append({
                "text": i18n.get("telegram.pagination.previous", language),
                "callback_data": f"loyalty_history_page_{page - 1}",
            })
        if page < total_pages:
            nav.append({
                "text": i18n.get("telegram.pagination.next", language),
                "callback_data": f"loyalty_history_page_{page + 1}",
            })
        rows = []
        if nav:
            rows.append(nav)
        rows.append([{"text": i18n.get("telegram.back", language), "callback_data": "back_to_main"}])
        return rows

    async def loyalty_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show loyalty points and rewards"""
        try:
            user = await user_middleware(update)
            if not user:
                return

            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            if not await self._ensure_loyalty_eligible(update, context, user_id, language):
                return

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                # Get loyalty points
                points_response = await client.get_loyalty_points(user_token)
                rewards_response = await client.get_loyalty_rewards(user_token)

                if points_response.success:
                    points_data = self._unwrap_response_data(points_response)
                    current_points = points_data.get('current_balance', points_data.get('points_balance', 0))
                    lifetime_points = points_data.get('lifetime_earned', points_data.get('lifetime_points', 0))
                else:
                    current_points = lifetime_points = 0

                if rewards_response.success:
                    rewards = self._unwrap_response_data(rewards_response).get('rewards', [])
                else:
                    rewards = []

            # Build loyalty message
            points_unit = i18n.get('telegram.loyalty.points_unit', language)
            loyalty_text = f"{i18n.get('telegram.menu.loyalty', language)}\n\n"
            loyalty_text += f"🏆 {i18n.get('telegram.loyalty.current_balance', language)}: {current_points} {points_unit}\n"
            loyalty_text += f"📈 {i18n.get('telegram.loyalty.lifetime_earned', language)}: {lifetime_points} {points_unit}\n\n"

            if rewards:
                loyalty_text += f"🎁 {i18n.get('telegram.loyalty.available_rewards', language)} ({len(rewards)}):\n"
                for reward in rewards[:3]:  # Show first 3 rewards
                    loyalty_text += (
                        f"• {reward.get('name', i18n.get('telegram.loyalty.reward_fallback', language))} - "
                        f"{reward.get('points_cost', 0)} {points_unit}\n"
                    )

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
            ]

            # Telegram rejects URL buttons pointing at localhost/private hosts, and
            # one invalid button fails the entire menu — so only add it when the
            # guide URL is publicly reachable (omitted on the dev localhost default).
            guide_url = self._loyalty_guide_url(language)
            if guide_url:
                keyboard_buttons.append([
                    {
                        'text': f"📖 {i18n.get('telegram.loyalty.guide_button', language)}",
                        'url': guide_url,
                    },
                ])

            keyboard_buttons.append([
                {'text': i18n.get('telegram.back', language), 'callback_data': 'back_to_main'}
            ])

            from keyboards import KeyboardBuilder
            keyboard = KeyboardBuilder.build_inline_keyboard(keyboard_buttons)

            if update.callback_query:
                await update.callback_query.edit_message_text(text=loyalty_text, reply_markup=keyboard)
                await update.callback_query.answer()
            else:
                await update.message.reply_text(text=loyalty_text, reply_markup=keyboard)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="loyalty_menu")

    async def loyalty_history(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show loyalty points history"""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            if not await self._ensure_loyalty_eligible(update, context, user_id, language):
                return

            points_unit = i18n.get('telegram.loyalty.points_unit', language)
            page = self._parse_history_page(query.data if query else None)

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                response = await client.get_loyalty_history(
                    user_token, page=page, per_page=self._HISTORY_PAGE_SIZE
                )
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return

                history = self._unwrap_paginated_items(response)
                meta = (getattr(response, 'data', None) or {}).get('meta', {}) or {}

            title = i18n.get('telegram.loyalty.points_history', language)
            total = meta.get('total', len(history))
            total_pages = meta.get('pages', 1) or 1
            # Guard against a stale button after the underlying list shrank.
            page = min(max(1, page), total_pages)

            if not history:
                history_text = f"{title}\n\n" + i18n.get('telegram.loyalty.no_history', language)
                keyboard = MenuKeyboards.back_button(language)
            else:
                # Header shows the total + current page so users know there's more.
                history_text = f"{title} ({total})\n"
                history_text += i18n.get(
                    'telegram.loyalty.history_page_info', language, page=page, pages=total_pages
                ) + "\n\n"
                for transaction in history:
                    date = transaction.get('created_at', '')[:10]
                    # Colour by sign (credit = green, debit = red), like the admin UI.
                    icon, signed_amount = self._signed_amount(transaction.get('points', 0))
                    # Localized, category-based label (never the raw English
                    # description or internal IDs).
                    category, fmt = self._transaction_category(transaction)
                    label = i18n.get(f'telegram.loyalty.txn.{category}', language, **fmt)

                    history_text += f"{icon} {signed_amount} {points_unit} - {label}\n"
                    history_text += f"   {date}\n\n"

                from keyboards import KeyboardBuilder
                keyboard = KeyboardBuilder.build_inline_keyboard(
                    self._history_nav_buttons(page, total_pages, language)
                )

            await query.edit_message_text(text=history_text, reply_markup=keyboard)
            await query.answer()

        except Exception as e:
            await self._handle_error(update, exc=e, operation="loyalty_history")

    async def loyalty_referral(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show the user's referral code, link and stats."""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            if not await self._ensure_loyalty_eligible(update, context, user_id, language):
                return

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                response = await client.get_referral_info(user_token)
                if not response.success:
                    await self._handle_api_error(update, response.error, language)
                    return

                data = self._unwrap_response_data(response) or {}

            code = data.get('referral_code', '')
            stats = data.get('statistics', {}) or {}

            # Prefer a Telegram deep link (taps open the bot and auto-apply the
            # code); fall back to the backend web link if the username is missing.
            link = self._referral_deep_link(context, code) or data.get('referral_link', '')

            text = i18n.get('telegram.loyalty.refer_friends', language) + "\n\n"
            text += f"🎟 {i18n.get('telegram.loyalty.referral_code', language)}: {code}\n"
            text += f"🔗 {link}\n\n"
            text += (
                f"👥 {i18n.get('telegram.loyalty.referral_total', language)}: "
                f"{stats.get('total_referrals', 0)}\n"
            )
            text += (
                f"⏳ {i18n.get('telegram.loyalty.referral_pending', language)}: "
                f"{stats.get('pending_referrals', 0)}\n"
            )
            text += (
                f"⭐ {i18n.get('telegram.loyalty.referral_points_earned', language)}: "
                f"{stats.get('points_earned_from_referrals', 0)}"
            )

            keyboard = MenuKeyboards.back_button(language)
            await query.edit_message_text(text=text, reply_markup=keyboard)
            await query.answer()

        except Exception as e:
            await self._handle_error(update, exc=e, operation="loyalty_referral")

    async def loyalty_rewards(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List available rewards with redeem buttons (makes the redeem flow reachable)."""
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            if not await self._ensure_loyalty_eligible(update, context, user_id, language):
                return

            points_unit = i18n.get('telegram.loyalty.points_unit', language)

            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                points_response = await client.get_loyalty_points(user_token)
                rewards_response = await client.get_loyalty_rewards(user_token)

                current_points = 0
                if points_response.success:
                    pd = self._unwrap_response_data(points_response)
                    current_points = pd.get('current_balance', pd.get('points_balance', 0))
                rewards = (
                    self._unwrap_response_data(rewards_response).get('rewards', [])
                    if rewards_response.success else []
                )

            text = f"🎁 {i18n.get('telegram.loyalty.available_rewards', language)}\n\n"
            text += f"🏆 {i18n.get('telegram.loyalty.current_balance', language)}: {current_points} {points_unit}\n\n"

            buttons = []
            if rewards:
                for reward in rewards:
                    name = reward.get('name', i18n.get('telegram.loyalty.reward_fallback', language))
                    cost = reward.get('points_cost', 0)
                    text += f"• {name} — {cost} {points_unit}\n"
                    # Only offer a redeem button for manually-redeemable, affordable rewards.
                    if not reward.get('is_system_reward') and reward.get('can_redeem', current_points >= cost):
                        buttons.append([{
                            'text': f"{i18n.get('telegram.loyalty.redeem', language)}: {name}",
                            'callback_data': f"redeem_{reward.get('id')}",
                        }])
            else:
                text += i18n.get('telegram.loyalty.no_rewards_available', language)

            buttons.append([{'text': i18n.get('telegram.back', language), 'callback_data': 'menu_loyalty'}])

            from keyboards import KeyboardBuilder
            keyboard = KeyboardBuilder.build_inline_keyboard(buttons)
            await query.edit_message_text(text=text, reply_markup=keyboard)
            await query.answer()

        except Exception as e:
            await self._handle_error(update, exc=e, operation="loyalty_rewards")

    async def redeem_reward(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Select a loyalty reward to apply at checkout.

        Phase 3 made reward redemption apply-at-checkout: there is no longer a
        standalone redeem endpoint. Tapping a ``redeem_<id>`` button just stores
        the chosen reward in the bot's per-user conversation state; the reward is
        applied when the order is created (see orders.confirm_order).
        """
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            if not await self._ensure_loyalty_eligible(update, context, user_id, language):
                return

            reward_id = int(query.data.split('_')[1])

            # Persist the selection in per-user conversation state so order
            # creation can pass it through to the backend.
            context.user_data['selected_reward_id'] = reward_id

            confirmation = i18n.get('telegram.loyalty.reward_selected', language)
            # show_alert=True surfaces a dismissable modal (not a 2s toast) so the
            # apply-at-checkout confirmation is unmissable — otherwise the redeem
            # tap appears to "do nothing" as it bounces back to the loyalty menu.
            await query.answer(confirmation, show_alert=True)

            # Return to loyalty menu
            await self.loyalty_menu(update, context)

        except Exception as e:
            await self._handle_error(update, exc=e, operation="redeem_reward")



# Global handler instance
loyalty_handlers = LoyaltyHandlers()
