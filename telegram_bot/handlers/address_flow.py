"""The address-creation step table and its pure renderers.

Split out of `profile.py` (3820 lines, five concerns) so the step machine can
be read and tested without an Update, a bot, or a ConversationHandler. Nothing
here touches `telegram.Update` or `ContextTypes.DEFAULT_TYPE`: callers supply
a `RenderTarget` and get back the step's PTB state int.
"""
from typing import Any, Callable, NamedTuple, Optional, Protocol

from telegram import constants, InlineKeyboardMarkup
from telegram.helpers import escape_markdown

from i18n import i18n
from keyboards import ProfileKeyboards
from shared.constants import get_all_districts
# Conversation-state ints live in `handlers.conversation_states`, a leaf
# module with no imports of its own, so pulling them in here creates no
# import-time dependency on `profile.py`. Re-imported by name (not just used
# locally) so that `profile.py`'s existing `from handlers.address_flow import
# ADDRESS_BUILDING`-style import keeps working unchanged.
from handlers.conversation_states import (
    SELECT_LANGUAGE, PHONE, ADDRESS_LOCATION, ADDRESS_TITLE,
    ADDRESS_REGION, ADDRESS_DISTRICT, ADDRESS_STREET, ADDRESS_BUILDING,
    ADDRESS_APARTMENT, ADDRESS_FLOOR,
    ADDRESS_DELIVERY_INSTRUCTIONS, ADDRESS_GEOCODE_CONFIRM,
    PHONE_VERIFY_PHONE, PHONE_VERIFY_NAME,
    LINK_ACCOUNT_CONFIRM, LINK_ACCOUNT_OTP, REGISTER_OTP,
)


class AddressStep(NamedTuple):
    """One step of the optional address-detail chain.

    Named rather than a bare tuple because all three members are read at
    unrelated call sites: the prompt key by the prompt helper, the keyboard by
    both the prompt and the too-long re-prompt, and the state by every caller
    that returns it to the ConversationHandler.

    Deliberately three fields only, covering the four steps both address
    flows converge on (`building` -> `apartment` -> `floor` ->
    `delivery_instructions`) — every one of them Skip-able.
    `tests/unit/test_customer_bot_address_detail_flow.py` proves the
    load-bearing invariant that keeps a Skip button from ever going dead:
    each entry's `state` must have a `^skip_{field}$` pattern registered in
    `bot.py` under that same state, and `keyboard` must emit that exact
    callback. That invariant does not hold for the rest of the address flow
    (`location`, `title`, `region`, `district`, `street`, `geocode_confirm` —
    none of which have a Skip button), so those live in the wider
    `_FLOW_STEPS` table below instead of here.
    """

    prompt_key: str
    keyboard: Callable[[str], InlineKeyboardMarkup]
    state: int


class RenderTarget(Protocol):
    """Where a step's prompt goes.

    Four shapes, because the flow's real transport is not one shape: plain
    text (`send_text`, with an OPTIONAL parse mode — street needs MARKDOWN_V2,
    most steps need none), text that falls back from Markdown to plain on a
    refusal (`send_markdown_or_plain`, mirroring
    `profile.py::_reply_markdown_or_plain`, used where the live handler
    already accepts "lose the bold rather than lose the flow"), a location pin
    (`send_location`), and the one send that also has a side effect — arming
    the NEXT pin the customer shares to route back into this flow
    (`request_location`, mirroring `utils.arm_location_request`: "a missed
    site means that flow's pin silently becomes a support ticket instead of
    an address").

    `profile.py::_send_for` builds the concrete implementation from an
    `Update` and a `ContextTypes.DEFAULT_TYPE`. Nothing in this module
    constructs one, and nothing here imports `telegram.Update` — the step
    table stays readable and testable without either.
    """

    async def send_text(
        self,
        text: str,
        keyboard: Optional[InlineKeyboardMarkup] = None,
        parse_mode: Optional[str] = None,
    ) -> None:
        ...

    async def send_markdown_or_plain(
        self, text: str, keyboard: Optional[InlineKeyboardMarkup] = None
    ) -> None:
        ...

    async def send_location(self, latitude: float, longitude: float) -> None:
        ...

    async def request_location(self, text: str, keyboard: Any) -> None:
        ...


