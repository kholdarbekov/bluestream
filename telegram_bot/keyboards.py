"""
Telegram keyboard layouts and UI components
"""
import re
from typing import Callable, List, Dict, Literal, NamedTuple, Optional, Any, Sequence, Tuple
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from i18n import i18n
from config import config
from utils import format_date
from shared.constants import ORDER_STATUS_ICONS, SUBSCRIPTION_STATUS_ICONS, DEFAULT_STATUS_ICON
from shared import reorder_reminder_copy


MAX_DISPLAYED_ADDRESSES = 5


# ---------------------------------------------------------------------------
# An order's Pay and its removal (Skip / Cancel / Decline), in ONE place (spec 2026-10-27 §4.2, §5.1-§5.3).
#
# The backend publishes both answers on every order payload (`customer_actions`). The bot holds no copy of either
# rule: it draws what is allowed, with the label it was given, and every tap goes back to the one write route that
# enforces the same function (`retry-payment`, `cancel`). Each callback carries what the screen showed, so a screen
# that went stale is answered 409 instead of acted on. The builders and parsers live together so the string this
# module renders and the one the handlers read back cannot drift apart.
# ---------------------------------------------------------------------------

# `payment_retry_<id>_<amount>`: Pay, with the amount the screen showed. `payment_retry_<id>` and
# `payment_switch_<id>` are the shapes earlier releases drew; Telegram keeps those messages forever.
_PAY_CALLBACK = re.compile(r'^payment_(?:retry|switch)_(\d+)(?:_(\d+\.\d{2}))?$')
# `cancel_order_<id>_confirm_yes_<via>` writes a cancel or a decline; `cancel_order_<id>_skip_<reason|none>` a skip.
# The reason codes are the backend's (`customer_actions.cancel.reasons`), so the bot keeps no list of them.
_REMOVE_CALLBACK = re.compile(r'^cancel_order_(\d+)_(?:confirm_yes_([a-z]+)|skip_([a-z_]+))$')
_SKIP_WITHOUT_REASON = 'none'


def pay_callback(order_id: int, expected_amount: str) -> str:
    """The Pay button's callback: the order and the `expected_amount` its published Pay carried."""
    return f"payment_retry_{order_id}_{expected_amount}"


def parse_pay_callback(data: Optional[str]) -> Optional[Tuple[int, Optional[str]]]:
    """`(order_id, expected_amount)` off a Pay callback; the amount is None for a button from before it rode there."""
    match = _PAY_CALLBACK.match(data or '')
    if not match:
        return None
    return int(match.group(1)), match.group(2)


# ---------------------------------------------------------------------------
# A subscription's schedule and item edits (spec 2026-10-27 §6.2, §7).
#
# A schedule change is previewed and then saved, in two taps: `subsched_<freq>_<day|x>_<slot|any>_<sub>` asks the
# backend for the preview, `subschedok_<same>_<state>` saves exactly what that preview showed, carrying its
# `expected_state`. An items change is the same two taps with the whole list rebuilt from the card
# (`subitems_<op>_<sub>_<product>_<qty>_<state>`). Everything the write needs rides the callback, so the screen
# survives the restart that empties `user_data`; the longest of these is 52 bytes, inside Telegram's 64.
# ---------------------------------------------------------------------------

_SCHEDULE_CALLBACK = re.compile(
    r'^subsched(ok)?_(daily|weekly|biweekly|monthly)_(\d+|x)_(\d+|any)_(\d+)(?:_([0-9a-f]{16}))?$'
)
_ITEMS_SAVE_CALLBACK = re.compile(r'^subitems_(add|set|del)_(\d+)_(\d+)_(\d+)_([0-9a-f]{16})$')


class ScheduleChoice(NamedTuple):
    """What a schedule callback carries. `day` and `slot_id` are None for daily and for Any time; `state` is the
    preview's `expected_state` on a save (`subschedok_`), None on a preview request (`subsched_`)."""
    frequency: str
    day: Optional[int]
    slot_id: Optional[int]
    subscription_id: int
    state: Optional[str]


def schedule_callback(frequency: str, day: Optional[int], slot_id: Optional[int], subscription_id: int,
                      state: Optional[str] = None) -> str:
    """`subsched_<freq>_<day|x>_<slot|any>_<sub>`, or `subschedok_<same>_<state>` once there is a preview state."""
    prefix = 'subsched' if state is None else 'subschedok'
    body = f"{prefix}_{frequency}_{'x' if day is None else day}_{'any' if slot_id is None else slot_id}_{subscription_id}"
    return body if state is None else f"{body}_{state}"


def parse_schedule_callback(data: Optional[str]) -> Optional[ScheduleChoice]:
    """The `ScheduleChoice` off a `subsched_` or `subschedok_` callback, else None. A save without its state, or a
    preview request with one, is not a schedule callback."""
    match = _SCHEDULE_CALLBACK.match(data or '')
    if not match:
        return None
    is_save, frequency, day, slot, subscription_id, state = match.groups()
    if bool(is_save) != (state is not None):
        return None
    return ScheduleChoice(
        frequency, None if day == 'x' else int(day), None if slot == 'any' else int(slot), int(subscription_id), state,
    )


def items_save_callback(op: Literal['add', 'set', 'del'], subscription_id: int, product_id: int, quantity: int,
                        state: str) -> str:
    """`subitems_<op>_<sub>_<product>_<qty>_<state>`: the one line the preview showed, and the state it carried."""
    return f"subitems_{op}_{subscription_id}_{product_id}_{quantity}_{state}"


def parse_items_save_callback(data: Optional[str]) -> Optional[Tuple[str, int, int, int, str]]:
    """`(op, subscription_id, product_id, quantity, state)` off an items save callback, else None."""
    match = _ITEMS_SAVE_CALLBACK.match(data or '')
    if not match:
        return None
    op, subscription_id, product_id, quantity, state = match.groups()
    return op, int(subscription_id), int(product_id), int(quantity), state


# ---------------------------------------------------------------------------
# A subscription's payment method and delivery address changes (spec 2026-10-27 §7; step 4 Task 15b).
#
# Each is previewed, then saved with the preview's `expected_state` riding the callback:
#   `subpay_<method>_<sub>` shows the consequence, `subpayok_<method>_<sub>_<state>` writes;
#   `subaddr_<address>_<sub>` previews a new address, `subaddrpay_<method>_<address>_<sub>` is the method asked for
#   when the current one is not offered there, `subaddrok_<address>_<method|keep>_<sub>_<state>` writes.
# The longest is `subaddrok_9999999_business_account_9999999_<16 hex>`: 59 bytes, inside Telegram's 64.
# ---------------------------------------------------------------------------

_PAYMENT_SAVE_CALLBACK = re.compile(r'^subpayok_([a-z_]+)_(\d+)_([0-9a-f]{16})$')
_ADDRESS_SAVE_CALLBACK = re.compile(r'^subaddrok_(\d+)_([a-z_]+)_(\d+)_([0-9a-f]{16})$')
_ADDRESS_METHOD_CALLBACK = re.compile(r'^subaddrpay_([a-z_]+)_(\d+)_(\d+)$')
_KEEP_METHOD = 'keep'


