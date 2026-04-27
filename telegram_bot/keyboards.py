"""
Telegram keyboard layouts and UI components
"""
from typing import List, Dict, Optional, Any
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from i18n import i18n
from config import config
from shared.constants import ORDER_STATUS_ICONS, SUBSCRIPTION_STATUS_ICONS, DEFAULT_STATUS_ICON


MAX_DISPLAYED_ADDRESSES = 5


def get_product_display_price(product: Dict[str, Any]) -> Any:
    """Return effective product price with schema-compatible fallbacks."""
    pricing = product.get('pricing') or {}
    for candidate in (
        pricing.get('current_price'),
        product.get('current_price'),
        pricing.get('base_price'),
        product.get('base_price'),
    ):
        if candidate is not None:
            return candidate
    return 0


def i18n_button(key: str, language: str, callback_data: str, **fmt) -> Dict[str, str]:
    """Create a button dict with translated text.

    Usage:
        i18n_button('telegram.menu.products', lang, 'menu_products')
    """
    return {'text': i18n.get(key, language, **fmt), 'callback_data': callback_data}


class KeyboardBuilder:
    """Helper class for building keyboards"""

    @staticmethod
    def build_inline_keyboard(buttons: List[List[Dict[str, str]]],
                             row_width: int = 2) -> InlineKeyboardMarkup:
        """Build inline keyboard from button definitions"""
        keyboard = []

        for row in buttons:
            keyboard_row = []
            for button in row:
                keyboard_row.append(
                    InlineKeyboardButton(
                        text=button['text'],
                        callback_data=button.get('callback_data'),
                        url=button.get('url'),
                        switch_inline_query=button.get('switch_inline_query'),
                        switch_inline_query_current_chat=button.get('switch_inline_query_current_chat')
                    )
                )
            keyboard.append(keyboard_row)

        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    def build_reply_keyboard(buttons: List[List[str]],
                           one_time: bool = False,
                           resize: bool = True) -> ReplyKeyboardMarkup:
        """Build reply keyboard from button texts"""
        keyboard = []

        for row in buttons:
            keyboard_row = []
            for button in row:
                keyboard_row.append(KeyboardButton(text=button['text']))
            keyboard.append(keyboard_row)

        return ReplyKeyboardMarkup(
            keyboard,
            one_time_keyboard=one_time,
            resize_keyboard=resize
        )


class MenuKeyboards:
    """Main menu keyboards"""

    @staticmethod
    def main_menu(language: str = 'en') -> InlineKeyboardMarkup:
        """Main menu keyboard"""
        buttons = [
            [
                {'text': i18n.get('telegram.menu.products', language), 'callback_data': 'menu_products'},
            ],
            [
                {'text': i18n.get('telegram.menu.orders', language), 'callback_data': 'menu_orders'}
            ],
            [
                {'text': i18n.get('telegram.cart_title', language), 'callback_data': 'cart_view'},
            ],
            [
                {'text': i18n.get('telegram.menu.subscriptions', language), 'callback_data': 'menu_subscriptions'},
                {'text': i18n.get('telegram.menu.loyalty', language), 'callback_data': 'menu_loyalty'}
            ],
            [
                {'text': i18n.get('telegram.menu.profile', language), 'callback_data': 'menu_profile'},
                {'text': i18n.get('telegram.menu.support', language), 'callback_data': 'menu_support'}
            ],
            [
                {'text': i18n.get('telegram.menu.language', language), 'callback_data': 'menu_language'}
            ]
        ]

        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def back_button(language: str = 'en') -> InlineKeyboardMarkup:
        """Simple back button"""
        buttons = [
            [{'text': i18n.get('telegram.back', language), 'callback_data': 'back_to_main'}]
        ]
        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def cancel_button(language: str = 'en') -> InlineKeyboardMarkup:
        """Simple cancel button"""
        buttons = [
            [{'text': i18n.get('telegram.cancel', language), 'callback_data': 'cancel_action'}]
        ]
        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def yes_no_buttons(language: str = 'en', yes_callback: str = 'confirm_yes', no_callback: str = 'confirm_no') -> InlineKeyboardMarkup:
        """Yes/No confirmation buttons"""
        buttons = [
            [
                {'text': i18n.get('telegram.yes', language), 'callback_data': yes_callback},
                {'text': i18n.get('telegram.no', language), 'callback_data': no_callback}
            ]
        ]
        return KeyboardBuilder.build_inline_keyboard(buttons)


