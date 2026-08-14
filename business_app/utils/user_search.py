"""SQLAlchemy clause builders for customer search.

The matching *rules* live in :mod:`shared.user_search` (pure string work, so
the staff bot process can import them). This module is the SQL half: it turns
those rules into a filter over :class:`~business_app.models.user.User` for the
admin API and the staff COD search, so both surfaces resolve a typed query the
same way.
"""

from typing import Optional

from sqlalchemy import and_, or_

from business_app.models.user import User
from shared.user_search import detect_search_type, normalize_phone_query, tokenize_name_variants


def build_name_match_clause(query: str):
    """Match a name query across ``first_name``/``last_name``.

    Every transliteration variant is tried, and within a variant EVERY token
    must appear in one of the two columns -- so ``Umar Xoldarbekov`` and
    ``Xoldarbekov Umar`` both match a customer whose name is split across the
    two columns, while someone sharing only one token does not.

    Returns ``None`` when the query tokenizes to nothing.
    """
    variant_clauses = []
    for tokens in tokenize_name_variants(query):
        variant_clauses.append(
            and_(
                *[
                    or_(
                        User.first_name.ilike(f"%{token}%"),
                        User.last_name.ilike(f"%{token}%"),
                    )
                    for token in tokens
                ]
            )
        )
    if not variant_clauses:
        return None
    return or_(*variant_clauses)


def build_user_search_filter(
    search: str,
    *,
    include_email: bool = True,
    include_company: bool = True,
) -> Optional[object]:
    """Build the filter behind a single free-text user-search box.

    Deliberately a SUPERSET of the flat five-column ILIKE it replaced: every
    column the admin Users page searched before is still ORed in on the raw
    term, so widening name matching cannot drop an existing match. What is new
    is Latin<->Cyrillic names, formatted phones and multi-word queries.

    Returns ``None`` for an empty query so callers can skip filtering.
    """
    term = (search or "").strip()
    if not term:
        return None

    raw = f"%{term}%"
    secondary = []
    if include_email:
        secondary.append(User.email.ilike(raw))
    if include_company:
        secondary.append(User.company_name.ilike(raw))

    if detect_search_type(term) == "phone":
        # Digits-only so '93 510-12-34' and '+998 93 510 12 34' both match the
        # canonical '+998935101234' the column stores.
        digits = normalize_phone_query(term)
        return or_(User.phone.ilike(f"%{digits}%"), *secondary)

    clauses = []
    name_clause = build_name_match_clause(term)
    if name_clause is not None:
        clauses.append(name_clause)
    # Short numeric fragments (1-3 digits) route to the name branch, so the raw
    # phone match stays here to preserve the pre-existing behaviour.
    clauses.append(User.phone.ilike(raw))
    clauses.extend(secondary)
    return or_(*clauses)