def payment_save_callback(method: str, subscription_id: int, state: str) -> str:
    """`subpayok_<method>_<sub>_<state>`: the method the consequence screen named, and the state it carried."""
    return f"subpayok_{method}_{subscription_id}_{state}"


def parse_payment_save_callback(data: Optional[str]) -> Optional[Tuple[str, int, str]]:
    """`(method, subscription_id, state)` off a payment save callback, else None."""
    match = _PAYMENT_SAVE_CALLBACK.match(data or '')
    return (match.group(1), int(match.group(2)), match.group(3)) if match else None


def address_method_callback(method: str, address_id: int, subscription_id: int) -> str:
    """`subaddrpay_<method>_<address>_<sub>`: the method picked for the new address (the second preview)."""
    return f"subaddrpay_{method}_{address_id}_{subscription_id}"


def parse_address_method_callback(data: Optional[str]) -> Optional[Tuple[str, int, int]]:
    """`(method, address_id, subscription_id)` off an address-method callback, else None."""
    match = _ADDRESS_METHOD_CALLBACK.match(data or '')
    return (match.group(1), int(match.group(2)), int(match.group(3))) if match else None


def address_confirm_callback(address_id: int, method: Optional[str], subscription_id: int, state: str) -> str:
    """`subaddrok_<address>_<method|keep>_<sub>_<state>`: the address change to save, the method picked for it (or
    `keep`, the current one stays), and the preview's `expected_state`."""
    return f"subaddrok_{address_id}_{method or _KEEP_METHOD}_{subscription_id}_{state}"


def parse_address_confirm_callback(data: Optional[str]) -> Optional[Tuple[int, Optional[str], int, str]]:
    """`(address_id, method or None, subscription_id, state)` off an address save callback, else None."""
    match = _ADDRESS_SAVE_CALLBACK.match(data or '')
    if not match:
        return None
    address_id, method, subscription_id, state = match.groups()
    return int(address_id), None if method == _KEEP_METHOD else method, int(subscription_id), state


