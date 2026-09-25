"""
Base handler class with shared error handling for Staff Bot handlers.
"""
import asyncio
import base64
import json
import logging
from datetime import datetime, timezone
from html import escape
from typing import Dict, Optional, Set, Tuple
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.error import BadRequest, NetworkError, TelegramError, TimedOut
from telegram.ext import ContextTypes

from staff_bot.i18n import i18n
from staff_bot.database import db_manager, StaffUserRepository
from staff_bot.keyboards.common import CommonKeyboards
from staff_bot.utils import auth_refusals, flow_state
# Module-level so this module carries the same `api_client` seam every
# handler module does — the guard below reads the backend, and a test that
# swaps a handler module's client has to be able to swap this one too.
from staff_bot.api_client import api_client
from staff_bot.utils.answered_callbacks import callback_already_answered
from staff_bot.utils.api_errors import ErrorDetailCopy, displayable_backend_reason, render_detail_copy

logger = logging.getLogger(__name__)


class BaseHandler:
    """Base class for staff bot handler groups with shared error handling."""

    MIN_TOKEN_TTL_SECONDS = 30
    TELEGRAM_RETRY_ATTEMPTS = 2
    TELEGRAM_RETRY_DELAY_SECONDS = 0.5
    # Telegram refuses answerCallbackQuery text longer than this (400).
    TELEGRAM_ALERT_MAX_CHARS = 200
    API_ERROR_CODE_KEY_MAP = {
        'STAFF_AUTH_REQUIRED': 'staff.error.api.auth_failed',
        'STAFF_TELEGRAM_ID_REQUIRED': 'staff.error.api.validation',
        'STAFF_REQUEST_BODY_REQUIRED': 'staff.error.api.validation',
        'STAFF_REFRESH_TOKEN_REQUIRED': 'staff.error.api.auth_failed',
        'STAFF_STATUS_REQUIRED': 'staff.error.api.validation',
        'STAFF_COORDINATES_REQUIRED': 'staff.error.api.validation',
        'STAFF_CLIENT_ID_REQUIRED': 'staff.error.api.validation',
        # The operator's client lookups (addresses, payment methods, add
        # address) and the sales outlet link: the customer row is gone.
        'STAFF_USER_NOT_FOUND': 'staff.error.api.customer_not_found',
        'STAFF_DELIVERY_NOT_FOUND': 'staff.error.api.delivery_not_found',
        # Mark-preparing on an order that was deleted under the card.
        'STAFF_ORDER_NOT_FOUND': 'staff.error.api.order_not_found',
        'STAFF_CLIENT_NOT_FOUND': 'staff.error.api.customer_not_found',
        # A basket line whose product was deleted after the operator picked it.
        'STAFF_PRODUCT_NOT_FOUND': 'staff.error.api.product_unavailable',
        'STAFF_DELIVERY_PERSON_NOT_FOUND': 'staff.error.api.not_found',
        'STAFF_PHONE_EXISTS': 'staff.operator.user_already_exists',
        'STAFF_EMAIL_EXISTS': 'staff.error.api.conflict',
        'STAFF_DELIVERY_PERSON_EXISTS': 'staff.error.api.conflict',
        'STAFF_EMPLOYEE_ID_EXISTS': 'staff.error.api.conflict',
        'STAFF_DELIVERY_ALREADY_TAKEN': 'staff.error.api.already_taken',
        # A claim (pool card, or a broadcast's Accept) on a delivery that stopped
        # being claimable before the tap landed (`assign_driver`'s claimable
        # guard, a 400). That includes losing a race to another driver:
        # `assign_driver` locks the row and judges its status BEFORE its owner,
        # and a driver-held delivery is never claimable, so the second of two
        # drivers arrives HERE, not at STAFF_DELIVERY_ALREADY_TAKEN (unreachable
        # from the bot). The copy therefore names every cause: taken, moved to a
        # later day, failed or cancelled. The generic 400 copy ("check the
        # entered data") would be addressed to a driver who typed nothing.
        'STAFF_DELIVERY_NOT_CLAIMABLE': 'staff.error.api.delivery_not_claimable',
        # `assign_driver` found no ACTIVE DeliveryPerson row for this user: the
        # account has the role but its driver profile is missing or switched off.
        'STAFF_DRIVER_NOT_FOUND': 'staff.error.api.driver_profile_missing',
        # The order-status write lost a race (`OrderService` compare-and-set, a
        # 409): the other writer's change stands and this one wrote nothing.
        'ORDER_STATUS_CONFLICT': 'staff.error.api.order_changed_concurrently',
        # The DELIVERY step was allowed but the ORDER refused to follow: someone
        # moved the order itself (e.g. cancelled it) while the driver held it.
        'ORDER_STATUS_TRANSITION_INVALID': 'staff.error.api.order_status_changed',
        'INVENTORY_CONFIRMATION_FAILED': 'staff.error.api.inventory_confirmation_failed',
        # Two active AMOUNT-mode contracts for one grocery customer: only an
        # administrator can say which one applies. Reached both when a driver
        # completes a delivery (the delivered charge) and when an operator
        # creates an order (`reserve_for_order`), so the copy names no audience.
        'CONTRACT_AMOUNT_MODE_AMBIGUOUS': 'staff.error.api.contract_needs_admin',
        # R20: the delivery a driver's card acts on was rescheduled, reassigned
        # or returned to the pool after the card was drawn. The three
        # delivery-status call sites render it as a stale card, not an alert
        # (`_handle_api_response_error`).
        'STAFF_DELIVERY_NOT_OWNED': 'staff.error.api.delivery_not_owned',
        # The place-scope lock ladder timed out (Postgres 55P03) — an admin is
        # regrouping this address right now. Transient and RETRYABLE, so it gets
        # its own "try again in a moment" copy instead of the generic conflict
        # text, which reads as a permanent refusal.
        'BOTTLE_SCOPE_LOCK_TIMEOUT': 'staff.error.api.scope_busy',
        'STAFF_DRIVER_COD_BLOCKED': 'staff.error.api.driver_cod_blocked',
        'COD_DRIVER_BLOCKED': 'staff.error.api.driver_cod_blocked',
        'COD_DEBT_LIMIT_REACHED': 'staff.error.api.cod_debt_limit_reached',
        'STAFF_INVALID_INVITE_TOKEN': 'staff.error.api.invalid_invite',
        'STAFF_TELEGRAM_NOT_APPROVED': 'staff.error.api.forbidden',
        'STAFF_NO_ROLE': 'staff.error.api.forbidden',
        'STAFF_ACCOUNT_DEACTIVATED': 'staff.error.api.account_deactivated',
        'STAFF_ACCOUNT_INACTIVE': 'staff.error.api.account_inactive',
        'STAFF_TELEGRAM_ALREADY_LINKED': 'staff.error.api.telegram_already_linked',
        'STAFF_OPERATOR_ROLE_REQUIRED': 'staff.error.api.forbidden',
        'STAFF_SEARCH_QUERY_TOO_SHORT': 'staff.error.api.search_too_short',
        'STAFF_SEARCH_TYPE_INVALID': 'staff.error.api.invalid_input',
        'STAFF_PHONE_REQUIRED': 'staff.error.api.validation',
        'STAFF_FULL_NAME_PHONE_REQUIRED': 'staff.error.api.validation',
        'STAFF_DELIVERY_PERSON_LINK_MISSING': 'staff.error.api.validation',
        'STAFF_FULL_NAME_EMPTY': 'staff.error.api.validation',
        'STAFF_INVALID_STATUS': 'staff.error.api.validation',
        'STAFF_ROLE_REQUIRED': 'staff.error.api.validation',
        'STAFF_INVITE_REDIS_UNAVAILABLE': 'staff.error.api.service_unavailable',
        'STAFF_INVITE_STORE_UNAVAILABLE': 'staff.error.api.service_unavailable',
        'STAFF_INVITE_PAYLOAD_MALFORMED': 'staff.error.api.invalid_input',
        'STAFF_INVITE_PAYLOAD_USER_ID_REQUIRED': 'staff.error.api.invalid_input',
        'STAFF_INVALID_COORDINATES': 'staff.error.api.invalid_input',
        'STAFF_MAX_CONCURRENT_REACHED': 'staff.error.api.conflict',
        # A status step the delivery can no longer take: its status changed
        # under the card. The plain sentence is for a backend that publishes no
        # `current_status`; with it, API_ERROR_DETAIL_COPY names the status.
        'STAFF_INVALID_STATUS_TRANSITION': 'staff.error.api.status_transition_refused',
        'STAFF_INVALID_FAIL_REASON': 'staff.error.api.invalid_input',
        # Mark-preparing on an order that already left CONFIRMED under the card.
        'STAFF_ORDER_STATUS_INVALID_FOR_PREPARING': 'staff.error.api.not_preparable',
        # Re-dispatch is a reschedule to today (R18, admin-order-reschedule spec),
        # so its refusals are the reschedule's 400s. Every one an operator can meet
        # means the row changed under their card, and each cause gets its own
        # sentence: the delivery is no longer FAILED (a colleague re-dispatched
        # first), the order was cancelled or delivered, or the contract ran out.
        # These used to share the generic conflict sentence, which never told the
        # operator which of those happened, or that there was nothing to retry.
        'ORDER_NOT_RESCHEDULABLE': 'staff.error.api.order_closed_for_redispatch',
        'DELIVERY_NOT_RESCHEDULABLE': 'staff.error.api.order_closed_for_redispatch',
        'STAFF_DELIVERY_NOT_REDISPATCHABLE': 'staff.error.api.not_redispatchable',
        'ORDER_RESCHEDULE_PAST_CONTRACT_END': 'staff.error.api.past_contract_end',
        'STAFF_PHONE_FIRST_NAME_REQUIRED': 'staff.error.api.validation',
        'STAFF_ORDER_ITEMS_REQUIRED': 'staff.error.api.validation',
        'BOTTLE_SESSION_REQUIRED': 'staff.error.api.bottle_session_required',
        'BOTTLE_SESSION_CAPACITY_EXCEEDED': 'staff.error.api.bottle_session_capacity_exceeded',
        # Bottle sessions, co-drivers and transfers. Each fact has its own code
        # and sentence: "you have no open session" (the driver's OWN session,
        # `_get_open_session_or_raise`) is not "the session you picked is gone"
        # (join target) nor "open one first to invite / to receive".
        'BOTTLE_SESSION_NOT_FOUND': 'staff.error.api.no_open_bottle_session',
        'BOTTLE_SESSION_TARGET_NOT_FOUND': 'staff.error.api.bottle_session_gone',
        # A JOIN on a session that closed after the list was drawn.
        'BOTTLE_SESSION_NOT_OPEN': 'staff.error.api.bottle_session_closed',
        # The member's own membership: the owner closed the session they joined.
        'BOTTLE_SESSION_MEMBERSHIP_CLOSED': 'staff.error.api.joined_session_closed',
        'BOTTLE_SESSION_REQUIRED_TO_INVITE': 'staff.error.api.open_session_to_invite',
        'BOTTLE_SESSION_REQUIRED_TO_RECEIVE': 'staff.error.api.open_session_to_receive',
        # Reached through JOIN only: the /open screen answers this code with its
        # own bespoke copy before the resolver sees it (bottle_collection.py).
        'BOTTLE_SESSION_ALREADY_OPEN': 'staff.error.api.close_own_session_to_join',
        # The invite route re-words join_session's two refusals for the INVITER.
        'BOTTLE_INVITEE_HAS_SESSION': 'staff.error.api.invitee_has_session',
        'BOTTLE_INVITEE_IN_OTHER_SESSION': 'staff.error.api.invitee_in_other_session',
        'BOTTLE_SESSION_MEMBERSHIP_ALREADY_ACTIVE': 'staff.error.api.already_in_session',
        'BOTTLE_SESSION_MEMBERSHIP_NOT_FOUND': 'staff.error.api.not_in_session',
        # A collection/fine at a place the customer is no longer a member of.
        'BOTTLE_SCOPE_MEMBERSHIP_REQUIRED': 'staff.error.api.customer_left_address',
        # The same retry token arrived with a different body: the entry that
        # landed is not the one on screen, so the driver starts over.
        'BOTTLE_IDEMPOTENCY_KEY_REUSED': 'staff.error.api.entry_already_submitted',
        # With `requested`/`available`, API_ERROR_DETAIL_COPY names both numbers.
        'BOTTLE_TRANSFER_EXCEEDS_INVENTORY': 'staff.error.api.transfer_exceeds_inventory',
        'BOTTLE_TRANSFER_NOT_FOUND': 'staff.error.api.transfer_not_found',
        'BOTTLE_TRANSFER_NOT_RECEIVER': 'staff.error.api.transfer_not_yours',
        'BOTTLE_TRANSFER_NOT_PENDING': 'staff.error.api.transfer_already_handled',
        # Sales-agent outlet flows (business_app/api/staff_sales.py).
        'SALES_OUTLET_NOT_FOUND': 'staff.error.api.outlet_not_found',
        'SALES_OUTLET_NOT_ASSIGNED': 'staff.error.api.outlet_not_assigned',
        'SALES_OUTLET_DUPLICATE': 'staff.sales.error.duplicate',
        'SALES_OUTLET_OUTSIDE_ZONE': 'staff.operator.outside_delivery_area',
        'SALES_OUTLET_PIN_REQUIRED': 'staff.sales.error.pin_required',
        'SALES_ACTIVATION_PIN_REQUIRED': 'staff.sales.error.pin_required',
        # Nearby without a usable pin (phase 2b). The bot only ever sends a
        # real Telegram location, so this is the backend refusing something
        # the bot did not do — but the generic 400 copy ("Check the data and
        # try again") would send the agent back to a form they never filled
        # in. Reuses the existing seeded key: no new `/health` requirement.
        'SALES_NEARBY_PIN_REQUIRED': 'staff.sales.error.pin_required',
        'SALES_ACTIVATION_PHONE_REQUIRED': 'staff.sales.error.phone_required',
        'SALES_OUTLET_STAGE_INVALID': 'staff.sales.error.stage_invalid',
        'SALES_APPROVAL_STEP_FAILED': 'staff.sales.error.approval_failed',
        'SALES_APPROVAL_PHONE_TAKEN': 'staff.sales.error.phone_taken',
        # D25 rule 3: Attach was tapped on an outlet whose contact phone names no account.
        # Its own sentence, not `phone_taken`'s: nothing is taken, there is simply nothing to
        # join, and the operator's next move is plain Approve.
        'SALES_ATTACH_NO_ACCOUNT': 'staff.sales.error.attach_no_account',
        'SALES_CONTACT_PHONE_INVALID': 'staff.operator.invalid_phone',
        'SALES_DISTRICT_INVALID': 'staff.sales.error.district_invalid',
        # Sales-agent visit loop (business_app/api/staff_sales.py, phase 2a).
        # `SALES_STOCK_PRODUCT_INVALID` has its OWN copy (M14): an admin
        # un-ticking `products.in_sales_stock_check` mid-visit is not a bad
        # number, and the quantity sentence sends the agent back to re-type a
        # count that was never the problem. Same screen, different sentence.
        'SALES_VISIT_ALREADY_OPEN': 'staff.sales.error.visit_open',
        'SALES_VISIT_NOT_OPEN': 'staff.sales.error.visit_not_open',
        'SALES_VISIT_NOT_FOUND': 'staff.error.api.visit_not_found',
        'SALES_VISIT_NOT_OWNED': 'staff.error.api.visit_not_owned',
        'SALES_VISIT_STEP_INVALID': 'staff.sales.error.visit_step',
        'SALES_STOCK_QTY_INVALID': 'staff.sales.error.stock_qty',
        'SALES_STOCK_PRODUCT_INVALID': 'staff.sales.error.stock_product',
        'SALES_OUTLET_NOT_ACTIVE': 'staff.sales.error.outlet_not_active',
        'SALES_VISIT_ORDER_EXISTS': 'staff.sales.error.order_exists',
        # Since Task 2 the client keeps `details` on every status, not only
        # 409s, so API_ERROR_DETAIL_COPY's own spec (below) usually renders
        # first and names the product, the floor and what was entered. This
        # plain key is the fallback for a raise site that publishes no
        # `min_order_quantity` (an older backend) -- generic on purpose,
        # because the screen it lands back on is the basket, where each line
        # prints its own floor anyway.
        'SALES_ORDER_MIN_QTY': 'staff.sales.error.order_min_qty',
        # M30's ceiling, the mirror of SALES_ORDER_MIN_QTY -- same fallback
        # role behind its own detail-copy spec below.
        'SALES_ORDER_QTY_INVALID': 'staff.sales.error.order_qty',
        # L47(4): the schedule branch of `place_order`. No NEW copy -- the day
        # screen already owns the sentence for an unusable date, and the
        # handler re-shows that screen rather than leaving the agent on the
        # confirm card reading the generic 400 sentence.
        'SALES_DELIVERY_DATE_INVALID': 'staff.sales.visit.day_invalid',
        'SALES_PAYMENT_METHOD_INVALID': 'staff.error.api.agent_payment_method',
        # `AgentStatsService`'s own period guard (business_app/utils/local_windows.py):
        # the agent's KPI card only ever sends today/week/month, so this is the
        # backend refusing a value the bot did not offer -- an older bot build
        # or a direct API call -- and the copy names the three it does accept.
        'SALES_STATS_PERIOD_INVALID': 'staff.error.api.stats_period_invalid',
        'SALES_VISIT_OUTCOME_REQUIRED': 'staff.sales.error.outcome_required',
        'SALES_VISIT_OUTCOME_INVALID': 'staff.error.api.validation',
        # D17. The bot refuses a FORWARD client-side (a photo taken somewhere
        # else is not evidence about this shop, and a hash can only catch a
        # repeat); this code is everything the backend refuses -- a non-image,
        # or a file past the storage service's own ceiling, which is not a
        # number the bot knows.
        'SALES_PHOTO_INVALID': 'staff.sales.error.photo_invalid',
        # Try-out from the field (phase 2b). `SALES_TRYOUT_PHONE_REQUIRED`
        # reuses the activation path's sentence rather than seeding a second
        # one: both refusals have the same cause (a try-out upserts its trial
        # contact BY PHONE) and the same remedy (add a contact on the card),
        # and the handler follows the alert with the screen that points there.
        'SALES_TRYOUT_PHONE_REQUIRED': 'staff.sales.error.phone_required',
        'SALES_TRYOUT_ITEMS_INVALID': 'staff.sales.error.tryout_items',
        # Cash reconciliation (DriverReconciliationService.submit_session): a
        # zero/negative manual amount, or "settle everything" on a session with
        # nothing collected and no prior handoffs. Prod 2026-09-21: a driver
        # typed 0 three times and read the generic "check the entered data".
        'RECONCILIATION_AMOUNT_NOT_POSITIVE': 'staff.error.api.handoff_amount_not_positive',
        'RECONCILIATION_NOTHING_TO_SUBMIT': 'staff.error.api.nothing_to_reconcile',
        # CashCollectionService: the customer behind an at-door collection or a
        # statement lookup was deleted/merged out from under the driver's card.
        'COD_CUSTOMER_NOT_FOUND': 'staff.error.api.customer_not_found',
        # Operator flows (StaffService.create_client_user / create_phone_order).
        'STAFF_PHONE_INVALID': 'staff.operator.invalid_phone',
        # The address on the order is not one of this client's (a stale picker).
        'STAFF_INVALID_DELIVERY_ADDRESS': 'staff.error.api.address_not_customers',
        # `assert_order_address_for_status`: a phone order opens as CONFIRMED,
        # which needs a delivery address (the class default code,
        # INVALID_STATE_TRANSITION, named no fact the operator could act on).
        'ORDER_DELIVERY_ADDRESS_REQUIRED': 'staff.error.api.address_required',
        # `OrderService.create_order`, reached from the sales visit's own
        # order POST (`VisitService.place_order`) -- the same two refusals the
        # customer bot and admin already hit, now coded. The plain key is the
        # fallback; API_ERROR_DETAIL_COPY's spec below names the floor when
        # the backend publishes `min_amount`.
        'ORDER_MIN_AMOUNT': 'staff.error.api.order_min_amount',
        'ORDER_STOCK_UNAVAILABLE': 'staff.error.api.stock_unavailable',
        # Try-out task and pickup flows (TryoutService), reached both by a
        # driver's own try-out screens and by the sales agent's field try-out
        # (`OutletService` re-raises its OWN item refusals as
        # SALES_TRYOUT_ITEMS_INVALID before these are ever seen -- see that
        # code's comment above).
        'TRYOUT_TASK_NOT_FOUND': 'staff.error.api.tryout_task_not_found',
        'TRYOUT_NOT_FOUND': 'staff.error.api.tryout_not_found',
        'TRYOUT_TASK_TAKEN': 'staff.error.api.tryout_task_taken',
        'TRYOUT_TASK_COMPLETED': 'staff.error.api.tryout_task_completed',
        'TRYOUT_PICKUP_EXCEEDS_OUTSTANDING': 'staff.error.api.tryout_pickup_exceeds',
        'TRYOUT_PHONE_INVALID': 'staff.error.api.tryout_phone_invalid',
        'TRYOUT_PRODUCT_UNAVAILABLE': 'staff.error.api.tryout_product_unavailable',
    }
    API_ERROR_MESSAGE_KEY_MAP = {
        'telegram_id is required': 'staff.error.api.validation',
        'request body is required': 'staff.error.api.validation',
        'refresh token is required': 'staff.error.api.auth_failed',
        'status field is required': 'staff.error.api.validation',
        'latitude and longitude are required': 'staff.error.api.validation',
        'client_id is required': 'staff.error.api.validation',
        'user not found': 'staff.error.api.not_found',
        'delivery not found': 'staff.error.api.not_found',
        'order not found': 'staff.error.api.not_found',
        'client user not found': 'staff.error.api.not_found',
        'this delivery has already been accepted by another driver': 'staff.error.api.already_taken',
        'a user with this phone number already exists': 'staff.error.api.conflict',
        'phone is already used by another user': 'staff.error.api.conflict',
        'search query must be at least 2 characters': 'staff.error.api.invalid_input',
        "search_type must be 'phone' or 'name'": 'staff.error.api.invalid_input',
        'invite token is invalid, expired, or already used': 'staff.error.api.invalid_invite',
        'this telegram account is not approved for staff bot access': 'staff.error.api.forbidden',
        'user does not have a staff role': 'staff.error.api.forbidden',
        'this telegram account is already linked to another user': 'staff.error.api.conflict',
        'service temporarily unavailable': 'staff.error.api.service_unavailable',
        'request failed after retries': 'staff.error.api.service_unavailable',
        'authentication failed': 'staff.error.api.auth_failed',
        'access denied': 'staff.error.api.forbidden',
        'not found': 'staff.error.api.not_found',
        'conflict': 'staff.error.api.conflict',
    }

    # Codes whose copy names the backend's numbers (`details`). Tried in order;
    # the first spec whose fields are ALL present wins, so a code with two
    # shapes lists both. Falls back to API_ERROR_CODE_KEY_MAP when none fits
    # (an older backend, or a raise site that publishes no details).
    API_ERROR_DETAIL_COPY: Dict[str, Tuple[ErrorDetailCopy, ...]] = {
        'BOTTLE_SESSION_CAPACITY_EXCEEDED': (
            ErrorDetailCopy(
                'staff.error.api.bottle_session_capacity_exceeded_detail',
                {'available': 'available', 'required': 'required', 'shortfall': 'shortfall'},
            ),
        ),
        'STAFF_INVALID_STATUS_TRANSITION': (
            ErrorDetailCopy(
                'staff.error.api.status_transition_refused_detail',
                {'current_status': 'current_status', 'requested_status': 'requested_status'},
                {'current_status': 'delivery_status', 'requested_status': 'delivery_status'},
            ),
        ),
        'BOTTLE_TRANSFER_EXCEEDS_INVENTORY': (
            ErrorDetailCopy(
                'staff.error.api.transfer_exceeds_inventory_detail',
                {'requested': 'requested', 'available': 'available'},
            ),
        ),
        # Two shapes for one code (CashCollectionService.validate_customer_can_use_cod):
        # place scope publishes only a COUNT (the NET total is a coworker's
        # money, spec §7 privacy boundary); person scope publishes the
        # customer's own debt_total/debt_limit. Tried in order — a place-scope
        # refusal never has debt_total/debt_limit, so it falls through to the
        # count spec.
        'COD_DEBT_LIMIT_REACHED': (
            ErrorDetailCopy('staff.error.api.cod_debt_limit_place_detail', {'count': 'place_debt_count'}),
            ErrorDetailCopy(
                'staff.error.api.cod_debt_limit_amount_detail',
                {'debt_total': 'debt_total', 'debt_limit': 'debt_limit'},
                {'debt_total': 'money', 'debt_limit': 'money'},
            ),
        ),
        'ORDER_MIN_AMOUNT': (
            ErrorDetailCopy(
                'staff.error.api.order_min_amount_detail',
                {'min_amount': 'min_amount'},
                {'min_amount': 'money'},
            ),
        ),
        'SALES_ORDER_MIN_QTY': (
            ErrorDetailCopy(
                'staff.sales.error.order_min_qty_detail',
                {'product': 'product_name', 'minimum': 'min_order_quantity', 'quantity': 'quantity'},
            ),
        ),
        'SALES_ORDER_QTY_INVALID': (
            ErrorDetailCopy(
                'staff.sales.error.order_qty_detail',
                {'product': 'product_name', 'maximum': 'max_quantity', 'quantity': 'quantity'},
            ),
        ),
        'SALES_STOCK_QTY_INVALID': (
            ErrorDetailCopy(
                'staff.sales.error.stock_qty_detail', {'maximum': 'max'},
            ),
        ),
    }

    # A delivery in one of these can take no further step: a refusal naming
    # one is a stale card, rendered like STAFF_DELIVERY_NOT_OWNED.
    STALE_CARD_STATUSES = frozenset({'cancelled', 'failed', 'returned'})

    # Codes staff can act on from a button: (emoji, label key, callback_data).
    # A popup cannot carry buttons, so these always arrive as a chat message.
    API_ERROR_REMEDY_BUTTONS: Dict[str, Tuple[str, str, str]] = {
        # The accountability screen shows the open session with Return to
        # warehouse / Transfer / Incoming transfers — the only ways to get more
        # bottles onto the truck.
        'BOTTLE_SESSION_CAPACITY_EXCEEDED': ('📊', 'staff.menu.my_bottle_accountability', 'staff_bottle_my_accountability'),
        'BOTTLE_SESSION_REQUIRED': ('📊', 'staff.menu.my_bottle_accountability', 'staff_bottle_my_accountability'),
        # No open session of the driver's own: the accountability screen is
        # where "Log bottles loaded" opens one.
        'BOTTLE_SESSION_NOT_FOUND': ('📊', 'staff.menu.my_bottle_accountability', 'staff_bottle_my_accountability'),
        'BOTTLE_SESSION_REQUIRED_TO_INVITE': ('📊', 'staff.menu.my_bottle_accountability', 'staff_bottle_my_accountability'),
        'BOTTLE_SESSION_REQUIRED_TO_RECEIVE': ('📊', 'staff.menu.my_bottle_accountability', 'staff_bottle_my_accountability'),
    }

    @classmethod
    def error_copy_keys(cls) -> Set[str]:
        """Every key the error renderer can reach — `/health`'s and the seed
        guard's single source (staff_bot/i18n.py, test_staff_translation_catalog_complete)."""
        keys = set(cls.API_ERROR_CODE_KEY_MAP.values())
        for specs in cls.API_ERROR_DETAIL_COPY.values():
            keys.update(spec.key for spec in specs)
        keys.update(label for _emoji, label, _callback in cls.API_ERROR_REMEDY_BUTTONS.values())
        keys.add('staff.error.api.backend_reason')
        return keys

    def __init__(self):
        self.user_repo = StaffUserRepository(db_manager)

    async def _get_language(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Get user language from context or database."""
        raw_lang = context.user_data.get('language')
        if raw_lang:
            lang = i18n.normalize_language(raw_lang)
        else:
            lang = await i18n.get_user_language(update.effective_user.id)
        context.user_data['language'] = lang
        return lang

    async def _get_auth_token(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Get auth token from context, using token_manager for refresh if needed."""
        token_manager = context.bot_data.get('token_manager') if context.bot_data else None
        user_id = update.effective_user.id

        if token_manager:
            from staff_bot.api_client import api_client
            token = await token_manager.get_valid_token(user_id, api_client)
            if token:
                context.user_data['access_token'] = token
                return token

        # Fallback to token persisted in context only if it is still valid.
        fallback_token = context.user_data.get('access_token')
        if self._is_token_usable(fallback_token):
            return fallback_token
        if fallback_token:
            context.user_data.pop('access_token', None)

        # Attempt transparent re-authentication for pre-linked staff users.
        token = await self._authenticate_staff_session(update, context, token_manager)
        if token:
            return token

        if token_manager:
            try:
                await token_manager.invalidate_tokens(user_id)
            except Exception as e:
                logger.warning("Failed to invalidate cached staff tokens for user %s: %s", user_id, e)
        return None

    @staticmethod
    def _decode_jwt_expiry(token: str) -> Optional[int]:
        """Extract JWT exp claim without verifying signature."""
        try:
            parts = token.split('.')
            if len(parts) != 3:
                return None

            payload_b64 = parts[1]
            padding = 4 - len(payload_b64) % 4
            if padding != 4:
                payload_b64 += '=' * padding
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))
            exp = payload.get('exp')
            return int(exp) if exp is not None else None
        except Exception:
            return None

    def _is_token_usable(self, token: Optional[str]) -> bool:
        """Return True only when token has at least MIN_TOKEN_TTL_SECONDS remaining."""
        if not token:
            return False
        exp = self._decode_jwt_expiry(token)
        if exp is None:
            return False

        now = int(datetime.now(timezone.utc).timestamp())
        return (exp - now) > self.MIN_TOKEN_TTL_SECONDS

    @staticmethod
    def _normalize_staff_roles(staff_roles) -> list:
        """Normalize role payload to list[str]."""
        if isinstance(staff_roles, list):
            return staff_roles
        if isinstance(staff_roles, str):
            try:
                decoded = json.loads(staff_roles)
                if isinstance(decoded, list):
                    return decoded
                if isinstance(decoded, str) and decoded:
                    return [decoded]
            except Exception:
                if staff_roles:
                    return [staff_roles]
        return []

    async def _authenticate_staff_session(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        token_manager=None
    ) -> Optional[str]:
        """Re-authenticate staff by telegram_id and update runtime context."""
        from staff_bot.api_client import api_client

        user_id = update.effective_user.id
        try:
            async with api_client as client:
                response = await client.staff_login(user_id)
        except Exception as e:
            logger.warning("Staff re-auth request failed for user %s: %s", user_id, e)
            return None

        if not response.success:
            logger.warning(
                "Staff re-auth failed for user %s: status=%s error=%s",
                user_id, response.status_code, response.error
            )
            if response.status_code in (401, 403, 404):
                context.user_data['authenticated'] = False
            # A switched-off account: "session expired" would send them to
            # /start, which is refused the same way. Keep the real reason.
            auth_refusals.remember(user_id, getattr(response, 'error_code', None))
            return None

        data = response.data or {}
        access_token = data.get('access_token')
        refresh_token = data.get('refresh_token')
        user_data = data.get('user') or {}

        if not access_token:
            logger.warning("Staff re-auth succeeded without access_token for user %s", user_id)
            return None

        staff_roles = self._normalize_staff_roles(user_data.get('staff_roles'))

        auth_refusals.forget(user_id)
        context.user_data['authenticated'] = True
        context.user_data['access_token'] = access_token
        if user_data.get('id') is not None:
            context.user_data['user_id'] = user_data.get('id')
        if user_data.get('first_name') is not None:
            context.user_data['first_name'] = user_data.get('first_name') or ''
        if user_data.get('last_name') is not None:
            context.user_data['last_name'] = user_data.get('last_name') or ''
        if user_data.get('phone') is not None:
            context.user_data['phone'] = user_data.get('phone') or ''
        if user_data.get('delivery_person_id') is not None:
            context.user_data['delivery_person_id'] = user_data.get('delivery_person_id')
        context.user_data['staff_roles'] = staff_roles
        preferred_language = user_data.get('preferred_language')
        if preferred_language:
            context.user_data['language'] = preferred_language

        if token_manager and refresh_token:
            try:
                await token_manager.store_tokens(
                    user_id, access_token, refresh_token, data.get('expires_in', 3600)
                )
            except Exception as e:
                logger.warning("Failed to cache re-auth tokens for user %s: %s", user_id, e)

        return access_token

    # ------------------------------------------------------------------
    # The stale-card guard.
    #
    # Every staff card is its own Telegram message; PTB gives the driver ONE
    # `context.user_data`, and `current_delivery` is a single overwritten key.
    # "Act on the delivery whose button was tapped" is therefore a rule of the
    # BOT, not of one handler group — the money handlers needed it first, then
    # Navigate, and the next screen that reads the snapshot will need it too.
    # It lives here so a caller inherits it instead of instantiating a sibling
    # handler to borrow its privates (which is how a second copy starts).
    # ------------------------------------------------------------------

    async def _anchor_current_delivery(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        delivery_id,
    ):
        """Re-anchor ``current_delivery`` on the delivery the driver TAPPED.

        Each active-delivery card is its own Telegram message, but PTB gives a
        driver ONE ``context.user_data`` and ``current_delivery`` is a single
        overwritten key. A driver who opens B and then acts on A's older —
        still perfectly live — card used to drive A's completion with B's
        anchor: B's "All N returned" posted against A's place, and A's screen
        titled with B's order number.

        Every handler that reads the snapshot must therefore compare it against
        the id in the callback and, on a mismatch, re-read the tapped delivery
        from ``/delivery/active`` (the same source ``view_active_delivery``
        builds the card from) rather than trust the stale one.

        Returns the snapshot to act on, or ``None`` when the tapped delivery can
        no longer be found — the caller MUST refuse to act on that.

        A MISSING snapshot re-reads, it does not pass. This used to return the
        empty dict on the stated grounds that "there is nothing to compare, and
        refusing there would strand drivers mid-trip on a deploy". Both halves
        were right; the conclusion was not. ``{}`` is not a neutral answer to
        the questions the callers then ask it — ``get_cod_cash_projection({})``
        is ``0.0`` and ``has_cash_due({})`` is ``False`` — so a button reading
        "✅ Cash collected: 150 000" filed a 0.00 collection, and the
        "Delivered" confirm dropped the cash and bottle steps entirely, on an
        order that then has no redo (``DELIVERY_STATUS_TRANSITIONS['delivered']``
        is empty). Re-reading from ``/delivery/active`` strands nobody: it
        REBUILDS the snapshot the deploy lost, and only returns ``None`` — the
        refusal — when the delivery genuinely is not on the driver's list any
        more, which is exactly when acting on it would be wrong.

        Reachable in two steps rather than one, which is why it survived: a bare
        post-restart callback is refused by ``@require_auth`` first. The driver
        taps a reply-keyboard menu button (the only path wired to
        ``StaffBot._recover_session``), which restores ``authenticated`` but not
        ``current_delivery``, and THEN taps the card still sitting in the chat.
        tests/staff_bot/test_at_door_money_after_state_loss.py
        """
        info = context.user_data.get('current_delivery') or {}
        current_id = info.get('delivery_id')
        if delivery_id is None or current_id == delivery_id:
            return info

        logger.info(
            "current_delivery snapshot (%s) does not match the tapped delivery "
            "(%s); re-anchoring from /delivery/active",
            current_id, delivery_id,
        )
        token = await self._get_auth_token(update, context)
        if not token:
            return None

        async with api_client as client:
            response = await client.get_active_deliveries(token)
        if not response.success:
            return None

        data = response.data
        rows = data if isinstance(data, list) else (data or {}).get('items', [])
        for row in rows or []:
            if (row.get('delivery_id') or row.get('id')) != delivery_id:
                continue
            # The row IS the card payload `view_active_delivery` whitelists
            # from; copy it wholesale and add only the derived keys that
            # handler renames, so this can never fall behind that whitelist.
            snapshot = dict(row)
            snapshot['delivery_id'] = delivery_id
            snapshot.setdefault('origin_lat', row.get('current_location_lat'))
            snapshot.setdefault('origin_lng', row.get('current_location_lng'))
            snapshot.setdefault('destination_lat', row.get('destination_latitude'))
            snapshot.setdefault('destination_lng', row.get('destination_longitude'))
            context.user_data['current_delivery'] = snapshot
            return snapshot
        return None

    async def _require_flow(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                            key: str):
        """The named flow's working dict, or ``None`` when it is gone.

        THE guard for every multi-step staff flow. Each keeps its half-built
        record under one ``user_data`` key — ``new_client``, ``new_order``,
        ``new_address``, ``new_tryout`` — and every step wrote into it as
        ``context.user_data['new_address']['title'] = label``. That looks like a
        write; it is a READ of the parent followed by a write into whatever came
        back, so a missing parent raises ``KeyError``.

        It is reachable without any deploy, and it takes TWO conversations —
        a menu tap on its own ends the conversation and clears the keys
        together, which strands nothing. ``staff_add_address`` is entered by a
        CALLBACK, so it never passes through the text router's
        menu-detect-and-clear: an operator can be inside *Create client* AND
        *Add address* at once, and one menu tap then ends only the former while
        ``flow_state.clear_pending_flows`` drops the GLOBAL key set —
        ``new_address`` included. The address conversation is left parked at
        ENTER_LABEL with its dict gone.

        What made that unrecoverable rather than merely annoying: the exception
        escaped the handler, so PTB neither advanced the state nor re-armed the
        timeout job it pops at the top of ``handle_update``. The operator was
        pinned to the prompt permanently — every retype failed identically and
        the flow could not even expire itself. So callers must END on ``None``,
        not simply return.

        ``staff.flow_timed_out`` is reused verbatim rather than seeded anew:
        "Nothing was saved — start again when you are ready" is precisely what
        happened, and a new key would be one more thing to seed at deploy.
        tests/staff_bot/test_operator_flows_after_state_loss.py
        """
        flow = (context.user_data or {}).get(key)
        if isinstance(flow, dict):
            return flow

        logger.info(
            "Flow %r is gone for user %s; ending it rather than raising.",
            key, update.effective_user.id if update.effective_user else None,
        )
        language = await self._get_language(update, context)
        text = i18n.get('staff.flow_timed_out', language)
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(text, parse_mode='HTML')
        elif update.message:
            await update.message.reply_text(text, parse_mode='HTML')
        return None

    async def _refuse_stale_card(self, update: Update, language: str, *, text: Optional[str] = None):
        """The tapped delivery is gone from the driver's active list.

        Acting on the snapshot that happens to be loaded is exactly the bug this
        guard exists to stop, so say so and send them back to the list.
        ``text`` replaces the sentence when the backend said WHY (the stale-card
        refusals in ``_handle_api_response_error``).
        The screen and the way back stay the same.
        """
        text = text or i18n.get('staff.delivery.not_found', language)
        keyboard = CommonKeyboards.back_button(language, "staff_active_deliveries")
        if update.callback_query:
            await update.callback_query.edit_message_text(
                text, reply_markup=keyboard, parse_mode='HTML'
            )
        else:
            await update.message.reply_text(
                text, reply_markup=keyboard, parse_mode='HTML'
            )

    async def _handle_auth_error(self, update: Update, language: str):
        """Tell the user their session is gone — and why, when the backend
        refused to re-establish it because the account is switched off."""
        key = auth_refusals.session_lost_key(getattr(update.effective_user, 'id', None))
        await self._notify_user(update, i18n.get(key, language), show_alert=True)

    def _resolve_api_error_message(
        self,
        language: str,
        error: Optional[str] = None,
        status_code: Optional[int] = None,
        error_code: Optional[str] = None,
        *,
        details=None,
        server_message: Optional[str] = None,
        error_type: Optional[str] = None,
        html: bool = False,
    ) -> str:
        """Resolve a backend refusal into the sentence staff read.

        Order: copy with the backend's numbers → the code's copy → a legacy
        message match → the backend's own reason (4xx business refusals only)
        → a status-based sentence. Plain text; with ``html=True`` interpolated
        values are escaped for a ``parse_mode='HTML'`` screen.
        """
        if error_code:
            code = str(error_code)
            for spec in self.API_ERROR_DETAIL_COPY.get(code, ()):
                rendered = render_detail_copy(spec, details, language, html=html)
                if rendered is not None:
                    return rendered
            key = self.API_ERROR_CODE_KEY_MAP.get(code)
            if key:
                return i18n.get(key, language)

        normalized_error = (error or '').strip().lower() if isinstance(error, str) else ''
        if normalized_error:
            key = self.API_ERROR_MESSAGE_KEY_MAP.get(normalized_error)
            if key:
                return i18n.get(key, language)
            if normalized_error.startswith('staff.'):
                return i18n.get(normalized_error, language)

        reason = displayable_backend_reason(status_code, error_type, server_message)
        if reason:
            return i18n.get(
                'staff.error.api.backend_reason', language,
                reason=escape(reason, quote=False) if html else reason,
            )

        if status_code == 400:
            return i18n.get('staff.error.api.validation', language)
        if status_code == 401:
            return i18n.get('staff.error.api.auth_failed', language)
        if status_code == 403:
            return i18n.get('staff.error.api.forbidden', language)
        if status_code == 404:
            return i18n.get('staff.error.api.not_found', language)
        if status_code == 409:
            return i18n.get('staff.error.api.conflict', language)
        if status_code == 422:
            return i18n.get('staff.error.api.invalid_input', language)
        if status_code == 429:
            return i18n.get('staff.error.api.rate_limited', language)
        if isinstance(status_code, int) and status_code >= 500:
            return i18n.get('staff.error.api.service_unavailable', language)

        return i18n.get('staff.error.api.unexpected', language)

    def _resolve_response_error(self, language: str, response, *, html: bool = False) -> str:
        """`_resolve_api_error_message` for an APIResponse — every field it has."""
        return self._resolve_api_error_message(
            language,
            getattr(response, 'error', None),
            status_code=getattr(response, 'status_code', None),
            error_code=getattr(response, 'error_code', None),
            details=getattr(response, 'details', None),
            server_message=getattr(response, 'server_message', None),
            error_type=getattr(response, 'error_type', None),
            html=html,
        )

    def _remedy_keyboard(self, language: str, error_code) -> Optional[InlineKeyboardMarkup]:
        spec = self.API_ERROR_REMEDY_BUTTONS.get(str(error_code)) if error_code else None
        if not spec:
            return None
        emoji, label_key, callback_data = spec
        return InlineKeyboardMarkup([[InlineKeyboardButton(
            f"{emoji} {i18n.get(label_key, language)}", callback_data=callback_data,
        )]])

    def _with_remedy(self, language: str, error_code, markup=None):
        """`markup` with the error code's remedy row (if it has one) placed
        first — for error screens that edit in place with their own keyboard,
        so the remedy rule lives only in API_ERROR_REMEDY_BUTTONS."""
        remedy = self._remedy_keyboard(language, error_code)
        if remedy is None:
            return markup
        rows = list(remedy.inline_keyboard)
        if markup is not None:
            rows += [list(row) for row in markup.inline_keyboard]
        return InlineKeyboardMarkup(rows)

    async def _handle_api_error(
        self,
        update: Update,
        error: Optional[str],
        language: str,
        *,
        status_code: Optional[int] = None,
        error_code: Optional[str] = None,
        details=None,
        server_message: Optional[str] = None,
        error_type: Optional[str] = None,
    ):
        """Tell staff why the backend refused, with a way out when there is one."""
        text = self._resolve_api_error_message(
            language, error, status_code, error_code,
            details=details, server_message=server_message, error_type=error_type,
        )
        await self._notify_user(
            update, f"❌ {text}", show_alert=True,
            reply_markup=self._remedy_keyboard(language, error_code),
        )

    @staticmethod
    def _status_already_applied(response) -> bool:
        """The status change was refused because the delivery is ALREADY in the
        requested status: a replay, not a failure.

        A double tap (updates in one chat are processed one at a time, so the
        second PUT lands after the first committed) or the client's retry of a
        PUT whose acknowledgement was lost. The backend names both statuses on
        STAFF_INVALID_STATUS_TRANSITION (``StaffService.update_delivery_status``);
        an older backend names neither, and this answers False.
        """
        if getattr(response, 'error_code', None) != 'STAFF_INVALID_STATUS_TRANSITION':
            return False
        details = getattr(response, 'details', None)
        if not isinstance(details, dict):
            return False
        current = details.get('current_status')
        return current is not None and current == details.get('requested_status')

    async def _handle_api_response_error(
        self,
        update: Update,
        response,
        language: str,
        context: Optional[ContextTypes.DEFAULT_TYPE] = None,
    ):
        """Handle API response object error.

        ``context`` is passed by the three ``update_delivery_status`` call sites
        in ``StatusUpdateHandler``, the only screens that can meet
        ``STAFF_DELIVERY_NOT_OWNED``, or ``STAFF_INVALID_STATUS_TRANSITION``
        naming a ``current_status`` in ``STALE_CARD_STATUSES``. Both refusals
        mean a STALE CARD, not bad input. Dispatch rescheduled, reassigned or
        pooled the delivery after the driver opened it, or the order was
        cancelled under it (the cancel cascade leaves the CANCELLED delivery on
        the driver, so the ownership check passes and only the transition guard
        refuses), and the backend wrote nothing. An alert alone would
        leave ``current_delivery`` naming a stop the driver no longer holds. It
        would also leave the at-door cash flow armed, so the next amount they
        typed would be submitted again.

        So the handler forgets the snapshot and leaves the flow through THE exit
        every other screen uses (``flow_state.clear_pending_flows``, which also
        drops the Redis mirror and drains deferred pool offers). It then answers
        the way ``_refuse_stale_card`` does: one sentence and the way back to
        the active list. ``current_delivery`` is popped by hand because it is
        session state that ``clear_pending_flows`` deliberately keeps. Here the
        snapshot itself is what went stale. Every other caller keeps the alert.

        A refusal naming current == requested is a replay of a change that DID
        land (``_status_already_applied``), never a stale card; the callers
        render it as the success it is before reaching here.
        """
        error_code = getattr(response, 'error_code', None)
        details = getattr(response, 'details', None)
        terminal = (
            error_code == 'STAFF_INVALID_STATUS_TRANSITION'
            and isinstance(details, dict)
            and details.get('current_status') in self.STALE_CARD_STATUSES
            and not self._status_already_applied(response)
        )
        if context is not None and (error_code == 'STAFF_DELIVERY_NOT_OWNED' or terminal):
            context.user_data.pop('current_delivery', None)
            await flow_state.clear_pending_flows(context, update)
            await self._refuse_stale_card(
                update,
                language,
                text=self._resolve_response_error(language, response, html=True),
            )
            return
        await self._handle_api_error(
            update,
            getattr(response, 'error', None),
            language,
            status_code=getattr(response, 'status_code', None),
            error_code=error_code,
            details=details,
            server_message=getattr(response, 'server_message', None),
            error_type=getattr(response, 'error_type', None),
        )

    async def _handle_error(self, update: Update, context: ContextTypes.DEFAULT_TYPE = None):
        """Handle general error with language fallback."""
        try:
            language = await i18n.get_user_language(update.effective_user.id)
            error_msg = i18n.get('staff.error_occurred', language)
        except Exception:
            # The DB lookup failed, not the user's preference — fall back to the
            # deployment default (uz), not English. `normalize_language(None)`
            # is the SSOT for "no language known".
            error_msg = i18n.get('staff.error_occurred', i18n.normalize_language(None))

        await self._notify_user(update, error_msg, show_alert=True)

    async def _notify_user(self, update: Update, message: str, show_alert: bool = False, *, reply_markup=None):
        """Put ``message`` in front of the user: a popup when Telegram will still
        show one, otherwise a chat message.

        Telegram shows only the FIRST answer to a tap. Most handlers answer bare
        on entry, so an error known later cannot be a popup any more — sending
        it as one is how a driver tapped a refused "Picked up" 20 times on
        2026-09-10 without ever seeing why. A popup also cannot carry buttons
        (``reply_markup``) or exceed ``TELEGRAM_ALERT_MAX_CHARS``.
        """
        callback_query = update.callback_query
        if callback_query:
            popup_possible = (
                reply_markup is None
                and len(message) <= self.TELEGRAM_ALERT_MAX_CHARS
                and not callback_already_answered(callback_query)
            )
            if popup_possible and await self._safe_callback_answer(
                callback_query, message, show_alert=show_alert
            ):
                return
            # Stop the spinner if nobody has; a no-op when already answered.
            await self._safe_callback_answer(callback_query, None, show_alert=False)
            fallback_message = callback_query.message
            if isinstance(fallback_message, Message):
                await self._safe_reply_text(fallback_message, message, reply_markup=reply_markup)
            elif update.effective_chat:
                # An InaccessibleMessage has no `reply_text` in PTB 22, but its
                # chat is still there, and dropping the error is how a tap goes
                # silent. (A tap with no message at all is an inline-message tap:
                # PTB gives it no chat, so there is nowhere to send.)
                await self._safe_send_to_chat(update.effective_chat, message, reply_markup=reply_markup)
            return

        if update.message:
            await self._safe_reply_text(update.message, message, reply_markup=reply_markup)

    async def _safe_callback_answer(self, callback_query, message: str, show_alert: bool) -> bool:
        """Answer a tap, retrying only transient network failures.

        Returns False when ``message`` could not be shown as an answer, so the
        caller can fall back to a chat message. A tap that is already answered
        needs no further acknowledgement (True for ``message=None``) and cannot
        show text (False).
        """
        if callback_already_answered(callback_query):
            return message is None
        for attempt in range(1, self.TELEGRAM_RETRY_ATTEMPTS + 1):
            try:
                await callback_query.answer(message, show_alert=show_alert)
                return True
            except BadRequest as e:
                # Deterministic ("query is too old", "message is too long").
                # BadRequest subclasses NetworkError in PTB 22, so without this
                # clause it was retried after a pointless 0.5 s sleep.
                logger.warning("Telegram refused the callback answer: %s", e)
                break
            except (TimedOut, NetworkError) as e:
                if attempt < self.TELEGRAM_RETRY_ATTEMPTS:
                    await asyncio.sleep(self.TELEGRAM_RETRY_DELAY_SECONDS * attempt)
                    continue
                logger.warning("Failed to answer callback query after retries: %s", e)
            except TelegramError as e:
                logger.warning("Telegram error while answering callback query: %s", e)
                break
            except Exception as e:
                logger.error("Unexpected error while answering callback query: %s", e, exc_info=True)
                break
        return False

    async def _safe_reply_text(self, target_message, message: str, reply_markup=None):
        """Reply to ``target_message``, retrying transient network failures.

        Silent (``disable_notification``), the bot's convention for
        driver-facing messages (staff_bot/bot.py).
        """
        await self._send_with_retries(lambda: target_message.reply_text(
            message, reply_markup=reply_markup, disable_notification=True,
        ))

    async def _safe_send_to_chat(self, chat, message: str, reply_markup=None):
        """`_safe_reply_text` for a chat with no message to reply to."""
        await self._send_with_retries(lambda: chat.send_message(
            message, reply_markup=reply_markup, disable_notification=True,
        ))

    async def _send_with_retries(self, send):
        """Await ``send()``, retrying transient network failures; log, never raise."""
        for attempt in range(1, self.TELEGRAM_RETRY_ATTEMPTS + 1):
            try:
                await send()
                return
            except (TimedOut, NetworkError) as e:
                if attempt < self.TELEGRAM_RETRY_ATTEMPTS:
                    await asyncio.sleep(self.TELEGRAM_RETRY_DELAY_SECONDS * attempt)
                    continue
                logger.warning("Failed to send reply message after retries: %s", e)
            except TelegramError as e:
                logger.warning("Telegram error while sending reply message: %s", e)
                return
            except Exception as e:
                logger.error("Unexpected error while sending reply message: %s", e, exc_info=True)
                return
