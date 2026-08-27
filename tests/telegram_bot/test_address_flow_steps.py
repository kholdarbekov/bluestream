from unittest.mock import AsyncMock

import pytest
from telegram import constants, ReplyKeyboardMarkup
from telegram.helpers import escape_markdown

from handlers.address_flow import (
    ADDRESS_APARTMENT, ADDRESS_DISTRICT, ADDRESS_GEOCODE_CONFIRM,
    ADDRESS_LOCATION, ADDRESS_REGION, ADDRESS_STREET, ADDRESS_TITLE,
    _ADDRESS_STEPS, _FLOW_STEPS, handle_input, render_prompt,
)

# `ProfileHandlers` (not just `handlers.address_flow`) on purpose: Task 3
# also covers `_send_for`, which lives on the handler class because it
# builds a `RenderTarget` from a live `Update`/`context` — the one seam this
# otherwise-pure test file cannot avoid. Imported at module (collection)
# level, not inside a test function, for the same reason
# test_address_location_entry.py gives: `i18n`, `keyboards` and `config` must
# be cached in sys.modules as the BOT's versions before any string-path
# monkeypatch below resolves them.
from handlers.profile import ProfileHandlers

from tests.telegram_bot.helpers import DummyCallbackQuery, DummyUpdate, make_context

pytestmark = [pytest.mark.anyio]


@pytest.fixture
def echo_i18n(monkeypatch):
    """`i18n.get` returns a humanised key tail and SILENTLY DROPS kwargs when
    no translation is loaded (this suite runs with an empty translation
    table), so an unstubbed interpolation test would pass even if the render
    code forgot to interpolate. Echo the key plus every kwarg instead — same
    fixture, same reasoning, as `echo_i18n` in test_address_location_entry.py.

    With no kwargs, this returns the key VERBATIM (not humanised), which is
    also what lets `test_each_render_based_step_uses_the_tables_prompt_key`
    assert the rendered text traces back to `_FLOW_STEPS[field].prompt_key`
    exactly, rather than a hardcoded copy inside the renderer.
    """
    def _get(key, language=None, *args, **kwargs):
        if kwargs:
            return f"{key}|" + "|".join(f"{k}={v}" for k, v in sorted(kwargs.items()))
        return key

    monkeypatch.setattr("i18n.i18n.get", _get)
    return _get


class _RecordingTarget:
    """A `RenderTarget` that records every call instead of talking to
    Telegram — the same job `sent = []` / `async def send(...)` played
    before `RenderTarget` replaced the single-method `Send` callable, now
    covering all four transport shapes a step can use.

    Each entry in `.calls` is a tuple whose first element names the method:
    ``('send_text', text, keyboard, parse_mode)``,
    ``('send_markdown_or_plain', text, keyboard)``,
    ``('send_location', latitude, longitude)``, or
    ``('request_location', text, keyboard)``.
    """

    def __init__(self):
        self.calls = []

    async def send_text(self, text, keyboard=None, parse_mode=None):
        self.calls.append(('send_text', text, keyboard, parse_mode))

    async def send_markdown_or_plain(self, text, keyboard=None):
        self.calls.append(('send_markdown_or_plain', text, keyboard))

    async def send_location(self, latitude, longitude):
        self.calls.append(('send_location', latitude, longitude))

    async def request_location(self, text, keyboard):
        self.calls.append(('request_location', text, keyboard))


def _rendered_text(target):
    """The text of whichever call in `target.calls` actually carries copy —
    `send_location` carries none, so callers (like `geocode_confirm`, whose
    pin goes out before its words) skip straight past it."""
    for call in target.calls:
        if call[0] != 'send_location':
            return call[1]
    raise AssertionError("no text-carrying call was recorded")


async def test_render_prompt_sends_through_the_given_target_and_returns_the_state():
    """The send path is a PARAMETER, not derived from an Update.

    Resume arrives as a plain message with no callback_query, so a renderer
    that branches on `update.callback_query` cannot serve it. Passing the
    target in is what lets the forward path (edit-a-callback-message) and the
    resume path (send-a-new-message) share one implementation.
    """
    target = _RecordingTarget()

    state = await render_prompt('apartment', 'en', target)

    assert len(target.calls) == 1, "the prompt must go through the supplied target exactly once"
    kind, text, keyboard, parse_mode = target.calls[0]
    assert kind == 'send_text', "a regular (non-render) step sends plain text"
    assert text, "the prompt text must be resolved from the step's translation key"
    assert parse_mode is None, "the apartment prompt carries no parse mode"
    callbacks = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    ]
    assert "skip_apartment" in callbacks, "the apartment step renders a Skip keyboard"
    assert state == ADDRESS_APARTMENT, "callers must get back the apartment step's own state"


