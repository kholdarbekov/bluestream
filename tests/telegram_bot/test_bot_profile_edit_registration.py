"""Verify Deliverable C profile-edit callbacks are registered with correct patterns."""

import inspect
import re

import pytest

import bot as bot_module


@pytest.mark.unit
def test_profile_edit_callbacks_registered():
    src = inspect.getsource(bot_module)
    expected = [
        r'pattern="\^edit_profile\$"',
        r'pattern="\^edit_profile_name\$"',
        r'pattern="\^edit_profile_birthday\$"',
        r'pattern="\^edit_profile_language\$"',
        r'pattern="\^edit_profile_phone\$"',
        r'pattern="\^cancel_action\$"',
    ]
    for pat in expected:
        assert re.search(pat, src), f"missing registration: {pat}"


@pytest.mark.unit
def test_birthday_picker_patterns_removed():
    """The old bday_year/month/day callback patterns must NOT be registered
    since the guided picker has been replaced with raw text entry."""
    src = inspect.getsource(bot_module)
    removed = [
        r'\^bday_year_',
        r'\^bday_month_',
        r'\^bday_day_',
    ]
    for pat in removed:
        assert not re.search(pat, src), (
            f"Old picker pattern '{pat}' should have been removed from bot.py"
        )
