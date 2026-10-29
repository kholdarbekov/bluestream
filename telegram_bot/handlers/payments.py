"""
Telegram payment-link handler.

Implements redirect-based external payment links for the configured PSP.
"""
import logging
from typing import Dict, Any

from telegram import Update
from telegram.ext import ContextTypes

from i18n import i18n
from api_client import api_client
from utils import get_auth_token, format_price
from keyboards import PaymentKeyboards, parse_pay_callback, published_cancel, published_pay
from handlers.base import BaseHandler
from shared.redis_keyspace import RedisKeyspace

logger = logging.getLogger('handlers.payments')

# The `retry-payment` answers whose `message` the route translates (business_app/api/orders.py
# `retry_order_payment`): 400 (the payment cannot be opened; the bot never sends the malformed body
# the route's other 400 answers), 404 (not this customer's order) and 409 (the refusal, or "This
# changed"). The bot shows only those sentences. Other 4xx messages come from layers that do not
# translate them: a 401 from the JWT loaders, a 429 from the rate limiter.
_RETRY_PAYMENT_SENTENCE_STATUSES = frozenset({400, 404, 409})


class PaymentHandlers(BaseHandler):
    """Handle redirect-based PSP payment links in Telegram."""

    def __init__(self):
        super().__init__()
        logger.info("PaymentHandlers initialized (external payment link mode)")

    # =========================================================================
    # CORE PAYMENT METHODS
    # =========================================================================

    async def show_payment_link(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        order_id: int,
        order_number: str,
        amount,
        payment_url: str,
        send_as_new_message: bool = False,
    ) -> None:
        """Show a Click link the backend already minted: checkout's `POST /orders`, or `retry-payment`.

        Render-only. The bot mints no link of its own (spec 2026-10-27 §4.2): the caller passes the
        backend's `payment_url` and the amount it charges, and this draws the message with its Pay
        button and a Back to that order.

        When send_as_new_message=True, the payment link is sent as a brand new
        message (which triggers a Telegram notification) and the original
        callback-query message is edited to a brief "ready" status. Used after
        the Asl Belgisi wait so users get a notification the link arrived.
        """
        language = await i18n.get_user_language(update.effective_user.id)
        msg_text = i18n.get(
            'telegram.payment.pay_message',
            language,
            order_number=order_number,
            amount=format_price(amount),
        )
        keyboard = PaymentKeyboards.payment_link(payment_url, language, order_id)

        query = update.callback_query
        if send_as_new_message and query:
            # New-message mode: deliver the payment link as a fresh message
            # (so Telegram pushes a notification) and then update the old
            # "preparing" message to a brief ready-status notice.
            sent_message = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=msg_text,
                reply_markup=keyboard,
            )
            ready_notice = i18n.get(
                'telegram.orders.payment_link_ready_notice',
                language,
                order_number=order_number,
            )
            try:
                await query.edit_message_text(text=ready_notice)
            except Exception as edit_err:
                logger.warning(
                    f"Failed to update preparing message after sending new payment link: {edit_err}"
                )
        else:
            sent_message = await self._show(update, msg_text, reply_markup=keyboard)

        message_id = getattr(sent_message, 'message_id', None)
        if message_id and order_id:
            try:
                # TokenManager lives on the running Application's bot_data
                # (see bot.py:166). Earlier code did `from token_manager
                # import token_manager` — but the module only exports the
                # *class*, no module-level instance — so the import always
                # failed and the message_id was never persisted, breaking
                # the in-place edit on payment-success.
                token_manager = context.bot_data.get('token_manager') if context.bot_data else None
                if token_manager and token_manager.redis:
                    redis_key = RedisKeyspace.bot_payment_message(order_id)
                    await token_manager.redis.setex(redis_key, 3600, str(message_id))
            except Exception as redis_err:
                logger.warning(f"Failed to store payment message_id in Redis: {redis_err}")

        # Ack the tap: the dedup middleware no longer pre-answers, so the
        # happy path must dismiss the spinner itself. Through `_ack`, so a
        # refused answer can't trigger the error path after success.
        await self._ack(query)
        logger.info(f"Payment link shown for order {order_id}")

    async def show_payment_link_failed(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        order: Dict[str, Any],
        language: str,
    ) -> None:
        """The ONE screen for "the order exists, the payment link does not".

        The copy leads with the order being PLACED and names it, because a
        screen that only says "payment failed" reads as "nothing happened" and
        the customer's next move is to order the same basket again. The
        keyboard is the shared recovery one, drawn from the order's published
        answers: its Pay carries the order id and the amount in its
        `callback_data`, so it pays THIS order (and survives a bot restart,
        which `user_data` would not).

        Draws only — the caller answers the callback query, because the caller
        owns when the interaction ends.
        """
        order_id = order.get('id')
        text = i18n.get(
            'telegram.orders.payment_link_failed_message', language,
            order_number=order.get('order_number') or str(order_id),
        )
        keyboard = PaymentKeyboards.payment_failed(
            order_id, language, pay=published_pay(order), cancel=published_cancel(order),
        )
        await self._show(update, text, reply_markup=keyboard)

    # =========================================================================
    # ERROR HANDLING & RECOVERY
    # =========================================================================

    async def retry_payment(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Pay an existing order (`payment_retry_<id>_<amount>`), through `retry-payment`.

        The callback carries the amount the screen's published Pay showed, and the backend
        decides: a link (shown), the refusal's sentence or "This changed" (an alert, then the
        order as it stands now, whose buttons are fresh). The bot holds no copy of the Pay rule.

        A button from before the amount rode the callback (`payment_retry_<id>`, and the
        `payment_switch_<id>` that delegates here) has nothing to send, so it writes nothing and
        opens the order, whose Pay carries the amount. Telegram messages are permanent: a notice
        or a recovery screen drawn by an older release still carries one.
        """
        from handlers.orders import order_handlers

        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            parsed = parse_pay_callback(query.data)
            if parsed is None:
                await self._ack(query, i18n.get('telegram.error.generic', language))
                return
            order_id, expected_amount = parsed

            if expected_amount is None:
                await order_handlers.order_details(update, context, order_id=order_id)
                return

            # No ack before the write: its answer is the only ack, so an alert it raises is the
            # first answer to this tap, and Telegram shows it.
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await self._handle_auth_error(update, language)
                    return

                response = await client.retry_order_payment(
                    user_token, order_id, expected_amount=expected_amount,
                )
                data = ((response.data or {}).get('data') or {}) if response.success else {}
                payment_url = data.get('payment_url')
                if payment_url:
                    # The answer names the payment, not the order; one read names the order in
                    # the message.
                    order_response = await client.get_order(user_token, order_id)
                    order = (
                        ((order_response.data or {}).get('data') or {}).get('order') or {}
                        if order_response.success else {}
                    )

            if payment_url:
                # The amount the link charges, which the backend just checked against the screen's.
                charged = (data.get('payment') or {}).get('amount')
                await self.show_payment_link(
                    update, context,
                    order_id=order_id,
                    order_number=order.get('order_number') or str(order_id),
                    amount=charged if charged is not None else float(expected_amount),
                    payment_url=payment_url,
                )
                logger.info(f"Payment retry initiated for order {order_id} by user {user_id}")
                return

            if response.status_code in _RETRY_PAYMENT_SENTENCE_STATUSES:
                # The backend's own sentence: the refusal, "This changed", or why the payment
                # cannot be opened. Then the order as it stands, which offers no stale Pay.
                await self._ack(query, response.error, show_alert=True)
                await order_handlers.order_details(update, context, order_id=order_id)
                logger.info(
                    "Payment retry for order %s by user %s answered %s",
                    order_id, user_id, response.status_code,
                )
                return

            if response.status_code == 401:
                # Still refused after the API client signed the customer in again (a deactivated
                # account, say). The JWT loaders' `message` is English, so the customer reads the
                # seeded sign-in sentence in their own language instead.
                await self._handle_auth_error(update, language)
                return

            await self._ack_failed_call(query, response, language, operation="retry_payment")

        except Exception as e:
            await self._handle_error(update, context, exc=e, operation="retry_payment")

    async def switch_payment_method(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """A tap on a STALE `payment_switch_{id}` button. Delegates to Retry.

        The button is no longer drawn anywhere (`PaymentKeyboards.payment_failed`
        records why), but Telegram messages are PERMANENT: every recovery screen
        already sitting in a customer's chat still carries it. Removing the
        renderer does nothing for those, so the HANDLER has to answer.

        What it used to do was the defect. It parsed the order id, LOGGED it, and
        then rendered `OrderKeyboards.payment_methods`, whose callbacks
        (`payment_cash` / `payment_card`) carry NO order id and route to
        `orders.payment_handler` -> `_show_order_confirmation` — the CART
        confirmation. Since an unpaid order deliberately keeps its cart, Confirm
        there placed a SECOND ORDER FOR THE SAME BASKET: the exact outcome the
        "your order is placed" copy on the screen it was reached from exists to
        prevent.

        The REGISTRATION stays (`bot.py`): an unclaimed callback leaves Telegram's
        spinner running with nothing able to stop it, so a removed pattern is a
        worse screen than a redirected one.

        `retry_payment` reads `payment_switch_{id}` as it reads the amount-less
        `payment_retry_{id}`: it writes nothing and opens that order, whose
        published Pay is the way to pay it. No customer moves a rail from here: a
        cash order is no longer flipped onto Click (spec 2026-10-27 §4.3, D4).
        """
        logger.info(
            "Stale payment_switch tap for %s from user %s; delegating to retry_payment",
            (update.callback_query.data if update.callback_query else None),
            update.effective_user.id if update.effective_user else None,
        )
        await self.retry_payment(update, context)

    async def cancel_payment(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """
        Handle payment cancellation.
        Shows the order's published Pay and Cancel, with My Orders.
        """
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Extract order ID from callback data
            order_id = int(query.data.split('_')[-1])

            await self._ack(query)

            # Only stale `payment_cancel_<id>` buttons reach this handler: nothing
            # has drawn one since 553472c, but the messages stay in the chat. The
            # order may have moved on since, so Pay and Cancel come from THIS
            # order's published answers. A failed fetch leaves `order` None, and
            # both readers fail closed on it: neither button, with My Orders still
            # one tap away.
            order = None
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if user_token:
                    response = await client.get_order(user_token, order_id)
                    if response.success:
                        order = response.data.get('data', {}).get('order', {})

            # Show cancellation options
            cancelled_text = i18n.get('telegram.payment.cancelled_message', language)

            keyboard = PaymentKeyboards.payment_failed(
                order_id, language, pay=published_pay(order), cancel=published_cancel(order),
            )

            await query.edit_message_text(
                text=f"❌ {cancelled_text}",
                reply_markup=keyboard
            )

            logger.info(f"Payment cancelled for order {order_id} by user {user_id}")

        except Exception as e:
            await self._handle_error(update, context, exc=e, operation="cancel_payment")


# Global handler instance
payment_handlers = PaymentHandlers()