class FlowStep(NamedTuple):
    """One step of the FULL address flow — the ten steps a draft can park on,
    not just the four Skip-able ones `AddressStep` covers.

    `prompt_key` / `keyboard` serve the six regular steps. `render` overrides
    both for the four that cannot be expressed that way: district needs a
    runtime list, street interpolates and escapes, geocode_confirm sends a pin
    first, and location arms the next pin and sends a REPLY keyboard.
    """

    prompt_key: str
    keyboard: Callable[[str], Optional[InlineKeyboardMarkup]]
    state: int
    render: Optional[Callable] = None


def _no_keyboard(language: str) -> None:
    """Placeholder for `FlowStep.keyboard` on the four steps whose `render`
    bypasses it entirely. `render_prompt` never calls `step.keyboard` when
    `step.render` is set, so this exists only to satisfy the NamedTuple field
    — see `FlowStep`'s own docstring for why those four cannot use it for
    real.
    """
    return None


async def _render_location(
    language: str, target: RenderTarget, data: dict, prompt_key: str
) -> None:
    """Ask for a pin — the entry point of the pin branch (`add_address`,
    profile.py).

    Routed through `RenderTarget.request_location` rather than `send_text`
    for two reasons: `ProfileKeyboards.location_request` returns a REPLY
    keyboard, not the `InlineKeyboardMarkup` `send_text` is typed for; and
    `request_location`'s concrete implementation is the site THIS
    RenderTarget-driven prompt uses to call `arm_location_request` — baked
    into the transport rather than left to this renderer, so a caller
    reaching this render function cannot forget it the way a hand-rolled
    call site could (`utils.arm_location_request`). It is not the only arm
    site: six call `arm_location_request` in total —
    `profile.py::_send_for`'s `request_location` (this one),
    `profile.py::add_address`, `profile.py::location_received` (out-of-zone
    re-prompt), `profile.py::geocode_and_confirm` (out-of-zone re-prompt),
    `profile.py::retry_geocode`, and `orders.py::checkout_handler`
    (zero-address checkout) — each arming its own location prompt
    separately, for the same reason. Cited by function name, not line
    number: this list has already rotted TWICE from a docstring edit
    shifting the very lines it cited, and the `parse_mode` citation just
    below rotted a THIRD time (M2, final whole-branch review) — line numbers
    in a cross-file citation do not survive an edit to the file they point
    into. The live handler (`profile.py::add_address`) also sends this
    prompt with `parse_mode='Markdown'` — its copy carries deliberate
    `*bold*` — which `request_location` applies internally for the same
    reason `add_address` always does.
    """
    text = i18n.get(prompt_key, language)
    keyboard = ProfileKeyboards.location_request(
        language,
        extra_rows=(i18n.get('telegram.address.enter_manually_button', language),),
    )
    await target.request_location(text, keyboard)


async def _render_district(
    language: str, target: RenderTarget, data: dict, prompt_key: str
) -> None:
    """District options come from `get_all_districts`, resolved at render
    time — a fixed `Callable[[str], ...]` keyboard cannot also load the
    district table, so this step needs `render` instead.
    """
    districts = get_all_districts(language)
    text = i18n.get(prompt_key, language)
    await target.send_text(text, ProfileKeyboards.district_selection(districts, language))


async def _render_street(
    language: str, target: RenderTarget, data: dict, prompt_key: str
) -> None:
    """Street is required (no Skip button) and its prompt names the district
    just chosen (`district_selected`, profile.py).

    Rendered as MARKDOWN_V2, matching `district_selected` exactly (cited by
    function name, not line number — see `_render_location`'s docstring
    above for why): escaping the interpolated district name and then sending it with
    no parse mode at all is worse than not escaping — the customer sees a
    literal backslash before every `.`, `(`, `)`, `-` and `!` instead of the
    hazard the escaping exists to prevent (a street or district name carrying
    `_`, `*`, `[` or a backtick making Telegram refuse the whole message,
    ending the flow one step before the customer could recover).

    `data['district_name']` is REQUIRED. A partial draft with no district
    name on it cannot render this prompt at all — a `.get(...)` default would
    silently draw "Enter the street in None" instead of surfacing that the
    draft it was asked to resume is broken.
    """
    if data.get('district_name') is None:
        raise ValueError(
            "render_prompt('street', ...) requires data['district_name']; "
            "a partial draft cannot resume on this step without it"
        )
    text = escape_markdown(
        i18n.get(prompt_key, language, district_name=data['district_name']),
        version=2,
    )
    await target.send_text(text, None, parse_mode=constants.ParseMode.MARKDOWN_V2)