async def test_every_step_in_the_flow_has_a_table_entry():
    """`_FLOW_STEPS` is the SSOT for position across the FULL ten-step flow.
    A step missing from it cannot be resumed, because resume renders from
    `draft["step"]` and nothing else.

    (`_ADDRESS_STEPS` is a narrower table — just the four Skip-able
    optional-detail steps — used where the Skip-button invariant applies;
    see `test_flow_steps_derives_its_optional_detail_entries_from_address_steps`.)
    """
    expected = {
        'location', 'title', 'region', 'district', 'street',
        'building', 'apartment', 'floor', 'delivery_instructions',
        'geocode_confirm',
    }
    assert set(_FLOW_STEPS) == expected, (
        "every step a draft can park on needs an entry, or resume cannot render it"
    )


async def test_flow_steps_derives_its_optional_detail_entries_from_address_steps():
    """`_FLOW_STEPS` must not restate the four optional-detail steps that
    `_ADDRESS_STEPS` already defines — the drift guard for having two tables.
    A future edit to one that isn't mirrored in the other would otherwise
    silently diverge.

    Also pins that none of the four derived entries can be silently upgraded
    to a `render`-based step by a future `.update()` — that would smuggle a
    Skip-able field onto the irregular path with no test noticing until a
    Skip button on that step went dead.
    """
    assert set(_ADDRESS_STEPS), "sanity: _ADDRESS_STEPS must not be empty for this to prove anything"
    for field, step in _ADDRESS_STEPS.items():
        flow_step = _FLOW_STEPS[field]
        assert flow_step.prompt_key == step.prompt_key
        assert flow_step.keyboard == step.keyboard
        assert flow_step.state == step.state
        assert flow_step.render is None, (
            f"{field} is Skip-able and must stay on the plain prompt_key/keyboard path"
        )


async def test_street_renders_no_keyboard_and_carries_its_district_name(echo_i18n):
    """Street is required (no Skip) and its copy interpolates the district,
    rendered as MARKDOWN_V2 to match `district_selected` (profile.py:2279)
    exactly — sending pre-escaped text with no parse mode would show the
    customer literal backslashes instead of the words they escape."""
    target = _RecordingTarget()

    state = await render_prompt('street', 'en', target, data={'district_name': 'Chilonzor'})

    assert len(target.calls) == 1
    kind, text, keyboard, parse_mode = target.calls[0]
    assert kind == 'send_text'
    assert keyboard is None, "street is required and renders no Skip button"
    assert parse_mode == constants.ParseMode.MARKDOWN_V2, (
        "unescaped MARKDOWN_V2 markup sent with no parse mode shows literal backslashes"
    )
    assert 'Chilonzor' in text, "the street prompt names the district just chosen"
    assert state == ADDRESS_STREET, "callers must get back the street step's own state"


async def test_street_prompt_escapes_markdownv2_special_characters(echo_i18n):
    """The escaping itself, pinned. `'Chilonzor' in text` passes whether or
    not `_render_street` actually escapes — this test fails if it stops."""
    target = _RecordingTarget()

    await render_prompt('street', 'en', target, data={'district_name': 'Chilonzor-5'})

    _, text, _, _ = target.calls[0]
    assert 'Chilonzor\\-5' in text, (
        "MARKDOWN_V2 must escape the hyphen or Telegram refuses the whole message"
    )
    assert 'Chilonzor-5' not in text, "the unescaped form must not appear at all"


async def test_street_render_raises_a_clear_error_when_district_name_is_missing():
    """The Task 4 resume path is exactly where a partial draft appears. A
    missing district name must raise loudly, not render 'None' into the
    customer-facing prompt."""
    target = _RecordingTarget()

    with pytest.raises(ValueError, match="district_name"):
        await render_prompt('street', 'en', target, data={})

    assert target.calls == [], "nothing should be sent when the render cannot proceed"


