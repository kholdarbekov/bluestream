"""`one_time_keyboard=True` asks the client to COLLAPSE the keyboard after
one use, which is exactly wrong for a driver's permanent control surface.
`is_persistent` (Bot API 6.4) is the flag that says 'always show it'.

The distinction is per-keyboard, not global — see the second test."""

import pytest

from staff_bot.keyboards.common import CommonKeyboards
from staff_bot.keyboards.menu import MenuKeyboards


@pytest.mark.unit
class TestKeyboardPersistence:
    def test_main_menu_is_persistent(self):
        kb = MenuKeyboards.main_menu("en", ["delivery_driver"])
        assert kb.is_persistent is True
        assert kb.resize_keyboard is True

    def test_transient_location_prompt_still_collapses_after_one_use(self):
        """A CONTROL SURFACE and a PROMPT want opposite flags.

        `main_menu` is the driver's permanent control surface: it must stay
        up, so it carries `is_persistent`. `location_request` is a transient
        prompt that asks one question, and its Cancel label has a handler
        only on the tryout path — on the delivery paths the tap falls through
        to `_handle_text_message`, matches no menu label and no flow flag, and
        is dropped. The client collapsing the keyboard is the ONLY feedback
        that tap produces, so `one_time_keyboard` is load-bearing here:
        without it Cancel becomes a completely silent no-op.
        """
        kb = CommonKeyboards.location_request("en", "Share location")
        assert kb.one_time_keyboard is True


@pytest.mark.unit
class TestLocationPromptButtonCount:
    """The driver complained about the Optimize flow: an inline button can
    NEVER carry `request_location` (that field exists only on KeyboardButton
    -- MTProto: "Available only in private chats, in reply keyboards"), so a
    stale fix forces the bot to draw a reply keyboard just to reach Telegram's
    own "Share your location?" dialog. That extra tap is unavoidable while the
    Optimize button stays on the card, and the driver accepted it -- but the
    SECOND button was pure cost: on the delivery paths Cancel has no handler
    at all, so it looked like an escape and was not one.

    Cancel stays on the tryout paths, where `receive_create_address` really
    does compare the text against `staff.cancel` and abort the conversation.
    """

    def _labels(self, kb):
        return [b.text for row in kb.keyboard for b in row]

    def test_delivery_prompt_offers_only_the_share_button(self):
        kb = CommonKeyboards.location_request(
            "en", "Share location", include_cancel=False
        )
        assert self._labels(kb) == ["Share location"]
        assert kb.keyboard[0][0].request_location is True

    def test_cancel_is_kept_by_default_for_the_tryout_paths(self):
        """Default stays True: `staff_bot/handlers/tryouts.py` is the one
        caller whose Cancel is real, and it is the only way to abort address
        entry. A default of False would silently strip a driver's only exit."""
        kb = CommonKeyboards.location_request("en", "Share location")
        assert len(self._labels(kb)) == 2

    def test_both_delivery_call_sites_drop_cancel(self):
        """AST-based, not substring: the 412 branch in active_delivery.py and
        the LOCATION_TOO_COARSE retry in location.py are the two delivery
        prompts the driver actually sees. Both must pass include_cancel=False;
        a future third call site that forgets it fails this test."""
        import ast
        from pathlib import Path

        root = Path(__file__).resolve().parents[2] / "staff_bot" / "handlers" / "delivery"
        found = 0
        for path in (root / "active_delivery.py", root / "location.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if getattr(node.func, "attr", "") != "location_request":
                    continue
                found += 1
                kwargs = {
                    kw.arg: kw.value.value
                    for kw in node.keywords
                    if isinstance(kw.value, ast.Constant)
                }
                assert kwargs.get("include_cancel") is False, (
                    f"{path.name}: delivery-path location_request must pass "
                    f"include_cancel=False"
                )
        assert found == 2, f"expected 2 delivery call sites, found {found}"
