"""Phone-query normalization for staff bot search utilities.

`staff_bot/utils/search.py` backs the bottle-collection and operator search
flows. (The COD collection flow no longer searches — it lists debtors.)
"""

import pytest

from staff_bot.utils.search import normalize_phone_query


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("+998 90 123-45-67", "998901234567"),
        ("998901234567", "998901234567"),
        ("(998) 90 123-45-67", "998901234567"),
        ("90-123-45", "9012345"),
        ("Aziz", ""),
        ("", ""),
    ],
)
def test_normalize_phone_query_strips_formatting(raw, expected):
    assert normalize_phone_query(raw) == expected
