"""Fix A5: ProfileKeyboards.profile_menu() must not render a
'payment_methods' button — no handler matches that callback_data (dead
button, tap spins forever). Other profile buttons must remain intact.
"""

from keyboards import ProfileKeyboards


def _callbacks(markup):
    return [btn.callback_data for row in markup.inline_keyboard for btn in row]


def test_profile_menu_has_no_dead_payment_methods_button():
    cbs = _callbacks(ProfileKeyboards.profile_menu("en"))
    assert "payment_methods" not in cbs


def test_profile_menu_keeps_other_expected_buttons():
    cbs = _callbacks(ProfileKeyboards.profile_menu("en"))
    assert "edit_profile" in cbs
    assert "manage_addresses" in cbs
    assert "phone_verification" in cbs
    assert "notification_settings" in cbs
    assert "my_bottles" in cbs
    assert "back_to_main" in cbs