class LanguageKeyboards:
    """Language selection keyboards"""
    @staticmethod
    def select_language() -> InlineKeyboardMarkup:
        """Language selection keyboard on start"""
        buttons = []

        for lang_code in config.localization.supported_languages:
            flag = i18n.get_language_flag(lang_code)
            name = i18n.get_language_name(lang_code, lang_code)
            buttons.append([{
                'text': f"{flag} {name}",
                'callback_data': f'set_language_{lang_code}'
            }])

        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def language_selection(current_language: str = 'en') -> InlineKeyboardMarkup:
        """Language selection keyboard with enhanced visual layout"""
        buttons = []
        language_row = []

        for lang_code in config.localization.supported_languages:
            flag = i18n.get_language_flag(lang_code)
            name = i18n.get_language_name(lang_code, current_language)

            # Enhanced visual indicator for current language
            if lang_code == current_language:
                text = f"✅ {flag} {name}"
            else:
                text = f"{flag} {name}"

            language_row.append({
                'text': text,
                'callback_data': f'set_language_{lang_code}'
            })

            # Create rows of 2 languages for better mobile UX
            # With 3 languages (uz, en, ru), we'll have 2 in first row, 1 in second
            if len(language_row) == 2:
                buttons.append(language_row)
                language_row = []

        # Add remaining languages if any
        if language_row:
            buttons.append(language_row)

        # Add back button on its own row
        buttons.append([{
            'text': i18n.get('telegram.back', current_language),
            'callback_data': 'back_to_main'
        }])

        return KeyboardBuilder.build_inline_keyboard(buttons)


class ProductKeyboards:
    """Product-related keyboards"""

    @staticmethod
    def product_categories(categories: List[Dict], language: str = 'en') -> InlineKeyboardMarkup:
        """Product categories keyboard"""
        buttons = []

        # Add category buttons in pairs
        for i in range(0, len(categories), 2):
            row = []
            row.append({
                'text': categories[i]['name'],
                'callback_data': f"category_{categories[i]['id']}"
            })

            if i + 1 < len(categories):
                row.append({
                    'text': categories[i + 1]['name'],
                    'callback_data': f"category_{categories[i + 1]['id']}"
                })

            buttons.append(row)

        # Add back button
        buttons.append([{
            'text': i18n.get('telegram.back', language),
            'callback_data': 'back_to_main'
        }])

        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def product_list(products: List[Dict], page: int = 1,
                    total_pages: int = 1, language: str = 'en') -> InlineKeyboardMarkup:
        """Product list keyboard with pagination"""
        buttons = []

        # Add product buttons
        for product in products:
            price = get_product_display_price(product)
            buttons.append([{
                'text': f"{product['name']} - {price} UZS",
                'callback_data': f"product_{product['id']}"
            }])

        # Add pagination if needed
        if total_pages > 1:
            nav_row = []
            if page > 1:
                nav_row.append({
                    'text': i18n.get('telegram.pagination.previous', language),
                    'callback_data': f'page_{page - 1}'
                })
            if page < total_pages:
                nav_row.append({
                    'text': i18n.get('telegram.pagination.next', language),
                    'callback_data': f'page_{page + 1}'
                })

            if nav_row:
                buttons.append(nav_row)

        # Add back button
        buttons.append([{
            'text': i18n.get('telegram.back', language),
            'callback_data': 'back_to_categories'
        }])

        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def product_details(product_id: int, category_id: Optional[int] = None, language: str = 'en') -> InlineKeyboardMarkup:
        """Product details keyboard"""
        # Determine back button action
        back_callback = f'category_{category_id}' if category_id else 'menu_products'

        buttons = [
            [{'text': i18n.get('telegram.product.add_to_cart', language), 'callback_data': f'add_to_cart_{product_id}'}],
            [{'text': i18n.get('telegram.back', language), 'callback_data': back_callback}]
        ]

        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def product_list_for_subscription(products: List[Dict], language: str = 'en') -> InlineKeyboardMarkup:
        """Product list keyboard for subscription creation"""
        buttons = []

        # Add product buttons
        for product in products:
            price = get_product_display_price(product)
            buttons.append([{
                'text': f"{product['name']} - {price} UZS",
                'callback_data': f"sub_product_{product['id']}"
            }])

        # Add navigation buttons
        buttons.append([
            {'text': i18n.get('telegram.subscription.add_more_items', language), 'callback_data': 'sub_add_more_items'},
            {'text': i18n.get('telegram.done', language), 'callback_data': 'sub_items_done'}
        ])

        # Add back button
        buttons.append([{
            'text': i18n.get('telegram.back', language),
            'callback_data': 'cancel_subscription_creation'
        }])

        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def quantity_selector(product_id: int, current_quantity: int = 1,
                         language: str = 'en') -> InlineKeyboardMarkup:
        """Quantity selection keyboard"""
        buttons = [
            [
                {'text': '➖', 'callback_data': f'qty_dec_{product_id}_{current_quantity}'},
                {'text': str(current_quantity), 'callback_data': 'qty_current'},
                {'text': '➕', 'callback_data': f'qty_inc_{product_id}_{current_quantity}'}
            ],
            [{'text': i18n.get('telegram.cart.checkout', language), 'callback_data': f'checkout'}],
            [{'text': i18n.get('telegram.back', language), 'callback_data': f'back_to_product_{product_id}'}]
        ]

        return KeyboardBuilder.build_inline_keyboard(buttons)