async def _render_geocode_confirm(
    language: str, target: RenderTarget, data: dict, prompt_key: str
) -> None:
    """Confirm a geocoded point: send the pin, THEN the words.

    A resume that shows only text asks the customer to confirm a map they
    cannot see, so the pin goes out via `RenderTarget.send_location` before
    the confirmation text.

    `data['latitude']` / `data['longitude']` are REQUIRED for the same reason
    `street` requires `district_name`: a partial draft missing them cannot
    render a pin, and a `.get(...)` default would either draw nothing or
    raise deep inside python-telegram-bot with a confusing error, instead of
    a clear one raised here.

    `data['full_address']` — NOT `data['address']`; `temp_address_data` never
    holds an `address` key, only `full_address` (`geocode_and_confirm`,
    profile.py — cited by function name, not line number; see
    `_render_location`'s docstring for why) — falls back to "Not set" exactly
    like the live handler, since an address that failed to geocode AND was
    never given a name is a real, expected state, not a bug.

    `data['approximate']` mirrors `geocode_and_confirm`'s own
    `geocode_success` flag: when geocoding failed and a district centre was
    substituted for the pin, the live handler appends a safety disclosure
    immediately before the customer taps "Yes, correct". Dropping that note
    would let them confirm a point the copy never told them was a guess, so
    it is carried here and appended on the same condition.
    `geocode_and_confirm` itself now persists the same flag onto
    `temp_address_data['approximate']` so a later resume of this step can
    reproduce it.
    """
    latitude = data.get('latitude')
    longitude = data.get('longitude')
    if latitude is None or longitude is None:
        raise ValueError(
            "render_prompt('geocode_confirm', ...) requires data['latitude'] "
            "and data['longitude']; a partial draft cannot resume on this "
            "step without a point to pin"
        )
    await target.send_location(latitude, longitude)

    text = i18n.get(
        prompt_key,
        language,
        address=data.get('full_address', i18n.get('telegram.common.not_set', language)),
    )
    if data.get('approximate'):
        text += i18n.get('telegram.address.geocode_note_approximate_center', language)

    await target.send_markdown_or_plain(
        text, ProfileKeyboards.geocode_confirmation(language, show_edit=False)
    )


# SSOT for the optional-detail chain both address flows converge on. There is
# deliberately no 'entrance' step. UserAddress has no entrance column, so
# entrance is captured as free text by the delivery-instructions prompt.
_ADDRESS_STEPS = {
    'building': AddressStep(
        'telegram.address.enter_building',
        lambda language: ProfileKeyboards.optional_field_keyboard('building', language),
        ADDRESS_BUILDING,
    ),
    'apartment': AddressStep(
        'telegram.address.enter_apartment',
        lambda language: ProfileKeyboards.optional_field_keyboard('apartment', language),
        ADDRESS_APARTMENT,
    ),
    'floor': AddressStep(
        'telegram.address.enter_floor',
        lambda language: ProfileKeyboards.optional_field_keyboard('floor', language),
        ADDRESS_FLOOR,
    ),
    'delivery_instructions': AddressStep(
        'telegram.address.enter_delivery_instructions',
        ProfileKeyboards.delivery_instructions_keyboard,
        ADDRESS_DELIVERY_INSTRUCTIONS,
    ),
}

