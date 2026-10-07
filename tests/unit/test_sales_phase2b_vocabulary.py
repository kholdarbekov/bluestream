"""Phase 2b's shared vocabulary: two staff actions, six named events, one seeded line.

Four facts in three files no other 2b task owns, each failing differently
when it is missed:

* `STAFF_ACTIONS['AGENT_TRYOUT_CREATED']` and `['VISIT_PHOTO_ADDED']` are the
  activity-log verbs Tasks 4 and 5 write after the row they describe already
  exists. A `KeyError` there aborts the request AFTER the photo was stored or
  the try-out created — the write the agent can see, without the trace an
  auditor needs.
* `SALES_EVENTS`, built from six `SALES_EVENT_*` names, is ONE definition with
  THREE consumers: `staff_bot/webhook_server.py::sales_event_handler` refuses
  any event outside the tuple at the door, and
  `staff_bot/i18n.py::_add_dynamic_family_keys` plus
  `scripts/seed_staff_translations.py::_add_dynamic_keys` both build
  `staff.sales.notify.<event>` from it. The event without the seeded row parks
  `staff_bot` at `/health` 503; the row without the event makes every digest a
  400 for a message the backend believes it delivered. The names exist because
  ruling 66 has producers pass a MEMBER, and a tuple of bare strings has no
  member to pass — Task 2 switches the four existing producers onto them and
  Task 3 passes `SALES_EVENT_MORNING_DIGEST`.
* the seeded line must carry EXACTLY the one field Task 8's renderer fills
  (`{date}`) — no more, no fewer.
  `shared/i18n_rendering.py::render_translation` answers the HUMANISED KEY for
  a template whose placeholders the caller did not fill, i.e. the English words
  "Morning digest" in all three languages, silently. A second field here is
  therefore an English leak no other gate can see. (The generic
  `sales_event_handler` path fills only `outlet_name`/`reason`/`order_number`,
  so it degrades this row too — which is why Task 8's dedicated digest branch
  must land before beat is ever restarted, a deploy ordering Task 9's
  runbook states.)
"""

import importlib.util
import re
from pathlib import Path

import pytest

from shared.i18n_rendering import humanise_key, render_translation
from shared.staff_constants import (
    SALES_EVENT_ACTIVATION_REQUESTED,
    SALES_EVENT_AGENT_ORDER_APPROVED,
    SALES_EVENT_AGENT_ORDER_CONFIRMED,
    SALES_EVENT_AGENT_ORDER_DECLINED,
    SALES_EVENT_AGENT_ORDER_REJECTED,
    SALES_EVENT_MORNING_DIGEST,
    SALES_EVENT_OUTLET_APPROVED,
    SALES_EVENT_OUTLET_REJECTED,
    SALES_EVENT_PAY_PENALTY_CONFIRMED,
    SALES_EVENT_PAY_STATEMENT_APPROVED,
    SALES_EVENTS,
    STAFF_ACTIONS,
)

DIGEST_KEY = "staff.sales.notify.morning_digest"
LANGUAGES = ("en", "uz", "ru")

# Exactly what `sales_event_handler` hands `i18n.get` for every sales event
# (staff_bot/webhook_server.py) — the three names, all empty, because none of
# them describes a digest. Task 8's dedicated branch passes `date` instead.
WEBHOOK_KWARGS = {"outlet_name": "", "reason": "", "order_number": ""}


