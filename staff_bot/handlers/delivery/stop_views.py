"""The two screens of one stop: its detail and its Navigate screen.

Each is drawn here once. The driver's tap draws it (`ActiveDeliveryHandler`), and the
"Address changed" alert redraws it in place when the borrowed card shows it
(`address_change.rerender_borrowed_stop`, spec D8b), so the two always draw the same screen.
"""
from typing import Mapping, Optional, Tuple

from telegram import InlineKeyboardMarkup

from staff_bot.i18n import i18n
from staff_bot.keyboards.delivery import DeliveryKeyboards
from staff_bot.utils.delivery_snapshot import active_row_id
from staff_bot.utils.formatters import escape_html, format_active_delivery_summary


def stop_detail_view(row: Mapping, language: str) -> Tuple[str, InlineKeyboardMarkup]:
    """The stop detail, drawn from the stop's `/delivery/active` row.

    Its status buttons carry the row's address id: a tap answers for the address this
    screen shows (R10), so a redraw after an address change is a fresh screen.
    """
    return (
        format_active_delivery_summary(row, language, include_money=True),
        DeliveryKeyboards.active_delivery_actions(
            language, active_row_id(row), row.get('status', ''), address_id=row.get('delivery_address_id'),
        ),
    )


def navigate_view(info: Mapping, language: str) -> Optional[Tuple[str, InlineKeyboardMarkup]]:
    """The Navigate screen, drawn from a `current_delivery` snapshot.

    None when the snapshot has no destination coordinates: there is no route to open.
    """
    destination_lat = info.get('destination_lat')
    destination_lng = info.get('destination_lng')
    if destination_lat is None or destination_lng is None:
        return None
    text = (
        f"📍 {i18n.get('staff.delivery.navigate_text', language)}\n"
        f"{escape_html(info.get('address', ''))}\n"
        f"({destination_lat}, {destination_lng})"
    )
    keyboard = DeliveryKeyboards.navigate_actions(
        language, info.get('delivery_id', 0), destination_lat, destination_lng
    )
    return text, keyboard