class OrderKeyboards:
    """Order-related keyboards"""

    @staticmethod
    def cart_actions(language: str = 'en', cart_is_empty: bool = True, meets_minimum: bool = True) -> InlineKeyboardMarkup:
        """Shopping cart action buttons

        Args:
            language: Language code
            cart_is_empty: Whether cart has no items
            meets_minimum: Whether cart total meets minimum order amount
        """
        if cart_is_empty:
            buttons = [
                [
                    {'text': i18n.get('telegram.cart.continue_shopping', language), 'callback_data': 'menu_products'}
                ],
                [{'text': i18n.get('telegram.back', language), 'callback_data': 'back_to_main'}]
            ]
        else:
            # Show checkout button only if minimum is met
            if meets_minimum:
                buttons = [
                    [{'text': i18n.get('telegram.cart.checkout', language), 'callback_data': 'cart_checkout'}],
                    [
                        {'text': i18n.get('telegram.cart.clear', language), 'callback_data': 'cart_clear'},
                        {'text': i18n.get('telegram.cart.continue_shopping', language), 'callback_data': 'menu_products'}
                    ],
                    [{'text': i18n.get('telegram.back', language), 'callback_data': 'back_to_main'}]
                ]
            else:
                # Minimum not met - show warning and no checkout button
                buttons = [
                    [{'text': '⚠️ ' + i18n.get('telegram.cart.add_more', language), 'callback_data': 'menu_products'}],
                    [
                        {'text': i18n.get('telegram.cart.clear', language), 'callback_data': 'cart_clear'},
                    ],
                    [{'text': i18n.get('telegram.back', language), 'callback_data': 'back_to_main'}]
                ]

        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def delivery_addresses(addresses: List[Dict], language: str = 'en') -> InlineKeyboardMarkup:
        """Delivery address selection"""
        buttons = []

        for address in addresses:
            buttons.append([{
                'text': f"📍 {address['title']} - {address['full_address'][:30]}...",
                'callback_data': f"address_{address['id']}"
            }])

        buttons.extend([
            [{'text': i18n.get('telegram.address.add_new', language), 'callback_data': 'add_new_address_checkout'}],
            [{'text': i18n.get('telegram.back', language), 'callback_data': 'back_to_cart'}]
        ])

        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def payment_methods(methods: List[Dict], language: str = 'en') -> InlineKeyboardMarkup:
        """Payment method selection"""
        buttons = []

        # Payment method icons
        icons = {
            'cash': '💵',
            'card': '💳',
            'click': '💳',
            'payme': '💳',
            'loyalty_points': '🏆',
            'business_account': '🏢'
        }

        for method in methods:
            icon = icons.get(method['type'], '💳')
            buttons.append([{
                'text': f"{icon} {method['name']}",
                'callback_data': f"payment_{method['type']}"
            }])

        buttons.append([{
            'text': i18n.get('telegram.back', language),
            'callback_data': 'back_to_delivery'
        }])

        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def delivery_time_slots(slots: List[Dict], language: str = 'en') -> InlineKeyboardMarkup:
        """Delivery time slot selection"""
        buttons = []

        for slot in slots:
            if slot['available']:
                buttons.append([{
                    'text': f"🕐 {slot['start_time']} - {slot['end_time']}",
                    'callback_data': f"timeslot_{slot['id']}"
                }])

        buttons.append([{
            'text': i18n.get('telegram.back', language),
            'callback_data': 'back_to_address'
        }])

        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def order_confirmation(language: str = 'en') -> InlineKeyboardMarkup:
        """Order confirmation buttons"""
        buttons = [
            [
                {'text': i18n.get('telegram.order.confirm', language), 'callback_data': 'confirm_order'},
                {'text': i18n.get('telegram.cancel', language), 'callback_data': 'cancel_order'}
            ],
            [{'text': i18n.get('telegram.order.edit', language), 'callback_data': 'edit_order'}]
        ]

        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def order_list(orders: List[Dict], language: str = 'en') -> InlineKeyboardMarkup:
        """Order list keyboard"""
        buttons = []

        for order in orders:
            icon = ORDER_STATUS_ICONS.get(order['status'], DEFAULT_STATUS_ICON)
            date = order['created_at'][:10] if 'created_at' in order else ''

            buttons.append([{
                'text': f"{icon} Order #{order['order_number']} - {date}",
                'callback_data': f"order_{order['id']}"
            }])

        buttons.append([{
            'text': i18n.get('telegram.back', language),
            'callback_data': 'back_to_main'
        }])

        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def order_details(order_id: int, order_status: str, language: str = 'en') -> InlineKeyboardMarkup:
        """Order details action buttons"""
        buttons = []

        # Add track button for active orders
        if order_status in ['confirmed', 'preparing', 'out_for_delivery']:
            buttons.append([{
                'text': i18n.get('telegram.order.track', language),
                'callback_data': f'track_order_{order_id}'
            }])

        # Add pay and cancel buttons for pending orders
        if order_status == 'pending':
            buttons.append([{
                'text': i18n.get('telegram.payment.pay_now', language),
                'callback_data': f'payment_retry_{order_id}'
            }])
            buttons.append([{
                'text': i18n.get('telegram.payment.cancel_order', language),
                'callback_data': f'cancel_order_{order_id}'
            }])

        # Add reorder button for delivered orders
        if order_status == 'delivered':
            buttons.append([{
                'text': i18n.get('telegram.order.reorder', language),
                'callback_data': f'reorder_{order_id}'
            }])

        buttons.append([{
            'text': i18n.get('telegram.back', language),
            'callback_data': 'back_to_orders'
        }])

        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def order_tracking(order_id: int, language: str = 'en') -> InlineKeyboardMarkup:
        """Order tracking view buttons - just a back button to return to order details"""
        buttons = [
            [{
                'text': f"⬅️ {i18n.get('telegram.back_to_order', language)}",
                'callback_data': f'order_{order_id}'
            }],
            [{
                'text': i18n.get('telegram.back', language),
                'callback_data': 'menu_orders'
            }]
        ]

        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def asl_belgisi_error(language: str = 'en') -> InlineKeyboardMarkup:
        """Shown when Tax Committee (Asl belgisi) is unavailable during order creation.
        Offers two recovery paths: switch to cash or retry the card order."""
        buttons = [
            [{'text': i18n.get('telegram.orders.asl_belgisi_switch_cash', language),
              'callback_data': 'select_payment_cash'}],
            [{'text': i18n.get('telegram.orders.asl_belgisi_retry', language),
              'callback_data': 'confirm_order'}],
        ]
        return KeyboardBuilder.build_inline_keyboard(buttons)


