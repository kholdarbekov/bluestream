"""
Telegram keyboard layouts and UI components
"""
from typing import List, Dict, Optional, Any
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from i18n import i18n
from config import config


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
            for button_text in row:
                keyboard_row.append(KeyboardButton(text=button_text))
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
                {'text': i18n.get('menu_products', language), 'callback_data': 'menu_products'},
                {'text': i18n.get('menu_orders', language), 'callback_data': 'menu_orders'}
            ],
            [
                {'text': i18n.get('menu_subscriptions', language), 'callback_data': 'menu_subscriptions'},
                {'text': i18n.get('menu_loyalty', language), 'callback_data': 'menu_loyalty'}
            ],
            [
                {'text': i18n.get('menu_profile', language), 'callback_data': 'menu_profile'},
                {'text': i18n.get('menu_support', language), 'callback_data': 'menu_support'}
            ],
            [
                {'text': i18n.get('menu_language', language), 'callback_data': 'menu_language'}
            ]
        ]
        
        return KeyboardBuilder.build_inline_keyboard(buttons)
    
    @staticmethod
    def back_button(language: str = 'en') -> InlineKeyboardMarkup:
        """Simple back button"""
        buttons = [
            [{'text': i18n.get('back', language), 'callback_data': 'back_to_main'}]
        ]
        return KeyboardBuilder.build_inline_keyboard(buttons)
    
    @staticmethod
    def cancel_button(language: str = 'en') -> InlineKeyboardMarkup:
        """Simple cancel button"""
        buttons = [
            [{'text': i18n.get('cancel', language), 'callback_data': 'cancel_action'}]
        ]
        return KeyboardBuilder.build_inline_keyboard(buttons)
    
    @staticmethod
    def yes_no_buttons(language: str = 'en') -> InlineKeyboardMarkup:
        """Yes/No confirmation buttons"""
        buttons = [
            [
                {'text': i18n.get('yes', language), 'callback_data': 'confirm_yes'},
                {'text': i18n.get('no', language), 'callback_data': 'confirm_no'}
            ]
        ]
        return KeyboardBuilder.build_inline_keyboard(buttons)


