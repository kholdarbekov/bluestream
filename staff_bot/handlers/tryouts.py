"""Driver-facing try-out task handlers for the staff bot."""

import logging
from decimal import Decimal, InvalidOperation
from typing import Dict, Optional

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

from shared.constants import is_within_tashkent
from staff_bot.api_client import api_client
from staff_bot.handlers.base import BaseHandler
from staff_bot.i18n import i18n
from staff_bot.keyboards.common import CommonKeyboards
from staff_bot.keyboards.menu import MenuKeyboards
from staff_bot.keyboards.tryouts import TryoutKeyboards
from staff_bot.permissions import require_auth, require_delivery_driver
from staff_bot.utils import flow_state
from staff_bot.utils.formatters import escape_html, format_quantity
from staff_bot.utils.validators import validate_name, validate_phone


logger = logging.getLogger(__name__)

ENTER_TRYOUT_PHONE, ENTER_TRYOUT_NAME, ENTER_TRYOUT_ADDRESS = range(90, 93)


class TryoutHandler(BaseHandler):
    """List and execute try-out tasks for delivery drivers."""

    @require_auth
    @require_delivery_driver
    async def show_hub(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show the try-outs sub-menu (Create / Tasks / Active)."""
        language = await self._get_language(update, context)
        title = f"🧪 <b>{i18n.get('staff.tryouts.hub_title', language)}</b>"
        keyboard = MenuKeyboards.tryouts_hub(language)

        try:
            if update.callback_query:
                await update.callback_query.answer()
                await update.callback_query.edit_message_text(
                    title, reply_markup=keyboard, parse_mode='HTML'
                )
            else:
                await update.message.reply_text(
                    title, reply_markup=keyboard, parse_mode='HTML'
                )
        except Exception as e:
            logger.error(f"Error showing try-outs hub: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_delivery_driver
    async def start_create_tryout(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        context.user_data.pop('new_tryout', None)
        context.user_data['new_tryout'] = {'items': []}

        prompt = i18n.get('staff.tryout.enter_phone', language)
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(prompt)
        else:
            await update.message.reply_text(prompt)
        return ENTER_TRYOUT_PHONE

    @require_auth
    @require_delivery_driver
    async def receive_create_phone(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        valid, result = validate_phone((update.message.text or '').strip())
        if not valid:
            await update.message.reply_text(i18n.get('staff.operator.invalid_phone', language))
            return ENTER_TRYOUT_PHONE

        context.user_data['new_tryout']['phone'] = result
        await update.message.reply_text(i18n.get('staff.tryout.enter_name', language))
        return ENTER_TRYOUT_NAME

    @require_auth
    @require_delivery_driver
    async def receive_create_name(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        name = (update.message.text or '').strip()
        valid, _ = validate_name(name)
        if not valid:
            await update.message.reply_text(i18n.get('staff.operator.invalid_name', language))
            return ENTER_TRYOUT_NAME

        context.user_data['new_tryout']['first_name'] = name
        await update.message.reply_text(
            i18n.get('staff.tryout.enter_address_or_location', language),
            reply_markup=CommonKeyboards.location_request(
                language,
                i18n.get('staff.tryout.send_location', language),
            ),
        )
        return ENTER_TRYOUT_ADDRESS

    @require_auth
    @require_delivery_driver
    async def receive_create_address(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        address = (update.message.text or '').strip()
        if address == i18n.get('staff.cancel', language):
            return await self.cancel_create_tryout(update, context)
        if len(address) < 5:
            await update.message.reply_text(i18n.get('staff.tryout.invalid_address', language))
            return ENTER_TRYOUT_ADDRESS

        context.user_data['new_tryout']['full_address'] = address
        await update.message.reply_text(
            i18n.get('staff.tryout.address_received', language),
            reply_markup=MenuKeyboards.main_menu(language, context.user_data.get('staff_roles', [])),
        )
        await self._show_product_selection(update, context, use_message=True)
        return ConversationHandler.END

    @require_auth
    @require_delivery_driver
    async def receive_create_location(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END

        location = update.message.location if update.message else None
        if not location:
            await update.message.reply_text(i18n.get('staff.tryout.invalid_address', language))
            return ENTER_TRYOUT_ADDRESS

        # Enforce the delivery-zone SSOT (TASHKENT_POLYGON) before accepting.
        # The backend re-validates authoritatively; this gives instant, localized UX.
        if not is_within_tashkent(location.latitude, location.longitude):
            await update.message.reply_text(
                i18n.get('staff.tryout.outside_delivery_area', language),
            )
            return ENTER_TRYOUT_ADDRESS

        context.user_data['new_tryout']['latitude'] = location.latitude
        context.user_data['new_tryout']['longitude'] = location.longitude

        async with api_client as client:
            response = await client.reverse_geocode_address(token, location.latitude, location.longitude)

        if not response.success or not isinstance(response.data, dict) or not response.data.get('formatted_address'):
            await update.message.reply_text(
                i18n.get('staff.tryout.location_geocode_failed', language),
                reply_markup=CommonKeyboards.location_request(
                    language, i18n.get('staff.tryout.send_location', language)
                ),
            )
            return ENTER_TRYOUT_ADDRESS

        context.user_data['new_tryout']['full_address'] = response.data['formatted_address']
        context.user_data['new_tryout']['district'] = response.data.get('district')
        context.user_data['new_tryout']['city'] = response.data.get('city') or 'Tashkent'
        await update.message.reply_text(
            i18n.get('staff.tryout.location_received', language, address=response.data['formatted_address']),
            reply_markup=MenuKeyboards.main_menu(language, context.user_data.get('staff_roles', [])),
        )
        await self._show_product_selection(update, context, use_message=True)
        return ConversationHandler.END

    async def _fetch_tryout_products(self, token: str):
        async with api_client as client:
            response = await client.get_products(token)
        if not response.success:
            return response, []

        items = response.data.get('items', []) if isinstance(response.data, dict) else []
        eligible = [
            product for product in items
            if product.get('is_active') is not False and product.get('is_tryout_eligible', True)
        ]
        return response, eligible

    @staticmethod
    def _as_decimal(value) -> Decimal:
        try:
            return Decimal(str(value or 0))
        except (InvalidOperation, TypeError, ValueError):
            return Decimal('0')

    @staticmethod
    def _create_selected_quantities(payload: dict) -> Dict[int, int]:
        quantities: Dict[int, int] = {}
        for item in payload.get('items', []):
            product_id = int(item.get('product_id'))
            quantities[product_id] = quantities.get(product_id, 0) + int(item.get('quantity') or 0)
        return quantities

    @staticmethod
    def _pickup_selected_map(state: dict) -> Dict[int, str]:
        raw_selected = state.get('selected', {})
        normalized: Dict[int, str] = {}
        for product_id, units in raw_selected.items():
            normalized[int(product_id)] = str(units)
        state['selected'] = normalized
        return normalized

    def _get_pickup_product(self, state: dict, product_id: int) -> Optional[dict]:
        return next(
            (row for row in state.get('products', []) if int(row.get('product_id')) == int(product_id)),
            None,
        )

    def _find_open_pickup_task_id(self, tryout: dict) -> Optional[int]:
        for task in tryout.get('tasks') or []:
            if task.get('task_type') == 'pickup' and task.get('status') in {'open', 'assigned'}:
                return int(task.get('id'))
        return None

    async def _clear_pickup_state(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        update: Update = None,
    ) -> None:
        """Clear the tryout-pickup flow flags plus the Redis mirror, and
        deliver any pool-insertion suggestions deferred while the driver was
        recording pickup quantities. `update` is optional for legacy callers
        that only have `context`."""
        context.user_data.pop('tryout_pickup_task_id', None)
        context.user_data.pop('tryout_pickup_products', None)
        context.user_data.pop('tryout_pickup_state', None)
        if update and update.effective_user:
            language = context.user_data.get('language') if context else None
            await flow_state.clear_and_drain(
                update.effective_user.id, context.bot, language=language
            )

    def _build_pickup_state(self, task: dict) -> dict:
        return {
            'task_id': int(task.get('id')),
            'tryout_id': int(task.get('tryout_id')),
            'tryout_number': task.get('tryout_number') or 'Try-out',
            'products': [
                {
                    'product_id': int(row.get('product_id')),
                    'product_name': row.get('product_name'),
                    'units': format_quantity(row.get('units')),
                }
                for row in task.get('outstanding_bottle_products') or []
            ],
            'selected': {},
        }

    def _build_pickup_overview(self, language: str, state: dict) -> str:
        selected = self._pickup_selected_map(state)
        lines = [
            f"♻️ <b>{escape_html(state.get('tryout_number'))}</b>",
            i18n.get('staff.tryout.pickup_select_product', language),
            "",
        ]

        for row in state.get('products', []):
            product_id = int(row.get('product_id'))
            selected_units = selected.get(product_id)
            selected_text = (
                i18n.get(
                    'staff.tryout.pickup_selected',
                    language,
                    selected=format_quantity(selected_units),
                )
                if selected_units
                else i18n.get('staff.tryout.pickup_not_selected', language)
            )
            lines.append(
                f"• {escape_html(row.get('product_name'))}: "
                f"{format_quantity(row.get('units'))} "
                f"({escape_html(selected_text)})"
            )

        return "\n".join(lines)

    async def _show_product_selection(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        use_message: bool = False,
    ):
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        response, products = await self._fetch_tryout_products(token)
        if not response.success:
            logger.warning(
                "Tryout product load failed after address capture: user=%s status=%s error_code=%s error=%s",
                update.effective_user.id if update and update.effective_user else None,
                getattr(response, 'status_code', None),
                getattr(response, 'error_code', None),
                getattr(response, 'error', None),
            )
            await self._handle_api_response_error(update, response, language)
            return

        context.user_data['new_tryout_products'] = products
        selected_quantities = self._create_selected_quantities(context.user_data.get('new_tryout', {}))
        text = i18n.get('staff.tryout.select_products', language)
        if use_message:
            await update.message.reply_text(
                text,
                reply_markup=TryoutKeyboards.product_list(language, products, selected_quantities)
            )
        else:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(
                text,
                reply_markup=TryoutKeyboards.product_list(language, products, selected_quantities)
            )

    @require_auth
    @require_delivery_driver
    async def show_create_products(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._show_product_selection(update, context)

    @require_auth
    @require_delivery_driver
    async def select_create_product(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)

        product_id = int(query.data.split('_')[-1])
        products = context.user_data.get('new_tryout_products', [])
        product = next((item for item in products if int(item.get('id')) == product_id), None)
        if not product:
            await query.edit_message_text(
                i18n.get('staff.tryout.product_not_found', language),
                reply_markup=CommonKeyboards.back_button(language, "staff_tryout_select_products"),
            )
            return

        selected_quantity = self._create_selected_quantities(context.user_data.get('new_tryout', {})).get(product_id, 0)
        await query.edit_message_text(
            "\n".join([
                i18n.get('staff.tryout.select_quantity', language, product=product.get('name')),
                i18n.get('staff.tryout.current_quantity', language, quantity=selected_quantity),
            ]),
            reply_markup=TryoutKeyboards.quantity_selection(language, product_id, selected_quantity),
        )

    @require_auth
    @require_delivery_driver
    async def select_create_quantity(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)

        _, _, _, product_id_raw, quantity_raw = query.data.split('_')
        product_id = int(product_id_raw)
        quantity = int(quantity_raw)
        product = next(
            (item for item in context.user_data.get('new_tryout_products', []) if int(item.get('id')) == product_id),
            None,
        )
        if not product:
            await query.edit_message_text(
                i18n.get('staff.tryout.product_not_found', language),
                reply_markup=CommonKeyboards.back_button(language, "staff_tryout_select_products"),
            )
            return

        items = context.user_data['new_tryout'].setdefault('items', [])
        existing = next((item for item in items if int(item['product_id']) == product_id), None)
        if existing:
            existing['quantity'] += quantity
        else:
            items.append({
                'product_id': product_id,
                'quantity': quantity,
                'product_name': product.get('name'),
            })

        await query.edit_message_text(
            self._build_create_summary(language, context.user_data['new_tryout']),
            parse_mode='HTML',
            reply_markup=TryoutKeyboards.create_summary(language),
        )

    @require_auth
    @require_delivery_driver
    async def remove_create_product(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        language = await self._get_language(update, context)

        product_id = int(query.data.split('_')[-1])
        payload = context.user_data.get('new_tryout', {})
        items = payload.get('items', [])
        remaining_items = [item for item in items if int(item.get('product_id')) != product_id]
        payload['items'] = remaining_items
        context.user_data['new_tryout'] = payload

        if not remaining_items:
            await self._show_product_selection(update, context)
            return

        await query.answer()
        await query.edit_message_text(
            self._build_create_summary(language, payload),
            parse_mode='HTML',
            reply_markup=TryoutKeyboards.create_summary(language),
        )

    def _build_create_summary(self, language: str, payload: dict) -> str:
        lines = [
            f"🧪 <b>{i18n.get('staff.tryout.confirm_create_title', language)}</b>",
            f"👤 {escape_html(payload.get('first_name'))}",
            f"📞 {escape_html(payload.get('phone'))}",
            f"📍 {escape_html(payload.get('full_address'))}",
        ]
        if payload.get('latitude') is not None and payload.get('longitude') is not None:
            lines.append(f"🗺️ {payload.get('latitude')}, {payload.get('longitude')}")
        lines.extend([
            "",
            f"<b>{i18n.get('staff.tryout.selected_products', language)}</b>",
        ])
        for item in payload.get('items', []):
            lines.append(f"• {escape_html(item.get('product_name'))} x{item.get('quantity')}")
        return "\n".join(lines)

    @require_auth
    @require_delivery_driver
    async def finish_product_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        payload = context.user_data.get('new_tryout', {})
        if not payload.get('items'):
            await query.edit_message_text(
                i18n.get('staff.tryout.no_products_selected', language),
                reply_markup=CommonKeyboards.back_button(language, "staff_tryout_select_products"),
            )
            return

        await query.edit_message_text(
            self._build_create_summary(language, payload),
            parse_mode='HTML',
            reply_markup=TryoutKeyboards.create_summary(language),
        )

    @require_auth
    @require_delivery_driver
    async def confirm_create_tryout(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return ConversationHandler.END

        payload = context.user_data.get('new_tryout', {})
        request_payload = {
            'trial_contact': {
                'first_name': payload.get('first_name'),
                'phone': payload.get('phone'),
                'preferred_language': language,
            },
            'address': {
                'label': 'Try-out',
                'full_address': payload.get('full_address'),
                'district': payload.get('district'),
                'city': payload.get('city') or 'Tashkent',
                'latitude': payload.get('latitude'),
                'longitude': payload.get('longitude'),
                'is_default': True,
            },
            'items': [
                {'product_id': item['product_id'], 'quantity': item['quantity']}
                for item in payload.get('items', [])
            ],
            'complete_handoff': True,
        }

        async with api_client as client:
            response = await client.create_tryout(token, request_payload)
        if not response.success:
            await self._handle_api_response_error(update, response, language)
            return ConversationHandler.END

        tryout = response.data.get('tryout') if isinstance(response.data, dict) else None
        context.user_data.pop('new_tryout', None)
        context.user_data.pop('new_tryout_products', None)
        await query.edit_message_text(
            i18n.get('staff.tryout.created_success', language, tryout_number=(tryout or {}).get('tryout_number') or ''),
            reply_markup=CommonKeyboards.back_button(language, "staff_tryout_active"),
        )
        return ConversationHandler.END

    @require_auth
    @require_delivery_driver
    async def cancel_create_tryout(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data.pop('new_tryout', None)
        context.user_data.pop('new_tryout_products', None)
        language = await self._get_language(update, context)
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(
                i18n.get('staff.cancelled', language),
                reply_markup=CommonKeyboards.back_button(language),
            )
        else:
            await update.message.reply_text(
                i18n.get('staff.cancelled', language),
                reply_markup=CommonKeyboards.back_button(language),
            )
        return ConversationHandler.END

    async def _load_task_pool(self, token: str):
        async with api_client as client:
            return await client.get_tryout_task_pool(token)

    @require_auth
    @require_delivery_driver
    async def show_task_pool(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        await self._clear_pickup_state(context, update)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        response = await self._load_task_pool(token)
        if not response.success:
            await self._handle_api_response_error(update, response, language)
            return

        items = response.data.get('items', []) if isinstance(response.data, dict) else []
        header = f"🧪 <b>{i18n.get('staff.tryout.tasks_title', language)}</b>"

        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(header, parse_mode='HTML')
        else:
            await update.message.reply_text(header, parse_mode='HTML')

        if not items:
            target = update.callback_query.message if update.callback_query else update.message
            await target.reply_text(
                i18n.get('staff.tryout.no_tasks', language),
                reply_markup=CommonKeyboards.back_button(language),
            )
            return

        target = update.callback_query.message if update.callback_query else update.message
        for task in items:
            await target.reply_text(
                self._format_task_card(task, language),
                parse_mode='HTML',
                reply_markup=TryoutKeyboards.task_actions(language, task),
            )

    def _format_task_card(self, task: dict, language: str) -> str:
        contact = task.get('trial_contact') or {}
        address = task.get('address_snapshot') or {}
        lines = [
            f"🧪 <b>{escape_html(task.get('tryout_number') or 'Try-out')}</b>",
            f"{i18n.get('staff.tryout.task_type', language)}: {escape_html(task.get('task_type'))}",
            f"{i18n.get('staff.tryout.task_status', language)}: {escape_html(task.get('status'))}",
        ]

        if contact.get('full_name'):
            lines.append(f"👤 {escape_html(contact.get('full_name'))}")
        if contact.get('phone'):
            lines.append(f"📞 {escape_html(contact.get('phone'))}")
        if address.get('full_address'):
            lines.append(f"📍 {escape_html(address.get('full_address'))}")
        if task.get('due_at'):
            lines.append(f"⏰ {escape_html(task.get('due_at'))}")
        lines.append(
            f"♻️ {i18n.get('staff.tryout.outstanding', language)}: "
            f"{task.get('outstanding_bottles_total', 0)}"
        )

        outstanding = task.get('outstanding_bottle_products') or []
        for row in outstanding:
            lines.append(f"  • {escape_html(row.get('product_name'))}: {row.get('units')}")

        return "\n".join(lines)

    @require_auth
    @require_delivery_driver
    async def accept_task(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        task_id = int(query.data.split('_')[-1])
        async with api_client as client:
            response = await client.accept_tryout_task(token, task_id)
        if not response.success:
            await self._handle_api_response_error(update, response, language)
            return

        await query.edit_message_text(
            i18n.get('staff.tryout.task_accepted', language),
            reply_markup=CommonKeyboards.back_button(language, "staff_tryout_tasks"),
        )

    async def _find_task(self, token: str, task_id: int):
        async with api_client as client:
            response = await client.get_tryout_task_pool(token)
        if not response.success:
            return response, None

        items = response.data.get('items', []) if isinstance(response.data, dict) else []
        return response, next((item for item in items if int(item.get('id')) == int(task_id)), None)

    @require_auth
    @require_delivery_driver
    async def complete_handoff(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        task_id = int(query.data.split('_')[-1])
        async with api_client as client:
            response = await client.complete_tryout_handoff(token, task_id, {})
        if not response.success:
            await self._handle_api_response_error(update, response, language)
            return

        await query.edit_message_text(
            i18n.get('staff.tryout.handoff_recorded', language),
            reply_markup=CommonKeyboards.back_button(language, "staff_tryout_tasks"),
        )

    @require_auth
    @require_delivery_driver
    async def prompt_pickup(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        task_id = int(query.data.split('_')[-1])
        response, task = await self._find_task(token, task_id)
        if not response.success:
            await self._handle_api_response_error(update, response, language)
            return
        if not task:
            await query.edit_message_text(
                i18n.get('staff.tryout.task_not_found', language),
                reply_markup=CommonKeyboards.back_button(language, "staff_tryout_tasks"),
            )
            return

        if not (task.get('outstanding_bottle_products') or []):
            await query.edit_message_text(
                i18n.get('staff.tryout.pickup_no_outstanding', language),
                reply_markup=CommonKeyboards.back_button(language, "staff_tryout_tasks"),
            )
            return

        state = self._build_pickup_state(task)
        context.user_data['tryout_pickup_task_id'] = task_id
        context.user_data['tryout_pickup_products'] = state.get('products', [])
        context.user_data['tryout_pickup_state'] = state
        # C-2: enter the text-input pickup flow → mirror so a webhook-driven
        # pool suggestion gets queued instead of clobbering the overview.
        await flow_state.mark_active(
            update.effective_user.id, 'tryout_pickup_task_id'
        )

        await query.edit_message_text(
            self._build_pickup_overview(language, state),
            parse_mode='HTML',
            reply_markup=TryoutKeyboards.pickup_overview(language, state),
        )

    @require_auth
    @require_delivery_driver
    async def show_pickup_overview(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        state = context.user_data.get('tryout_pickup_state')
        if not state:
            await query.edit_message_text(
                i18n.get('staff.tryout.task_not_found', language),
                reply_markup=CommonKeyboards.back_button(language, "staff_tryout_tasks"),
            )
            return

        await query.edit_message_text(
            self._build_pickup_overview(language, state),
            parse_mode='HTML',
            reply_markup=TryoutKeyboards.pickup_overview(language, state),
        )

    @require_auth
    @require_delivery_driver
    async def edit_pickup_product(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        _, _, _, _, task_id_raw, product_id_raw = query.data.split('_')
        task_id = int(task_id_raw)
        product_id = int(product_id_raw)

        state = context.user_data.get('tryout_pickup_state')
        if not state or int(state.get('task_id')) != task_id:
            await query.edit_message_text(
                i18n.get('staff.tryout.task_not_found', language),
                reply_markup=CommonKeyboards.back_button(language, "staff_tryout_tasks"),
            )
            return

        product = self._get_pickup_product(state, product_id)
        if not product:
            await query.edit_message_text(
                i18n.get('staff.tryout.product_not_found', language),
                reply_markup=CommonKeyboards.back_button(language, f"staff_tryout_pickup_back_{task_id}"),
            )
            return

        selected = self._pickup_selected_map(state).get(product_id, '0')
        await query.edit_message_text(
            "\n".join([
                i18n.get('staff.tryout.pickup_select_quantity', language, product=product.get('product_name')),
                i18n.get(
                    'staff.tryout.pickup_current_quantity',
                    language,
                    quantity=format_quantity(selected),
                    outstanding=format_quantity(product.get('units')),
                ),
            ]),
            reply_markup=TryoutKeyboards.pickup_quantity_selection(
                language,
                task_id,
                product_id,
                product.get('units'),
                selected,
            ),
        )

    @require_auth
    @require_delivery_driver
    async def select_pickup_quantity(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        _, _, _, _, task_id_raw, product_id_raw, units_raw = query.data.split('_')
        task_id = int(task_id_raw)
        product_id = int(product_id_raw)
        units = Decimal(units_raw) / Decimal('100')

        state = context.user_data.get('tryout_pickup_state')
        if not state or int(state.get('task_id')) != task_id:
            await query.edit_message_text(
                i18n.get('staff.tryout.task_not_found', language),
                reply_markup=CommonKeyboards.back_button(language, "staff_tryout_tasks"),
            )
            return

        product = self._get_pickup_product(state, product_id)
        if not product:
            await query.edit_message_text(
                i18n.get('staff.tryout.product_not_found', language),
                reply_markup=CommonKeyboards.back_button(language, f"staff_tryout_pickup_back_{task_id}"),
            )
            return

        selected = self._pickup_selected_map(state)
        selected[product_id] = format_quantity(units)
        context.user_data['tryout_pickup_state'] = state

        await query.edit_message_text(
            self._build_pickup_overview(language, state),
            parse_mode='HTML',
            reply_markup=TryoutKeyboards.pickup_overview(language, state),
        )

    @require_auth
    @require_delivery_driver
    async def clear_pickup_product(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        _, _, _, _, task_id_raw, product_id_raw = query.data.split('_')
        task_id = int(task_id_raw)
        product_id = int(product_id_raw)

        state = context.user_data.get('tryout_pickup_state')
        if not state or int(state.get('task_id')) != task_id:
            await query.edit_message_text(
                i18n.get('staff.tryout.task_not_found', language),
                reply_markup=CommonKeyboards.back_button(language, "staff_tryout_tasks"),
            )
            return

        selected = self._pickup_selected_map(state)
        selected.pop(product_id, None)
        context.user_data['tryout_pickup_state'] = state

        await query.edit_message_text(
            self._build_pickup_overview(language, state),
            parse_mode='HTML',
            reply_markup=TryoutKeyboards.pickup_overview(language, state),
        )

    @require_auth
    @require_delivery_driver
    async def fill_pickup_all(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        task_id = int(query.data.split('_')[-1])

        state = context.user_data.get('tryout_pickup_state')
        if not state or int(state.get('task_id')) != task_id:
            await query.edit_message_text(
                i18n.get('staff.tryout.task_not_found', language),
                reply_markup=CommonKeyboards.back_button(language, "staff_tryout_tasks"),
            )
            return

        state['selected'] = {
            int(row.get('product_id')): format_quantity(row.get('units'))
            for row in state.get('products', [])
        }
        context.user_data['tryout_pickup_state'] = state

        await query.edit_message_text(
            self._build_pickup_overview(language, state),
            parse_mode='HTML',
            reply_markup=TryoutKeyboards.pickup_overview(language, state),
        )

    @require_auth
    @require_delivery_driver
    async def clear_pickup_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        task_id = int(query.data.split('_')[-1])

        state = context.user_data.get('tryout_pickup_state')
        if not state or int(state.get('task_id')) != task_id:
            await query.edit_message_text(
                i18n.get('staff.tryout.task_not_found', language),
                reply_markup=CommonKeyboards.back_button(language, "staff_tryout_tasks"),
            )
            return

        state['selected'] = {}
        context.user_data['tryout_pickup_state'] = state

        await query.edit_message_text(
            self._build_pickup_overview(language, state),
            parse_mode='HTML',
            reply_markup=TryoutKeyboards.pickup_overview(language, state),
        )

    @require_auth
    @require_delivery_driver
    async def submit_pickup(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        task_id = int(query.data.split('_')[-1])
        state = context.user_data.get('tryout_pickup_state')
        if not state or int(state.get('task_id')) != task_id:
            await query.edit_message_text(
                i18n.get('staff.tryout.task_not_found', language),
                reply_markup=CommonKeyboards.back_button(language, "staff_tryout_tasks"),
            )
            return

        selected = self._pickup_selected_map(state)
        if not selected:
            await query.edit_message_text(
                i18n.get('staff.tryout.pickup_nothing_selected', language),
                reply_markup=TryoutKeyboards.pickup_overview(language, state),
            )
            return

        pickups = [
            {'product_id': product_id, 'units': float(self._as_decimal(units))}
            for product_id, units in selected.items()
        ]

        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        async with api_client as client:
            response = await client.record_tryout_pickup(token, task_id, {'pickups': pickups})
        if not response.success:
            await self._handle_api_response_error(update, response, language)
            return

        await self._clear_pickup_state(context, update)
        await query.edit_message_text(
            i18n.get('staff.tryout.pickup_recorded', language),
            reply_markup=CommonKeyboards.back_button(language, "staff_tryout_active"),
        )

    @require_auth
    @require_delivery_driver
    async def receive_pickup_quantities(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        state = context.user_data.get('tryout_pickup_state')
        if not state:
            return

        await update.message.reply_text(
            i18n.get('staff.tryout.pickup_use_buttons', language),
            reply_markup=TryoutKeyboards.pickup_overview(language, state),
        )

    @require_auth
    @require_delivery_driver
    async def show_active_tryouts(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        await self._clear_pickup_state(context, update)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        async with api_client as client:
            response = await client.get_active_tryouts(token)
        if not response.success:
            await self._handle_api_response_error(update, response, language)
            return

        items = response.data.get('items', []) if isinstance(response.data, dict) else []
        target = update.callback_query.message if update.callback_query else update.message
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(
                f"🧪 <b>{i18n.get('staff.tryout.active_title', language)}</b>",
                parse_mode='HTML',
            )

        if not items:
            await target.reply_text(
                i18n.get('staff.tryout.no_active', language),
                reply_markup=CommonKeyboards.back_button(language),
            )
            return

        for tryout in items:
            contact = tryout.get('trial_contact') or {}
            address = tryout.get('address_snapshot') or {}
            pickup_task_id = self._find_open_pickup_task_id(tryout)
            lines = [
                f"🧪 <b>{escape_html(tryout.get('tryout_number'))}</b>",
                f"👤 {escape_html(contact.get('full_name'))}",
                f"📍 {escape_html(address.get('full_address'))}",
                f"♻️ {i18n.get('staff.tryout.outstanding', language)}: {tryout.get('outstanding_bottles_total', 0)}",
                f"⏰ {escape_html(tryout.get('return_due_at'))}",
            ]
            await target.reply_text(
                "\n".join(lines),
                parse_mode='HTML',
                reply_markup=TryoutKeyboards.tryout_actions(language, tryout.get('id'), pickup_task_id),
            )

    @require_auth
    @require_delivery_driver
    async def view_tryout(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        language = await self._get_language(update, context)
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        tryout_id = int(query.data.split('_')[-1])
        async with api_client as client:
            response = await client.get_tryout_details(token, tryout_id)
        if not response.success:
            await self._handle_api_response_error(update, response, language)
            return

        tryout = response.data.get('tryout') if isinstance(response.data, dict) else None
        if not tryout:
            await query.edit_message_text(
                i18n.get('staff.tryout.tryout_not_found', language),
                reply_markup=CommonKeyboards.back_button(language, "staff_tryout_active"),
            )
            return

        contact = tryout.get('trial_contact') or {}
        address = tryout.get('address_snapshot') or {}
        pickup_task_id = self._find_open_pickup_task_id(tryout)
        lines = [
            f"🧪 <b>{escape_html(tryout.get('tryout_number'))}</b>",
            f"👤 {escape_html(contact.get('full_name'))}",
            f"📞 {escape_html(contact.get('phone'))}",
            f"📍 {escape_html(address.get('full_address'))}",
            f"♻️ {i18n.get('staff.tryout.outstanding', language)}: {tryout.get('outstanding_bottles_total', 0)}",
            f"⏰ {escape_html(tryout.get('return_due_at'))}",
        ]
        for row in tryout.get('outstanding_bottle_products') or []:
            lines.append(f"  • {escape_html(row.get('product_name'))}: {row.get('units')}")

        await query.edit_message_text(
            "\n".join(lines),
            parse_mode='HTML',
            reply_markup=TryoutKeyboards.tryout_actions(
                language,
                tryout.get('id'),
                pickup_task_id,
                back_callback="staff_tryout_active",
            ),
        )
