"""`is_deliverable_email`: may a send decision use this address? (spec 2026-10-09 §7.1, §15.5)

A deliverability test, not an input validator: `validate_email` refuses mixed case because it
guards what users TYPE, while a stored "Dilnoza.K@Example.COM" is a perfectly good mailbox. The
placeholder domains are the ones this code base invents (`PLACEHOLDER_EMAIL_DOMAINS`), and `.local`
(RFC 6762) / `.internal` never receive mail.
"""

import pytest

from shared.validators import (
    PLACEHOLDER_EMAIL_DOMAIN_BOT,
    PLACEHOLDER_EMAIL_DOMAIN_LOCAL,
    PLACEHOLDER_EMAIL_DOMAINS,
    is_deliverable_email,
    validate_email,
)

pytestmark = pytest.mark.unit


def test_the_placeholder_domains_are_the_two_this_code_base_invents():
    assert PLACEHOLDER_EMAIL_DOMAIN_LOCAL == "bluestream.local"
    assert PLACEHOLDER_EMAIL_DOMAIN_BOT == "bot.internal"
    assert PLACEHOLDER_EMAIL_DOMAINS == ("bluestream.local", "bot.internal")


@pytest.mark.parametrize(
    "email",
    ["dilnoza@example.com", "Dilnoza.K@Example.COM", "  dilnoza@example.com  ", "ops+water@aqua-element.uz"],
    ids=["plain", "mixed-case", "padded", "plus-and-hyphen"],
)
def test_real_mailboxes_are_deliverable(email):
    assert is_deliverable_email(email) is True


@pytest.mark.parametrize(
    "email",
    [
        "telegram_8134062686@bluestream.local",
        "telegram_8134062686@bot.internal",
        "TELEGRAM_8134062686@BlueStream.LOCAL",
        "deleted_42@bluestream.local",
        "printer@office.local",
        "ops@corp.internal",
        None,
        "",
        "   ",
        "no-at-sign.example.com",
        "two@@example.com",
        "a b@example.com",
        "user@localhost",
        "user@example.",
        "user@.com",
        "user@exa..mple.com",
    ],
    ids=[
        "registration-placeholder",
        "login-placeholder",
        "placeholder-any-case",
        "merge-tombstone",
        "dot-local",
        "dot-internal",
        "none",
        "empty",
        "blank",
        "no-at",
        "two-ats",
        "whitespace",
        "undotted-domain",
        "empty-tld",
        "empty-first-label",
        "empty-middle-label",
    ],
)
def test_placeholders_tombstones_and_non_addresses_are_not(email):
    assert is_deliverable_email(email) is False


def test_it_is_not_the_input_validator():
    """The input validator refuses what the deliverability test accepts — two questions."""
    assert validate_email("Dilnoza.K@Example.COM")[0] is False
    assert is_deliverable_email("Dilnoza.K@Example.COM") is True
