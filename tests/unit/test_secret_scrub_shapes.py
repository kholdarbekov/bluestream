"""The shared secret rule (shared/secret_scrub.py) on the shapes a review found it missed or handled badly.

Every case goes through `scrub_event`, the function the backend, the customer bot and the staff bot install as
their Sentry `before_send`.
"""

import time

import pytest

from business_app.utils.logging_config import SecuritySensitiveFilter
from shared.constants import STAFF_INVITE_START_PREFIX, WEB_LINK_START_PREFIX
from shared.secret_scrub import REDACTED, names_a_secret, scrub_event, worth_following


def _scrubbed_message(text: str) -> str:
    return scrub_event({"message": text})["message"]


def test_a_long_run_of_jwt_lookalikes_is_scrubbed_in_linear_time():
    """`\\b` used to start a JWT match before every `eyJ` that follows a `-`, and each start rescanned the run:
    quadratic on a string an attacker shapes (a request body, a header) and so a hung worker inside before_send."""
    hostile = "-eyJ" * 50_000  # 200 000 characters, no dot: never a JWT

    started = time.perf_counter()
    scrubbed = _scrubbed_message(hostile)
    elapsed = time.perf_counter() - started

    assert scrubbed == hostile
    assert elapsed < 1.0, f"scrubbing took {elapsed:.2f}s"


@pytest.mark.parametrize(
    "text",
    [
        "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl",
        "'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl'",
        "jwt=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl",
    ],
)
def test_a_real_jwt_is_still_hidden(text):
    assert "eyJzdWIiOiIxIn0" not in _scrubbed_message(text)


def test_a_bearer_jwt_is_hidden_and_the_word_bearer_kept():
    scrubbed = _scrubbed_message("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl")

    assert "eyJzdWIiOiIxIn0" not in scrubbed
    assert "Bearer" in scrubbed


@pytest.mark.parametrize(
    "text, token",
    [
        (f"https://t.me/aqua_bot?start={WEB_LINK_START_PREFIX}Ab3dEf9hJk", "Ab3dEf9hJk"),
        (f"https://t.me/staff_bot?start={STAFF_INVITE_START_PREFIX}Zx81QwErTy", "Zx81QwErTy"),
        (f"invite link: https://t.me/staff_bot?start={STAFF_INVITE_START_PREFIX}Zx81QwErTy sent", "Zx81QwErTy"),
        (f"/start {STAFF_INVITE_START_PREFIX}Zx81QwErTy", "Zx81QwErTy"),
        (f"/start {WEB_LINK_START_PREFIX}Ab3dEf9hJk", "Ab3dEf9hJk"),
    ],
)
def test_a_one_time_token_in_a_telegram_deep_link_or_start_message_is_hidden(text, token):
    scrubbed = _scrubbed_message(text)

    assert token not in scrubbed
    assert REDACTED in scrubbed


@pytest.mark.parametrize("text", ["https://t.me/aqua_bot?start=order_4521", "/start sub_77", "/start ref_promo"])
def test_a_deep_link_that_carries_only_an_id_stays_readable(text):
    assert _scrubbed_message(text) == text


@pytest.mark.parametrize("name", ["Cookie", "set-cookie", "refresh_token_cookie", "csrf_token", "X-CSRFToken"])
def test_cookie_and_csrf_names_are_secret_names(name):
    assert names_a_secret(name)


def test_every_name_the_log_filter_hides_sentry_hides_too():
    """business_app/utils/logging_config.py keeps its own list for the log filter. Whatever a log line hides, an
    event must never show: the filter's names are read from the class itself, not copied here."""
    missing = sorted(name for name in SecuritySensitiveFilter.SENSITIVE_KEYS if not names_a_secret(name))

    assert missing == []


def test_a_cookie_header_is_hidden_by_its_name():
    event = scrub_event({"request": {"headers": {"Cookie": "session=abc123; refresh_token_cookie=xyz789"}}})

    assert event["request"]["headers"]["Cookie"] == REDACTED


@pytest.mark.parametrize(
    "value, followed",
    [
        ("482913", True),  # a sign-up code
        ("Zx81QwErTy", True),  # a token
        ("Basic", False),  # an ordinary word under a secret name: following it would blank it everywhere
        ("application/json", False),
        ("12", False),  # too short to look for safely
    ],
)
def test_only_code_or_token_shaped_values_are_followed_across_an_event(value, followed):
    assert worth_following(value) is followed
