"""profile-settings.js draws the reorder switch from the published value and sends one field.

Source-level, because business_app/static/js/ has no JS runner (the convention of
tests/unit/test_web_order_pages_read_published_answers.py) and the test image has no node. The page
and the HTTP round trip are driven in tests/integration/test_reorder_reminder_web_switch.py.

The contract these checks pin (spec 2026-10-09 §8, §18):
- the switch sends `reorder_reminders_enabled` from its own change handler and from nowhere else,
  so the page's other saves (and the existing inert preferences form) can never write the setting;
- the switch is enabled only by a published value, and after every save it is redrawn from the
  server's answer, never from the clicked state (D20: a customer's "off" over an admin-off is a
  server no-op, and the switch must show the published off);
- a save that fails, or that answers 200 WITHOUT a published value, is a failure: the last published
  value goes back and the customer is told in their language;
- the page names no route and no actor: the server derives both from the X-Platform header that
  base.js `apiRequest` adds.

The substring bans apply to the switch's own block, so an unrelated future edit elsewhere on the
page cannot fail them. The "second sender" check covers the whole file, because that is its point.
"""

import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "business_app" / "static" / "js" / "pages" / "profile-settings.js"
FIELD = "reorder_reminders_enabled"
SWITCH_OPENING = "if (reorderSwitch) {"
HANDLER_OPENING = "reorderSwitch.addEventListener('change'"
RENDERER_OPENING = "var renderReorderReminders = function (state) {"
PUBLISHED = "renderReorderReminders(result.data.preferences.reorder_reminders)"


def _code() -> str:
    """The script without its comments, which name the rules these checks keep out of the code."""
    src = SCRIPT.read_text(encoding="utf-8")
    return "\n".join(re.sub(r"//.*$", "", line) for line in src.splitlines())


def _span(code: str, opening: str, open_char: str, close_char: str) -> str:
    """The text from `opening` to the delimiter that closes its first `open_char`, string literals
    skipped so a quote or brace inside a string cannot end the span early."""
    start = code.index(opening)
    depth = 0
    quote = None
    for index in range(start + opening.index(open_char), len(code)):
        char = code[index]
        if quote:
            if char == quote and code[index - 1] != "\\":
                quote = None
        elif char in "'\"":
            quote = char
        elif char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return code[start : index + 1]
    raise AssertionError(f"{opening} is never closed")


def _switch() -> str:
    """The whole reorder-switch block: its element lookup and the `if (reorderSwitch) { ... }`."""
    tail = _code()
    tail = tail[tail.index("var reorderSwitch = document.getElementById('reorderRemindersSwitch');") :]
    guarded = _span(tail, SWITCH_OPENING, "{", "}")
    return tail[: tail.index(guarded) + len(guarded)]


def _handler() -> str:
    return _span(_switch(), HANDLER_OPENING, "(", ")")


def _renderer() -> str:
    return _span(_switch(), RENDERER_OPENING, "{", "}")


def test_the_switch_reads_and_writes_the_preferences_api():
    block = _switch()
    assert "apiRequest('/notifications/preferences', { method: 'GET' })" in block
    assert "apiRequest('/notifications/preferences', {" in block and "method: 'PUT'" in block
    assert f"JSON.stringify({{ {FIELD}: wanted }})" in block


def test_the_only_place_the_field_is_sent_is_the_switchs_change_handler():
    code = _code()
    assert code.count(FIELD) == 1, "another save on the page names the reminder field"

    start = code.index(_handler())
    assert start < code.index(FIELD) < start + len(_handler()), (
        "the reminder field is sent outside the switch's change handler"
    )
    # The handler is the switch's alone: it is registered once, on the switch.
    assert code.count(HANDLER_OPENING) == 1


def test_the_switch_is_disabled_before_the_request_leaves():
    """The in-flight guard: a second tap cannot start a second save while the first is unanswered."""
    handler = _handler()
    assert handler.index(".disabled = true") < handler.index("apiRequest(")


