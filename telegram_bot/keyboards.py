"""
Telegram keyboard layouts and UI components
"""
import re
from typing import List, Dict, Optional, Any, Sequence, Tuple
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from i18n import i18n
from config import config
from shared.constants import ORDER_STATUS_ICONS, SUBSCRIPTION_STATUS_ICONS, DEFAULT_STATUS_ICON


MAX_DISPLAYED_ADDRESSES = 5


def customer_may_pay(order: Optional[Dict[str, Any]]) -> bool:
    """Should this customer be offered a way to pay THIS order right now?

    THE bot's single expression of the question. Both keyboards that draw a Pay
    button and the `retry_payment` handler that services the tap read it, so the
    button and the handler can never disagree about the same order.

    It is the BACKEND'S answer, plus exactly one case the backend field
    deliberately does not cover:

    * ``payment_info.is_payable`` is `order_is_payable_online` published
      verbatim (payment_projection.py, THE authority — the same predicate the
      Click PREPARE guard refuses on). Under policy 2026-08-24 it stays True
      THROUGH delivery for an unpaid Click order (case B), which is the whole
      point of B3: the old gate here was `order_status == 'pending'`, so the one
      population whose link we deliberately keep payable was shown no way to pay
      it.

    * A PENDING order whose rail is not online yet. `is_payable` asks "is THIS
      payment's gateway link live", and a cash order has no link — but the
      customer has always been able to move a pending cash order onto Click from
      this screen, and POST /api/v1/payments/create owns that flip together with
      its marking-code pool guard (`MARKING_CODES_POOL_SHORT`). Without this
      disjunct B3 would DELETE the Pay button from every cash order, i.e.
      narrow a change whose entire purpose is to widen. It is deliberately
      spelled `status == 'pending'` and not "any live order": `cancel_order`
      and the rail flip are both checkout-window affordances, and widening past
      the window is a separate decision nobody has made.

    Unpaid is required on both sides — `is_payable` already excludes a settled
    order, and offering a second payment on a paid one is how double-payments
    start.
    """
    if not order:
        return False
    payment_info = order.get('payment_info') or {}
    if payment_info.get('is_payable'):
        return True
    if order.get('is_paid'):
        return False
    if (payment_info.get('payment_status') or '').lower() == 'completed':
        # A COMPLETED payment on an order whose `is_paid` flag has not caught up
        # yet. The open-checkout arm would otherwise draw a Pay button whose tap
        # runs `create_payment`, which REWRITES the row to PENDING and mints a
        # second link -- downgrading a settled payment and inviting a second
        # debit. `is_payable` already excludes this; the OR-arm has to as well.
        return False
    return (order.get('status') or '').lower() == 'pending'


def customer_may_cancel(order: Optional[Dict[str, Any]]) -> bool:
    """May this customer cancel THIS order themselves?

    A SIBLING of :func:`customer_may_pay`, never folded into it. The two used to
    be one `order_status == 'pending'` test and B3 split them because payability
    now runs THROUGH delivery while `OrderService.cancel_order`
    (`order_service.py:1050`) still refuses DELIVERED/CANCELLED. Written down
    once here so both screens that offer Cancel ask the same question.

    Deliberately still the checkout window and not "anything cancel_order would
    accept": widening customer self-cancel to CONFIRMED / PREPARING /
    OUT_FOR_DELIVERY orders is a business decision nobody has made. This helper
    exists so that decision has exactly one place to land.
    """
    if not order:
        return False
    if order.get('is_paid'):
        return False
    return (order.get('status') or '').lower() == 'pending'


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