class LanguageKeyboards:
    """Language selection keyboards"""
    
    @staticmethod
    def language_selection(current_language: str = 'en') -> InlineKeyboardMarkup:
        """Language selection keyboard"""
        buttons = []
        
        for lang_code in config.localization.supported_languages:
            flag = i18n.get_language_flag(lang_code)
            name = i18n.get_language_name(lang_code, current_language)
            
            text = f"{flag} {name}"
            if lang_code == current_language:
                text += " ✓"
            
            buttons.append([{
                'text': text,
                'callback_data': f'set_language_{lang_code}'
            }])
        
        buttons.append([{
            'text': i18n.get('back', current_language),
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
            'text': i18n.get('back', language),
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
            buttons.append([{
                'text': f"{product['name']} - {product['base_price']} UZS",
                'callback_data': f"product_{product['id']}"
            }])
        
        # Add pagination if needed
        if total_pages > 1:
            nav_row = []
            if page > 1:
                nav_row.append({
                    'text': '⬅️ Previous',
                    'callback_data': f'page_{page - 1}'
                })
            if page < total_pages:
                nav_row.append({
                    'text': 'Next ➡️',
                    'callback_data': f'page_{page + 1}'
                })
            
            if nav_row:
                buttons.append(nav_row)
        
        # Add back button
        buttons.append([{
            'text': i18n.get('back', language),
            'callback_data': 'back_to_categories'
        }])
        
        return KeyboardBuilder.build_inline_keyboard(buttons)
    
    @staticmethod
    def product_details(product_id: int, language: str = 'en') -> InlineKeyboardMarkup:
        """Product details keyboard"""
        buttons = [
            [{'text': i18n.get('add_to_cart', language), 'callback_data': f'add_to_cart_{product_id}'}],
            [{'text': i18n.get('back', language), 'callback_data': 'back_to_products'}]
        ]
        
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
            [{'text': i18n.get('add_to_cart', language), 'callback_data': f'confirm_add_cart_{product_id}_{current_quantity}'}],
            [{'text': i18n.get('back', language), 'callback_data': f'back_to_product_{product_id}'}]
        ]
        
        return KeyboardBuilder.build_inline_keyboard(buttons)


class OrderKeyboards:
    """Order-related keyboards"""
    
    @staticmethod
    def cart_actions(language: str = 'en') -> InlineKeyboardMarkup:
        """Shopping cart action buttons"""
        buttons = [
            [{'text': i18n.get('checkout', language), 'callback_data': 'cart_checkout'}],
            [
                {'text': '🗑️ Clear Cart', 'callback_data': 'cart_clear'},
                {'text': '🛍️ Continue Shopping', 'callback_data': 'menu_products'}
            ],
            [{'text': i18n.get('back', language), 'callback_data': 'back_to_main'}]
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
            [{'text': '➕ Add New Address', 'callback_data': 'add_new_address'}],
            [{'text': i18n.get('back', language), 'callback_data': 'back_to_cart'}]
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
            'payme': '📱',
            'click': '💙',
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
            'text': i18n.get('back', language),
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
            'text': i18n.get('back', language),
            'callback_data': 'back_to_address'
        }])
        
        return KeyboardBuilder.build_inline_keyboard(buttons)
    
    @staticmethod
    def order_confirmation(language: str = 'en') -> InlineKeyboardMarkup:
        """Order confirmation buttons"""
        buttons = [
            [
                {'text': '✅ Confirm Order', 'callback_data': 'confirm_order'},
                {'text': '❌ Cancel', 'callback_data': 'cancel_order'}
            ],
            [{'text': '✏️ Edit Order', 'callback_data': 'edit_order'}]
        ]
        
        return KeyboardBuilder.build_inline_keyboard(buttons)
    
    @staticmethod
    def order_list(orders: List[Dict], language: str = 'en') -> InlineKeyboardMarkup:
        """Order list keyboard"""
        buttons = []
        
        # Status icons
        status_icons = {
            'pending': '🕐',
            'confirmed': '✅',
            'preparing': '👨‍🍳',
            'out_for_delivery': '🚚',
            'delivered': '📦',
            'cancelled': '❌'
        }
        
        for order in orders:
            icon = status_icons.get(order['status'], '📋')
            date = order['created_at'][:10] if 'created_at' in order else ''
            
            buttons.append([{
                'text': f"{icon} Order #{order['order_number']} - {date}",
                'callback_data': f"order_{order['id']}"
            }])
        
        buttons.append([{
            'text': i18n.get('back', language),
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
                'text': '📍 Track Order',
                'callback_data': f'track_order_{order_id}'
            }])
        
        # Add cancel button for pending orders
        if order_status == 'pending':
            buttons.append([{
                'text': '❌ Cancel Order',
                'callback_data': f'cancel_order_{order_id}'
            }])
        
        # Add reorder button for delivered orders
        if order_status == 'delivered':
            buttons.append([{
                'text': '🔄 Reorder',
                'callback_data': f'reorder_{order_id}'
            }])
        
        buttons.append([{
            'text': i18n.get('back', language),
            'callback_data': 'back_to_orders'
        }])
        
        return KeyboardBuilder.build_inline_keyboard(buttons)


class SubscriptionKeyboards:
    """Subscription-related keyboards"""
    
    @staticmethod
    def subscription_frequency(language: str = 'en') -> InlineKeyboardMarkup:
        """Subscription frequency selection"""
        buttons = [
            [
                {'text': i18n.get('frequency_daily', language), 'callback_data': 'freq_daily'},
                {'text': i18n.get('frequency_weekly', language), 'callback_data': 'freq_weekly'}
            ],
            [
                {'text': i18n.get('frequency_biweekly', language), 'callback_data': 'freq_biweekly'},
                {'text': i18n.get('frequency_monthly', language), 'callback_data': 'freq_monthly'}
            ],
            [{'text': i18n.get('back', language), 'callback_data': 'back_to_subscriptions'}]
        ]
        
        return KeyboardBuilder.build_inline_keyboard(buttons)
    
    @staticmethod
    def subscription_list(subscriptions: List[Dict], language: str = 'en') -> InlineKeyboardMarkup:
        """Subscription list keyboard"""
        buttons = []
        
        # Status icons
        status_icons = {
            'active': '✅',
            'paused': '⏸️',
            'cancelled': '❌',
            'expired': '⏰'
        }
        
        for sub in subscriptions:
            icon = status_icons.get(sub['status'], '📋')
            buttons.append([{
                'text': f"{icon} {sub['name']} - {sub['frequency']}",
                'callback_data': f"subscription_{sub['id']}"
            }])
        
        buttons.extend([
            [{'text': '➕ Create Subscription', 'callback_data': 'create_subscription'}],
            [{'text': i18n.get('back', language), 'callback_data': 'back_to_main'}]
        ])
        
        return KeyboardBuilder.build_inline_keyboard(buttons)
    
    @staticmethod
    def subscription_actions(subscription_id: int, status: str, language: str = 'en') -> InlineKeyboardMarkup:
        """Subscription action buttons"""
        buttons = []
        
        if status == 'active':
            buttons.append([{
                'text': '⏸️ Pause',
                'callback_data': f'pause_sub_{subscription_id}'
            }])
        elif status == 'paused':
            buttons.append([{
                'text': '▶️ Resume',
                'callback_data': f'resume_sub_{subscription_id}'
            }])
        
        buttons.extend([
            [{'text': '✏️ Edit', 'callback_data': f'edit_sub_{subscription_id}'}],
            [{'text': '❌ Cancel', 'callback_data': f'cancel_sub_{subscription_id}'}],
            [{'text': i18n.get('back', language), 'callback_data': 'back_to_subscriptions'}]
        ])
        
        return KeyboardBuilder.build_inline_keyboard(buttons)