async def test_location_step_renders_through_request_location_with_an_armed_reply_keyboard():
    """`location` cannot use `send_text` (typed for inline markup, and never
    arms anything) because sharing a pin needs both Telegram's
    request_location REPLY keyboard AND `arm_location_request` — routing
    through `RenderTarget.request_location` is what makes the arming
    impossible to forget (utils.py:714-744: "a missed site means that flow's
    pin silently becomes a support ticket instead of an address")."""
    target = _RecordingTarget()

    state = await render_prompt('location', 'en', target)

    assert len(target.calls) == 1
    kind, text, keyboard = target.calls[0]
    assert kind == 'request_location', "the location step must arm the next pin via request_location"
    assert text, "the prompt text must be resolved from the step's translation key"
    assert isinstance(keyboard, ReplyKeyboardMarkup), "location shares a pin via a reply keyboard"
    assert state == ADDRESS_LOCATION, "callers must get back the location step's own state"


async def test_district_step_renders_the_runtime_district_list():
    """District options come from `get_all_districts`, resolved at render
    time — a step missing this would show an empty picker."""
    target = _RecordingTarget()

    state = await render_prompt('district', 'en', target)

    assert len(target.calls) == 1
    kind, text, keyboard, parse_mode = target.calls[0]
    assert kind == 'send_text'
    callbacks = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    ]
    assert any(cb.startswith('district_') for cb in callbacks), (
        "the district step renders a button per district"
    )
    assert state == ADDRESS_DISTRICT, "callers must get back the district step's own state"


async def test_geocode_confirm_sends_the_pin_before_the_text(echo_i18n):
    """A resume that shows only words asks the customer to confirm a map they
    cannot see — the pin has to go out first. Reads `full_address` (the key
    `temp_address_data` actually carries — `address` does not exist there),
    and — with no `approximate` flag in `data` — carries no approximate-centre
    disclosure, matching a normal, successfully geocoded confirmation."""
    target = _RecordingTarget()

    state = await render_prompt(
        'geocode_confirm', 'en', target,
        data={
            'full_address': 'Chilonzor 15',
            'latitude': 41.32,
            'longitude': 69.19,
        },
    )

    kinds = [call[0] for call in target.calls]
    assert kinds == ['send_location', 'send_markdown_or_plain'], (
        "the pin must be sent before the confirmation text, via send_location then "
        "send_markdown_or_plain (the live handler's own Markdown-with-fallback transport)"
    )
    assert target.calls[0][1:] == (41.32, 69.19)
    _, text, keyboard = target.calls[1]
    assert 'Chilonzor 15' in text, "the confirmation names the geocoded address"
    assert 'geocode_note_approximate_center' not in text, (
        "no approximate-centre note without the approximate flag"
    )
    assert keyboard is not None, "geocode_confirm renders Confirm/Retry buttons"
    assert state == ADDRESS_GEOCODE_CONFIRM, "callers must get back the geocode_confirm step's own state"


async def test_geocode_confirm_appends_the_approximate_center_note_when_flagged(echo_i18n):
    """profile.py:2782-2788/2837-2838: when geocoding fails, a district centre
    substitutes for the pin, and the live handler appends a safety note right
    before the customer taps "Yes, correct" — dropping it would let them
    confirm a guess the copy never disclosed was one."""
    target = _RecordingTarget()

    await render_prompt(
        'geocode_confirm', 'en', target,
        data={
            'full_address': 'Chilonzor 15',
            'latitude': 41.32,
            'longitude': 69.19,
            'approximate': True,
        },
    )

    _, text, _ = target.calls[1]
    assert 'telegram.address.geocode_note_approximate_center' in text, (
        "the approximate-centre disclosure must be appended when the flag is set"
    )


async def test_geocode_confirm_render_raises_a_clear_error_when_coordinates_are_missing():
    """A partial draft with no pin recorded cannot render this step — raise
    clearly rather than sending `send_location(None, None)` into PTB."""
    target = _RecordingTarget()

    with pytest.raises(ValueError, match="latitude"):
        await render_prompt('geocode_confirm', 'en', target, data={'full_address': 'x'})

    assert target.calls == [], "nothing should be sent when the render cannot proceed"


async def test_title_and_region_are_regular_table_entries():
    """`title` and `region` need no `render` override — they fit the plain
    prompt_key + keyboard shape like the four steps Task 1 already covered."""
    target = _RecordingTarget()

    state = await render_prompt('region', 'en', target)

    assert len(target.calls) == 1
    kind, text, keyboard, parse_mode = target.calls[0]
    assert kind == 'send_text'
    assert text
    assert keyboard is not None
    assert state == ADDRESS_REGION

    target = _RecordingTarget()
    state = await render_prompt('title', 'en', target)

    kind, text, keyboard, parse_mode = target.calls[0]
    assert kind == 'send_text'
    assert text
    assert keyboard is not None
    assert state == ADDRESS_TITLE


