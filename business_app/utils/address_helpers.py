"""
Address formatting helpers shared across API/service layers.
"""


def get_address_line(address) -> str:
    """Return a display-safe primary address line across schema variants."""
    if not address:
        return ''
    return (
        getattr(address, 'full_address', None)
        or getattr(address, 'street_address', None)
        or ''
    )


def get_address_label(address) -> str:
    """Return address label/title across schema variants."""
    if not address:
        return ''
    return getattr(address, 'title', None) or getattr(address, 'label', None) or ''
