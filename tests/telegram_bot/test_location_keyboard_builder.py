"""Spec §6: three copies of the same request_location button collapse to one.
Nothing today proves request_location is even set - the existing tests stub the
builder out to a string."""

import pytest
from telegram import KeyboardButton

from keyboards import ProfileKeyboards

pytestmark = pytest.mark.unit


def test_location_button_actually_requests_a_location():
    markup = ProfileKeyboards.location_request("en")
    button = markup.keyboard[0][0]
    assert isinstance(button, KeyboardButton)
    assert button.request_location is True


def test_extra_rows_are_appended_below_the_location_button():
    markup = ProfileKeyboards.location_request("en", extra_rows=("Manually", "Cancel"))
    assert len(markup.keyboard) == 3
    assert markup.keyboard[0][0].request_location is True
    assert markup.keyboard[1][0].text == "Manually"
    assert markup.keyboard[2][0].text == "Cancel"


def test_old_builders_are_gone():
    assert not hasattr(ProfileKeyboards, "location_request_with_skip")
    assert not hasattr(ProfileKeyboards, "location_request_with_retry")