# SSOT for every step either address flow can be resumed on: the four
# Skip-able optional-detail steps above, DERIVED from `_ADDRESS_STEPS` rather
# than restated (so the two tables cannot silently drift), plus the six
# regular-flow steps that route through `render` (a runtime list, an
# interpolation, a pin, or an armed reply keyboard) or are otherwise not
# Skip-able.
_FLOW_STEPS: dict = {
    field: FlowStep(step.prompt_key, step.keyboard, step.state)
    for field, step in _ADDRESS_STEPS.items()
}
_FLOW_STEPS.update({
    'location': FlowStep(
        'telegram.address.location_prompt_enhanced',
        _no_keyboard,
        ADDRESS_LOCATION,
        render=_render_location,
    ),
    'title': FlowStep(
        'telegram.address.title_prompt',
        ProfileKeyboards.address_title_suggestions,
        ADDRESS_TITLE,
    ),
    'region': FlowStep(
        'telegram.address.select_region',
        ProfileKeyboards.region_selection,
        ADDRESS_REGION,
    ),
    'district': FlowStep(
        'telegram.address.select_district',
        _no_keyboard,
        ADDRESS_DISTRICT,
        render=_render_district,
    ),
    'street': FlowStep(
        'telegram.address.enter_street_required',
        _no_keyboard,
        ADDRESS_STREET,
        render=_render_street,
    ),
    'geocode_confirm': FlowStep(
        'telegram.address.geocode_found_with_address',
        _no_keyboard,
        ADDRESS_GEOCODE_CONFIRM,
        render=_render_geocode_confirm,
    ),
})

# Where a Skip tap lands. Skipping the building number means there is no building
# to be inside, so apartment and floor are skipped along with it (private house).
# Street is required and renders no Skip button; it stays here as a safety net.
_SKIP_TARGETS = {
    'street': 'building',
    'building': 'delivery_instructions',
    'apartment': 'floor',
    'floor': 'delivery_instructions',
}

# Which key in temp_address_data each step writes, IN FLOW ORDER. Skip CLEARS
# it, so Skip means what it says: retry_geocode reruns the whole chain, so a
# value typed before the retry would otherwise survive a later Skip and still be
# saved. The order is load-bearing — `_cleared_by_skip` walks it to find the
# steps a Skip jumps OVER.
_ADDRESS_FIELD_DATA_KEYS = {
    'street': 'street_address',
    'building': 'building_number',
    'apartment': 'apartment_number',
    'floor': 'floor_number',
    'delivery_instructions': 'delivery_instructions',
}


def _cleared_by_skip(field: str) -> tuple[str, ...]:
    """The temp_address_data keys a Skip on `field` must clear.

    Not just the field that was tapped: a Skip that JUMPS OVER steps clears
    those too. Skipping the building number means there is no building to be
    inside, so `_SKIP_TARGETS` lands on delivery instructions — and an
    apartment and floor typed before a `retry_geocode` rerun would otherwise be
    saved onto a house whose owner has just said it has neither.

    An unknown field (a Skip button rendered by an older deploy) clears
    nothing: it must not take somebody's real answers with it on its way out.
    """
    fields = list(_ADDRESS_FIELD_DATA_KEYS)
    if field not in fields:
        return ()

    start = fields.index(field)
    target = _SKIP_TARGETS.get(field)
    stop = fields.index(target) if target in fields else start + 1
    return tuple(_ADDRESS_FIELD_DATA_KEYS[name] for name in fields[start:stop])


# Where a typed ANSWER (not a Skip) leads next. Deliberately DIFFERENT from
# `_SKIP_TARGETS` above: skipping the building number means there is no
# building to be inside, so a Skip jumps apartment and floor along with it.
# Answering the building does not skip anything — the next step after a
# typed answer is always just the next one in the chain.
#
# DERIVED from `_ADDRESS_FIELD_DATA_KEYS`'s own order, not restated as a
# second literal table. That table's own docstring already calls its order
# "load-bearing", and `_cleared_by_skip` already walks it for the same
# reason. A literal copy here would be a second, unguarded expression of
# that order: insert a future `'entrance'` step into
# `_ADDRESS_FIELD_DATA_KEYS` between `apartment` and `floor` and
# `_cleared_by_skip` picks it up automatically, but a hand-written
# `_NEXT_FIELD['apartment']` would still say `'floor'` — silently never
# asking the new step. Deriving closes that gap by construction.
#
# `title` is deliberately NOT a key here, even though it is a typed step,
# because it cannot be expressed as "the next key in the chain" the way
# every entry below can: its next step is BRANCH-dependent
# (`_is_shared_pin_address` in profile.py picks `'apartment'` or a save,
# not a fixed field), and it has no Skip button. `handle_input` raises for
# it rather than deriving a wrong answer — see its docstring.
_ADDRESS_FIELD_ORDER = list(_ADDRESS_FIELD_DATA_KEYS)
_NEXT_FIELD = {
    field: (
        _ADDRESS_FIELD_ORDER[index + 1]
        if index + 1 < len(_ADDRESS_FIELD_ORDER)
        else None
    )
    for index, field in enumerate(_ADDRESS_FIELD_ORDER)
}