class SubscriptionKeyboards:
    """Subscription-related keyboards"""

    @staticmethod
    def subscription_frequency(language: str = 'en') -> InlineKeyboardMarkup:
        """Subscription frequency selection"""
        buttons = [
            [
                {'text': i18n.get('telegram.subscription.frequency_daily', language), 'callback_data': 'subscription_freq_daily'},
                {'text': i18n.get('telegram.subscription.frequency_weekly', language), 'callback_data': 'subscription_freq_weekly'}
            ],
            [
                {'text': i18n.get('telegram.subscription.frequency_biweekly', language), 'callback_data': 'subscription_freq_biweekly'},
                {'text': i18n.get('telegram.subscription.frequency_monthly', language), 'callback_data': 'subscription_freq_monthly'}
            ],
            [{'text': i18n.get('telegram.back', language), 'callback_data': 'back_to_subscriptions'}]
        ]

        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def subscription_list(subscriptions: List[Dict], language: str = 'en') -> InlineKeyboardMarkup:
        """Subscription list keyboard"""
        buttons = []

        for sub in subscriptions:
            icon = SUBSCRIPTION_STATUS_ICONS.get(sub['status'], DEFAULT_STATUS_ICON)
            buttons.append([{
                'text': f"{icon} {sub['name']} - {sub['delivery_frequency']}",
                'callback_data': f"subscription_{sub['id']}"
            }])

        buttons.extend([
            [{'text': i18n.get('telegram.subscription.create', language), 'callback_data': 'create_subscription'}],
            [{'text': i18n.get('telegram.subscription.statistics', language), 'callback_data': 'subscription_statistics'}],
            [{'text': i18n.get('telegram.back', language), 'callback_data': 'back_to_main'}]
        ])

        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def subscription_actions(subscription_id: int, status: str, language: str = 'en') -> InlineKeyboardMarkup:
        """Subscription action buttons"""
        buttons = []

        if status == 'active':
            buttons.append([{
                'text': i18n.get('telegram.subscription.pause', language),
                'callback_data': f'pause_sub_{subscription_id}'
            }])
            buttons.append([{
                'text': i18n.get('telegram.subscription.skip_next', language),
                'callback_data': f'skip_sub_{subscription_id}'
            }])
        elif status == 'paused':
            buttons.append([{
                'text': i18n.get('telegram.subscription.resume', language),
                'callback_data': f'resume_sub_{subscription_id}'
            }])

        buttons.extend([
            [{'text': i18n.get('telegram.subscription.edit', language), 'callback_data': f'edit_sub_{subscription_id}'}],
            [{'text': i18n.get('telegram.subscription.manage_items', language), 'callback_data': f'manage_items_{subscription_id}'}],
            [
                {'text': i18n.get('telegram.subscription.billing', language), 'callback_data': f'billing_history_{subscription_id}'},
                {'text': i18n.get('telegram.subscription.logs', language), 'callback_data': f'view_logs_{subscription_id}'}
            ],
            [{'text': i18n.get('telegram.cancel', language), 'callback_data': f'cancel_sub_{subscription_id}'}],
            [{'text': i18n.get('telegram.back', language), 'callback_data': 'back_to_subscriptions'}]
        ])

        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def subscription_creation_options(language: str = 'en') -> InlineKeyboardMarkup:
        """Options for creating subscription (template or custom)"""
        buttons = [
            [{'text': i18n.get('telegram.subscription.use_template', language), 'callback_data': 'subscription_use_template'}],
            [{'text': i18n.get('telegram.subscription.create_custom', language), 'callback_data': 'subscription_custom'}],
            [{'text': i18n.get('telegram.back', language), 'callback_data': 'back_to_subscriptions'}]
        ]
        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def quantity_selector(language: str = 'en') -> InlineKeyboardMarkup:
        """Quantity selection keyboard"""
        buttons = [
            [
                {'text': '1', 'callback_data': 'sub_qty_1'},
                {'text': '2', 'callback_data': 'sub_qty_2'},
                {'text': '3', 'callback_data': 'sub_qty_3'}
            ],
            [
                {'text': '4', 'callback_data': 'sub_qty_4'},
                {'text': '5', 'callback_data': 'sub_qty_5'},
                {'text': '10', 'callback_data': 'sub_qty_10'}
            ],
            [{'text': i18n.get('telegram.back', language), 'callback_data': 'back_to_product_selection'}]
        ]
        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def payment_methods(language: str = 'en') -> InlineKeyboardMarkup:
        """Payment method selection for subscription"""
        buttons = [
            [{'text': i18n.get('telegram.payment_card', language), 'callback_data': 'sub_payment_card'}],
            [{'text': i18n.get('telegram.payment_cash', language), 'callback_data': 'sub_payment_cash'}],
            [{'text': i18n.get('telegram.back', language), 'callback_data': 'back_to_address_selection'}]
        ]
        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def item_management_menu(subscription_id: int, items: List[Dict], language: str = 'en') -> InlineKeyboardMarkup:
        """Item management keyboard"""
        buttons = []

        # Add buttons for each existing item
        for item in items:
            item_id = item.get('id')
            product_name = item.get('product', {}).get('name', 'Unknown')
            quantity = item.get('quantity', 1)
            buttons.append([
                {'text': f"✏️ {product_name} x{quantity}", 'callback_data': f'update_item_{subscription_id}_{item_id}'},
                {'text': '🗑️', 'callback_data': f'remove_item_{subscription_id}_{item_id}'}
            ])

        # Add new item button
        buttons.append([{'text': i18n.get('telegram.subscription.add_item', language), 'callback_data': f'add_item_{subscription_id}'}])

        # Back button
        buttons.append([{'text': i18n.get('telegram.back', language), 'callback_data': f'subscription_{subscription_id}'}])

        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def edit_subscription_menu(subscription_id: int, language: str = 'en') -> InlineKeyboardMarkup:
        """Edit subscription menu"""
        buttons = [
            [{'text': i18n.get('telegram.subscription.change_frequency', language), 'callback_data': f'change_frequency_{subscription_id}'}],
            [{'text': i18n.get('telegram.subscription.change_payment', language), 'callback_data': f'change_payment_{subscription_id}'}],
            [{'text': i18n.get('telegram.subscription.manage_items', language), 'callback_data': f'manage_items_{subscription_id}'}],
            [{'text': i18n.get('telegram.subscription.view_logs', language), 'callback_data': f'view_logs_{subscription_id}'}],
            [{'text': i18n.get('telegram.back', language), 'callback_data': f'subscription_{subscription_id}'}]
        ]
        return KeyboardBuilder.build_inline_keyboard(buttons)