def test_the_switch_is_enabled_only_by_the_renderer_and_only_for_a_published_state():
    block = _switch()
    # Two writes to `disabled` exist: the renderer, which enables, and the save in flight, which disables.
    assert sorted(re.findall(r"\.disabled\s*=\s*([^;]+);", block)) == ["false", "true"]
    renderer = _renderer()
    assert ".disabled = false" in renderer
    # Nothing is drawn, and nothing enabled, unless a state was published.
    assert re.search(r"\{\s*if \(!state\) \{\s*return false;\s*\}", renderer)
    assert renderer.index("if (!state)") < renderer.index(".disabled = false") < renderer.index("return true")
    assert renderer.index("if (!state)") < renderer.index("publishedReorder = state")


def test_the_switch_redraws_from_the_servers_answer():
    # Once after the first GET, once after each PUT.
    assert _switch().count(PUBLISHED) == 2


def test_the_switch_is_never_drawn_from_the_clicked_state():
    block = _switch()
    assert "!wanted" not in block, "a failed save must revert to the last published value, not to the opposite of the click"
    assert not re.search(r"\.checked\s*=\s*wanted", block)
    # The renderer is the one place that sets `checked`, and it reads the published state.
    assert re.findall(r"\.checked\s*=\s*([^;]+);", block) == ["!!state.enabled"]


def test_a_200_without_a_published_state_is_a_failed_save_not_a_saved_one():
    """`{data: {preferences: {}}}` (the serializer's fallback path) must not draw a dead, disabled switch
    under a 'saved' toast: it reverts and fails like a refusal."""
    handler = _handler()
    refusal = re.search(r"if \(!response\.ok\) \{\s*failReorderSave\(\);\s*return;\s*\}", handler)
    missing = re.search(
        r"if \(!renderReorderReminders\(result\.data\.preferences\.reorder_reminders\)\) \{\s*failReorderSave\(\);\s*return;\s*\}",
        handler,
    )
    assert refusal and missing, "a refusal and an answer with no published value both fail the save"
    assert refusal.end() < missing.start() < handler.index("PAGE_DATA.i18n.reorder_reminders_saved")
    # The toast says 'saved' once, after both checks.
    assert handler.count("PAGE_DATA.i18n.reorder_reminders_saved") == 1


def test_a_failed_save_reverts_to_the_last_published_value_and_says_so_in_the_customers_language():
    block = _switch()
    handler = _handler()
    failure = _span(block, "var failReorderSave = function () {", "{", "}")
    assert "revertReorderReminders();" in failure
    assert "PAGE_DATA.i18n.reorder_reminders_failed" in failure
    # The revert goes through the one renderer, with the state it last drew.
    assert "renderReorderReminders(publishedReorder)" in _span(block, "var revertReorderReminders = function () {", "{", "}")
    # A thrown error reverts as well, and is told as a network error.
    catch = handler[handler.index("catch (error) {") :]
    assert "revertReorderReminders();" in catch and "PAGE_DATA.i18n.network_error" in catch
    # The server's own message is English (api_responses.error_response); it never reaches the toast.
    assert "result.message" not in handler


def test_keyboard_focus_comes_back_to_the_switch_after_the_save():
    """Disabling the switch drops its focus; a keyboard user must land back on it once it is usable."""
    handler = _handler()
    assert handler.index("document.activeElement === reorderSwitch") < handler.index(".disabled = true")
    finally_block = handler[handler.index("finally {") :]
    assert "reorderSwitch.focus()" in finally_block
    assert "!reorderSwitch.disabled" in finally_block, "only a usable switch can take focus back"


def test_the_switchs_block_never_names_a_route_or_rederives_the_setting():
    block = _switch()
    for leak in ("changed_via", "web_settings", "telegram_settings", "changed_by_kind", "admin", "X-Platform"):
        assert leak not in block, leak
    assert not re.search(r"\bvia\b", block), "the route is derived on the server from X-Platform"
    assert "headers" not in block, "apiRequest adds the headers; the switch sends none of its own"