@pytest.mark.parametrize(
    "field, data",
    [
        ('location', None),
        ('district', None),
        ('street', {'district_name': 'Chilonzor'}),
        ('geocode_confirm', {'latitude': 41.32, 'longitude': 69.19, 'full_address': 'X'}),
    ],
)
async def test_each_render_based_step_uses_the_tables_prompt_key(echo_i18n, field, data):
    """`render_prompt` never reads `step.prompt_key` itself when `step.render`
    is set — every one of these four renderers used to hardcode its OWN copy
    of the key, so editing `_FLOW_STEPS[field].prompt_key` was a silent
    no-op. This proves the table's key is what actually reaches the
    customer, by asserting the rendered text starts with it exactly (via
    `echo_i18n`, which returns an unstubbed key verbatim)."""
    target = _RecordingTarget()

    await render_prompt(field, 'en', target, data=data)

    text = _rendered_text(target)
    prompt_key = _FLOW_STEPS[field].prompt_key
    # `street` runs its whole rendered string through `escape_markdown`
    # (MARKDOWN_V2), which escapes the key's own dots and underscores too —
    # so the prefix to look for is the ESCAPED key, not the raw one.
    expected_prefix = escape_markdown(prompt_key, version=2) if field == 'street' else prompt_key
    assert text.startswith(expected_prefix), (
        f"{field}'s render must use step.prompt_key, not a hardcoded copy"
    )


def test_handle_input_writes_the_field_and_names_the_next_step():
    data = {}
    nxt = handle_input('street', 'Bunyodkor', data)

    assert data['street_address'] == 'Bunyodkor', (
        "the value lands under the step's own key in _ADDRESS_FIELD_DATA_KEYS"
    )
    assert nxt == 'building', "street is followed by building in both branches"


def test_handle_input_on_the_last_optional_step_is_terminal():
    data = {}
    assert handle_input('delivery_instructions', 'ring twice', data) is None, (
        "the chain ends here; the caller saves"
    )


def test_handle_input_walks_every_regular_step_of_the_chain():
    """Answering does not jump the way `_SKIP_TARGETS` does — every step
    leads to the very next one, never past a neighbour."""
    data = {}
    assert handle_input('building', '12', data) == 'apartment'
    assert handle_input('apartment', '4', data) == 'floor'
    assert handle_input('floor', '2', data) == 'delivery_instructions'
    assert data == {
        'building_number': '12',
        'apartment_number': '4',
        'floor_number': '2',
    }, "each answer must land under its own key, none of the others touched"


def test_next_field_tracks_address_field_data_keys_order():
    """`_NEXT_FIELD` is DERIVED from `_ADDRESS_FIELD_DATA_KEYS`'s own order
    rather than restated as a second literal table (that table's own
    docstring calls the order load-bearing). Pins the derivation stays
    correct if either table changes: an insertion into
    `_ADDRESS_FIELD_DATA_KEYS` must shift `_NEXT_FIELD` with it, not leave a
    stale hand-written chain link behind."""
    from handlers.address_flow import _ADDRESS_FIELD_DATA_KEYS, _NEXT_FIELD

    fields = list(_ADDRESS_FIELD_DATA_KEYS)
    assert set(_NEXT_FIELD) == set(fields), (
        "_NEXT_FIELD must cover exactly the chain-advance fields, no more, no less"
    )
    for index, field in enumerate(fields):
        expected = fields[index + 1] if index + 1 < len(fields) else None
        assert _NEXT_FIELD[field] == expected, (
            f"{field} must lead to the field that follows it in "
            "_ADDRESS_FIELD_DATA_KEYS, or None at the end of the chain"
        )


def test_handle_input_raises_a_named_error_for_title_instead_of_a_bare_keyerror():
    """`title` is a typed address step but NOT in `_ADDRESS_FIELD_DATA_KEYS`
    (its next step is branch-dependent — `_is_shared_pin_address` picks
    'apartment' vs a save — and it has no Skip button, so it cannot be
    expressed as a chain-advance). Every profile.py text handler wraps its
    body in `except Exception: return ConversationHandler.END`
    (profile.py:2585-2587 and siblings), so a bare `KeyError` here would
    silently end the customer's conversation with no message and no error —
    on a flow this whole effort is making resumable. Must raise loudly
    instead."""
    with pytest.raises(ValueError, match="title"):
        handle_input('title', 'Home', {})