class ProfileKeyboards:
    """User profile keyboards"""
    
    @staticmethod
    def profile_menu(language: str = 'en') -> InlineKeyboardMarkup:
        """Profile menu keyboard"""
        buttons = [
            [
                {'text': '✏️ Edit Profile', 'callback_data': 'edit_profile'},
                {'text': '📍 Addresses', 'callback_data': 'manage_addresses'}
            ],
            [
                {'text': '💳 Payment Methods', 'callback_data': 'payment_methods'},
                {'text': '🔔 Notifications', 'callback_data': 'notification_settings'}
            ],
            [{'text': i18n.get('back', language), 'callback_data': 'back_to_main'}]
        ]
        
        return KeyboardBuilder.build_inline_keyboard(buttons)
    
    @staticmethod
    def phone_request(language: str = 'en') -> ReplyKeyboardMarkup:
        """Phone number request keyboard"""
        button = KeyboardButton(
            text="📱 Share Phone Number",
            request_contact=True
        )
        
        return ReplyKeyboardMarkup(
            [[button]],
            one_time_keyboard=True,
            resize_keyboard=True
        )
    
    @staticmethod
    def location_request(language: str = 'en') -> ReplyKeyboardMarkup:
        """Location request keyboard"""
        button = KeyboardButton(
            text="📍 Share Location",
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
        for address in addresses[:5]:  # Limit to 5 addresses to avoid clutter
            status = "🏠" if address.get('is_default') else "📍"
            title = address.get('title', f"Address {address.get('id')}")
            buttons.append([{
                'text': f"{status} {title}",
                'callback_data': f"view_address_{address['id']}"
            }])
        
        # Add management action buttons
        buttons.extend([
            [{'text': '➕ Add New Address', 'callback_data': 'add_new_address'}],
            [
                {'text': '✏️ Edit Address', 'callback_data': 'select_edit_address'},
                {'text': '🗑️ Delete Address', 'callback_data': 'select_delete_address'}
            ],
            [{'text': i18n.get('back', language), 'callback_data': 'menu_profile'}]
        ])
        
        return KeyboardBuilder.build_inline_keyboard(buttons)
    
    @staticmethod
    def empty_addresses(language: str = 'en') -> InlineKeyboardMarkup:
        """Keyboard for when user has no addresses"""
        buttons = [
            [{'text': '➕ Add Your First Address', 'callback_data': 'add_new_address'}],
            [{'text': i18n.get('back', language), 'callback_data': 'menu_profile'}]
        ]
        
        return KeyboardBuilder.build_inline_keyboard(buttons)


class AdminKeyboards:
    """Admin panel keyboards"""
    
    @staticmethod
    def admin_menu(language: str = 'en') -> InlineKeyboardMarkup:
        """Admin panel main menu"""
        buttons = [
            [
                {'text': i18n.get('admin_orders', language), 'callback_data': 'admin_orders'},
                {'text': i18n.get('admin_analytics', language), 'callback_data': 'admin_analytics'}
            ],
            [
                {'text': i18n.get('admin_users', language), 'callback_data': 'admin_users'},
                {'text': i18n.get('admin_products', language), 'callback_data': 'admin_products'}
            ],
            [{'text': i18n.get('back', language), 'callback_data': 'back_to_main'}]
        ]
        
        return KeyboardBuilder.build_inline_keyboard(buttons)