# ---------------------------------------------------------------------------
# Product-list pagination, in ONE place.
#
# The regex `bot.py` registers, the string this module renders and the parser
# `handlers/products.py` reads it back with are the same rule; they live here
# together so a change to the shape cannot land in two of the three.
#
# The first version of this row emitted `page_{n}`. It named the PAGE and
# nothing else, so no handler could re-render the list from it and both buttons
# were dead: the tap matched no pattern, nothing answered the callback query,
# and the customer watched a spinner until Telegram gave up.
#
# The category and the single-category Back target therefore ride on the
# CALLBACK rather than in `context.user_data`, for exactly the reason the
# cancel-confirmation card carries its order id
# (handlers/orders.py::_cancel_confirmation_callback): the Application is built
# with no `persistence`, so bot memory is empty after every deploy, and a
# product list still open on a customer's screen has to keep working.
#
# The registered pattern also claims the LEGACY `page_{n}` shape. Nothing
# renders it any more, but cards rendered before this release outlive the
# deploy, and a tap no handler claims is a spinner nobody can stop — claimed
# here, `ProductHandlers.product_page_handler` can at least say so.
PRODUCT_PAGE_PATTERN = r"^page_\d+(_\d+)?(_single)?$"

_PRODUCT_PAGE_RE = re.compile(r"^page_(\d+)_(\d+)(_single)?$")


def product_page_callback(category_id: Any, page: int,
                          single_category: bool = False) -> Optional[str]:
    """`callback_data` for one Previous/Next button of a category's product list.

    `None` when the button could not address itself — an unknown category, or a
    page below the first. A button that cannot say what it pages is the defect
    this replaced, so the caller renders nothing at all rather than a dead one.
    """
    category = str(category_id if category_id is not None else '')
    try:
        page_number = int(page)
    except (TypeError, ValueError):
        return None
    if not category.isdigit() or page_number < 1:
        return None
    return f"page_{category}_{page_number}" + ('_single' if single_category else '')


