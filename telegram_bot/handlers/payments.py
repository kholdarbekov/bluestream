"""
Telegram payment-link handler.

Implements redirect-based external payment links for the configured PSP.
"""
import logging
from typing import Dict, Any, Optional

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from i18n import i18n
from api_client import api_client
from utils import get_auth_token, format_price
from keyboards import PaymentKeyboards, customer_may_pay, customer_may_cancel
from handlers.base import BaseHandler
from shared.redis_keyspace import RedisKeyspace

logger = logging.getLogger('handlers.payments')


class PaymentLinkResult:
    """Outcome of `send_payment_link`.

    Bool-like on purpose: every existing call site (`orders.py`'s
    `confirm_order`, this module's `retry_payment`) tests it with
    `if not result:` and only cares that success is truthy. That keeps this a
    drop-in replacement for the plain `bool` the method used to return, while
    still letting a call site that DOES care -- `retry_payment`, which needs
    to tell a pool-guard refusal apart from every other kind of failure --
    read `.error_code` off the same object instead of a second return value.
    """

    __slots__ = ("success", "error_code")

    def __init__(self, success: bool, error_code: Optional[str] = None):
        self.success = success
        self.error_code = error_code

    def __bool__(self) -> bool:
        return self.success


class PaymentHandlers(BaseHandler):
    """Handle redirect-based PSP payment links in Telegram."""

    def __init__(self):
        super().__init__()
        logger.info("PaymentHandlers initialized (external payment link mode)")

    # =========================================================================
    # CORE PAYMENT METHODS
    # =========================================================================

    async def send_payment_link(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        order_data: Dict[str, Any],
        payment_method: str = 'click',
        send_as_new_message: bool = False,
    ) -> "PaymentLinkResult":
        """
        Send external payment link to user via Redirect Method.

        Differs from native invoice: sends a message with an inline button
        that redirects to the configured PSP checkout page.

        When send_as_new_message=True, the payment link is sent as a brand new
        message (which triggers a Telegram notification) and the original
        callback-query message is edited to a brief "ready" status. Used after
        the Asl Belgisi wait so users get a notification the link arrived.

        FAILURE IS SIGNALLED, NOT DRAWN. Every failure path below logs and
        returns a falsy `PaymentLinkResult` WITHOUT touching the customer's
        screen. This method cannot know what the failure means: by the time
        `confirm_order` calls it the order already exists, so "payment
        failed" would be a lie that makes the customer buy the same basket
        twice. Only the caller knows, so the caller renders — via
        `show_payment_link_failed` when the right screen is "the order
        stands, here is Retry", or, when the backend's `error_code` says the
        marking-code pool refused the rail move, a message that the order
        stays on cash.

        Drawing a screen here as well as at the call site is not merely
        redundant: when Telegram refuses the first edit,
        `_edit_or_replace_callback_message` DELETES the bubble and posts a
        replacement, so the caller's edit then lands on a stale message and
        posts a second one — two messages for one failure.
        """
        try:
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Extract order details
            order_id = order_data.get('id')
            order_number = order_data.get('order_number', str(order_id))
            total_amount = order_data.get('total_amount', 0)

            # 1. Authenticate with Backend
            async with api_client as client:
                token = await get_auth_token(update, context, client)
                if not token:
                    logger.error("Failed to get auth token for payment-link generation")
                    return PaymentLinkResult(False)

                # 2. Request Payment Link
                # We use the generic 'POST /payments/create' endpoint via api_client
                # Use dynamic bot username
                bot = context.bot
                bot_username = bot.username or (await bot.get_me()).username
                return_url = f"https://t.me/{bot_username}"

                result = await client.create_payment(token, {
                    'order_id': order_id,
                    'payment_method': payment_method,
                    'return_url': return_url
                })

                if not result.success:
                    logger.error(f"Failed to create {payment_method} link: {result.error}")
                    # `_make_request` (api_client.py) surfaces the full error
                    # body as `result.data` on failure, so a structured
                    # `data.error_code` (e.g. the pool-guard refusal) survives
                    # the trip even though this method only returns a bool-like
                    # result, not the raw response.
                    error_code = None
                    if isinstance(result.data, dict):
                        nested_data = result.data.get('data')
                        if isinstance(nested_data, dict):
                            error_code = nested_data.get('error_code')
                    return PaymentLinkResult(False, error_code=error_code)

                # Let's inspect result structure safely
                response_body = result.data or {}
                if 'data' in response_body:
                    response_data = response_body['data']
                else:
                    response_data = response_body

                payment_link_data = response_data.get('payment_link', {})
                # It accepts dict (from payment_service) or string?
                # payment_service returns dict.
                if isinstance(payment_link_data, dict):
                    payment_url = payment_link_data.get('payment_url')
                else:
                    payment_url = str(payment_link_data)

                if not payment_url:
                    logger.error(f"No payment_url in response: {result.data}")
                    return PaymentLinkResult(False)

            # 3. Send Message with Button
            msg_text = i18n.get(
                'telegram.payment.pay_message',
                language,
                order_number=order_number,
                amount=format_price(total_amount)
            )

            keyboard = PaymentKeyboards.payment_link(payment_url, language)

            query = update.callback_query
            message_id = None

            if send_as_new_message and query:
                # New-message mode: deliver the payment link as a fresh message
                # (so Telegram pushes a notification) and then update the old
                # "preparing" message to a brief ready-status notice.
                chat_id = update.effective_chat.id
                sent_message = await context.bot.send_message(
                    chat_id=chat_id,
                    text=msg_text,
                    reply_markup=keyboard,
                )
                message_id = sent_message.message_id

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
            elif query:
                # Edit the existing callback-query message in place
                sent_message = await query.edit_message_text(
                    text=msg_text,
                    reply_markup=keyboard
                )
                if hasattr(sent_message, 'message_id'):
                    message_id = sent_message.message_id
                else:
                    message_id = query.message.message_id
            else:
                sent_message = await update.effective_message.reply_text(
                    text=msg_text,
                    reply_markup=keyboard
                )
                if hasattr(sent_message, 'message_id'):
                    message_id = sent_message.message_id

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
            await self._ack(update.callback_query)

            logger.info(f"{payment_method} link sent for order {order_id}")
            return PaymentLinkResult(True)

        except Exception as e:
            logger.error(f"Error sending payment link: {e}", exc_info=True)
            return PaymentLinkResult(False)

    async def show_payment_link_failed(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        order: Dict[str, Any],
        language: str,
    ) -> None:
        """The ONE screen for "the order exists, the payment link does not".

        Both callers of `send_payment_link` need exactly this screen, so it is
        written once here rather than copied into each of them: the copy in
        `confirm_order` and the copy in `retry_payment` would be two places
        deciding what a link failure looks like, which is how they drift.

        The copy leads with the order being PLACED and names it, because a
        screen that only says "payment failed" reads as "nothing happened" and
        the customer's next move is to order the same basket again. The
        keyboard is the shared recovery one: its Retry button carries the order
        id in its `callback_data`, so it re-pays THIS order (and survives a bot
        restart, which `user_data` would not).

        Draws only — the caller answers the callback query, because the caller
        owns when the interaction ends.
        """
        order_id = order.get('id')
        text = i18n.get(
            'telegram.orders.payment_link_failed_message', language,
            order_number=order.get('order_number') or str(order_id),
        )
        keyboard = PaymentKeyboards.payment_failed(
            order_id,
            language,
            may_pay=customer_may_pay(order),
            may_cancel=customer_may_cancel(order),
        )

        query = update.callback_query
        if query is not None:
            await self._edit_or_replace_callback_message(
                query, text, reply_markup=keyboard,
            )
        else:
            await update.effective_message.reply_text(text, reply_markup=keyboard)

    async def send_payme_invoice(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        order_data: Dict[str, Any],
        payment_method: str = 'click',
    ) -> "PaymentLinkResult":
        """Backward-compatible wrapper for old call sites."""
        return await self.send_payment_link(
            update,
            context,
            order_data,
            payment_method=payment_method,
        )

    # =========================================================================
    # ERROR HANDLING & RECOVERY
    # =========================================================================

    async def retry_payment(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """
        Handle payment retry request.
        Re-fetches order and sends new invoice.
        """
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Extract order ID from callback data
            order_id = int(query.data.split('_')[-1])

            await self._ack(query)

            # Fetch order details
            async with api_client as client:
                user_token = await get_auth_token(update, context, client)
                if not user_token:
                    await query.edit_message_text(
                        i18n.get('telegram.error.auth_failed', language)
                    )
                    return

                response = await client.get_order(user_token, order_id)
                if not response.success:
                    await query.edit_message_text(
                        i18n.get('telegram.payment.error_order_not_found', language)
                    )
                    return

                order = response.data.get('data', {}).get('order', {})

            # The id on the callback data is the one of record for this retry —
            # it is what survives a restart. Keep it on the payload so a thin
            # response body can still be rendered as a named order.
            order.setdefault('id', order_id)

            # Check if order can still be paid
            if order.get('is_paid'):
                await query.edit_message_text(
                    i18n.get('telegram.payment.error_already_paid', language)
                )
                return

            # THE payability gate. This handler had none: it re-fetched the
            # order, tested `is_paid` alone and minted a link — so a stale Pay
            # button on a CANCELLED or already-settled order produced a Click
            # link that the PREPARE guard (`order_is_payable_online`, the same
            # authority `customer_may_pay` reads through the published
            # `is_payable`) then had to refuse with -9. The button is gone from
            # the keyboard now, but the MESSAGE carrying it survives in the
            # customer's chat forever, so the handler must refuse too.
            if not customer_may_pay(order):
                await query.edit_message_text(
                    i18n.get('telegram.payment.error_not_payable', language)
                )
                logger.info(
                    "Payment retry refused for order %s by user %s: backend says not payable",
                    order_id, user_id,
                )
                return

            payment_info = order.get('payment_info') or {}
            provider_method = payment_info.get('payment_provider') or order.get('payment_method') or 'click'
            # Remember the rail this order was actually on BEFORE this retry
            # rewrites `provider_method` to 'click' below -- the pool-short
            # refusal only means "stays on cash" when the source really was
            # cash. An order already on CLICK that gets refused (a genuinely
            # exhausted pool, unrelated to this task's flip guard, which
            # credits codes the order already holds) is not on cash and must
            # not be told so.
            was_cash = provider_method == 'cash'
            if provider_method == 'card':
                provider_method = 'click'
            elif provider_method == 'cash':
                # A cash order moves to an online rail only if the backend's
                # marking-code pool can cover it. Ask, and keep the customer on
                # cash with a real message when it cannot, instead of silently
                # rewriting the rail into a payment link that never prepares.
                provider_method = 'click'

            # Send new payment link
            link_sent = await self.send_payment_link(
                update, context, order, payment_method=provider_method
            )
            if not link_sent:
                if was_cash and getattr(link_sent, "error_code", None) == "MARKING_CODES_POOL_SHORT":
                    # The backend refused the cash-to-click flip because the
                    # marking-code pool is short; the order was left on cash.
                    # Say so in the customer's language rather than showing
                    # the generic "link failed, retry?" screen -- there is
                    # nothing to retry, the order already stands as cash.
                    await query.edit_message_text(
                        i18n.get('telegram.payment.marking_codes_unavailable', language)
                    )
                    return
                # `send_payment_link` signals, it does not render. This call
                # site owns the screen: the order is still there and still
                # unpaid, so the customer needs the same "it stands, here is
                # Retry" screen rather than a silent no-op that reads as a tap
                # that never registered.
                await self.show_payment_link_failed(update, context, order, language)
                logger.warning(
                    "Payment retry for order %s by user %s produced no %s link; "
                    "left the retry screen up",
                    order_id, user_id, provider_method,
                )
                return

            logger.info(f"Payment retry initiated for order {order_id} by user {user_id}")

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
        renderer does nothing for those, so the HANDLER has to answer — the same
        reasoning `retry_payment` carries for its own stale button.

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

        Delegating to `retry_payment` rather than drawing a dead end is the
        honest answer, because Retry IS the rail move a customer has:
        `POST /api/v1/payments/create` normalizes card->click and flips a pending
        cash order onto Click behind the marking-code pool guard. It also
        inherits the payability gate wholesale — a stale tap on a dead order is
        refused there rather than needing a second copy of the rule here.
        `retry_payment` reads the id as `query.data.split('_')[-1]`, which reads
        `payment_switch_{id}` exactly as it reads `payment_retry_{id}`.
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
        Shows options to retry, switch method, or cancel order.
        """
        try:
            query = update.callback_query
            user_id = update.effective_user.id
            language = await i18n.get_user_language(user_id)

            # Extract order ID from callback data
            order_id = int(query.data.split('_')[-1])

            await self._ack(query)

            # Show cancellation options
            cancelled_text = i18n.get('telegram.payment.cancelled_message', language)

            keyboard = PaymentKeyboards.payment_failed(order_id, language)

            await query.edit_message_text(
                text=f"❌ {cancelled_text}",
                reply_markup=keyboard
            )

            logger.info(f"Payment cancelled for order {order_id} by user {user_id}")

        except Exception as e:
            await self._handle_error(update, context, exc=e, operation="cancel_payment")




# Global handler instance
payment_handlers = PaymentHandlers()
