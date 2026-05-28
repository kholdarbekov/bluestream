"""Search-related helper functions for staff bot handlers."""


# Minimum digits required before a fully-numeric query is treated as a phone
# substring search. Anything shorter (1–3 digits) is routed to name search to
# avoid noisy partial matches and to dovetail with the backend's name path.
MIN_PHONE_DIGITS = 4


def detect_search_type(query_text: str) -> str:
    """Infer whether query should use phone or name search.

    Strips common formatting characters (``+``, spaces, dashes, parentheses)
    and classifies the result as ``phone`` only when it's all-digits with at
    least :data:`MIN_PHONE_DIGITS` characters; otherwise ``name``.
    """
    compact = (
        query_text
        .replace('+', '')
        .replace(' ', '')
        .replace('-', '')
        .replace('(', '')
        .replace(')', '')
    )
    if compact.isdigit() and len(compact) >= MIN_PHONE_DIGITS:
        return 'phone'
    return 'name'


def normalize_phone_query(query_text: str) -> str:
    """Strip everything except digits so a typed phone with spaces/dashes
    matches against canonical ``+998901234567``-style values via ILIKE."""
    return ''.join(ch for ch in query_text if ch.isdigit())