class ProfileKeyboards:
    """User profile keyboards"""

    @staticmethod
    def profile_menu(language: str = 'en', phone_verified: bool = False) -> InlineKeyboardMarkup:
        """Profile menu keyboard"""
        buttons = [
            [
                {'text': i18n.get('telegram.profile.edit', language), 'callback_data': 'edit_profile'},
                {'text': i18n.get('telegram.profile.addresses', language), 'callback_data': 'manage_addresses'}
            ],
            [
                {'text': i18n.get('telegram.profile.phone_verification', language), 'callback_data': 'phone_verification'},
                {'text': i18n.get('telegram.profile.notifications', language), 'callback_data': 'notification_settings'}
            ],
            [
                {'text': i18n.get('telegram.profile.payment_methods', language), 'callback_data': 'payment_methods'}
            ],
            [
                {'text': i18n.get('telegram.profile.my_bottles', language), 'callback_data': 'my_bottles'}
            ],
            [{'text': i18n.get('telegram.back', language), 'callback_data': 'back_to_main'}]
        ]

        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def phone_request(language: str = 'en') -> ReplyKeyboardMarkup:
        """Phone number request keyboard"""
        button = KeyboardButton(
            text=i18n.get('telegram.profile.share_phone', language),
            request_contact=True
        )

        return ReplyKeyboardMarkup(
            [[button]],
            one_time_keyboard=True,
            resize_keyboard=True
        )

    @staticmethod
    def notification_settings(
        language: str = 'en',
        delivery_telegram_status_updates_enabled: bool = True,
    ) -> InlineKeyboardMarkup:
        """Notification settings keyboard."""
        toggle_enabled = bool(delivery_telegram_status_updates_enabled)
        toggle_callback = (
            'toggle_delivery_telegram_status_off'
            if toggle_enabled
            else 'toggle_delivery_telegram_status_on'
        )
        toggle_text = (
            i18n.get('telegram.notifications.toggle_disable_button', language)
            if toggle_enabled
            else i18n.get('telegram.notifications.toggle_enable_button', language)
        )

        buttons = [
            [{'text': toggle_text, 'callback_data': toggle_callback}],
            [{'text': i18n.get('telegram.back', language), 'callback_data': 'menu_profile'}],
        ]

        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def location_request(language: str = 'en') -> ReplyKeyboardMarkup:
        """Location request keyboard"""
        button = KeyboardButton(
            text=i18n.get('telegram.address.share_location_button', language),
            request_location=True
        )

        return ReplyKeyboardMarkup(
            [[button]],
            one_time_keyboard=True,
            resize_keyboard=True
        )

    @staticmethod
    def addresses_management(addresses: List[Dict], language: str = 'en') -> InlineKeyboardMarkup:
        """Address management keyboard with existing addresses"""
        buttons = []

        # Add individual address buttons
        for address in addresses[:MAX_DISPLAYED_ADDRESSES]:
            status = "🏠" if address.get('is_default') else "📍"
            title = address.get('title', f"Address {address.get('id')}")
            buttons.append([{
                'text': f"{status} {title}",
                'callback_data': f"view_address_{address['id']}"
            }])

        # Add management action buttons
        buttons.extend([
            [{'text': i18n.get('telegram.address.add_new', language), 'callback_data': 'add_new_address'}],
            [
                {'text': i18n.get('telegram.address.edit', language), 'callback_data': 'select_edit_address'},
                {'text': i18n.get('telegram.address.delete', language), 'callback_data': 'select_delete_address'}
            ],
            [{'text': i18n.get('telegram.back', language), 'callback_data': 'menu_profile'}]
        ])

        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def empty_addresses(language: str = 'en') -> InlineKeyboardMarkup:
        """Keyboard for when user has no addresses"""
        buttons = [
            [{'text': i18n.get('telegram.address.add_first', language), 'callback_data': 'add_new_address'}],
            [{'text': i18n.get('telegram.back', language), 'callback_data': 'menu_profile'}]
        ]

        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def location_request_with_skip(language: str = 'en') -> ReplyKeyboardMarkup:
        """Location request keyboard with manual entry option"""
        location_button = KeyboardButton(
            text=i18n.get('telegram.address.share_location_button', language),
            request_location=True
        )
        skip_button = KeyboardButton(
            text=i18n.get('telegram.address.enter_manually_button', language)
        )

        return ReplyKeyboardMarkup(
            [[location_button], [skip_button]],
            one_time_keyboard=True,
            resize_keyboard=True
        )

    @staticmethod
    def location_request_with_retry(language: str = 'en') -> ReplyKeyboardMarkup:
        """Location request keyboard for retry after wrong geocode"""
        location_button = KeyboardButton(
            text=i18n.get('telegram.address.share_location_button', language),
            request_location=True
        )
        retry_button = KeyboardButton(
            text=i18n.get('telegram.address.reenter_manually_button', language)
        )
        cancel_button = KeyboardButton(
            text=i18n.get('telegram.cancel', language)
        )

        return ReplyKeyboardMarkup(
            [[location_button], [retry_button], [cancel_button]],
            one_time_keyboard=True,
            resize_keyboard=True
        )

    @staticmethod
    def region_selection(language: str = 'en') -> InlineKeyboardMarkup:
        """Region selection keyboard (only Tashkent for now)"""
        region_names = {
            'en': '🏙️ Tashkent City',
            'uz': '🏙️ Toshkent shahri',
            'ru': '🏙️ Город Ташкент'
        }
        buttons = [
            [{'text': region_names.get(language, region_names['en']),
              'callback_data': 'region_tashkent_city'}],
            [{'text': i18n.get('telegram.cancel', language),
              'callback_data': 'cancel_address_creation'}]
        ]
        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def district_selection(districts: List[Dict], language: str = 'en') -> InlineKeyboardMarkup:
        """District selection keyboard for Tashkent

        Args:
            districts: List of {'key': str, 'name': str} dicts
            language: Language code
        """
        buttons = []

        # Create 2-column layout for districts
        for i in range(0, len(districts), 2):
            row = []
            for j in range(2):
                if i + j < len(districts):
                    district = districts[i + j]
                    row.append({
                        'text': district['name'],
                        'callback_data': f"district_{district['key']}"
                    })
            buttons.append(row)

        # Add back button
        buttons.append([
            {'text': i18n.get('telegram.back', language),
             'callback_data': 'back_to_region'}
        ])

        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def optional_field_keyboard(field_name: str, language: str = 'en') -> InlineKeyboardMarkup:
        """Keyboard for optional address fields with skip option"""
        buttons = [
            [{'text': i18n.get('telegram.address.skip_field', language),
              'callback_data': f'skip_{field_name}'}]
        ]
        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def geocode_confirmation(language: str = 'en', show_edit: bool = True) -> InlineKeyboardMarkup:
        """Confirmation keyboard after geocoding

        Args:
            language: Language code
            show_edit: If True, show Edit Details button (for existing addresses)
                       If False, hide it (for new address creation)
        """
        buttons = [
            [
                {'text': i18n.get('telegram.address.location_correct', language),
                 'callback_data': 'confirm_geocode'},
                {'text': i18n.get('telegram.address.location_wrong', language),
                 'callback_data': 'retry_geocode'}
            ]
        ]

        if show_edit:
            buttons.append([{'text': i18n.get('telegram.address.edit_details', language),
                            'callback_data': 'edit_address_details'}])

        buttons.append([{'text': i18n.get('telegram.cancel', language),
                        'callback_data': 'cancel_address_creation'}])

        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def address_title_suggestions(language: str = 'en') -> InlineKeyboardMarkup:
        """Quick title suggestions for address"""
        titles = {
            'home': {'en': '🏠 Home', 'uz': '🏠 Uy', 'ru': '🏠 Дом'},
            'work': {'en': '🏢 Work', 'uz': '🏢 Ish', 'ru': '🏢 Работа'},
            'other': {'en': '📍 Other', 'uz': '📍 Boshqa', 'ru': '📍 Другое'}
        }
        buttons = [
            [
                {'text': titles['home'].get(language, titles['home']['en']),
                 'callback_data': 'addr_title_home'},
                {'text': titles['work'].get(language, titles['work']['en']),
                 'callback_data': 'addr_title_work'}
            ],
            [
                {'text': titles['other'].get(language, titles['other']['en']),
                 'callback_data': 'addr_title_other'}
            ]
        ]
        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def address_view_actions(address_id: int, is_default: bool, language: str = 'en') -> InlineKeyboardMarkup:
        """Actions for viewing a single address"""
        buttons = []

        if not is_default:
            buttons.append([{
                'text': i18n.get('telegram.address.set_default', language),
                'callback_data': f'set_default_address_{address_id}'
            }])

        buttons.extend([
            [
                {'text': i18n.get('telegram.edit', language), 'callback_data': f'edit_address_{address_id}'},
                {'text': i18n.get('telegram.delete', language), 'callback_data': f'delete_address_{address_id}'}
            ],
            [{'text': i18n.get('telegram.back', language),
              'callback_data': 'manage_addresses'}]
        ])

        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def delivery_instructions_keyboard(language: str = 'en') -> InlineKeyboardMarkup:
        """Keyboard for delivery instructions step"""
        buttons = [
            [{'text': i18n.get('telegram.address.skip_instructions', language),
              'callback_data': 'skip_delivery_instructions'}]
        ]
        return KeyboardBuilder.build_inline_keyboard(buttons)