def parse_product_page_callback(data: Any) -> Optional[Tuple[str, int, bool]]:
    """`(category_id, page, single_category)` carried by a pagination callback.

    `None` means the data carries no category — the legacy `page_{n}` shape from
    a card rendered before this release. It is a screen the bot can no longer
    reconstruct, not an error to swallow.
    """
    match = _PRODUCT_PAGE_RE.match(str(data or ''))
    if match is None:
        return None
    return match.group(1), int(match.group(2)), match.group(3) is not None


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
    def main_menu(language: str = 'en', show_loyalty: bool = True) -> InlineKeyboardMarkup:
        """Main menu keyboard. ``show_loyalty`` hides the loyalty button for
        users not eligible for the loyalty program (ineligible entity users)."""
        subs_loyalty_row = [
            {'text': i18n.get('telegram.menu.subscriptions', language), 'callback_data': 'menu_subscriptions'},
        ]
        if show_loyalty:
            subs_loyalty_row.append(
                {'text': i18n.get('telegram.menu.loyalty', language), 'callback_data': 'menu_loyalty'}
            )

        buttons = [
            [{'text': i18n.get('telegram.menu.products', language), 'callback_data': 'menu_products'}],
            [{'text': i18n.get('telegram.menu.orders', language), 'callback_data': 'menu_orders'}],
            [{'text': i18n.get('telegram.cart_title', language), 'callback_data': 'cart_view'}],
            subs_loyalty_row,
            [
                {'text': i18n.get('telegram.menu.profile', language), 'callback_data': 'menu_profile'},
                {'text': i18n.get('telegram.menu.support', language), 'callback_data': 'menu_support'},
            ],
            [{'text': i18n.get('telegram.menu.language', language), 'callback_data': 'menu_language'}],
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
    def product_categories(categories: List[Dict], language: str = 'en',
                          quick_suggestions: Optional[List[Dict]] = None) -> InlineKeyboardMarkup:
        """Product categories keyboard, optionally prefixed by Quick Order suggestions.

        `quick_suggestions` is a list of dicts produced by
        `QuickOrderHandlers.build_quick_suggestions`. Each adds one button row
        at the top.
        """
        buttons = []

        if quick_suggestions:
            for sug in quick_suggestions:
                label = i18n.get(sug['label_key'], language, **(sug.get('label_args') or {}))
                buttons.append([{
                    'text': label,
                    'callback_data': sug['callback_data'],
                }])

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
                    total_pages: int = 1, language: str = 'en',
                    quick_suggestions: Optional[List[Dict]] = None,
                    single_category: bool = False,
                    category_id: Any = None) -> InlineKeyboardMarkup:
        """Product list keyboard with pagination.

        When `single_category` is True the products menu skipped the category
        step, so "Back" must go straight to the main menu (the category list
        would have been empty). When False, "Back" goes to the category list.

        `quick_suggestions` is rendered as a top section when present (used
        when the products list is shown directly without a category step).

        `category_id` is what the Previous/Next buttons page WITHIN: it is
        carried by their callback_data so the tap can be served with no bot
        memory at all (see `product_page_callback`). Without it there is no
        pagination row, even across several pages — a caller that cannot name
        its category has nothing a paging button could re-render.
        """
        buttons = []

        if quick_suggestions:
            for sug in quick_suggestions:
                label = i18n.get(sug['label_key'], language, **(sug.get('label_args') or {}))
                buttons.append([{
                    'text': label,
                    'callback_data': sug['callback_data'],
                }])

        # Add product buttons
        for product in products:
            price = get_product_display_price(product)
            buttons.append([{
                'text': f"{product['name']} - {price} UZS",
                'callback_data': f"product_{product['id']}"
            }])

        # Add pagination if needed. Each button is rendered only when it can
        # address itself; `product_page_callback` returns None otherwise.
        if total_pages > 1:
            nav_row = []
            previous_callback = product_page_callback(category_id, page - 1, single_category)
            next_callback = product_page_callback(category_id, page + 1, single_category)
            if page > 1 and previous_callback:
                nav_row.append({
                    'text': i18n.get('telegram.pagination.previous', language),
                    'callback_data': previous_callback,
                })
            if page < total_pages and next_callback:
                nav_row.append({
                    'text': i18n.get('telegram.pagination.next', language),
                    'callback_data': next_callback,
                })

            if nav_row:
                buttons.append(nav_row)

        # Add back button — context-sensitive: when there's only one category
        # the category list would be empty, so go to main menu instead.
        back_callback = 'back_to_main' if single_category else 'back_to_categories'
        buttons.append([{
            'text': i18n.get('telegram.back', language),
            'callback_data': back_callback,
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

    QUANTITY_PRESET_OFFSETS = (3, 6, 10, 13, 18)

    @staticmethod
    def _build_quantity_presets(min_order_qty: int, max_quantity: int) -> List[int]:
        """Build preset quantity values as offsets above the per-product minimum.

        The cart already starts at min_order_qty when an item is first added, so
        presets are jumps from that floor.

        This builder does NOT decide what is orderable. ``max_quantity`` is the
        ceiling its CALLER already resolved (``ProductHandlers._purchase_bounds``
        — stock, the backend per-item cap, and what "unknown stock" means all
        live there). It used to re-derive that ceiling from ``stock_quantity``
        with ``stock_quantity > 0``, which switches the ceiling OFF at exactly
        the moment it matters: a sold-out product fell back to the per-item cap and
        rendered buttons up to min+18 for water that does not exist. A ceiling
        below the floor yields no presets at all, which is the honest screen.

        Returned list is deduplicated and sorted.
        """
        candidates = [min_order_qty + offset for offset in ProductKeyboards.QUANTITY_PRESET_OFFSETS]
        # Keep only values strictly above min (presets are shortcuts, not the floor)
        # and at-or-below the cap.
        presets = [v for v in candidates if min_order_qty < v <= max_quantity]
        return sorted(set(presets))

    @staticmethod
    def quantity_selector(product_id: int, current_quantity: int = 1,
                         language: str = 'en', *, min_order_qty: int,
                         max_quantity: int) -> InlineKeyboardMarkup:
        """Quantity selection keyboard with offset-based presets + fine-tune row.

        Layout:
            [ +3 ] [ +6 ] [ +10 ] [ +13 ] [ +18 ]   (preset jumps from min)
            [ −1 ]     {qty}     [ +1 ]              (fine-tune)
            [ Checkout ]
            [ Back ]

        ``min_order_qty`` and ``max_quantity`` are the purchase bounds the
        caller resolved (``ProductHandlers._purchase_bounds``) and are REQUIRED:
        a default here would be a second, silent opinion about what the customer
        may order, and the sold-out case is precisely where the default would be
        wrong. The preset row disappears when nothing above the floor is
        orderable.
        """
        buttons: List[List[Dict[str, str]]] = []

        presets = ProductKeyboards._build_quantity_presets(min_order_qty, max_quantity)
        if presets:
            preset_row = []
            for value in presets:
                # Show the absolute target quantity (e.g. "5", "8", "12")
                # rather than an offset — customers think in terms of how
                # many they want, not deltas from a hidden floor.
                preset_row.append({
                    'text': str(value),
                    'callback_data': f'qty_set_{product_id}_{value}'
                })
            buttons.append(preset_row)

        # Fine-tune row — explicit -1/+1 with a labelled center button so the
        # number is unambiguously "the current quantity" and not a third action.
        qty_label = i18n.get('telegram.quantity', language)
        buttons.append([
            {'text': '−1', 'callback_data': f'qty_dec_{product_id}_{current_quantity}'},
            {'text': f'{qty_label}: {current_quantity}', 'callback_data': 'qty_current'},
            {'text': '+1', 'callback_data': f'qty_inc_{product_id}_{current_quantity}'}
        ])
        buttons.append([{'text': i18n.get('telegram.cart.checkout', language), 'callback_data': 'checkout'}])
        buttons.append([{'text': i18n.get('telegram.back', language), 'callback_data': f'back_to_product_{product_id}'}])

        return KeyboardBuilder.build_inline_keyboard(buttons)


class OrderKeyboards:
    """Order-related keyboards"""

    @staticmethod
    def cart_actions(language: str = 'en', cart_is_empty: bool = True,
                     meets_minimum: bool = True, edit_mode: bool = False,
                     cart_items: Optional[List[Dict]] = None,
                     edit_return: Optional[str] = None) -> InlineKeyboardMarkup:
        """Shopping cart action buttons.

        Args:
            language: Language code
            cart_is_empty: Whether cart has no items
            meets_minimum: Whether cart total meets minimum order amount
            edit_mode: When True, render per-item +/- and remove controls plus an
                'Add product' row and a 'Done' row (Deliverable B). The normal
                checkout/clear/continue buttons are hidden in edit mode so the
                screen stays focused on editing.
            cart_items: The cart line-items (each {'product': {...}, 'quantity': N})
                used to build per-item rows; only consulted when edit_mode is True.
            edit_return: Where 'Done' routes. 'order_confirm' -> back to the order
                confirmation screen; anything else -> back to the cart summary.
        """
        if edit_mode and not cart_is_empty:
            buttons: List[List[Dict[str, str]]] = []
            for item in (cart_items or []):
                product = item.get('product') or {}
                product_id = product.get('id') or item.get('product_id')
                quantity = item.get('quantity', 0)
                name = product.get('name', '')
                # Row 1: product name + quantity (display only, no-op tap reuses
                # the cart-view callback so a stray tap is harmless).
                buttons.append([
                    {'text': f"{name} ×{quantity}", 'callback_data': 'cart_view'}
                ])
                # Row 2: [−] {qty} [+]  +  remove
                buttons.append([
                    {'text': '−', 'callback_data': f'cart_dec_{product_id}'},
                    {'text': str(quantity), 'callback_data': 'cart_view'},
                    {'text': '+', 'callback_data': f'cart_inc_{product_id}'},
                    {'text': i18n.get('telegram.cart.remove', language),
                     'callback_data': f'cart_rm_{product_id}'},
                ])
            buttons.append([
                {'text': i18n.get('telegram.cart.add_product', language),
                 'callback_data': 'menu_products'}
            ])
            done_cb = 'back_to_order_confirm' if edit_return == 'order_confirm' else 'back_to_cart'
            buttons.append([
                {'text': i18n.get('telegram.cart.done', language), 'callback_data': done_cb}
            ])
            return KeyboardBuilder.build_inline_keyboard(buttons)

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
    def single_address_confirm(address: Dict, language: str = 'en',
                              *, back_callback: str = 'back_to_cart',
                              show_change: bool = False) -> InlineKeyboardMarkup:
        """Confirmation step showing the auto-selected delivery address.

        Used when:
          * The user has exactly one saved address (single-address auto-skip
            on the cart checkout flow) → `back_callback='back_to_cart'`.
          * A Quick Order auto-selected one of several addresses → caller
            sets `back_callback='menu_products'` so Back returns to the
            screen the user actually came from.

        The Continue button uses the existing `address_{id}` callback so the
        downstream payment flow is unchanged.

        `show_change` adds a 'Change address' button that returns to the full
        picker. Pass it only when the customer HAS other saved addresses — the
        Quick Order case. Without it that customer can only accept the
        auto-selected address or add another one, never pick one they already
        have.
        """
        address_id = address['id']
        buttons = [
            [{'text': i18n.get('telegram.checkout.continue', language),
              'callback_data': f'address_{address_id}'}],
        ]

        if show_change:
            buttons.append([{'text': i18n.get('telegram.checkout.change_address', language),
                             'callback_data': 'checkout_change_address'}])

        buttons.extend([
            [{'text': i18n.get('telegram.address.add_new', language),
              'callback_data': 'add_new_address_checkout'}],
            [{'text': i18n.get('telegram.back', language), 'callback_data': back_callback}],
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
            'business_account': '🏦'
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
    def order_confirmation(
        language: str = 'en', meets_minimum: bool = True, has_reward: bool = False,
        show_reward: bool = True,
    ) -> InlineKeyboardMarkup:
        """Order confirmation buttons.

        When ``meets_minimum`` is False, the Confirm button is replaced by a
        non-actionable warning so the user must go back and fix the cart
        before placing the order.

        ``has_reward`` toggles the loyalty-reward row: an "Apply reward" button
        when none is selected, or "Change"/"Remove" buttons when one is. The row
        is only shown once the order meets the minimum (it's part of placing the
        order, not fixing the cart).
        """
        if meets_minimum:
            primary_row = [
                {'text': i18n.get('telegram.order.confirm', language), 'callback_data': 'confirm_order'},
                {'text': i18n.get('telegram.cancel', language), 'callback_data': 'cancel_order'},
            ]
        else:
            primary_row = [
                {
                    'text': '⚠️ ' + i18n.get('telegram.cart.add_more', language),
                    'callback_data': 'cart_view',
                },
                {'text': i18n.get('telegram.cancel', language), 'callback_data': 'cancel_order'},
            ]

        buttons = [primary_row]

        if meets_minimum and show_reward:
            if has_reward:
                buttons.append([
                    {'text': '🎁 ' + i18n.get('telegram.loyalty.change_reward', language),
                     'callback_data': 'checkout_choose_reward'},
                    {'text': '🗑 ' + i18n.get('telegram.loyalty.remove_reward', language),
                     'callback_data': 'checkout_remove_reward'},
                ])
            else:
                buttons.append([
                    {'text': '🎁 ' + i18n.get('telegram.loyalty.apply_reward', language),
                     'callback_data': 'checkout_choose_reward'},
                ])

        buttons.append([{'text': i18n.get('telegram.order.edit', language), 'callback_data': 'edit_order'}])
        buttons.append([{'text': i18n.get('telegram.back', language), 'callback_data': 'back_to_payment'}])

        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def checkout_reward_picker(rewards: List[Dict[str, Any]], language: str = 'en') -> InlineKeyboardMarkup:
        """Picker shown during checkout: one button per redeemable reward + Back.

        ``rewards`` is the already-filtered list of rewards that will actually
        apply to this order (caller checks affordability + min order value).
        """
        points_unit = i18n.get('telegram.loyalty.points_unit', language)
        buttons = []
        for reward in rewards:
            name = reward.get('name') or i18n.get('telegram.loyalty.reward_fallback', language)
            cost = reward.get('points_cost', 0)
            buttons.append([{
                'text': f"🎁 {name} — {cost} {points_unit}",
                'callback_data': f"checkout_apply_reward_{reward.get('id')}",
            }])
        buttons.append([{
            'text': i18n.get('telegram.back', language),
            'callback_data': 'back_to_order_confirm',
        }])
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
    def order_details(
        order_id: int,
        order_status: str,
        language: str = 'en',
        may_pay: bool = False,
        may_cancel: bool = False,
    ) -> InlineKeyboardMarkup:
        """Order details action buttons.

        ``may_pay`` is `customer_may_pay(order)`, computed by the caller from
        the order it already fetched. It is passed in rather than re-derived
        here because this builder is handed a status STRING and cannot see the
        published `payment_info.is_payable`.
        """
        buttons = []

        # Add track button for active orders
        if order_status in ['confirmed', 'preparing', 'out_for_delivery']:
            buttons.append([{
                'text': i18n.get('telegram.order.track', language),
                'callback_data': f'track_order_{order_id}'
            }])

        # PAY and CANCEL were one `order_status == 'pending'` block and had to
        # SPLIT. Payability now runs through delivery (case B), but
        # `OrderService.cancel_order` still refuses DELIVERED/CANCELLED, so
        # widening the pair wholesale would have granted customers a self-cancel
        # button on an out-for-delivery order that can only fail.
        if may_pay:
            buttons.append([{
                'text': i18n.get('telegram.payment.pay_now', language),
                'callback_data': f'payment_retry_{order_id}'
            }])
        if may_cancel:
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
    def quantity_selector(
        language: str = 'en',
        back_callback: str = None,
    ) -> InlineKeyboardMarkup:
        """Quantity selection keyboard.

        Three flows render this: subscription creation, add-an-item and
        update-an-item. The first two came from a product list, so the default
        Back returns there; the update flow never showed one and passes the
        item-management menu instead. One callback per destination is what
        makes each Back landable — `back_to_product_selection` used to be the
        only option and, in the update flow, meant nothing.
        """
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
            [{
                'text': i18n.get('telegram.back', language),
                'callback_data': back_callback or 'back_to_product_selection',
            }]
        ]
        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def payment_methods(
        available_methods: List[Dict[str, Any]],
        language: str = 'en',
        back_callback: str = 'back_to_address_selection',
    ) -> InlineKeyboardMarkup:
        """Payment method selection for subscription.

        Buttons are derived from GET /payments/methods so the subscription menu
        can never diverge from checkout. Callback data is `sub_payment_<type>`;
        handlers MUST parse it with split('_', 2)[2] because `business_account`
        contains an underscore.

        Two screens render this: the creation flow, whose previous step was the
        address list (the default Back), and the "change payment method" screen
        reached from an existing subscription's edit menu, which passes
        `edit_sub_<id>`. One Back callback for both would have to mean two
        different destinations, so it meant neither and the button was dead.
        """
        from payment_methods import build_payment_method_buttons

        buttons = [
            [{'text': option['name'], 'callback_data': f"sub_payment_{option['type']}"}]
            for option in build_payment_method_buttons(available_methods, language)
        ]
        buttons.append([{'text': i18n.get('telegram.back', language), 'callback_data': back_callback}])
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
                {'text': i18n.get('telegram.profile.my_bottles', language), 'callback_data': 'my_bottles'}
            ],
            [{'text': i18n.get('telegram.back', language), 'callback_data': 'back_to_main'}]
        ]

        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def profile_edit_menu(language: str = 'en') -> InlineKeyboardMarkup:
        """Profile field-edit sub-menu (Name / Birthday / Language / Phone)."""
        buttons = [
            [
                {'text': i18n.get('telegram.profile.edit_field_name', language), 'callback_data': 'edit_profile_name'},
                {'text': i18n.get('telegram.profile.edit_field_birthday', language), 'callback_data': 'edit_profile_birthday'}
            ],
            [
                {'text': i18n.get('telegram.profile.edit_field_language', language), 'callback_data': 'edit_profile_language'},
                {'text': i18n.get('telegram.profile.edit_field_phone', language), 'callback_data': 'edit_profile_phone'}
            ],
            [{'text': i18n.get('telegram.back', language), 'callback_data': 'menu_profile'}]
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
    def location_request(language: str = 'en', *, extra_rows: Sequence[str] = ()) -> ReplyKeyboardMarkup:
        """Location-request keyboard: the share button, plus any plain rows.

        One builder, because the three that preceded it constructed the SAME
        KeyboardButton(request_location=True) from the same translation key and
        differed only in which plain buttons sat underneath.

        `one_time_keyboard=True` hides the keyboard after a press but does NOT
        restore whatever keyboard preceded it — every caller must therefore send
        an explicit keyboard (or ReplyKeyboardRemove) when the flow moves on.
        """
        rows = [[KeyboardButton(
            text=i18n.get('telegram.address.share_location_button', language),
            request_location=True,
        )]]
        rows.extend([[KeyboardButton(text=label)] for label in extra_rows])
        return ReplyKeyboardMarkup(rows, one_time_keyboard=True, resize_keyboard=True)

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
    def payment_failed(
        order_id: int,
        language: str = 'en',
        may_pay: bool = True,
        may_cancel: bool = True,
    ) -> InlineKeyboardMarkup:
        """Recovery options for an order that exists but is not paid.

        Every button carries `order_id` (My Orders needs none — it lists them),
        so the whole screen keeps working against THAT order after a bot
        restart, when `context.user_data` is gone. Rendered both when a payment
        is cancelled and when the payment link could not be created at all
        (`confirm_order`), which is why My Orders is here: after a failed link
        the customer's first question is "so does my order exist?", and the
        answer has to be one tap away.

        ``may_pay`` is `customer_may_pay(order)` and ``may_cancel`` is
        `customer_may_cancel(order)` — THE SAME SPLIT `order_details` applies,
        and it belongs here for the same reason. Before B3 a DELIVERED order
        could never reach this screen; it can now, because `retry_payment`
        renders it on any link-creation failure and case B is precisely the
        population B3 routes here. `OrderService.cancel_order` still refuses
        DELIVERED, so an unconditional Cancel button hands a customer reading
        Uzbek the backend's raw English "Order cannot be cancelled".

        Both default True for the one caller that holds no order —
        `cancel_payment`, whose screen by construction runs on an order whose
        payment attempt just ended without money moving — so that caller's
        behaviour is unchanged rather than fail-closed into a recovery screen
        with nothing to recover with.

        SWITCH METHOD IS DELIBERATELY ABSENT. `payment_switch_{id}` parsed the
        order id, logged it, and then rendered `OrderKeyboards.payment_methods`,
        whose callbacks (`payment_cash` / `payment_card`) carry NO order id and
        route to `orders.payment_handler` -> `_show_order_confirmation` — the
        CART checkout screen. So it never moved the named order's rail, and
        because an unpaid order deliberately keeps its cart, the Confirm button
        it leads to places a SECOND order for the same basket: the exact
        double-order this screen's "your order is placed" copy exists to
        prevent. The rail move a customer really has is Retry —
        POST /api/v1/payments/create normalizes card->click and flips a pending
        cash order onto Click behind the marking-code pool guard. Two
        expressions of one affordance, one of them broken; this is the deletion.
        """
        buttons = []
        if may_pay:
            buttons.append([{'text': i18n.get('telegram.payment.retry', language),
                             'callback_data': f'payment_retry_{order_id}'}])
        buttons.append([{'text': i18n.get('telegram.menu.orders', language),
                         'callback_data': 'menu_orders'}])
        if may_cancel:
            buttons.append([{'text': i18n.get('telegram.payment.cancel_order', language),
                             'callback_data': f'cancel_order_{order_id}'}])
        return KeyboardBuilder.build_inline_keyboard(buttons)

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
