"""The Profile screen never prints "None" for a missing name, phone or email (D23).

The backend publishes the finished lines (`full_name`, `contact_email`) in `GET /api/v1/auth/profile`;
the bot prints them, or `telegram.common.not_set` when the answer is empty.
"""

from __future__ import annotations

import pytest

import utils as utils_module

from tests.telegram_bot.ptb_harness import build_bot_harness

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

NOT_SET = "Not set"
COPY = {
    "telegram.common.not_set": NOT_SET,
    "telegram.profile_title": "Profile",
    "telegram.profile_name": "Name",
    "telegram.profile_phone": "Phone",
    "telegram.profile_email": "Email",
    "telegram.profile_birthday": "Birthday",
    "telegram.profile_language": "Language",
}


def _profile(**overrides):
    """Every key `AuthService.get_user_profile_data` publishes (pinned against the real method in
    tests/integration/test_cabinet_pages_never_print_none.py)."""
    body = {
        "id": 398,
        "email": "ali@example.com",
        "contact_email": "ali@example.com",
        "phone": "+998901112233",
        "first_name": "Ali",
        "last_name": None,
        "full_name": "Ali",
        "date_of_birth": None,
        "gender": None,
        "role": "customer",
        "status": "active",
        "email_verified": False,
        "phone_verified": True,
        "created_at": "2026-01-01T00:00:00",
        "last_login": None,
        "preferred_language": "en",
        "loyalty_eligible": True,
        "permissions": [],
    }
    body.update(overrides)
    return {"data": body}


async def _profile_text(monkeypatch, profile):
    bot = await build_bot_harness(monkeypatch, translations=COPY)

    async def _always_allow(_user_id):
        return True

    monkeypatch.setattr(utils_module.rate_limiter, "allow_request", _always_allow)
    bot.backend.route("GET", "/api/v1/auth/profile", lambda _call: profile)
    await bot.send(bot.updates().tap("menu_profile"))
    texts = [text for text in bot.telegram.texts() if "Profile" in text]
    assert texts, bot.telegram.texts()
    return texts[-1]


@pytest.mark.parametrize(
    "overrides, name_line, phone_line, email_line",
    [
        ({}, "Name: Ali", "Phone: +998901112233", "Email: ali@example.com"),
        (
            {"email": "telegram_810000001@bluestream.local", "contact_email": None},
            "Name: Ali",
            "Phone: +998901112233",
            f"Email: {NOT_SET}",
        ),
        (
            {"first_name": None, "full_name": "", "email": None, "contact_email": None, "phone": None},
            f"Name: {NOT_SET}",
            f"Phone: {NOT_SET}",
            f"Email: {NOT_SET}",
        ),
    ],
    ids=["real-email", "placeholder-email", "nothing-on-file"],
)
async def test_the_profile_screen_prints_the_published_lines_and_never_none(
    monkeypatch, overrides, name_line, phone_line, email_line
):
    text = await _profile_text(monkeypatch, _profile(**overrides))

    lines = text.splitlines()
    assert name_line in lines and phone_line in lines and email_line in lines
    assert "None" not in text