class PaymentKeyboards:
    """Payment-related keyboards for Telegram Payments integration"""

    @staticmethod
    def payment_pending(order_id: int, language: str = 'en') -> InlineKeyboardMarkup:
        """Shown while waiting for payment completion"""
        buttons = [
            [{'text': i18n.get('telegram.payment.cancel', language),
              'callback_data': f'payment_cancel_{order_id}'}]
        ]
        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def payment_success(order_id: int, language: str = 'en') -> InlineKeyboardMarkup:
        """Shown after successful payment"""
        buttons = [
            [{'text': i18n.get('telegram.payment.view_order', language),
              'callback_data': f'order_{order_id}'}],
            [{'text': i18n.get('telegram.payment.back_to_menu', language),
              'callback_data': 'back_to_main'}]
        ]
        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def payment_failed(order_id: int, language: str = 'en') -> InlineKeyboardMarkup:
        """Shown on payment failure with retry and switch options"""
        buttons = [
            [{'text': i18n.get('telegram.payment.retry', language),
              'callback_data': f'payment_retry_{order_id}'}],
            [{'text': i18n.get('telegram.payment.switch_method', language),
              'callback_data': f'payment_switch_{order_id}'}],
            [{'text': i18n.get('telegram.payment.cancel_order', language),
              'callback_data': f'cancel_order_{order_id}'}]
        ]
        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def payment_cancelled(order_id: int, language: str = 'en') -> InlineKeyboardMarkup:
        """Shown when user cancels payment - same as failed but different context"""
        return PaymentKeyboards.payment_failed(order_id, language)

    @staticmethod
    def payment_link(payment_url: str, language: str = 'en') -> InlineKeyboardMarkup:
        """Payment link with Pay button and Back button"""
        buttons = [
            [InlineKeyboardButton(
                text=i18n.get('telegram.payment.pay_btn', language),
                url=payment_url
            )],
            [InlineKeyboardButton(
                text=i18n.get('telegram.back', language),
                callback_data='back_to_order_confirm'
            )]
        ]
        return InlineKeyboardMarkup(buttons)


class AdminKeyboards:
    """Admin panel keyboards"""

    @staticmethod
    def admin_menu(language: str = 'en') -> InlineKeyboardMarkup:
        """Admin panel main menu"""
        buttons = [
            [
                {'text': i18n.get('telegram.admin.orders', language), 'callback_data': 'admin_orders'},
                {'text': i18n.get('telegram.admin.analytics', language), 'callback_data': 'admin_analytics'}
            ],
            [
                {'text': i18n.get('telegram.admin.users', language), 'callback_data': 'admin_users'},
                {'text': i18n.get('telegram.admin.products', language), 'callback_data': 'admin_products'}
            ],
            [{'text': i18n.get('telegram.back', language), 'callback_data': 'back_to_main'}]
        ]

        return KeyboardBuilder.build_inline_keyboard(buttons)
