"""Customer-search matching rules shared by the staff bot and the admin API.

ONE place decides what "this typed query matches this customer" means. Before
this module the rule had two expressions: the staff bot's COD search
(``StaffService.search_cod_collection_users``) understood Latin<->Cyrillic
names, formatted phones and multi-word queries, while the admin API's
``GET /admin/users`` — which backs the create-order customer picker, the Users
page, Subscriptions, SupportInbox and LinkedAccountsPanel — ran a single raw
``%term%`` ILIKE across five columns and matched none of them.

The SQL half lives in :mod:`business_app.utils.user_search`, which needs the
``User`` model; everything here is pure string work so the bot process can
import it without pulling in SQLAlchemy.

``transliterate`` is imported lazily inside :func:`expand_name_variants` so
importing this module stays cheap for the bot, which only needs the routing
helpers.
"""

from typing import List

# Minimum digits required before a fully-numeric query is treated as a phone
# substring search. Anything shorter (1-3 digits) is routed to name search to
# avoid noisy partial matches and to dovetail with the backend's name path.
MIN_PHONE_DIGITS = 4

# GOST 7.79-2000 (the scheme the ``transliterate`` package implements) renders
# the Russian soft/hard signs as apostrophes -- ``Дональд`` becomes
# ``Donal'd``. Real-world Latin spellings of names carry no such punctuation,
# so every variant is also folded into an apostrophe-stripped form to keep
# matching symmetric in practice.
_GOST_PUNCTUATION = ("'", "ʹ", "ʺ")

_PHONE_FORMATTING_CHARS = ("+", " ", "-", "(", ")")


def detect_search_type(query_text: str) -> str:
    """Infer whether a query should use phone or name search.

    Strips common formatting characters (``+``, spaces, dashes, parentheses)
    and classifies the result as ``phone`` only when it is all-digits with at
    least :data:`MIN_PHONE_DIGITS` characters; otherwise ``name``.
    """
    compact = query_text or ""
    for char in _PHONE_FORMATTING_CHARS:
        compact = compact.replace(char, "")
    if compact.isdigit() and len(compact) >= MIN_PHONE_DIGITS:
        return "phone"
    return "name"


def normalize_phone_query(query_text: str) -> str:
    """Strip everything except digits so a typed phone with spaces/dashes
    matches against canonical ``+998901234567``-style values via ILIKE."""
    return "".join(ch for ch in (query_text or "") if ch.isdigit())


def expand_name_variants(query: str) -> List[str]:
    """Return the original query plus its Latin<->Cyrillic transliterations.

    Best-effort: when ``transliterate`` cannot detect/convert a string the
    helper silently falls back to the inputs collected so far, so a query in
    an alphabet the package does not recognise still searches verbatim.
    """
    from transliterate import translit  # local import -- keep top-level lean
    from transliterate.exceptions import LanguageDetectionError

    variants = {query}
    try:
        variants.add(translit(query, "ru"))  # Latin -> Cyrillic
    except (LanguageDetectionError, Exception):  # noqa: BLE001 -- best-effort
        pass
    try:
        variants.add(translit(query, "ru", reversed=True))  # Cyrillic -> Latin
    except (LanguageDetectionError, Exception):  # noqa: BLE001 -- best-effort
        pass

    cleaned = set()
    for variant in variants:
        folded = variant
        for punctuation in _GOST_PUNCTUATION:
            folded = folded.replace(punctuation, "")
        cleaned.add(folded)
    variants |= cleaned
    return list(variants)


def tokenize_name_variants(query: str) -> List[List[str]]:
    """Expand ``query`` into transliteration variants, each split into tokens.

    Multi-word handling lives here so both callers agree: a variant matches
    when EVERY one of its tokens appears in the name, which is what lets
    ``Donald Trump`` and ``Trump Donald`` both find the same customer while
    an unrelated person sharing only one token stays out of the results.

    Variants that tokenize to nothing (whitespace-only) are dropped.
    """
    token_lists = []
    for variant in expand_name_variants(query):
        tokens = [token for token in variant.split() if token]
        if tokens:
            token_lists.append(tokens)
    return token_lists