def _load_seed_script():
    path = Path(__file__).resolve().parents[2] / "scripts" / "seed_staff_translations.py"
    spec = importlib.util.spec_from_file_location("seed_staff_translations", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
def test_the_two_field_actions_are_registered():
    assert STAFF_ACTIONS["AGENT_TRYOUT_CREATED"] == "agent_tryout_created"
    assert STAFF_ACTIONS["VISIT_PHOTO_ADDED"] == "visit_photo_added"


@pytest.mark.unit
def test_every_event_has_a_name_a_producer_can_pass():
    """Ruling 66: a producer passes a MEMBER, so each member needs an identifier.

    Pinned as the exact tuple, ORDER included: the six phase-2 events, then the same-day
    hold's two outcomes (compensation spec C14), then the two pay pushes (§7.5). A member
    added as a bare literal would satisfy "in SALES_EVENTS" and still leave ruling 66 unmet
    for its producer; a member dropped from the end would refuse that push at the bot's door.
    """
    named = (
        SALES_EVENT_OUTLET_APPROVED,
        SALES_EVENT_OUTLET_REJECTED,
        SALES_EVENT_ACTIVATION_REQUESTED,
        SALES_EVENT_AGENT_ORDER_CONFIRMED,
        SALES_EVENT_AGENT_ORDER_DECLINED,
        SALES_EVENT_MORNING_DIGEST,
        SALES_EVENT_AGENT_ORDER_APPROVED,
        SALES_EVENT_AGENT_ORDER_REJECTED,
        SALES_EVENT_PAY_PENALTY_CONFIRMED,
        SALES_EVENT_PAY_STATEMENT_APPROVED,
    )
    assert SALES_EVENT_MORNING_DIGEST == "morning_digest"
    assert (SALES_EVENT_AGENT_ORDER_APPROVED, SALES_EVENT_AGENT_ORDER_REJECTED) == (
        "agent_order_approved",
        "agent_order_rejected",
    )
    assert (SALES_EVENT_PAY_PENALTY_CONFIRMED, SALES_EVENT_PAY_STATEMENT_APPROVED) == (
        "pay_penalty_confirmed",
        "pay_statement_approved",
    )
    assert SALES_EVENTS == named


@pytest.mark.unit
def test_both_registries_require_the_digest_key():
    """/health and the seeder derive the family; this pins that the derivation reaches it.

    Both loops read `SALES_EVENTS`, so they cannot drift from each other — they
    can only drift together, from the event the backend actually pushes. This
    asserts the named key on both sides rather than comparing them to one
    another.
    """
    from staff_bot.i18n import Translation

    health = set()
    Translation._add_dynamic_family_keys(health)
    assert DIGEST_KEY in health

    seeded = set()
    _load_seed_script()._add_dynamic_keys(seeded)
    assert DIGEST_KEY in seeded


@pytest.mark.unit
@pytest.mark.parametrize("language", LANGUAGES)
def test_the_digest_line_is_seeded_in_every_language(language):
    module = _load_seed_script()
    value = module._curated_value(DIGEST_KEY, language)
    assert value, f"{DIGEST_KEY} has no {language} value"
    assert value == module.STAFF_TRANSLATIONS[DIGEST_KEY][language]


@pytest.mark.unit
@pytest.mark.parametrize("language", LANGUAGES)
def test_the_digest_header_carries_exactly_the_one_field_its_renderer_fills(language):
    """Rendered through production's own renderer, with the digest's own kwarg.

    `{date}` is the whole contract between this row and
    `staff_bot/webhook_server.py::_render_morning_digest` (Task 8). A SECOND
    field here does not raise and does not print braces: `render_translation`
    degrades the template to `humanise_key`, which is the English words
    "Morning digest" for a Russian-speaking agent. The `findall` is what makes
    that visible, and it is an equality rather than a membership check for
    exactly that reason.
    """
    module = _load_seed_script()
    template = module.STAFF_TRANSLATIONS[DIGEST_KEY][language]

    rendered = render_translation(DIGEST_KEY, template, kwargs={**WEBHOOK_KWARGS, "date": "15.09.2026"})

    assert re.findall(r"\{(\w+)\}", template) == ["date"]
    assert rendered == template.format(date="15.09.2026")
    assert rendered != humanise_key(DIGEST_KEY)
    assert "15.09.2026" in rendered


@pytest.mark.unit
def test_the_digest_line_passes_the_seeds_own_russian_quality_gate():
    """The seed's own rule, called rather than restated.

    This row carries `<b>`, and the validator used to count the Latin letters
    inside a tag as Latin text — which is why Step 7 teaches it to strip tags
    first. Calling the real function here is what proves the fix: a validator
    that still counted `<b>` would fail this case, and a row that lost its
    Cyrillic would fail it too.
    """
    _load_seed_script()._validate_russian_translations({DIGEST_KEY})


@pytest.mark.unit
def test_the_russian_gate_still_catches_a_latin_value_behind_a_tag():
    """The strip must not become a hole.

    A `<b>Plan na segodnya</b>` — the transliteration a hurried edit produces —
    has to keep failing, or Step 7's two-line fix would have disabled the only
    gate that reads Russian copy.
    """
    module = _load_seed_script()
    module.STAFF_TRANSLATIONS[DIGEST_KEY] = {
        **module.STAFF_TRANSLATIONS[DIGEST_KEY],
        "ru": "\U0001F4CB <b>Vash den — {date}</b>",
    }
    with pytest.raises(Exception):
        module._validate_russian_translations({DIGEST_KEY})


# The two outcome pushes of a held order (compensation spec §7.6, §8.2). They render through the
# webhook's GENERIC branch, which fills exactly `outlet_name`, `order_number` and `reason`
# (WEBHOOK_KWARGS above), so a row naming any other field would ship its humanised English key
# tail. Unlike the pay rows, these keep the leading glyph their siblings `agent_order_confirmed`
# and `agent_order_declined` carry (§8.5, second exception): the generic renderer prints the row
# verbatim.
HOLD_NOTIFY_ROWS = {
    "staff.sales.notify.agent_order_approved": {
        "fields": ["order_number", "outlet_name"],
        "en": "✅ Order {order_number} for <b>{outlet_name}</b> was approved and is in the delivery queue.",
        "uz": "✅ <b>{outlet_name}</b> uchun {order_number} buyurtmasi tasdiqlandi va yetkazish navbatida.",
        "ru": "✅ Заказ {order_number} для <b>{outlet_name}</b> одобрен и стоит в очереди на доставку.",
    },
    "staff.sales.notify.agent_order_rejected": {
        "fields": ["order_number", "outlet_name", "reason"],
        "en": "❌ Order {order_number} for <b>{outlet_name}</b> was not approved and is cancelled. Reason: {reason}",
        "uz": "❌ <b>{outlet_name}</b> uchun {order_number} buyurtmasi tasdiqlanmadi va bekor qilindi. Sabab: {reason}",
        "ru": "❌ Заказ {order_number} для <b>{outlet_name}</b> не одобрен и отменён. Причина: {reason}",
    },
}


@pytest.mark.unit
@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("key", sorted(HOLD_NOTIFY_ROWS))
def test_the_held_order_outcome_rows_are_seeded_as_the_spec_words_them(key, language):
    value = _load_seed_script()._curated_value(key, language)

    assert value == HOLD_NOTIFY_ROWS[key][language]
    assert sorted(set(re.findall(r"\{(\w+)\}", value))) == HOLD_NOTIFY_ROWS[key]["fields"]
    assert set(HOLD_NOTIFY_ROWS[key]["fields"]) <= set(WEBHOOK_KWARGS)


# The two pay pushes' headline rows (compensation spec §8.2) and the fields their renderers
# fill. `render_translation` degrades a row with an unfilled field to the humanised ENGLISH
# key in every language, so a stray second field is an English leak no other gate sees.
PAY_NOTIFY_FIELDS = {
    "staff.sales.notify.pay_penalty_confirmed": [],
    "staff.sales.notify.pay_statement_approved": ["month"],
}


@pytest.mark.unit
@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("key", sorted(PAY_NOTIFY_FIELDS))
def test_the_pay_push_headlines_are_seeded_with_exactly_their_fields(key, language):
    value = _load_seed_script()._curated_value(key, language)

    assert value, f"{key} has no {language} row"
    assert sorted(re.findall(r"\{(\w+)\}", value)) == PAY_NOTIFY_FIELDS[key]
    assert value.startswith("<b>") and value.endswith("</b>"), "words only; the glyph is the renderer's (§8.5)"
