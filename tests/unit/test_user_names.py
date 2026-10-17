"""`greeting_name`: how an automated message names the customer (spec 2026-10-09 §7.3 `name`).

First name for a person, company name for an entity; nothing at all (None, so the copy falls back
to its no-name greeting) when the only name is empty or the "Telegram User" the sign-up invents.
Never "None" (Review Focus 3).
"""

import pytest

from shared.user_names import GENERATED_TELEGRAM_NAME, greeting_name, joined_name

pytestmark = pytest.mark.unit


def test_the_generated_name_is_the_one_sign_up_writes():
    assert GENERATED_TELEGRAM_NAME == "Telegram User"


@pytest.mark.parametrize(
    "first_name, full_name, company_name, is_entity, expected",
    [
        ("Kamola", "Kamola Yusupova", None, False, "Kamola"),
        ("  Kamola ", None, None, False, "Kamola"),
        ("Telegram User", "Telegram User", None, False, None),
        ("telegram user", None, None, False, None),
        ("", "Kamola Yusupova", None, False, "Kamola Yusupova"),
        (None, "Telegram User", None, False, None),
        ("Ali", "Ali Valiyev", "Bahor market", True, "Bahor market"),
        ("Ali", "Ali Valiyev", "   ", True, "Ali"),
        ("Ali", "Ali Valiyev", "Bahor market", False, "Ali"),
        (None, None, None, False, None),
        (None, None, None, True, None),
    ],
    ids=[
        "person",
        "person-padded",
        "generated-placeholder",
        "generated-placeholder-any-case",
        "no-first-name-uses-full-name",
        "placeholder-full-name",
        "entity-company",
        "entity-blank-company-uses-person",
        "person-ignores-company",
        "nothing",
        "entity-nothing",
    ],
)
def test_greeting_name(first_name, full_name, company_name, is_entity, expected):
    assert (
        greeting_name(first_name=first_name, full_name=full_name, company_name=company_name, is_entity=is_entity)
        == expected
    )


@pytest.mark.parametrize(
    "first_name, last_name, expected",
    [("Ali", "Valiyev", "Ali Valiyev"), ("Ali", None, "Ali"), (None, "Smith", "Smith"), (None, None, ""), ("", "", "")],
)
def test_a_full_name_joins_only_the_parts_that_are_set(first_name, last_name, expected):
    """Final review B2: the bot greets from the parts, never `User.to_dict()`'s "None None"."""
    from business_app.models.user import User

    assert joined_name(first_name, last_name) == expected
    assert User(first_name=first_name, last_name=last_name).full_name == expected


def test_the_generated_name_is_spelled_once_in_the_producers_and_comparers():
    """Deferred 105: every site that writes or compares the sign-up placeholder name reads
    GENERATED_TELEGRAM_NAME, so the name the bot and the email refuse to greet with is the very
    name AuthService writes. Docstrings and comments may still say it in words."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    literal = re.compile(r"""(["'])Telegram User\1""")
    offenders = [
        f"{path.relative_to(root)}:{number}"
        for tree in ("business_app", "telegram_bot", "staff_bot")
        for path in (root / tree).rglob("*.py")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if literal.search(line.split("#", 1)[0])
    ]
    assert offenders == []
