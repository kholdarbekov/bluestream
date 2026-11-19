"""
Address formatting helpers shared across API/service layers.
"""

from typing import Any, Dict


def get_address_line(address) -> str:
    """Return a display-safe primary address line across schema variants."""
    if not address:
        return ""
    return getattr(address, "full_address", None) or getattr(address, "street_address", None) or ""


def get_address_label(address) -> str:
    """Return the address's title ("Office"), or "" when it has none."""
    if not address:
        return ""
    return getattr(address, "title", None) or ""


def address_display_label(address) -> str:
    """An address as the customer names it: its title ("Office"), else its street line.

    The one display rule. `UserAddress.to_dict` publishes it as `display_label`, so the bots, the website and the
    admin print that field instead of re-deriving it. Deliberately not the street-only `address_label` the dispatch
    and customer-map screens publish: drivers need the location, not the customer's name for it.
    """
    return get_address_label(address) or get_address_line(address)


def door_fields(address) -> Dict[str, Any]:
    """The address's door details as the driver reads them: the street line and the structured extras.

    One builder for the `/delivery/active` row and the "Address changed" payload, so the two
    cannot describe the same door differently.
    """
    return {
        "address": get_address_line(address),
        "district": address.district if address else "",
        "apartment_number": address.apartment_number or "" if address else "",
        "floor_number": address.floor_number or "" if address else "",
        "landmark": address.landmark or "" if address else "",
        "delivery_instructions": address.delivery_instructions or "" if address else "",
    }
