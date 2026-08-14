"""Search-related helper functions for staff bot handlers.

Thin delegators over :mod:`shared.user_search`, which is the single place the
customer-search matching rules live so the staff bot and the admin API resolve
a typed query the same way. The wrappers are kept (rather than re-exporting)
so the bot's import surface stays explicit and stable for its handlers.
"""

from shared.user_search import MIN_PHONE_DIGITS as _MIN_PHONE_DIGITS
from shared.user_search import detect_search_type as _detect_search_type
from shared.user_search import normalize_phone_query as _normalize_phone_query

# Re-exported so existing handler imports and tests keep their meaning.
MIN_PHONE_DIGITS = _MIN_PHONE_DIGITS


def detect_search_type(query_text: str) -> str:
    """Infer whether query should use phone or name search.

    Strips common formatting characters (``+``, spaces, dashes, parentheses)
    and classifies the result as ``phone`` only when it's all-digits with at
    least :data:`MIN_PHONE_DIGITS` characters; otherwise ``name``.
    """
    return _detect_search_type(query_text)


def normalize_phone_query(query_text: str) -> str:
    """Strip everything except digits so a typed phone with spaces/dashes
    matches against canonical ``+998901234567``-style values via ILIKE."""
    return _normalize_phone_query(query_text)