def _published_action(order: Optional[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    action = ((order or {}).get('customer_actions') or {}).get(name)
    return action if isinstance(action, dict) and action.get('allowed') else None


def published_pay(order: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The order's published Pay when it is allowed, else None. A payload without the field fails closed."""
    return _published_action(order, 'pay')


def published_cancel(order: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The order's published removal (skip, cancel or decline) when it is allowed, else None. Fails closed."""
    return _published_action(order, 'cancel')


def order_remove_callback(order_id: int, via: str, reason: Optional[str] = None) -> str:
    """The callback that writes a removal: the `via` the screen showed, and for a skip its reason (or none)."""
    if via == 'skip':
        return f"cancel_order_{order_id}_skip_{reason or _SKIP_WITHOUT_REASON}"
    return f"cancel_order_{order_id}_confirm_yes_{via}"


def parse_order_remove_callback(data: Optional[str]) -> Optional[Tuple[int, str, Optional[str]]]:
    """`(order_id, via, reason_code)` off a removal write; None for every other cancel callback.

    The legacy `cancel_order_<id>_confirm_yes` (it named no `via`) and `cancel_order_<id>_confirm_no` are not
    writes, so they parse to None.
    """
    match = _REMOVE_CALLBACK.match(data or '')
    if not match:
        return None
    order_id = int(match.group(1))
    if match.group(2):
        return order_id, match.group(2), None
    reason = match.group(3)
    return order_id, 'skip', None if reason == _SKIP_WITHOUT_REASON else reason


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
# (`order_remove_callback` above): the Application is built
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


# My orders' pager: the pattern `bot.py` registers and `OrderHandlers.orders_menu` reads the page
# back with. `OrderKeyboards.order_list` renders it.
ORDERS_PAGE_PATTERN = r"^orders_page_(\d+)$"


def orders_page_callback(page: int) -> str:
    return f"orders_page_{page}"


# The quantity screen's Checkout. The screen is a draft the cart has not seen, so the product and
# the number on screen ride on the button. `bot.py` registers the pattern and
# `ProductHandlers.checkout_product` reads it back.
BUY_PATTERN = r"^buy_(\d+)_(\d+)$"


def buy_callback(product_id: int, quantity: int) -> str:
    return f"buy_{product_id}_{quantity}"


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
    def build_reply_keyboard(buttons: List[List[Dict[str, str]]],
                           one_time: bool = False,
                           resize: bool = True,
                           is_persistent: bool = False) -> ReplyKeyboardMarkup:
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
            resize_keyboard=resize,
            is_persistent=is_persistent,
        )


# The customer's bottom-keyboard buttons, as translation keys in layout order.
# Rendering (the keyboard itself), the flow exit and the label router in bot.py
# all read this one tuple, so a label is recognised wherever it is drawn.
MAIN_MENU_KEYS: tuple[str, ...] = (
    "telegram.menu.products",
    "telegram.menu.orders",
    "telegram.cart_title",
    "telegram.menu.subscriptions",
    "telegram.menu.loyalty",
    "telegram.menu.profile",
    "telegram.menu.support",
)


# How many MAIN_MENU_KEYS go on each row of the bottom keyboard, top to bottom.
_BOTTOM_MENU_ROW_SIZES: tuple[int, ...] = (1, 2, 2, 2)


class MenuKeyboards:
    """Main menu keyboards"""

    @staticmethod
    def bottom_menu(language: str = 'en', show_loyalty: bool = True) -> ReplyKeyboardMarkup:
        """The persistent bottom keyboard. Send it only through
        ``menu_keyboard.send_menu_keyboard``, which is what keeps the install marker true."""
        # Lazy: menu_keyboard imports this module.
        from menu_keyboard import wanted_menu_labels

        labels = wanted_menu_labels(language, show_loyalty)
        rows = []
        start = 0
        for size in _BOTTOM_MENU_ROW_SIZES:
            row = [{'text': labels[key]} for key in MAIN_MENU_KEYS[start:start + size] if key in labels]
            start += size
            if row:
                rows.append(row)
        return KeyboardBuilder.build_reply_keyboard(rows, one_time=False, resize=True, is_persistent=True)

    @staticmethod
    def back_button(language: str, callback_data: str) -> InlineKeyboardMarkup:
        """One Back button to the screen's real parent. No default: nobody can mint `back_to_main`."""
        buttons = [
            [{'text': i18n.get('telegram.back', language), 'callback_data': callback_data}]
        ]
        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def cancel_button(language: str = 'en') -> InlineKeyboardMarkup:
        """Simple cancel button"""
        buttons = [
            [{'text': i18n.get('telegram.cancel', language), 'callback_data': 'cancel_action'}]
        ]
        return KeyboardBuilder.build_inline_keyboard(buttons)


class LanguageKeyboards:
    """Language selection keyboards"""
    @staticmethod
    def select_language(referral_code: str = None) -> InlineKeyboardMarkup:
        """Language selection keyboard on start.

        ``set_language_<lang>``, optionally ``set_language_<lang>_ref<code>``.

        The referral rides the BUTTON because it has nowhere durable to live
        before the account exists. `_capture_referral_arg` puts it in
        `context.user_data` at /start, and it is not read until the registration
        POST several screens later — but the customer has no `users` row yet, so
        there is no `bot_state` to dual-write it to, and a deploy in that window
        dropped it silently AND unrecoverably: their natural recovery is to
        re-send /start, which arrives with no args. The referrer is never
        credited and the acquisition channel is misattributed.

        The language stays at index 2 (`split('_')[2]`) precisely so appending
        this suffix cannot move it — the parsers used to read `[-1]`.

        Omitted rather than truncated when it would push callback_data past
        Telegram's 64-byte limit: a rejected keyboard would cost the customer
        the whole signup, which is strictly worse than losing an attribution.
        """
        buttons = []

        for lang_code in config.localization.supported_languages:
            flag = i18n.get_language_flag(lang_code)
            name = i18n.get_language_name(lang_code, lang_code)
            callback = f'set_language_{lang_code}'
            if referral_code:
                with_referral = f'{callback}_ref{referral_code}'
                if len(with_referral.encode('utf-8')) <= 64:
                    callback = with_referral
            buttons.append([{
                'text': f"{flag} {name}",
                'callback_data': callback
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

        # Back goes to the screen this one opens from: My Profile
        buttons.append([{
            'text': i18n.get('telegram.back', current_language),
            'callback_data': 'menu_profile'
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

        # Back returns to the category list. With a single category that list is skipped, so
        # there is no Back row at all: the bottom keyboard is the way out.
        if not single_category:
            buttons.append([{
                'text': i18n.get('telegram.back', language),
                'callback_data': 'back_to_categories',
            }])

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
                         max_quantity: int, orderable: bool,
                         back_callback: str) -> InlineKeyboardMarkup:
        """Quantity selection keyboard with offset-based presets + fine-tune row.

        Layout:
            [ +3 ] [ +6 ] [ +10 ] [ +13 ] [ +18 ]   (preset jumps from min)
            [ −1 ]     {qty}     [ +1 ]              (fine-tune)
            [ Checkout ]                             (puts {qty} in the cart)
            [ Back ]

        ``min_order_qty``, ``max_quantity`` and ``orderable`` are the purchase
        bounds the caller resolved (``ProductHandlers._purchase_bounds`` /
        ``_is_orderable``) and are REQUIRED: a default here would be a second,
        silent opinion about what the customer may order, and the sold-out case
        is precisely where the default would be wrong. A product nothing of
        which is orderable gets Back alone.
        """
        buttons: List[List[Dict[str, str]]] = []

        if orderable:
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
            buttons.append([{
                'text': i18n.get('telegram.cart.checkout', language),
                'callback_data': buy_callback(product_id, current_quantity),
            }])
        buttons.append([{'text': i18n.get('telegram.back', language), 'callback_data': back_callback}])

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
                ]
            ]
        else:
            # Show checkout button only if minimum is met
            if meets_minimum:
                buttons = [
                    [{'text': i18n.get('telegram.cart.checkout', language), 'callback_data': 'cart_checkout'}],
                    [
                        {'text': i18n.get('telegram.cart.clear', language), 'callback_data': 'cart_clear'},
                        {'text': i18n.get('telegram.cart.continue_shopping', language), 'callback_data': 'menu_products'}
                    ]
                ]
            else:
                # Minimum not met - show warning and no checkout button
                buttons = [
                    [{'text': '⚠️ ' + i18n.get('telegram.cart.add_more', language), 'callback_data': 'menu_products'}],
                    [
                        {'text': i18n.get('telegram.cart.clear', language), 'callback_data': 'cart_clear'},
                    ]
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
        """Payment method selection.

        `method['name']` is already the full button label — icon included.
        It comes from `payment_methods.build_payment_method_buttons`, which
        reads `telegram.payment_cash` / `telegram.payment_card` /
        `telegram.payment_business_account` (each seeded with its own emoji),
        and `orders._show_payment_picker` may additionally append a discount
        suffix (e.g. "−3% 🏷") onto the cash entry before calling this.
        Prepending a SECOND icon here used to double it — 💵💰 Naqd pul,
        💳💳 Karta — so the translation is now the single source of the icon.
        """
        buttons = [
            [{
                'text': method['name'],
                'callback_data': f"payment_{method['type']}"
            }]
            for method in methods
        ]

        buttons.append([{
            'text': i18n.get('telegram.back', language),
            'callback_data': 'back_to_delivery'
        }])

        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def order_confirmation(
        language: str = 'en', meets_minimum: bool = True, has_reward: bool = False,
        show_reward: bool = True, address_id: int = None,
    ) -> InlineKeyboardMarkup:
        """Order confirmation buttons.

        When ``meets_minimum`` is False, the Confirm button is replaced by a
        non-actionable warning so the user must go back and fix the cart
        before placing the order.

        ``has_reward`` toggles the loyalty-reward row: an "Apply reward" button
        when none is selected, or "Change"/"Remove" buttons when one is. The row
        is only shown once the order meets the minimum (it's part of placing the
        order, not fixing the cart). "Apply reward" shares its row with Edit;
        Change/Remove fill theirs, so Edit gets its own.

        There is no Change address button (owner 2026-10-23): Back reaches the
        payment picker, whose Back reaches the address step in every flow.
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
        edit_button = {'text': i18n.get('telegram.order.edit', language), 'callback_data': 'edit_order'}

        if meets_minimum and show_reward and has_reward:
            buttons.append([
                {'text': '🎁 ' + i18n.get('telegram.loyalty.change_reward', language),
                 'callback_data': 'checkout_choose_reward'},
                {'text': '🗑 ' + i18n.get('telegram.loyalty.remove_reward', language),
                 'callback_data': 'checkout_remove_reward'},
            ])
            buttons.append([edit_button])
        elif meets_minimum and show_reward:
            buttons.append([
                {'text': '🎁 ' + i18n.get('telegram.loyalty.apply_reward', language),
                 'callback_data': 'checkout_choose_reward'},
                edit_button,
            ])
        else:
            buttons.append([edit_button])

        # The address travels WITH the Back button. Without it the handler has
        # to read `selected_address_id` out of `context.user_data`, which a
        # deploy empties while this card stays tappable forever — the
        # 2026-09-03 `KeyError: 'selected_address_id'`. `None` keeps the legacy
        # shape for any caller that has no address to name; the handler answers
        # that honestly instead of raising.
        back_callback = (
            'back_to_payment' if address_id is None else f'back_to_payment_{address_id}'
        )
        buttons.append([{'text': i18n.get('telegram.back', language), 'callback_data': back_callback}])

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
    def order_list(orders: List[Dict], language: str = 'en', *, page: int = 1,
                   has_prev: bool = False, has_next: bool = False) -> InlineKeyboardMarkup:
        """One page of the order list, then Previous / Next where the backend's pagination
        (`has_prev` / `has_next`) says there is another page."""
        buttons = []

        for order in orders:
            # The backend's customer-facing status (F15), so an order awaiting a new
            # delivery date does not wear its unchanged order status's icon.
            icon = ORDER_STATUS_ICONS.get(order.get('display_status') or order['status'], DEFAULT_STATUS_ICON)
            date = order['created_at'][:10] if 'created_at' in order else ''

            buttons.append([{
                'text': f"{icon} Order #{order['order_number']} - {date}",
                'callback_data': f"order_{order['id']}"
            }])

        nav_row = []
        if has_prev:
            nav_row.append({
                'text': i18n.get('telegram.pagination.previous', language),
                'callback_data': orders_page_callback(page - 1),
            })
        if has_next:
            nav_row.append({
                'text': i18n.get('telegram.pagination.next', language),
                'callback_data': orders_page_callback(page + 1),
            })
        if nav_row:
            buttons.append(nav_row)

        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def order_details(
        order_id: int,
        order_status: str,
        language: str,
        *,
        pay: Optional[Dict[str, Any]],
        cancel: Optional[Dict[str, Any]],
    ) -> InlineKeyboardMarkup:
        """Order details action buttons.

        ``pay`` and ``cancel`` are the order's published answers (`published_pay` / `published_cancel`), passed
        by the caller that fetched the order: each button is drawn only when the backend allows it, with the
        label it published. Pay carries the amount the screen showed; the removal (skip, cancel or decline) opens
        its confirmation, which names the consequence.
        """
        buttons = []

        # Add track button for active orders
        if order_status in ['confirmed', 'preparing', 'out_for_delivery']:
            buttons.append([{
                'text': i18n.get('telegram.order.track', language),
                'callback_data': f'track_order_{order_id}'
            }])

        if pay:
            buttons.append([{'text': pay['label'], 'callback_data': pay_callback(order_id, pay['expected_amount'])}])
        if cancel:
            buttons.append([{'text': cancel['label'], 'callback_data': f'cancel_order_{order_id}'}])

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
    def remove_confirmation(order_id: int, cancel: Dict[str, Any], language: str) -> InlineKeyboardMarkup:
        """The confirmation under an order's published removal (spec 2026-10-27 §5.3).

        A skip asks its reason with the backend's own chips, or none, and keeps the delivery. A cancel or a
        decline is confirmed with its published label, or the order is kept. Every write names the `via` the
        screen showed; the keep button goes back to the order.
        """
        keep = f'cancel_order_{order_id}_confirm_no'
        if cancel.get('via') == 'skip':
            buttons = [
                [{'text': reason['label'], 'callback_data': order_remove_callback(order_id, 'skip', reason['code'])}]
                for reason in cancel.get('reasons') or []
            ]
            buttons.append([{
                'text': i18n.get('telegram.orders.skip_no_reason', language),
                'callback_data': order_remove_callback(order_id, 'skip'),
            }])
            buttons.append([{'text': i18n.get('telegram.orders.keep_delivery', language), 'callback_data': keep}])
        else:
            buttons = [
                [{'text': cancel['label'], 'callback_data': order_remove_callback(order_id, cancel['via'])}],
                [{'text': i18n.get('telegram.orders.keep_order', language), 'callback_data': keep}],
            ]
        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def order_tracking(order_id: int, language: str = 'en') -> InlineKeyboardMarkup:
        """The way back from one order's screen: to its details, or to the orders list.

        The Track screen's buttons, and the refused cancel's (F5), which would otherwise leave
        the customer on a card with nothing to tap."""
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
    def asl_belgisi_error(language: str = 'en', *, cod_restricted: bool = False) -> InlineKeyboardMarkup:
        """Shown when Tax Committee (Asl belgisi) is unavailable during order creation.

        Offers the cash rescue and a retry of the card order. ``cod_restricted`` is the 503's published
        `payment_restrictions.cod_restricted`: a customer the cash-debt limit keeps off cash is offered the retry
        only (the backend's message already says so), never a button the rescue would refuse."""
        buttons = []
        if not cod_restricted:
            buttons.append([{'text': i18n.get('telegram.orders.asl_belgisi_switch_cash', language),
                             'callback_data': 'select_payment_cash'}])
        buttons.append([{'text': i18n.get('telegram.orders.asl_belgisi_retry', language),
                         'callback_data': 'confirm_order'}])
        return KeyboardBuilder.build_inline_keyboard(buttons)


class SubscriptionKeyboards:
    """Subscription-related keyboards"""

    # The 1/2/3/4/5/10 ladder, as steps above the product's minimum order quantity.
    QUANTITY_LADDER_OFFSETS = (0, 1, 2, 3, 4, 9)

    @staticmethod
    def subscription_frequency(
        language: str = 'en',
        subscription_id: int = None,
    ) -> InlineKeyboardMarkup:
        """Subscription frequency selection.

        Two screens render this, and they must not share a callback namespace.

        * CREATION (`subscription_id=None`) keeps `subscription_freq_<freq>`,
          which the creation conversation claims in its SELECT_ADDRESS state.
        * EDIT (an id given) emits `subfreq_<freq>_<id>`, claimed TOP LEVEL.

        Reusing the creation namespace for the edit screen is what left the edit
        buttons dead: the only handler for `^subscription_freq_` lives inside the
        creation conversation, so with no creation flow open the tap reached
        nothing at all. Carrying the id also means the screen survives the
        restart that empties `user_data`.
        """
        def freq_callback(frequency: str) -> str:
            if subscription_id is None:
                return f'subscription_freq_{frequency}'
            return f'subfreq_{frequency}_{subscription_id}'

        buttons = [
            [
                {'text': i18n.get('telegram.subscription.frequency_daily', language), 'callback_data': freq_callback('daily')},
                {'text': i18n.get('telegram.subscription.frequency_weekly', language), 'callback_data': freq_callback('weekly')}
            ],
            [
                {'text': i18n.get('telegram.subscription.frequency_biweekly', language), 'callback_data': freq_callback('biweekly')},
                {'text': i18n.get('telegram.subscription.frequency_monthly', language), 'callback_data': freq_callback('monthly')}
            ],
            # The edit screen returns to the subscription being edited; creation returns to the list.
            [{'text': i18n.get('telegram.back', language),
              'callback_data': 'back_to_subscriptions' if subscription_id is None else f'subscription_{subscription_id}'}]
        ]

        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def subscription_list(subscriptions: List[Dict], language: str = 'en') -> InlineKeyboardMarkup:
        """Subscription list keyboard"""
        buttons = []

        for sub in subscriptions:
            icon = SUBSCRIPTION_STATUS_ICONS.get(sub['status'], DEFAULT_STATUS_ICON)
            buttons.append([{
                'text': f"{icon} {sub['name']} - {SubscriptionKeyboards.frequency_label(sub.get('frequency'), language)}",
                'callback_data': f"subscription_{sub['id']}"
            }])

        buttons.append([{'text': i18n.get('telegram.subscription.create', language), 'callback_data': 'create_subscription'}])

        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def item_name(item: Dict, language: str = 'en') -> str:
        """A subscription item's name as the backend publishes it (``product_name``, spec 2026-10-26 B10). A
        deleted product is published as ``product: None`` with no name, and reads as unknown."""
        return (
            item.get('product_name')
            or (item.get('product') or {}).get('name')
            or i18n.get('telegram.common.unknown', language)
        )

    @staticmethod
    def is_allowed(allowed_actions: Optional[Dict], action: str) -> bool:
        """Whether the backend's published ``allowed_actions`` allows ``action``. The bot never derives this
        from the subscription's status: a payload that publishes nothing offers nothing."""
        return bool(((allowed_actions or {}).get(action) or {}).get('allowed'))

    @staticmethod
    def subscription_actions(subscription_id: int, allowed_actions: Optional[Dict], language: str = 'en') -> InlineKeyboardMarkup:
        """Subscription card buttons, in the order Pay, Skip, Pause/Resume, Edit, Items, Orders, Cancel, Back.

        Exactly the actions the backend published as allowed (spec 2026-10-26 §6), and Pay, Skip, Pause, Resume
        and Cancel wear the label it published (spec 2026-10-27 §16 M3), so the bot and the website say the same
        words. Edit, Items, Orders and Back are the bot's own. Pay carries the amount the screen showed, and Skip
        names the delivery it will skip (``skip_sub_<id>_<yyyy-mm-dd>``), so the backend can refuse either with a
        409 when the card has gone stale. Pause and Cancel ask first (`action_confirmation`)."""
        allow = lambda action: SubscriptionKeyboards.is_allowed(allowed_actions, action)  # noqa: E731
        buttons = []

        if allow('pay'):
            pay = allowed_actions['pay']
            buttons.append([{'text': pay['label'], 'callback_data': pay_callback(pay['order_id'], pay['expected_amount'])}])
        if allow('skip'):
            skip = allowed_actions['skip']
            skip_date = skip.get('date')
            buttons.append([{
                'text': skip['label'],
                'callback_data': f'skip_sub_{subscription_id}_{skip_date}' if skip_date else f'skip_sub_{subscription_id}'
            }])
        if allow('pause'):
            buttons.append([{'text': allowed_actions['pause']['label'], 'callback_data': f'pause_sub_{subscription_id}'}])
        if allow('resume'):
            buttons.append([{'text': allowed_actions['resume']['label'], 'callback_data': f'resume_sub_{subscription_id}'}])
        if allow('edit'):
            buttons.append([{'text': i18n.get('telegram.subscription.edit', language), 'callback_data': f'edit_sub_{subscription_id}'}])
            buttons.append([{'text': i18n.get('telegram.subscription.manage_items', language), 'callback_data': f'manage_items_{subscription_id}'}])
        buttons.append([{'text': i18n.get('telegram.subscription.orders', language), 'callback_data': f'subhist_{subscription_id}_1'}])
        if allow('cancel'):
            buttons.append([{'text': allowed_actions['cancel']['label'], 'callback_data': f'cancel_sub_{subscription_id}'}])
        buttons.append([{'text': i18n.get('telegram.back', language), 'callback_data': 'back_to_subscriptions'}])

        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def action_confirmation(subscription_id: int, action: str, allowed_actions: Dict, language: str = 'en') -> InlineKeyboardMarkup:
        """The buttons under a consequence confirm (spec 2026-10-27 §5.3 D12, §7 D19), all in the backend's words.

        * ``pause`` and ``cancel``: its ``confirm_label`` writes (``confirmpause_sub_<id>_<state>`` /
          ``confirmcancel_sub_<id>_<state>``, the ``expected_state`` the card showed), its ``keep_label`` goes back.
        * ``skip``: one button per published reason (``skip_sub_<id>_<date>_<code>``), then none
          (``…_none``), then its ``keep_label``.
        The id, the 16-character state and the date keep every callback inside Telegram's 64 bytes."""
        published = allowed_actions[action]
        keep = {'text': published['keep_label'], 'callback_data': f'subscription_{subscription_id}'}
        if action == 'skip':
            prefix = f"skip_sub_{subscription_id}_{published['date']}"
            buttons = [
                [{'text': reason['label'], 'callback_data': f"{prefix}_{reason['code']}"}]
                for reason in published.get('reasons') or []
            ]
            buttons.append([{'text': i18n.get('telegram.orders.skip_no_reason', language), 'callback_data': f'{prefix}_none'}])
        else:
            buttons = [[{
                'text': published['confirm_label'],
                'callback_data': f"confirm{action}_sub_{subscription_id}_{allowed_actions['expected_state']}",
            }]]
        buttons.append([keep])
        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def history(subscription_id: int, rows: List[Dict], page: int, has_prev: bool, has_next: bool,
                language: str = 'en') -> InlineKeyboardMarkup:
        """One page of a subscription's order history. A row whose order publishes `customer_actions` gets
        its Pay (the amount it showed) and its removal (opens the order's own confirm), each with the backend's
        label; then Previous / Next where the backend says there is another page, and Back to the card."""
        buttons = []
        for row in rows:
            order_id = row.get('order_id')
            pay = published_pay(row)
            cancel = published_cancel(row)
            if pay:
                buttons.append([{
                    'text': f"{row['date_label']} · {pay['label']}",
                    'callback_data': pay_callback(order_id, pay['expected_amount']),
                }])
            if cancel:
                buttons.append([{
                    'text': f"{row['date_label']} · {cancel['label']}",
                    'callback_data': f'cancel_order_{order_id}',
                }])

        nav_row = []
        if has_prev:
            nav_row.append({
                'text': i18n.get('telegram.pagination.previous', language),
                'callback_data': f'subhist_{subscription_id}_{page - 1}',
            })
        if has_next:
            nav_row.append({
                'text': i18n.get('telegram.pagination.next', language),
                'callback_data': f'subhist_{subscription_id}_{page + 1}',
            })
        if nav_row:
            buttons.append(nav_row)
        buttons.append([{'text': i18n.get('telegram.back', language), 'callback_data': f'subscription_{subscription_id}'}])

        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def frequency_label(frequency: Optional[str], language: str = 'en') -> str:
        """The seeded name of a frequency (`daily`, `weekly`, `biweekly`, `monthly`), never the raw enum."""
        return i18n.get(f'telegram.subscription.frequency_{frequency}', language)

    @staticmethod
    def weekday_label(weekday: int, language: str = 'en') -> str:
        """The seeded name of a weekday, 0 = Monday (the backend's convention, Python's `weekday()`)."""
        return i18n.get(f'telegram.subscription.weekday_{weekday}', language)

    @staticmethod
    def day_picker(frequency: str, language: str = 'en', subscription_id: int = None, *,
                   delivery_weekdays: Sequence[int] = range(7)) -> InlineKeyboardMarkup:
        """The delivery day: a weekday for weekly and biweekly (0 = Monday), a day of the month for monthly.

        Weekly and biweekly offer only the ``delivery_weekdays`` the backend published (a weekday with no active
        slot is no delivery day, spec 2026-10-27 §6.4); the default is all seven, for a payload that carries none.

        Two screens render this, in separate callback namespaces for the reason `subscription_frequency`
        gives: CREATION (`subscription_id=None`) emits `subscription_day_<n>`, claimed inside the creation
        conversation's SELECT_DAY state and Back to the frequency list; EDIT emits
        `subday_<frequency>_<n>_<id>`, claimed top level, carrying everything the next step needs so it
        survives the restart that empties `user_data`.
        """
        def day_callback(day: int) -> str:
            if subscription_id is None:
                return f'subscription_day_{day}'
            return f'subday_{frequency}_{day}_{subscription_id}'

        if frequency == 'monthly':
            days = [{'text': str(day), 'callback_data': day_callback(day)} for day in range(1, 32)]
            buttons = [days[i:i + 7] for i in range(0, len(days), 7)]
        else:
            weekdays = [
                {'text': SubscriptionKeyboards.weekday_label(day, language), 'callback_data': day_callback(day)}
                for day in sorted(set(delivery_weekdays))
            ]
            buttons = [weekdays[i:i + 2] for i in range(0, len(weekdays), 2)]
        back = 'back_to_sub_frequency' if subscription_id is None else f'change_frequency_{subscription_id}'
        buttons.append([{'text': i18n.get('telegram.back', language), 'callback_data': back}])
        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def time_slot_picker(slots: List[Dict], language: str = 'en', subscription_id: int = None, *,
                         frequency: Optional[str] = None, day: Optional[int] = None) -> InlineKeyboardMarkup:
        """The delivery time slot: the slots the backend offered for the chosen day, then "Any time".

        Two screens render this, in separate callback namespaces for the reason `subscription_frequency`
        gives. CREATION (`subscription_id=None`) emits `subscription_slot_<id>` / `subscription_slot_any` and
        Back to the day. EDIT emits `schedule_callback(frequency, day, <slot|None>, subscription_id)`: the
        schedule being asked about rides every button, Any time included, and Back goes to the day picker
        (`subfreq_<frequency>_<id>`), or to the frequency list for daily, which has no day. `frequency` is
        required with a `subscription_id`: a button with no frequency would name a callback nothing claims."""
        if subscription_id is not None and frequency is None:
            raise ValueError('time_slot_picker needs the frequency of the schedule it edits')

        def slot_callback(slot_id: Optional[int]) -> str:
            if subscription_id is None:
                return 'subscription_slot_any' if slot_id is None else f'subscription_slot_{slot_id}'
            return schedule_callback(frequency, day, slot_id, subscription_id)

        buttons = [
            [{
                'text': f"{slot.get('name')} · {slot.get('time_range')}" if slot.get('name') else str(slot.get('time_range')),
                'callback_data': slot_callback(slot['id']),
            }]
            for slot in slots
        ]
        buttons.append([{'text': i18n.get('telegram.subscription.any_time', language), 'callback_data': slot_callback(None)}])
        if subscription_id is None:
            back = 'back_to_sub_day'
        elif frequency == 'daily':
            back = f'change_frequency_{subscription_id}'
        else:
            back = f'subfreq_{frequency}_{subscription_id}'
        buttons.append([{'text': i18n.get('telegram.back', language), 'callback_data': back}])
        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def quantity_selector(
        language: str = 'en',
        back_callback: str = None,
        *,
        min_order_qty: int,
    ) -> InlineKeyboardMarkup:
        """Quantity selection keyboard.

        Three flows render this: subscription creation, add-an-item and
        update-an-item. The first two came from a product list, so the default
        Back returns there; the update flow never showed one and passes the
        item-management menu instead. One callback per destination is what
        makes each Back landable — `back_to_product_selection` used to be the
        only option and, in the update flow, meant nothing.

        ``min_order_qty`` is REQUIRED and is the first button: a subscription line
        below it is one billing's `create_order` refuses every cycle. The fixed
        1/2/3/4/5/10 keyboard let a customer subscribe to 1 of a product whose
        minimum is 2 (prod subscription 8, 2026-10-04).
        """
        quantities = [min_order_qty + offset for offset in SubscriptionKeyboards.QUANTITY_LADDER_OFFSETS]
        buttons = [
            [{'text': str(qty), 'callback_data': f'sub_qty_{qty}'} for qty in quantities[:3]],
            [{'text': str(qty), 'callback_data': f'sub_qty_{qty}'} for qty in quantities[3:]],
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
        subscription_id: int = None,
        *,
        callback: Optional[Callable[[str], str]] = None,
    ) -> InlineKeyboardMarkup:
        """Payment method selection for subscription.

        Buttons are derived from GET /payments/methods so the subscription menu
        can never diverge from checkout.

        Callback data depends on which screen asked, for the same reason
        `subscription_frequency` splits its namespace: CREATION
        (`subscription_id=None`) keeps `sub_payment_<type>`, claimed inside the
        creation conversation's CONFIRM_SUBSCRIPTION state; EDIT emits
        `subpay_<type>_<id>`, claimed top level. `business_account` contains an
        underscore, so the type is everything between the prefix and the
        trailing id — never a fixed index.

        `callback(method) -> str` overrides both, for a screen with its own namespace (the address change's
        `subaddrpay_<method>_<address>_<sub>`). It receives the method code the backend published (`click`, not
        the "Card" button's name), because that is what the write must send.

        Two screens render this: the creation flow, whose previous step was the
        address list (the default Back), and the "change payment method" screen
        reached from an existing subscription's edit menu, which passes
        `edit_sub_<id>`. One Back callback for both would have to mean two
        different destinations, so it meant neither and the button was dead.
        """
        from payment_methods import build_payment_method_buttons, published_method

        def pay_callback(method_type: str) -> str:
            if callback is not None:
                return callback(published_method(method_type, available_methods))
            if subscription_id is None:
                return f'sub_payment_{method_type}'
            return f'subpay_{method_type}_{subscription_id}'

        buttons = [
            [{'text': option['name'], 'callback_data': pay_callback(option['type'])}]
            for option in build_payment_method_buttons(available_methods, language)
        ]
        buttons.append([{'text': i18n.get('telegram.back', language), 'callback_data': back_callback}])
        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def item_management_menu(subscription_id: int, items: List[Dict], allowed_actions: Optional[Dict],
                             language: str = 'en') -> InlineKeyboardMarkup:
        """Item management keyboard. Change and Add follow the backend's ``edit``, the bin its ``remove_item``
        (refused on the last item, and on a subscription that cannot be edited)."""
        can_edit = SubscriptionKeyboards.is_allowed(allowed_actions, 'edit')
        can_remove = SubscriptionKeyboards.is_allowed(allowed_actions, 'remove_item')
        buttons = []

        if can_edit:
            for item in items:
                item_id = item.get('id')
                product_name = SubscriptionKeyboards.item_name(item, language)
                quantity = item.get('quantity', 1)
                row = [{'text': f"✏️ {product_name} x{quantity}", 'callback_data': f'update_item_{subscription_id}_{item_id}'}]
                if can_remove:
                    row.append({'text': '🗑️', 'callback_data': f'remove_item_{subscription_id}_{item_id}'})
                buttons.append(row)
            buttons.append([{'text': i18n.get('telegram.subscription.add_item', language), 'callback_data': f'add_item_{subscription_id}'}])

        buttons.append([{'text': i18n.get('telegram.back', language), 'callback_data': f'subscription_{subscription_id}'}])

        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def edit_subscription_menu(subscription_id: int, language: str = 'en') -> InlineKeyboardMarkup:
        """Edit subscription menu"""
        buttons = [
            [{'text': i18n.get('telegram.subscription.change_frequency', language), 'callback_data': f'change_frequency_{subscription_id}'}],
            [{'text': i18n.get('telegram.subscription.change_time_slot', language), 'callback_data': f'change_slot_{subscription_id}'}],
            [{'text': i18n.get('telegram.subscription.change_payment', language), 'callback_data': f'change_payment_{subscription_id}'}],
            [{'text': i18n.get('telegram.subscription.change_address', language), 'callback_data': f'change_address_{subscription_id}'}],
            [{'text': i18n.get('telegram.subscription.manage_items', language), 'callback_data': f'manage_items_{subscription_id}'}],
            [{'text': i18n.get('telegram.subscription.orders', language), 'callback_data': f'subhist_{subscription_id}_1'}],
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
            [{'text': i18n.get('telegram.menu.language', language), 'callback_data': 'menu_language'}]
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
    def phone_request(language: str = 'en', *, with_cancel: bool = False) -> ReplyKeyboardMarkup:
        """Phone number request keyboard.

        `with_cancel` adds a Cancel row, for a customer who already has the bottom menu
        (Profile → Phone): it is their way back to it. Signup has no menu to go back to.
        """
        rows = [[KeyboardButton(
            text=i18n.get('telegram.profile.share_phone', language),
            request_contact=True
        )]]
        if with_cancel:
            rows.append([KeyboardButton(i18n.get('telegram.cancel', language))])

        return ReplyKeyboardMarkup(
            rows,
            one_time_keyboard=True,
            resize_keyboard=True
        )

    @staticmethod
    def phone_code_prompt(language: str = 'en') -> InlineKeyboardMarkup:
        """The Cancel under the profile "verify phone" code prompt (spec 2026-10-21 §2).

        `phone_otp_cancel` is this prompt's alone, and stays out of the `cancel_`
        namespace other flows' handlers match on.
        """
        return KeyboardBuilder.build_inline_keyboard([[
            {'text': i18n.get('telegram.cancel', language), 'callback_data': 'phone_otp_cancel'},
        ]])

    @staticmethod
    def notification_settings(
        language: str = 'en',
        delivery_telegram_status_updates_enabled: bool = True,
        reorder_reminders_enabled: Optional[bool] = None,
    ) -> InlineKeyboardMarkup:
        """Notification settings keyboard.

        `reorder_reminders_enabled` is the EFFECTIVE value the preferences API publishes (spec
        2026-10-09 §8). None (a backend that does not publish it) draws no reminder row, rather
        than a guessed one.
        """
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

        buttons = [[{'text': toggle_text, 'callback_data': toggle_callback}]]
        if reorder_reminders_enabled is not None:
            buttons.append([{
                'text': reorder_reminder_copy.settings_button(i18n.renderer(language), reorder_reminders_enabled),
                'callback_data': (
                    'toggle_reorder_reminders_off' if reorder_reminders_enabled else 'toggle_reorder_reminders_on'
                ),
            }])
        buttons.append([{'text': i18n.get('telegram.back', language), 'callback_data': 'menu_profile'}])

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
    def _cancel_address_row(language: str = 'en') -> List[Dict[str, str]]:
        """One row: Cancel, routed to `cancel_address_creation`.

        That callback is already registered in the address conversation's
        `fallbacks` (`bot.py`), so it answers from EVERY state with zero new
        wiring — the same reason `region_selection` and `geocode_confirmation`
        already render it. Reuses `telegram.cancel`, the same translation key
        those two already use for this exact button, rather than adding a new
        DB-backed key that would render as raw text until seeded.

        C1 (final whole-branch review, SDD 2026-08-26-address-flow-bot-state):
        `conversation_timeout` on this conversation grew from 10 minutes to
        24 hours, but six states (title/street/building/apartment/floor/
        delivery_instructions) matched `filters.TEXT & ~filters.COMMAND` and
        swallowed ANY typed text as an answer to whatever they asked, with no
        button on screen to back out of it — location_received had already
        sent `ReplyKeyboardRemove()`, so there was no menu either. A customer
        who abandoned at, say, delivery-instructions and came back nine hours
        later to ask "Where is my order?" had that sentence written down as
        their delivery instructions instead of reaching the Support Inbox.
        """
        return [{'text': i18n.get('telegram.cancel', language),
                 'callback_data': 'cancel_address_creation'}]

    @staticmethod
    def optional_field_keyboard(field_name: str, language: str = 'en') -> InlineKeyboardMarkup:
        """Keyboard for optional address fields with skip option"""
        buttons = [
            [{'text': i18n.get('telegram.address.skip_field', language),
              'callback_data': f'skip_{field_name}'}],
            ProfileKeyboards._cancel_address_row(language),
        ]
        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def required_field_keyboard(language: str = 'en') -> InlineKeyboardMarkup:
        """Cancel-only keyboard for a required (non-Skip-able) address step.

        The one caller today is the street prompt (`district_selected`,
        `handlers/profile.py`): street has deliberately no Skip button (see
        `handlers/address_flow.py::_render_street`), so it cannot reuse
        `optional_field_keyboard` without resurrecting that removed Skip
        affordance. It still needs the escape `_cancel_address_row` documents
        — see C1 there for the incident this prevents.
        """
        return KeyboardBuilder.build_inline_keyboard([ProfileKeyboards._cancel_address_row(language)])

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
            ],
            ProfileKeyboards._cancel_address_row(language),
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
              'callback_data': 'skip_delivery_instructions'}],
            ProfileKeyboards._cancel_address_row(language),
        ]
        return KeyboardBuilder.build_inline_keyboard(buttons)


class PaymentKeyboards:
    """Payment-related keyboards for Telegram Payments integration"""

    @staticmethod
    def payment_success(order_id: int, language: str = 'en') -> InlineKeyboardMarkup:
        """Shown after successful payment"""
        buttons = [
            [{'text': i18n.get('telegram.payment.view_order', language),
              'callback_data': f'order_{order_id}'}]
        ]
        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def payment_failed(
        order_id: int,
        language: str,
        *,
        pay: Optional[Dict[str, Any]],
        cancel: Optional[Dict[str, Any]],
    ) -> InlineKeyboardMarkup:
        """Recovery options for an order that exists but is not paid.

        Every button carries `order_id` (My Orders needs none — it lists them),
        so the whole screen keeps working against THAT order after a bot
        restart, when `context.user_data` is gone. Rendered when the checkout
        answer carried no payment link (`confirm_order`) and for a stale
        `payment_cancel_<id>` tap, which is why My Orders is here: after a
        failed link the customer's first question is "so does my order exist?",
        and the answer has to be one tap away.

        ``pay`` and ``cancel`` are the order's published answers
        (`published_pay` / `published_cancel`), the same pair `order_details`
        draws: Retry is drawn only when Pay is published and pays with the
        amount it carried, and Cancel is the published removal with its label.
        Retry keeps its own name because the screen's copy tells the customer to
        tap «Retry Payment». Both are required: every caller holds the order, so
        every caller decides.

        SWITCH METHOD IS DELIBERATELY ABSENT. `payment_switch_{id}` parsed the
        order id, logged it, and then rendered `OrderKeyboards.payment_methods`,
        whose callbacks (`payment_cash` / `payment_card`) carry NO order id and
        route to `orders.payment_handler` -> `_show_order_confirmation` — the
        CART checkout screen. So it never moved the named order's rail, and
        because an unpaid order deliberately keeps its cart, the Confirm button
        it leads to places a SECOND order for the same basket: the exact
        double-order this screen's "your order is placed" copy exists to
        prevent. A customer moves no rail from here (spec 2026-10-27 §4.3).
        """
        buttons = []
        if pay:
            buttons.append([{'text': i18n.get('telegram.payment.retry', language),
                             'callback_data': pay_callback(order_id, pay['expected_amount'])}])
        buttons.append([{'text': i18n.get('telegram.menu.orders', language),
                         'callback_data': 'menu_orders'}])
        if cancel:
            buttons.append([{'text': cancel['label'], 'callback_data': f'cancel_order_{order_id}'}])
        return KeyboardBuilder.build_inline_keyboard(buttons)

    @staticmethod
    def payment_link(payment_url: str, language: str, order_id: int) -> InlineKeyboardMarkup:
        """Payment link with Pay button and Back button.

        Back opens that order's details (Pay now / Cancel). It cannot return to the
        confirmation screen: ``confirm_order`` has already cleared the checkout state.
        """
        buttons = [
            [InlineKeyboardButton(
                text=i18n.get('telegram.payment.pay_btn', language),
                url=payment_url
            )],
            [InlineKeyboardButton(
                text=i18n.get('telegram.back', language),
                callback_data=f'order_{order_id}'
            )]
        ]
        return InlineKeyboardMarkup(buttons)


class ReorderReminderKeyboards:
    """The buttons on a reorder reminder (spec 2026-10-09 §7.3).

    Built here, not in the webhook, so the callback-contract test
    (tests/telegram_bot/test_callback_contract_customer.py) harvests every callback it can emit.
    """

    @staticmethod
    def reminder(
        language: str, *, episode_id: int, reorder_order_id: Optional[int], show_later: bool, snooze_days: int
    ) -> InlineKeyboardMarkup:
        """Order again on every reminder; Later only when the backend says so (R1 of a two-step
        ladder, `show_later`); Stop on every reminder.

        `reorder_<id>` is the existing Quick Order flow on the RECIPIENT'S own order (the cart
        is refilled and confirmed at checkout; nothing is placed on the tap). With no own
        order the button opens the catalogue.
        """
        labels = reorder_reminder_copy.button_labels(i18n.renderer(language), language, snooze_days=snooze_days)
        order_callback = f'reorder_{reorder_order_id}' if reorder_order_id else 'menu_products'
        buttons = [[{'text': labels['order'], 'callback_data': order_callback}]]
        if show_later:
            buttons.append([{'text': labels['later'], 'callback_data': f'rr_later_{episode_id}'}])
        buttons.append([{'text': labels['stop'], 'callback_data': f'rr_stop_{episode_id}'}])
        return KeyboardBuilder.build_inline_keyboard(buttons)


class ReorderKeyboards:
    """The notice shown when Order water / Order again meets an order already in progress."""

    @staticmethod
    def open_order_notice(language: str, order_id: int, continue_callback: str) -> InlineKeyboardMarkup:
        """View the open order, or carry on (`continue_callback`): `reorder_go_<id>` repeats that order,
        `order_water_go` starts a fresh one (Products)."""
        return KeyboardBuilder.build_inline_keyboard([
            [{'text': i18n.get('telegram.payment.view_order', language), 'callback_data': f'order_{order_id}'}],
            [{
                'text': reorder_reminder_copy.order_label(i18n.renderer(language)),
                'callback_data': continue_callback,
            }],
        ])


class CustomerNoticeKeyboards:
    """The buttons on a customer notice (plan 2026-10-24 Task 3; spec 2026-10-23 §9).

    Built here, not in the webhook, so the callback-contract test
    (tests/telegram_bot/test_callback_contract_customer.py) harvests every callback it can emit.
    """

    @staticmethod
    def notice(language: str, *, order_id: Optional[int], pay_now: bool) -> Optional[InlineKeyboardMarkup]:
        """Pay now (an unpaid Click order: `payment_retry_<id>`, which carries no amount, so `retry_payment`
        opens the order with its published Pay and writes nothing) above View order. None for a notice without
        an order (a skipped delivery)."""
        if order_id is None:
            return None
        rows = []
        if pay_now:
            rows.append([{
                'text': i18n.get('telegram.payment.pay_now', language),
                'callback_data': f'payment_retry_{order_id}',
            }])
        rows.append([{
            'text': i18n.get('telegram.payment.view_order', language),
            'callback_data': f'order_{order_id}',
        }])
        return KeyboardBuilder.build_inline_keyboard(rows)


class DeliverySummaryKeyboards:
    """The buttons under the delivered-order summary (spec 2026-10-21 §3, R8)."""

    @staticmethod
    def summary(language: str, order_id: int, reorder_order_id: Optional[int]) -> InlineKeyboardMarkup:
        """Order again above Report an issue.

        `reorder_order_id` is the backend's published reorder candidate; None (subscription and
        reward-only orders) drops Order again. `reorder_<id>` is the existing Quick Order flow and
        sends checkout as a new message, so the summary and its Report button are never edited.
        """
        rows = []
        if reorder_order_id is not None:
            rows.append([{
                'text': reorder_reminder_copy.order_label(i18n.renderer(language)),
                'callback_data': f'reorder_{reorder_order_id}',
            }])
        rows.append([{
            'text': i18n.get('telegram.delivery_summary.report_button', language),
            'callback_data': f'report_issue_{order_id}',
        }])
        return KeyboardBuilder.build_inline_keyboard(rows)
