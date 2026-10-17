"""`shared.i18n_rendering.neutralise_braces`: the one way a customer-provided string enters the
template context of a placeholder-strict email (reorder-reminders plan Task 22b, ruling T22a).

`NotificationService` skips a strict email whose rendered subject or body still holds a literal
`{name}`. A customer who types `{x}` as a name must not be able to switch their own reminder off, nor
block the whole morning digest while their row stays in the top five. The guard is the REAL one
(`NotificationService._unrendered_placeholders`), not a copy of its pattern.
"""

import pytest

from business_app.services.notification_service import NotificationService
from shared.i18n_rendering import neutralise_braces

FULL_WIDTH_OPEN, FULL_WIDTH_CLOSE = "｛", "｝"


@pytest.mark.parametrize(
    "name, neutralised",
    [
        ("{evil}", f"{FULL_WIDTH_OPEN}evil{FULL_WIDTH_CLOSE}"),
        ("Ali {x} Valiev", f"Ali {FULL_WIDTH_OPEN}x{FULL_WIDTH_CLOSE} Valiev"),
        ("{{double}}", f"{FULL_WIDTH_OPEN * 2}double{FULL_WIDTH_CLOSE * 2}"),
        ("lone { brace", f"lone {FULL_WIDTH_OPEN} brace"),
        ("lone } brace", f"lone {FULL_WIDTH_CLOSE} brace"),
    ],
)
def test_braces_become_their_full_width_forms_and_the_guard_no_longer_sees_a_placeholder(name, neutralised):
    assert neutralise_braces(name) == neutralised
    assert neutralise_braces(neutralised) == neutralised  # idempotent
    assert NotificationService._unrendered_placeholders(neutralise_braces(name)) == []


def test_the_guard_does_see_the_raw_name_so_the_neutralising_is_what_prevents_the_skip():
    assert NotificationService._unrendered_placeholders("Hi {evil}!") == ["evil"]


@pytest.mark.parametrize("value", ["Ali", "Мир & Co <b>", "O'Neil", "", None])
def test_a_string_without_braces_passes_through_untouched_and_none_stays_none(value):
    assert neutralise_braces(value) == value