# ---------------------------------------------------------------------------
# `_send_for` — ZERO coverage before this task. `request_location` is the
# site the RenderTarget path uses to call `arm_location_request`
# (`profile.py::_send_for`'s `request_location`) — one of SIX arm sites in
# the bot: `profile.py::add_address`, `profile.py::location_received`
# (out-of-zone re-prompt), `profile.py::geocode_and_confirm` (out-of-zone
# re-prompt), `profile.py::retry_geocode`, and `orders.py::checkout_handler`
# (zero-address checkout) each arm their own prompt too, for the same
# reason. Cited by function name, not line number: this exact list has
# already rotted twice from a docstring edit shifting the very lines it
# cited. utils.py:714-744 spells out the cost of a missed site: "that
# flow's pin silently becomes a support ticket instead of an address."
# ---------------------------------------------------------------------------


async def test_send_for_request_location_arms_before_replying_on_the_callback_message(monkeypatch):
    """The common case: a Skip/Back tap landed on the location step, and the
    tap's own message is still there to reply on."""
    order = []
    monkeypatch.setattr(
        "handlers.profile.arm_location_request",
        lambda context: order.append("armed"),
    )

    handler = ProfileHandlers()
    update = DummyUpdate()
    update.callback_query = DummyCallbackQuery()
    update.callback_query.message.reply_text = AsyncMock(
        side_effect=lambda *a, **k: order.append("sent")
    )
    context = make_context()

    target = handler._send_for(update, context)
    await target.request_location('share your pin', 'KEYBOARD')

    assert order == ["armed", "sent"], (
        "arm_location_request must run BEFORE the prompt goes out — a pin "
        "shared in the gap between them would have nothing armed to route it"
    )
    update.callback_query.answer.assert_awaited_once()
    update.callback_query.message.reply_text.assert_awaited_once_with(
        'share your pin', reply_markup='KEYBOARD', parse_mode='Markdown'
    )


async def test_send_for_request_location_arms_before_falling_back_when_the_callback_message_is_gone(monkeypatch):
    """`query.message is None` — Telegram redelivered a callback whose own
    message is no longer attached. The fallback still has to arm first and
    still has to reach the customer, via `context.bot.send_message` addressed
    by user id rather than by replying on a message that does not exist."""
    order = []
    monkeypatch.setattr(
        "handlers.profile.arm_location_request",
        lambda context: order.append("armed"),
    )

    handler = ProfileHandlers()
    update = DummyUpdate(user_id=4242)
    update.callback_query = DummyCallbackQuery()
    update.callback_query.message = None
    context = make_context()
    context.bot.send_message = AsyncMock(side_effect=lambda **k: order.append("sent"))

    target = handler._send_for(update, context)
    await target.request_location('share your pin', 'KEYBOARD')

    assert order == ["armed", "sent"], (
        "the fallback path must arm before sending too, not only the happy path"
    )
    context.bot.send_message.assert_awaited_once_with(
        chat_id=4242, text='share your pin', reply_markup='KEYBOARD', parse_mode='Markdown',
    )


async def test_send_for_request_location_arms_before_sending_with_no_callback_query_at_all(monkeypatch):
    """The plain-message branch — `update.callback_query is None`, not just
    `query.message is None` — is exactly the resume path a later task drives:
    a prompt re-sent with no tap behind it, via `update.message.reply_text`.

    Both tests above pass even if `arm_location_request` were moved inside
    `if query is not None:`, because both supply a callback_query. This one
    pins the branch that move would silently stop arming, which is the
    failure utils.py:725-726 describes."""
    order = []
    monkeypatch.setattr(
        "handlers.profile.arm_location_request",
        lambda context: order.append("armed"),
    )

    handler = ProfileHandlers()
    update = DummyUpdate()
    assert update.callback_query is None, "sanity: this is the no-callback-query branch"
    update.message.reply_text = AsyncMock(side_effect=lambda *a, **k: order.append("sent"))
    context = make_context()

    target = handler._send_for(update, context)
    await target.request_location('share your pin', 'KEYBOARD')

    assert order == ["armed", "sent"], (
        "the no-callback-query branch must arm before sending too — this is "
        "the resume path a later task drives"
    )
    update.message.reply_text.assert_awaited_once_with(
        'share your pin', reply_markup='KEYBOARD', parse_mode='Markdown'
    )
