"""The staff main menu is a REPLY keyboard, so every tap is just TEXT.

That single fact is why this file exists. A driver taps "💰 Naqd pul" and
Telegram delivers an ordinary text message; nothing about it says "button".
The bot recognises it only because ``_resolve_tapped_label`` looks the text up
in a label→action dict built, WHEN THE TAP ARRIVES, from the same translation
rows the KEYBOARD renders from — and because ``MenuTapFilter`` asks that one
function for both of its jobs: the escape hatch guarding every conversation
state, and the three operator labels that ENTER a conversation instead of
routing. No menu label is resolved at handler-build time any more, so retitling
a button in the admin UI cannot leave a live keyboard that nothing answers.

The whole menu is therefore one seed script away from silence: change the
shape of a value — a stray space, a different emoji, a word order that no
longer survives the prefix strip — and the button renders perfectly and does
nothing. Nobody notices, because the bot's reply to an unrecognised tap is to
re-render the menu, which looks exactly like a menu.

Every test here therefore closes the loop the same way: log a real staff member
in through ``/start``, read the labels **off the reply keyboard the bot
actually sent**, and feed those exact strings back through
``Application.process_update``. If the rendered label and the router's idea of
that label ever drift apart, the loop breaks and these tests go red.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from telegram.ext import ConversationHandler

from staff_bot.handlers.operator.create_user import ENTER_FIRST_NAME

from tests.staff_bot.ptb_harness import (
    DEFAULT_DRIVER_TELEGRAM_ID,
    FakeStaffDatabase,
    build_staff_harness,
    staff_backend_failure,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


# ---------------------------------------------------------------------------
# Real copy, not hand-pasted copy
# ---------------------------------------------------------------------------
# The labels are loaded live from the seeding script, via the same
# `_curated_value` that `seed_translations()` itself calls (the technique
# tests/staff_bot/test_conversation_menu_escape.py established). Pasting the
# strings by hand would let a future edit to the seed leave this file asserting
# copy production no longer ships — i.e. it would test the test.

_SEED_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "seed_staff_translations.py"


def _load_seed_module():
    spec = importlib.util.spec_from_file_location("seed_staff_translations", _SEED_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SEED = _load_seed_module()

LANGUAGES = ("en", "uz", "ru")

# Every key `MenuKeyboards.main_menu` renders, for any role combination.
MENU_KEYS = (
    "staff.menu.new_orders",
    "staff.menu.active_deliveries",
    "staff.menu.tryouts",
    "staff.menu.cash",
    "staff.menu.create_client",
    "staff.menu.search_client",
    "staff.menu.create_order",
    "staff.menu.profile",
    "staff.menu.settings",
    "staff.menu.help",
)

# Copy the driver reads on screen; asserted verbatim where it matters.
MESSAGE_KEYS = (
    "staff.session_expired",
    "staff.menu.title",
    "staff.unauthorized",
)

CATCH_ALL = "StaffBot._handle_text_message"

# Which handler must CLAIM the tap. The three operator labels are entry points
# of their own ConversationHandlers and are deliberately absent from
# `_menu_action_map`; everything else falls through to the catch-all text
# router. Getting this wrong in either direction is a dead button: a label the
# router doesn't know, or a conversation that swallows a navigation tap.
EXPECTED_CLAIMANT = {
    "staff.menu.new_orders": CATCH_ALL,
    "staff.menu.active_deliveries": CATCH_ALL,
    "staff.menu.tryouts": CATCH_ALL,
    "staff.menu.cash": CATCH_ALL,
    "staff.menu.profile": CATCH_ALL,
    "staff.menu.settings": CATCH_ALL,
    "staff.menu.help": CATCH_ALL,
    "staff.menu.create_client": "conversation:staff_create_user",
    "staff.menu.search_client": "conversation:staff_search_user",
    "staff.menu.create_order": "conversation:staff_create_order",
}

# The action the catch-all router must resolve the label to. This is the
# contract stated from outside the code, not a copy of production's map: it is
# what a driver expects the button to DO.
EXPECTED_ACTION = {
    "staff.menu.new_orders": "staff_new_orders_unified",
    "staff.menu.active_deliveries": "staff_active_deliveries",
    "staff.menu.tryouts": "staff_tryouts_hub",
    "staff.menu.cash": "staff_cash_hub",
    "staff.menu.profile": "staff_profile",
    "staff.menu.settings": "staff_settings",
    "staff.menu.help": "staff_help",
}

DRIVER_KEYS = {
    "staff.menu.new_orders",
    "staff.menu.active_deliveries",
    "staff.menu.tryouts",
    "staff.menu.cash",
    "staff.menu.profile",
    "staff.menu.settings",
    "staff.menu.help",
}
OPERATOR_KEYS = {
    "staff.menu.new_orders",
    "staff.menu.create_client",
    "staff.menu.search_client",
    "staff.menu.create_order",
    "staff.menu.profile",
    "staff.menu.settings",
    "staff.menu.help",
}


def _curated(key: str, language: str) -> str:
    value = _SEED._curated_value(key, language)
    assert value, (
        f"{key} has no curated {language} value in scripts/seed_staff_translations.py — "
        "production would render a humanised placeholder for it"
    )
    return value


def _translation_table(overrides: dict = None) -> dict:
    """The staff translations these tests run against.

    The dict handed to ``build_staff_harness`` IS the live table ``i18n.get``
    reads, for the keyboard and the router alike — the same coupling production
    has between the seeded rows and the router. A test that reseeds it AFTER
    startup is exercising an admin retitling a button on a running bot.
    """
    table = {}
    for key in MENU_KEYS + MESSAGE_KEYS:
        for language in LANGUAGES:
            table[(language, key)] = _curated(key, language)
    table.update(overrides or {})
    return table


# ---------------------------------------------------------------------------
# A logged-in staff member
# ---------------------------------------------------------------------------


def _staff_row(roles, language):
    return {
        "id": 55,
        "telegram_id": str(DEFAULT_DRIVER_TELEGRAM_ID),
        "first_name": "Aziz",
        "last_name": "Karimov",
        "phone": "+998901112233",
        "preferred_language": language,
        "role": "delivery",
        "status": "active",
        "staff_roles": json.dumps(roles),
        "staff_bot_state": "{}",
    }


def _login_payload(roles, language):
    return {
        "access_token": "staff-access-token",
        "refresh_token": "staff-refresh-token",
        "expires_in": 3600,
        "user": {
            "id": 55,
            "first_name": "Aziz",
            "last_name": "Karimov",
            "phone": "+998901112233",
            "preferred_language": language,
            "staff_roles": roles,
            "delivery_person_id": 7,
        },
    }


LOGIN_ENDPOINT = "/api/v1/staff/auth/login"


async def build_staff(monkeypatch, *, roles, language="en", translations=None, login=None):
    """A harness whose backend will hand back ``roles`` at login."""
    harness = await build_staff_harness(
        monkeypatch,
        translations=translations if translations is not None else _translation_table(),
        database=FakeStaffDatabase(staff_user=_staff_row(roles, language)),
    )
    harness.backend.route(
        "POST",
        LOGIN_ENDPOINT,
        login if login is not None else (lambda _call: _login_payload(roles, language)),
    )
    return harness


async def sign_in(harness):
    """Run the real ``/start`` login and return (updates, rendered menu labels)."""
    staff_member = harness.updates()
    await harness.send(staff_member.command("start"))

    shown = harness.telegram.shown
    assert shown, "/start produced no message at all — the staff member sees a dead bot"
    labels = shown[-1].button_labels()
    assert labels, "login did not attach the reply-keyboard main menu"
    harness.telegram.reset()
    return staff_member, labels


def _user_data(harness):
    return harness.application.user_data[DEFAULT_DRIVER_TELEGRAM_ID]


def _staff_bot(harness):
    """The live ``StaffBot`` whose regexes were compiled by this harness."""
    for group in sorted(harness.application.handlers):
        for handler in harness.application.handlers[group]:
            callback = getattr(handler, "callback", None)
            if getattr(callback, "__qualname__", "") == CATCH_ALL:
                return callback.__self__
    raise AssertionError("the catch-all text router is not registered at all")


def _claimed_by(harness, update):
    """Name of the handler PTB would dispatch this update to, or None."""
    matched = harness.handlers_matching(update)
    if not matched:
        return None
    _group, handler = matched[0]
    if isinstance(handler, ConversationHandler):
        return f"conversation:{handler.name}"
    return getattr(handler.callback, "__qualname__", repr(handler.callback))


def capture_errors(harness) -> list:
    """Collect every exception PTB would have apologised for.

    Without a registered error handler PTB logs the traceback and moves on, so
    a handler that raises looks identical to one that quietly did nothing. This
    turns that into a visible list.
    """
    errors = []

    async def _record(_update, context):
        errors.append(context.error)

    harness.application.add_error_handler(_record)
    return errors


def _labels_by_key(labels, language, keys):
    """Map each menu key to the ONE rendered label that carries its translation.

    Matching on the translated value rather than rebuilding ``f"{emoji} {value}"``
    keeps the emoji an implementation detail of the keyboard: this file asserts
    that the label the driver sees routes, whatever decoration it wears.
    """
    resolved = {}
    for key in keys:
        value = _curated(key, language)
        hits = [label for label in labels if label.strip().endswith(value)]
        assert len(hits) == 1, (
            f"expected exactly one {language} menu button carrying {value!r} "
            f"(key {key}), found {hits} in {labels}"
        )
        resolved[key] = hits[0]
    unmatched = set(labels) - set(resolved.values())
    assert not unmatched, (
        f"the menu renders buttons this test has no route expectation for: {sorted(unmatched)}. "
        "A new main-menu button needs a row in EXPECTED_CLAIMANT/EXPECTED_ACTION, "
        "otherwise nobody is checking that it is wired to anything."
    )
    return resolved


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def driver_bot(monkeypatch):
    return await build_staff(monkeypatch, roles=["delivery_driver"], language="en")


@pytest.fixture
async def operator_bot(monkeypatch):
    return await build_staff(monkeypatch, roles=["operator"], language="en")


# ---------------------------------------------------------------------------
# Every rendered label, every language
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
async def test_every_button_on_the_drivers_menu_routes_in_every_language(monkeypatch, language):
    """A driver's whole control surface, tapped one button at a time, in the
    language they actually run the bot in.

    If this fails, a driver taps a button on a keyboard the bot itself drew and
    gets the menu back instead of the screen they asked for — the exact,
    invisible failure a reshaped translation row produces. Uzbek and Russian
    are not decoration here: the labels are multi-word in both, and the
    router's emoji-prefix strip works on CHARACTER COUNTS, so a language whose
    label survives the strip in English can still die in Cyrillic.
    """
    harness = await build_staff(monkeypatch, roles=["delivery_driver"], language=language)
    staff_member, labels = await sign_in(harness)
    bot = _staff_bot(harness)

    by_key = _labels_by_key(labels, language, DRIVER_KEYS)

    for key, label in sorted(by_key.items()):
        update = staff_member.text(label)
        assert _claimed_by(harness, update) == EXPECTED_CLAIMANT[key], (
            f"{language} label {label!r} ({key}) is claimed by the wrong handler"
        )
        assert bot._match_menu_action(label, language) == EXPECTED_ACTION[key], (
            f"{language} label {label!r} ({key}) renders on the keyboard but the text "
            "router does not recognise it — the button is dead"
        )


@pytest.mark.parametrize("language", LANGUAGES)
async def test_every_button_on_the_operator_menu_routes_in_every_language(monkeypatch, language):
    """The operator's menu is a different keyboard with three buttons that are
    ConversationHandler ENTRY POINTS rather than router actions.

    Those three are the fragile ones: each has its own regex compiled from its
    own translation key, and they are deliberately missing from the router's
    action map. A shape change there doesn't fall back to the menu — the tap
    reaches the catch-all, matches nothing, and the operator is bounced to the
    main menu forever, unable to create a client.
    """
    harness = await build_staff(monkeypatch, roles=["operator"], language=language)
    operator, labels = await sign_in(harness)

    by_key = _labels_by_key(labels, language, OPERATOR_KEYS)

    for key, label in sorted(by_key.items()):
        assert _claimed_by(harness, operator.text(label)) == EXPECTED_CLAIMANT[key], (
            f"{language} operator label {label!r} ({key}) is claimed by the wrong handler"
        )


async def test_the_two_parcel_buttons_on_a_dual_role_menu_do_not_shadow_each_other(monkeypatch):
    """A driver-and-operator sees "📦 New Orders" AND "📦 Create Order".

    Same emoji, two regexes, one keyboard. If the Create Order pattern ever
    widened enough to swallow New Orders (or the router's prefix strip
    collapsed them), one of the two buttons would silently open the other
    person's screen — and an operator who meant to look at the pool would find
    themselves halfway through creating an order for a client they never chose.
    """
    harness = await build_staff(monkeypatch, roles=["delivery_driver", "operator"], language="en")
    staff_member, labels = await sign_in(harness)

    by_key = _labels_by_key(labels, "en", DRIVER_KEYS | OPERATOR_KEYS)
    parcel_labels = [label for label in labels if label.startswith("📦")]
    assert len(parcel_labels) == 2, f"expected both 📦 buttons on a dual-role menu, got {parcel_labels}"

    assert _claimed_by(harness, staff_member.text(by_key["staff.menu.new_orders"])) == CATCH_ALL
    assert (
        _claimed_by(harness, staff_member.text(by_key["staff.menu.create_order"]))
        == "conversation:staff_create_order"
    )


async def test_a_driver_only_menu_never_offers_operator_buttons(driver_bot):
    """Role separation as the driver experiences it: the buttons are simply not
    there.

    The role guards behind those handlers would refuse a driver anyway, but a
    refusal is a dead end that reads as a broken bot. Keeping the operator
    buttons off the keyboard is what makes the guard a backstop rather than the
    user experience.
    """
    _staff_member, labels = await sign_in(driver_bot)

    for key in ("staff.menu.create_client", "staff.menu.search_client", "staff.menu.create_order"):
        value = _curated(key, "en")
        assert not [label for label in labels if label.strip().endswith(value)], (
            f"{value!r} must not appear on a delivery-driver-only menu"
        )


# ---------------------------------------------------------------------------
# What the tap actually does
# ---------------------------------------------------------------------------


async def test_a_driver_taps_each_menu_button_and_the_bot_answers_every_time(driver_bot):
    """Walk the driver's menu end to end through the dispatcher.

    Claiming the update is not the same as answering it: a handler that raises
    is swallowed by PTB's error plumbing and the driver sees nothing at all
    while the update log looks fine. This is the test that notices.

    It also pins the echo cleanup — the reply keyboard sends TEXT, so every tap
    leaves the driver's own message in the chat, and those pile up on top of
    the pinned route card until it is scrolled out of reach.
    """
    staff_member, labels = await sign_in(driver_bot)
    by_key = _labels_by_key(labels, "en", DRIVER_KEYS)
    errors = capture_errors(driver_bot)

    for key, label in sorted(by_key.items()):
        driver_bot.telegram.reset()
        await driver_bot.send(staff_member.text(label))

        assert not errors, f"tapping {label!r} ({key}) raised {errors}"
        assert driver_bot.telegram.shown, f"tapping {label!r} ({key}) showed the driver nothing"
        assert driver_bot.telegram.of("deleteMessage"), (
            f"the driver's own {label!r} tap was left in the chat; menu echoes bury the route card"
        )


async def test_a_telegram_refusal_to_delete_the_echo_does_not_undo_the_navigation(driver_bot):
    """Telegram refuses ``deleteMessage`` routinely — older than 48 hours,
    already gone, rights revoked in that chat.

    The echo cleanup is cosmetic; the navigation is not. If the refusal escaped
    it would reach PTB's error handler AFTER the screen the driver asked for
    had already been sent, and the bot would apologise for a tap that worked.
    """
    staff_member, labels = await sign_in(driver_bot)
    cash = _labels_by_key(labels, "en", DRIVER_KEYS)["staff.menu.cash"]
    errors = capture_errors(driver_bot)

    driver_bot.telegram.fail("deleteMessage", "Bad Request: message can't be deleted")
    await driver_bot.send(staff_member.text(cash))

    assert driver_bot.telegram.of("deleteMessage"), "the echo delete was never attempted"
    assert driver_bot.telegram.shown, "the cash hub never reached the driver"
    assert errors == [], f"a cosmetic delete failure surfaced as a handler error: {errors}"

    # And the next tap still works — a swallowed failure must not wedge the router.
    driver_bot.telegram.reset()
    await driver_bot.send(staff_member.text(cash))
    assert driver_bot.telegram.shown


async def test_an_operator_abandons_create_client_by_tapping_another_menu_button(operator_bot):
    """The give-up path, which is the one that broke in production.

    Create Client parks the operator in a text state whose own MessageHandler
    would otherwise eat the next thing they type — including a menu tap. So:
    start the flow, get asked for a phone, change your mind, tap Profile. The
    conversation must END (not linger, waiting to swallow the next phone number
    they type at some later flow) and Profile must actually open.
    """
    operator, labels = await sign_in(operator_bot)
    by_key = _labels_by_key(labels, "en", OPERATOR_KEYS)

    await operator_bot.send(operator.text(by_key["staff.menu.create_client"]))
    assert operator_bot.conversation_state("staff_create_user") is not None, (
        "tapping Create Client did not open the create-user conversation"
    )

    operator_bot.telegram.reset()
    await operator_bot.send(operator.text(by_key["staff.menu.profile"]))

    assert operator_bot.conversation_state("staff_create_user") is None, (
        "the abandoned create-client conversation is still armed and will capture "
        "the operator's next unrelated message"
    )
    assert operator_bot.telegram.shown, "abandoning the flow left the operator with no reply"
    assert any("Aziz" in call.text for call in operator_bot.telegram.shown), (
        "tapping Profile must actually show the profile, not just kill the flow"
    )


async def test_the_operator_can_reenter_create_client_after_walking_away_from_it(operator_bot):
    """Second tap on the same button, after an escape. `allow_reentry` is what
    makes this work; without it the entry point is inert for the rest of the
    session and the operator has to restart the bot to create a client."""
    operator, labels = await sign_in(operator_bot)
    by_key = _labels_by_key(labels, "en", OPERATOR_KEYS)

    await operator_bot.send(operator.text(by_key["staff.menu.create_client"]))
    await operator_bot.send(operator.text(by_key["staff.menu.help"]))
    assert operator_bot.conversation_state("staff_create_user") is None

    await operator_bot.send(operator.text(by_key["staff.menu.create_client"]))
    assert operator_bot.conversation_state("staff_create_user") is not None, (
        "Create Client is dead after the operator used it once and backed out"
    )


# ---------------------------------------------------------------------------
# The shapes a tap can arrive in
# ---------------------------------------------------------------------------


async def test_a_menu_label_still_routes_without_its_emoji_and_with_stray_whitespace(driver_bot):
    """Not every tap arrives byte-identical to the button.

    Staff retype labels, paste them, and keep keyboards from older releases
    whose emoji differed. The router is built to tolerate that — one optional
    prefix token and surrounding whitespace — and this pins the tolerance so a
    future tightening of the regex is caught before a driver's keyboard goes
    quiet.
    """
    _staff_member, labels = await sign_in(driver_bot)
    bot = _staff_bot(driver_bot)

    cash_label = _labels_by_key(labels, "en", DRIVER_KEYS)["staff.menu.cash"]
    bare = _curated("staff.menu.cash", "en")

    for variant in (cash_label, bare, f"  {cash_label}  ", f"{cash_label}\n", f" {bare} "):
        assert bot._match_menu_action(variant, "en") == "staff_cash_hub", (
            f"{variant!r} is the Cash button and must open the cash hub"
        )


async def test_text_that_merely_contains_a_menu_word_is_not_treated_as_a_tap(driver_bot):
    """The router must not be so tolerant that a typed word hijacks the flow.

    Drivers type notes and amounts into these same text updates. If "Cashier
    paid" opened the cash hub, the note would be lost and the transaction the
    driver was completing would be abandoned mid-way.
    """
    await sign_in(driver_bot)
    bot = _staff_bot(driver_bot)

    for typed in ("Cashier", "Cash paid at the door", "54000", "+998901112233", "Profile photo sent"):
        assert bot._match_menu_action(typed, "en") is None, (
            f"typed text {typed!r} was mistaken for a menu button"
        )


async def test_a_stale_keyboard_in_the_previous_language_still_navigates(monkeypatch):
    """A staff member switches to Russian; their PHONE still shows the Uzbek
    keyboard until Telegram redraws it.

    Every tap in that window is an Uzbek label arriving at a Russian session.
    The router resolves labels across all languages precisely so that window is
    survivable — when it did not, the tap ended the driver's conversation with
    zero output and they assumed the bot had crashed.
    """
    harness = await build_staff(monkeypatch, roles=["delivery_driver"], language="ru")
    _staff_member, labels = await sign_in(harness)
    bot = _staff_bot(harness)

    russian = _labels_by_key(labels, "ru", DRIVER_KEYS)["staff.menu.cash"]
    uzbek_from_the_old_keyboard = f"💰 {_curated('staff.menu.cash', 'uz')}"
    assert russian != uzbek_from_the_old_keyboard, "fixture failed to produce distinct languages"

    assert bot._match_menu_action(uzbek_from_the_old_keyboard, "ru") == "staff_cash_hub"


async def test_unknown_text_lands_on_the_main_menu_and_is_left_in_the_chat(driver_bot):
    """Something that is not a button at all — a driver typing a question.

    Two things must hold. It reaches the catch-all (nothing else may claim it),
    and the bot answers with the menu so the driver is not stranded. And the
    message must NOT be deleted: the echo cleanup exists to remove taps the bot
    consumed, and silently deleting what a person typed reads as the bot eating
    their message.
    """
    staff_member, labels = await sign_in(driver_bot)

    update = staff_member.text("kogda budet zakaz?")
    assert _claimed_by(driver_bot, update) == CATCH_ALL

    await driver_bot.send(update)

    shown = driver_bot.telegram.shown
    assert shown, "unknown text left the driver with no reply at all"
    assert shown[-1].button_labels() == labels, (
        "the fallback reply must re-render the main menu keyboard the driver already had"
    )
    assert not driver_bot.telegram.of("deleteMessage"), (
        "the driver's own typed message was deleted; only consumed menu taps may be cleaned up"
    )


async def test_tapping_the_same_button_twice_answers_twice(driver_bot):
    """Double taps are normal on a phone in a moving van.

    A reply-keyboard tap can never be answered with a Telegram toast, so the
    ONLY feedback is a new message. If the second tap produces nothing the
    driver concludes the bot is frozen and taps harder.
    """
    staff_member, labels = await sign_in(driver_bot)
    cash = _labels_by_key(labels, "en", DRIVER_KEYS)["staff.menu.cash"]

    await driver_bot.send(staff_member.text(cash))
    first = len(driver_bot.telegram.shown)
    assert first, "the first Cash tap showed nothing"

    await driver_bot.send(staff_member.text(cash))
    assert len(driver_bot.telegram.shown) > first, (
        "the second Cash tap produced no message; a reply-keyboard tap has no other "
        "feedback channel, so the driver sees a frozen bot"
    )


# ---------------------------------------------------------------------------
# Taps that arrive with no session behind them
# ---------------------------------------------------------------------------


async def test_a_menu_tap_after_a_restart_recovers_the_session_and_still_navigates(monkeypatch):
    """The reply keyboard lives on the driver's phone; the session lives in
    process memory. Every deploy separates them.

    So the first tap after a restart arrives authenticated-by-nobody. It must
    silently re-establish the session and do what the driver asked, because the
    alternative — the router bailing out — made every button on the keyboard
    dead until the driver happened to guess ``/start``.
    """
    harness = await build_staff(monkeypatch, roles=["delivery_driver"], language="en")
    staff_member = harness.updates()
    label = f"💰 {_curated('staff.menu.cash', 'en')}"

    assert not _user_data(harness).get("authenticated"), "fixture is not a cold process"

    await harness.send(staff_member.text(label))

    logins = [call for call in harness.backend.calls if call.endpoint == LOGIN_ENDPOINT]
    assert logins, "a menu tap from a cold process never tried to re-establish the session"
    assert logins[0].data == {"telegram_id": str(DEFAULT_DRIVER_TELEGRAM_ID)}

    assert _user_data(harness).get("authenticated") is True
    assert _user_data(harness).get("staff_roles") == ["delivery_driver"]
    assert harness.telegram.shown, "the recovered tap navigated nowhere"


async def test_unknown_text_from_a_logged_out_person_never_reaches_the_auth_endpoint(monkeypatch):
    """Recovery costs a signed POST plus database work, and the staff bot has
    no rate limiter in front of it.

    So recovery is scoped to text that IS a menu label. Anyone who never ran
    ``/start`` typing at the bot must not be able to drive backend load. If
    this fails, a stranger with a keyboard is an unauthenticated load generator
    pointed at the production auth endpoint.
    """
    harness = await build_staff(monkeypatch, roles=["delivery_driver"], language="en")
    stranger = harness.updates()

    for _ in range(5):
        await harness.send(stranger.text("hello? is anyone there?"))

    assert harness.backend.calls == [], (
        f"unauthenticated free text reached the backend: {harness.backend.calls}"
    )
    assert not harness.telegram.shown, "the bot answered an unauthenticated stranger"


async def test_a_failed_recovery_explains_itself_once_no_matter_how_often_it_is_tapped(monkeypatch):
    """Backend down, driver taps the dead keyboard five times.

    They must get exactly one explanation. The failure window arms a cooldown,
    and repeating the message on every tap would both spam the chat and bury
    the pinned route card under the driver's own undeleted taps.
    """
    harness = await build_staff(
        monkeypatch,
        roles=["delivery_driver"],
        language="en",
        login=lambda _call: staff_backend_failure("boom", status_code=500),
    )
    staff_member = harness.updates()
    label = f"💰 {_curated('staff.menu.cash', 'en')}"

    for _ in range(5):
        await harness.send(staff_member.text(label))

    expected = _curated("staff.session_expired", "en")
    replies = [call.text for call in harness.telegram.shown]
    assert replies.count(expected) == 1, (
        f"expected exactly one {expected!r} for five taps in a failed-session window, got {replies}"
    )


async def test_a_staff_member_whose_driver_role_was_revoked_is_refused_not_ignored(monkeypatch):
    """Roles change; the keyboard on the phone does not.

    An operator who used to drive still has "💰 Cash" on screen. The tap must
    produce a refusal they can read — silence here is indistinguishable from a
    broken bot, and they will keep tapping.
    """
    harness = await build_staff(monkeypatch, roles=["operator"], language="en")
    operator, _labels = await sign_in(harness)

    stale_cash_button = f"💰 {_curated('staff.menu.cash', 'en')}"
    await harness.send(operator.text(stale_cash_button))

    shown = harness.telegram.shown
    assert shown, "a stale role-gated button did nothing at all"
    assert _curated("staff.unauthorized", "en") in [call.text for call in shown], (
        f"expected the unauthorized refusal, got {[call.text for call in shown]}"
    )


# ---------------------------------------------------------------------------
# One decider: the escape filter and the action matcher are the same question
# ---------------------------------------------------------------------------
# Was three ratchets. All three were the same split rule — a loose predicate
# saying "menu tap" while a strict one said "which button?" — and they are kept
# here, inverted, as the regression tests for it.


async def test_a_two_word_value_typed_into_a_flow_stays_in_the_flow(operator_bot):
    """A typed value that merely ENDS with a menu label is not a tap.

    ``_match_menu_action`` used to retry the text with its first 2, 3 and 4
    CHARACTERS chopped off, to survive emoji prefixes of varying width, with
    nothing checking that what it chopped off was an emoji. So ``"Aziz
    Profil"`` — a four-character word, a space, and the Uzbek label for
    Profile — was read as a Profile tap: typed into the create-client flow the
    operator's input was discarded, the conversation torn down, and the profile
    card shown instead.

    The prefix the router strips is an EMOJI now, so the name reaches the
    handler that asked for it.
    """
    operator, labels = await sign_in(operator_bot)
    by_key = _labels_by_key(labels, "en", OPERATOR_KEYS)
    bot = _staff_bot(operator_bot)

    await operator_bot.send(operator.text(by_key["staff.menu.create_client"]))
    await operator_bot.send(operator.text("+998901112233"))
    assert operator_bot.conversation_state("staff_create_user") == ENTER_FIRST_NAME, (
        "fixture: the operator should be parked on the first-name prompt"
    )

    hijacking_name = f"Aziz {_curated('staff.menu.profile', 'uz')}"
    assert bot._match_menu_action(hijacking_name, "en") is None, (
        "a typed name ending in a menu label is not a menu tap"
    )

    operator_bot.telegram.reset()
    await operator_bot.send(operator.text(hijacking_name))

    assert operator_bot.conversation_state("staff_create_user") is not None, (
        "typing a name navigated the operator out of the client they were creating"
    )
    assert not any("Aziz Karimov" in call.text for call in operator_bot.telegram.shown), (
        "the profile card was shown instead of the next prompt"
    )


async def test_the_menu_escape_filter_and_the_action_matcher_are_the_same_predicate(operator_bot):
    """One rule ("is this text a main-menu tap?"), one implementation.

    The escape hatch guarding every conversation state used to be a REGEX
    (``_main_menu_text_pattern``) that allowed ANY single leading token, while
    the decider that says WHICH button it was (``_match_menu_action``) only
    stripped 2-4 characters. For a leading word of five characters or more the
    filter said "menu tap" and the matcher said "nothing":
    ``_conv_menu_escape`` ended the conversation, dispatched nothing, and did
    not even delete the echo — the operator got absolutely no output. An order
    note reading "Оплата Наличные" or "To'lov Naqd pul" is exactly this shape.

    The filter now IS the matcher (``MenuTapFilter`` calls it), so the two
    cannot disagree: text the matcher cannot resolve is never claimed, and
    falls through to the state's own ``receive_*`` handler.
    """
    operator, labels = await sign_in(operator_bot)
    by_key = _labels_by_key(labels, "en", OPERATOR_KEYS)
    bot = _staff_bot(operator_bot)

    typed = f"Sardor {_curated('staff.menu.profile', 'uz')}"
    tap_filter = bot._main_menu_tap_filter()
    probe = operator.text(typed)
    assert not tap_filter.check_update(probe), (
        "the escape filter still claims text the matcher cannot resolve"
    )
    assert bot._match_menu_action(typed, "en") is None

    await operator_bot.send(operator.text(by_key["staff.menu.create_client"]))
    await operator_bot.send(operator.text("+998901112233"))
    assert operator_bot.conversation_state("staff_create_user") == ENTER_FIRST_NAME

    operator_bot.telegram.reset()
    await operator_bot.send(operator.text(typed))

    assert operator_bot.conversation_state("staff_create_user") is not None, (
        "a two-word note still kills the conversation"
    )
    assert operator_bot.telegram.shown, (
        "the operator got no output at all — the exact silence this fix exists to end"
    )


async def test_a_translation_with_a_trailing_space_still_routes_its_button(monkeypatch):
    """A stray space in a translations row must not make a live button dead.

    The tap has always been STRIPPED before it was compared; ``_menu_action_map``
    used the RAW translation row as its dict key. So a row seeded as ``"Cash "``
    was recognised as navigation and resolved to no action. Outside a
    conversation the driver was bounced to the main menu, which looks like the
    button did nothing; inside one, the same row silently ended their flow. A
    single trailing space, invisible in the admin UI, was enough.

    Both sides strip now.
    """
    harness = await build_staff(
        monkeypatch,
        roles=["delivery_driver"],
        language="en",
        translations=_translation_table({("en", "staff.menu.cash"): "Cash "}),
    )
    staff_member, labels = await sign_in(harness)
    bot = _staff_bot(harness)

    rendered = [label for label in labels if "Cash" in label]
    assert rendered, f"the Cash button should still render, got {labels}"

    assert bot._match_menu_action(rendered[0], "en") == "staff_cash_hub", (
        "the rendered button resolves to nothing — it is dead on the driver's phone"
    )

    harness.telegram.reset()
    await harness.send(staff_member.text(rendered[0]))
    assert harness.telegram.shown, "the tap produced nothing at all"
    assert harness.telegram.shown[-1].callback_data(), (
        "the tap was answered with the main menu again instead of the cash hub"
    )


# ---------------------------------------------------------------------------
# Copy edited while the bot is running
# ---------------------------------------------------------------------------


async def test_a_menu_label_retitled_after_startup_still_routes(monkeypatch):
    """An admin retitles a button in the admin UI. Nobody restarts the bot.

    The KEYBOARD reads its labels at render time, so the new copy is on the
    staff member's phone the moment the menu is drawn again. Anything that
    resolved labels at HANDLER-BUILD time keeps hunting for the old string, and
    the button they can SEE is dead — no error, no log line, nothing on screen
    to suggest a restart is what is missing. (The customer bot carries its own
    ratchet for the same hazard:
    ``tests/telegram_bot/test_language_and_i18n_journeys.py::
    test_copy_reseeded_after_startup_renders_but_no_longer_matches``.)

    Every kind of menu button is tapped here, because they used to be resolved
    in two different places: the three operator labels that ENTER a
    ConversationHandler of their own, and the labels that route through the
    catch-all text router.
    """
    table = _translation_table()
    harness = await build_staff(
        monkeypatch,
        roles=["delivery_driver", "operator"],
        language="en",
        translations=table,
    )

    # The reseed. `table` IS the live lookup `i18n.get` reads, which is what
    # `i18n.reload_translations()` swaps in production.
    retitled = {
        "staff.menu.create_client": "Register a household",
        "staff.menu.search_client": "Find a household",
        "staff.menu.create_order": "Place an order",
        "staff.menu.cash": "Money owed",
        "staff.menu.profile": "My account",
    }
    for key, value in retitled.items():
        table[("en", key)] = value

    staff_member, labels = await sign_in(harness)
    bot = _staff_bot(harness)

    rendered = {}
    for key, value in retitled.items():
        hits = [label for label in labels if label.strip().endswith(value)]
        assert len(hits) == 1, (
            f"expected exactly one button carrying the reseeded copy {value!r} "
            f"({key}), found {hits} in {labels} — the keyboard picks new copy up "
            "immediately, which is what makes the mismatch invisible"
        )
        rendered[key] = hits[0]

    for key, label in rendered.items():
        assert _claimed_by(harness, staff_member.text(label)) == EXPECTED_CLAIMANT[key], (
            f"the reseeded {key} button renders as {label!r} and lands nowhere it "
            "should — it is dead until someone restarts the staff bot"
        )
        if key in EXPECTED_ACTION:
            assert bot._match_menu_action(label, "en") == EXPECTED_ACTION[key]

    # …and the loop closed through the real dispatcher: the retitled operator
    # button must actually OPEN the client it names.
    harness.telegram.reset()
    await harness.send(staff_member.text(rendered["staff.menu.create_client"]))
    assert harness.conversation_state("staff_create_user") is not None, (
        "tapping the retitled Create Client button did not start the flow"
    )
    assert harness.telegram.shown, "the tap produced nothing at all"


async def test_a_label_that_no_longer_exists_stops_routing(monkeypatch):
    """The other half of a live reseed: the OLD copy must go dead with it.

    A matcher that merely *added* the new label would keep the pre-reseed
    string working forever, and the union of every label a translations row has
    ever held is a growing set of strings that quietly hijack typed input —
    an order note or a client's name has to collide with only one of them.
    """
    table = _translation_table()
    harness = await build_staff(
        monkeypatch, roles=["delivery_driver", "operator"], language="en", translations=table
    )
    staff_member, _labels = await sign_in(harness)
    bot = _staff_bot(harness)

    stale_cash = f"💰 {_curated('staff.menu.cash', 'en')}"
    stale_create = f"👤 {_curated('staff.menu.create_client', 'en')}"
    assert bot._match_menu_action(stale_cash, "en") == "staff_cash_hub", "fixture"
    assert _claimed_by(harness, staff_member.text(stale_create)) == (
        "conversation:staff_create_user"
    ), "fixture"

    for language in LANGUAGES:
        table[(language, "staff.menu.cash")] = "Money owed"
        table[(language, "staff.menu.create_client")] = "Register a household"

    assert bot._match_menu_action(stale_cash, "en") is None, (
        "the retired label still navigates — every label the row has ever held "
        "stays live and can hijack typed text"
    )
    assert _claimed_by(harness, staff_member.text(stale_create)) == CATCH_ALL, (
        "the retired operator label still opens a conversation nobody asked for"
    )