def handle_input(field: str, value: str, data: dict) -> str | None:
    """Record one typed answer and name the step that follows it.

    Writes `value` into `data` under `_ADDRESS_FIELD_DATA_KEYS[field]` — the
    same table `_cleared_by_skip` walks — and returns the next field name,
    or `None` when `field` is the chain's last step (the caller saves there
    instead of prompting again).

    Separate from `_SKIP_TARGETS`, which is where a SKIP on `field` lands:
    skipping the building number means there is no building, so a Skip
    jumps apartment and floor along with it. Answering it does not — this
    function always names the very next step, never a jump.

    Raises a NAMED `ValueError` (not a bare `KeyError`) for a field outside
    `_ADDRESS_FIELD_DATA_KEYS` — chiefly `title`, the one typed address step
    excluded on purpose (see `_NEXT_FIELD`'s comment). Every profile.py text
    handler wraps its body in `except Exception: return
    ConversationHandler.END`, so a bare `KeyError` would silence-kill the
    customer's conversation with no message and no error the moment a
    future caller (a Task 4 driver reading `draft['step']`, say) reaches
    this function for a step it does not cover. A clear, named exception at
    least surfaces in logs instead of vanishing into that catch-all.

    Stays pure on purpose: no I/O, no collaborators (`api_client`,
    `get_auth_token`, `main_menu_for`, ...). `profile.py`'s text handlers
    patch those by name on `handlers.profile` in tests; a call to one of
    them moved in here would make that patch silently stop applying.
    """
    if field not in _ADDRESS_FIELD_DATA_KEYS:
        raise ValueError(
            f"handle_input({field!r}, ...) is not a chain-advance step. "
            f"{field!r} is not in _ADDRESS_FIELD_DATA_KEYS — 'title' is the "
            "one typed address step deliberately excluded, because its next "
            "step is branch-dependent (_is_shared_pin_address decides "
            "'apartment' vs a save, not a fixed field) and it has no Skip "
            "button. Route it through its own handler instead of "
            "handle_input."
        )
    data[_ADDRESS_FIELD_DATA_KEYS[field]] = value
    return _NEXT_FIELD[field]


async def render_prompt(
    field: str, language: str, target: RenderTarget, data: dict | None = None
) -> int:
    """Render one step's prompt through `target` and return its PTB state.

    Reads `_FLOW_STEPS`, the SSOT covering all ten steps a draft can park on
    — not `_ADDRESS_STEPS`, which only covers the four Skip-able ones.

    The target is a parameter rather than being derived from an Update: the
    forward path edits a callback message, the resume path sends a fresh one,
    and both must render identical copy or resume drifts silently.

    `data` carries whatever a step's `render` needs beyond text and a
    keyboard — an interpolated district name, a geocoded address, or the
    coordinates of a pin to send. It defaults to None so every existing
    two-argument caller keeps working unchanged. The table's OWN
    `prompt_key` is always passed into `render` (never a copy hardcoded
    inside the renderer), so editing a step's key in `_FLOW_STEPS` cannot
    become a silent no-op for the four steps that render irregularly.
    """
    step = _FLOW_STEPS[field]
    if step.render is not None:
        await step.render(language, target, data or {}, step.prompt_key)
    else:
        await target.send_text(i18n.get(step.prompt_key, language), step.keyboard(language))
    return step.state
