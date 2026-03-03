"""Driver-facing try-out task handlers for the staff bot."""

import logging

from telegram import ReplyKeyboardRemove, Update
from telegram.ext import ContextTypes, ConversationHandler

from api_client import api_client
from handlers.base import BaseHandler
from i18n import i18n
from keyboards.common import CommonKeyboards
from keyboards.tryouts import TryoutKeyboards
from permissions import require_auth, require_delivery_driver
from utils.formatters import escape_html
from utils.validators import validate_name, validate_phone


logger = logging.getLogger(__name__)

ENTER_TRYOUT_PHONE, ENTER_TRYOUT_NAME, ENTER_TRYOUT_ADDRESS = range(90, 93)


class TryoutHandler(BaseHandler):
    """List and execute try-out tasks for delivery drivers."""

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

    async def receive_create_phone(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        valid, result = validate_phone((update.message.text or '').strip())
        if not valid:
            await update.message.reply_text(i18n.get('staff.operator.invalid_phone', language))
            return ENTER_TRYOUT_PHONE

        context.user_data['new_tryout']['phone'] = result
        await update.message.reply_text(i18n.get('staff.tryout.enter_name', language))
        return ENTER_TRYOUT_NAME

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
            reply_markup=ReplyKeyboardRemove(),
        )
        await self._show_product_selection(update, context, use_message=True)
        return ConversationHandler.END

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

        context.user_data['new_tryout']['latitude'] = location.latitude
        context.user_data['new_tryout']['longitude'] = location.longitude

        async with api_client as client:
            response = await client.reverse_geocode_address(token, location.latitude, location.longitude)

        if not response.success or not isinstance(response.data, dict) or not response.data.get('formatted_address'):
            await update.message.reply_text(
                i18n.get('staff.tryout.location_geocode_failed', language),
                reply_markup=ReplyKeyboardRemove(),
            )
            return ENTER_TRYOUT_ADDRESS

        context.user_data['new_tryout']['full_address'] = response.data['formatted_address']
        context.user_data['new_tryout']['district'] = response.data.get('district')
        context.user_data['new_tryout']['city'] = response.data.get('city') or 'Tashkent'
        await update.message.reply_text(
            i18n.get('staff.tryout.location_received', language, address=response.data['formatted_address']),
            reply_markup=ReplyKeyboardRemove(),
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
            await self._handle_api_response_error(update, response, language)
            return

        context.user_data['new_tryout_products'] = products
        text = i18n.get('staff.tryout.select_products', language)
        if use_message:
            await update.message.reply_text(
                text,
                reply_markup=TryoutKeyboards.product_list(language, products)
            )
        else:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(
                text,
                reply_markup=TryoutKeyboards.product_list(language, products)
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

        await query.edit_message_text(
            i18n.get('staff.tryout.select_quantity', language, product=product.get('name')),
            reply_markup=TryoutKeyboards.quantity_selection(language, product_id),
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

        context.user_data['tryout_pickup_task_id'] = task_id
        context.user_data['tryout_pickup_products'] = task.get('outstanding_bottle_products') or []

        instructions = [
            i18n.get('staff.tryout.pickup_prompt', language),
            "",
            "Format:",
            "product_id:units",
        ]
        for row in context.user_data['tryout_pickup_products']:
            instructions.append(
                f"{row.get('product_id')}: {escape_html(row.get('product_name'))} "
                f"({row.get('units')})"
            )

        await query.edit_message_text(
            "\n".join(instructions),
            reply_markup=CommonKeyboards.back_button(language, "staff_tryout_tasks"),
        )

    @require_auth
    @require_delivery_driver
    async def receive_pickup_quantities(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
        task_id = context.user_data.get('tryout_pickup_task_id')
        if not task_id:
            return

        lines = [line.strip() for line in (update.message.text or '').splitlines() if line.strip()]
        pickups = []
        try:
            for line in lines:
                product_id_raw, units_raw = [segment.strip() for segment in line.split(':', 1)]
                pickups.append({
                    'product_id': int(product_id_raw),
                    'units': float(units_raw),
                })
        except Exception:
            await update.message.reply_text(i18n.get('staff.tryout.pickup_invalid_format', language))
            return

        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return

        async with api_client as client:
            response = await client.record_tryout_pickup(token, task_id, {'pickups': pickups})
        if not response.success:
            await self._handle_api_response_error(update, response, language)
            return

        context.user_data.pop('tryout_pickup_task_id', None)
        context.user_data.pop('tryout_pickup_products', None)
        await update.message.reply_text(
            i18n.get('staff.tryout.pickup_recorded', language),
            reply_markup=CommonKeyboards.back_button(language, "staff_tryout_active"),
        )

    @require_auth
    @require_delivery_driver
    async def show_active_tryouts(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        language = await self._get_language(update, context)
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
                reply_markup=TryoutKeyboards.tryout_actions(language, tryout.get('id')),
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
            reply_markup=CommonKeyboards.back_button(language, "staff_tryout_active"),
        )
