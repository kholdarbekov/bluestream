"""POST /internal/sales-event.

The staff bot renders a sales event into a localized message with a deep-link
button.

NOTE: importing the bot module runs setup_logging(), which reconfigures the
root logger and breaks pytest's caplog. Assert on the handler's RESPONSE, never
on captured logs.

NOTE: this project does not run pytest-asyncio (no `asyncio` marker is
registered and `--strict-markers` is on — `async def test_...` would either
fail to collect or silently pass without executing). This file follows the
convention of every other async staff_bot test in the suite: a plain sync
`def test_...` driving the coroutine with `asyncio.run(...)`.
"""

import asyncio
import importlib.util
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.redis_keyspace import RedisKeyspace
from shared.staff_constants import SALES_EVENTS

_spec = importlib.util.spec_from_file_location(
    "seed_staff_translations",
    Path(__file__).resolve().parents[2] / "scripts" / "seed_staff_translations.py",
)
_SEED = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_SEED)


def _fake_i18n_get(key, lang, **kw):
    """Reason labels resolve through the SEED; everything else is a sentinel.

    The reason family has to come from `scripts/seed_staff_translations.py`
    rather than a literal in this file, or the assertion below would keep
    passing against copy production no longer ships.

    `order_number` is appended only when the handler passed one, so the three
    outlet events render exactly the same sentinel they always did and their
    assertions still say what they said.
    """
    if key.startswith("staff.sales.approvals.reason."):
        return _SEED._curated_value(key, lang)
    rendered = f"{key}|{kw.get('outlet_name', '')}|{kw.get('reason', '')}"
    if kw.get("order_number"):
        rendered = f"{rendered}|{kw['order_number']}"
    # The digest's own two kwargs. Appended only when passed, so the five
    # one-line events keep rendering exactly the sentinel they always did.
    if kw.get("date"):
        rendered = f"{rendered}|{kw['date']}"
    if kw.get("days") is not None:
        rendered = f"{rendered}|{kw['days']}"
    if kw.get("count") is not None:
        rendered = f"{rendered}|{kw['count']}"
    return rendered


@pytest.fixture
def server():
    from staff_bot.webhook_server import StaffWebhookServer

    instance = StaffWebhookServer()
    instance.bot_app = MagicMock()
    instance.bot_app.bot.send_message = AsyncMock()
    return instance


def make_request(payload):
    request = MagicMock()
    request.path = "/internal/sales-event"
    request.json = AsyncMock(return_value=payload)
    return request


# The payload every one-line sales event carries. The vocabulary, sound and release tests drive
# EVERY event in `SALES_EVENTS` with it, including the digest and the two pay pushes, whose
# renderers must skip what is missing rather than raise (compensation spec §7.5).
GENERIC_PAYLOAD = {"outlet_id": 5, "outlet_name": "Bahor", "order_id": 9,
                   "order_number": "SA_000413_26", "reason": "Incomplete"}


def _send_generic(server, event):
    """One event with `GENERIC_PAYLOAD`, through the sentinel copy; the send's kwargs."""
    payload = {
        "event_id": f"sales:{event}",
        "telegram_id": 777000111,
        "event": event,
        "payload": dict(GENERIC_PAYLOAD),
    }
    with patch("staff_bot.webhook_server.verify_webhook_signature", AsyncMock(return_value=True)), \
         patch.object(server, "_check_rate_limit", AsyncMock(return_value=None)), \
         patch.object(server, "_is_duplicate_event", AsyncMock(return_value=False)), \
         patch("staff_bot.webhook_server.i18n.get_user_language", AsyncMock(return_value="en")), \
         patch("staff_bot.webhook_server.i18n.get", side_effect=_fake_i18n_get):
        response = asyncio.run(server.sales_event_handler(make_request(payload)))
    assert response.status == 200
    return server.bot_app.bot.send_message.await_args.kwargs


class TestSalesEventHandler:
    def test_has_its_own_rate_limit_bucket(self, server):
        assert "/internal/sales-event" in server._rate_limiters

    def test_sends_text_and_button(self, server):
        payload = {
            "event_id": "sales:1",
            "telegram_id": 777000111,
            "event": "outlet_rejected",
            "payload": {"outlet_id": 5, "outlet_name": "Bahor", "reason": "Incomplete"},
        }
        with patch("staff_bot.webhook_server.verify_webhook_signature", AsyncMock(return_value=True)), \
             patch.object(server, "_check_rate_limit", AsyncMock(return_value=None)), \
             patch.object(server, "_is_duplicate_event", AsyncMock(return_value=False)), \
             patch("staff_bot.webhook_server.i18n.get_user_language", AsyncMock(return_value="en")), \
             patch("staff_bot.webhook_server.i18n.get", side_effect=_fake_i18n_get):
            response = asyncio.run(server.sales_event_handler(make_request(payload)))

        assert response.status == 200
        kwargs = server.bot_app.bot.send_message.await_args.kwargs
        assert kwargs["chat_id"] == 777000111
        assert kwargs["text"] == "staff.sales.notify.outlet_rejected|Bahor|Incomplete"
        assert kwargs["disable_notification"] is True
        assert kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "staff_sales_outlet_5"

    def _send(self, server, reason, language):
        payload = {
            "event_id": f"sales:{reason}:{language}",
            "telegram_id": 777000111,
            "event": "outlet_rejected",
            "payload": {"outlet_id": 5, "outlet_name": "Bahor", "reason": reason},
        }
        with patch("staff_bot.webhook_server.verify_webhook_signature", AsyncMock(return_value=True)), \
             patch.object(server, "_check_rate_limit", AsyncMock(return_value=None)), \
             patch.object(server, "_is_duplicate_event", AsyncMock(return_value=False)), \
             patch("staff_bot.webhook_server.i18n.get_user_language", AsyncMock(return_value=language)), \
             patch("staff_bot.webhook_server.i18n.get", side_effect=_fake_i18n_get):
            response = asyncio.run(server.sales_event_handler(make_request(payload)))
        assert response.status == 200
        return server.bot_app.bot.send_message.await_args.kwargs["text"]

    def test_a_canonical_reason_is_localized_and_free_text_is_echoed(self, server):
        """The agent must not read English inside a Russian sentence.

        `Outlet.rejected_reason` is stored as the plain-English string the
        operator's button carried (`REJECT_REASONS`), so it has to be mapped
        back to its key here and rendered in the RECIPIENT's language. The
        admin UI can reject with anything, so an unmapped reason is echoed
        unchanged rather than swallowed or replaced with "Other".
        """
        localized = self._send(server, "Duplicate outlet", "ru")
        assert localized.endswith(
            "|" + _SEED._curated_value("staff.sales.approvals.reason.duplicate", "ru")
        )
        assert "Duplicate outlet" not in localized

        echoed = self._send(server, "Wrong phone on the sign", "ru")
        assert echoed.endswith("|Wrong phone on the sign")

    def test_rejects_a_bad_signature(self, server):
        with patch("staff_bot.webhook_server.verify_webhook_signature", AsyncMock(return_value=False)):
            response = asyncio.run(server.sales_event_handler(make_request({})))
        assert response.status == 401

    def test_rejects_an_unknown_event(self, server):
        payload = {"telegram_id": 777000111, "event": "nope", "payload": {}}
        with patch("staff_bot.webhook_server.verify_webhook_signature", AsyncMock(return_value=True)), \
             patch.object(server, "_check_rate_limit", AsyncMock(return_value=None)):
            response = asyncio.run(server.sales_event_handler(make_request(payload)))
        assert response.status == 400
        server.bot_app.bot.send_message.assert_not_awaited()

    def test_is_idempotent_on_a_repeated_event_id(self, server):
        payload = {
            "event_id": "dup",
            "telegram_id": 777000111,
            "event": "activation_requested",
            "payload": {"outlet_id": 5, "outlet_name": "Bahor", "reason": None},
        }
        with patch("staff_bot.webhook_server.verify_webhook_signature", AsyncMock(return_value=True)), \
             patch.object(server, "_check_rate_limit", AsyncMock(return_value=None)), \
             patch.object(server, "_is_duplicate_event", AsyncMock(return_value=True)):
            response = asyncio.run(server.sales_event_handler(make_request(payload)))

        assert response.status == 200
        server.bot_app.bot.send_message.assert_not_awaited()

    def test_sends_the_agent_order_confirmed_event(self, server):
        payload = {
            "event_id": "sales:confirmed:1229",
            "telegram_id": 777000111,
            "event": "agent_order_confirmed",
            "payload": {"outlet_id": 5, "outlet_name": "Bahor", "order_id": 1229,
                        "order_number": "SA_001229_26", "reason": None},
        }
        with patch("staff_bot.webhook_server.verify_webhook_signature", AsyncMock(return_value=True)), \
             patch.object(server, "_check_rate_limit", AsyncMock(return_value=None)), \
             patch.object(server, "_is_duplicate_event", AsyncMock(return_value=False)), \
             patch("staff_bot.webhook_server.i18n.get_user_language", AsyncMock(return_value="en")), \
             patch("staff_bot.webhook_server.i18n.get", side_effect=_fake_i18n_get):
            response = asyncio.run(server.sales_event_handler(make_request(payload)))

        assert response.status == 200
        kwargs = server.bot_app.bot.send_message.await_args.kwargs
        assert kwargs["chat_id"] == 777000111
        assert kwargs["text"] == "staff.sales.notify.agent_order_confirmed|Bahor||SA_001229_26"
        assert kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "staff_sales_outlet_5"

    def test_sends_the_agent_order_declined_event_with_the_customers_reason(self, server):
        payload = {
            "event_id": "sales:declined:1229",
            "telegram_id": 777000111,
            "event": "agent_order_declined",
            "payload": {"outlet_id": 5, "outlet_name": "Bahor", "order_id": 1229,
                        "order_number": "SA_001229_26", "reason": "Too much stock"},
        }
        with patch("staff_bot.webhook_server.verify_webhook_signature", AsyncMock(return_value=True)), \
             patch.object(server, "_check_rate_limit", AsyncMock(return_value=None)), \
             patch.object(server, "_is_duplicate_event", AsyncMock(return_value=False)), \
             patch("staff_bot.webhook_server.i18n.get_user_language", AsyncMock(return_value="en")), \
             patch("staff_bot.webhook_server.i18n.get", side_effect=_fake_i18n_get):
            response = asyncio.run(server.sales_event_handler(make_request(payload)))

        assert response.status == 200
        kwargs = server.bot_app.bot.send_message.await_args.kwargs
        assert kwargs["text"] == (
            "staff.sales.notify.agent_order_declined|Bahor|Too much stock|SA_001229_26"
        )
        assert kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "staff_sales_outlet_5"

    def test_an_order_event_dedups_on_the_order_not_the_outlet(self, server):
        """Two orders for the same outlet on the same day are two events.

        The fallback key is what runs when the producer sent no `event_id`;
        keying it on `outlet_id` alone would let a decline for order B be
        swallowed as a duplicate of the confirmation of order A.
        """
        dedup = AsyncMock(return_value=False)
        payload = {
            "telegram_id": 777000111,
            "event": "agent_order_declined",
            "payload": {"outlet_id": 5, "outlet_name": "Bahor", "order_id": 1229,
                        "order_number": "SA_001229_26", "reason": "Too much stock"},
        }
        with patch("staff_bot.webhook_server.verify_webhook_signature", AsyncMock(return_value=True)), \
             patch.object(server, "_check_rate_limit", AsyncMock(return_value=None)), \
             patch.object(server, "_is_duplicate_event", dedup), \
             patch("staff_bot.webhook_server.i18n.get_user_language", AsyncMock(return_value="en")), \
             patch("staff_bot.webhook_server.i18n.get", side_effect=_fake_i18n_get):
            response = asyncio.run(server.sales_event_handler(make_request(payload)))

        assert response.status == 200
        assert dedup.await_args.args[1] == "sales:agent_order_declined:777000111:1229"

    def test_an_outlet_event_still_dedups_on_the_outlet(self, server):
        """The order_id fallback must not change the three original events."""
        dedup = AsyncMock(return_value=False)
        payload = {
            "telegram_id": 777000111,
            "event": "outlet_approved",
            "payload": {"outlet_id": 5, "outlet_name": "Bahor", "reason": None},
        }
        with patch("staff_bot.webhook_server.verify_webhook_signature", AsyncMock(return_value=True)), \
             patch.object(server, "_check_rate_limit", AsyncMock(return_value=None)), \
             patch.object(server, "_is_duplicate_event", dedup), \
             patch("staff_bot.webhook_server.i18n.get_user_language", AsyncMock(return_value="en")), \
             patch("staff_bot.webhook_server.i18n.get", side_effect=_fake_i18n_get):
            response = asyncio.run(server.sales_event_handler(make_request(payload)))

        assert response.status == 200
        assert dedup.await_args.args[1] == "sales:outlet_approved:777000111:5"


class TestTheSalesEventVocabularyHasOneDefinition:
    """M27: the allowlist below is the SAME list /health and the seed read.

    It was written out three times — the tuple in `sales_event_handler`,
    the family loop in `staff_bot/i18n.py` and its twin in
    `scripts/seed_staff_translations.py` — and only the last two were kept in
    step by a test. A sixth event accepted here but unknown to the registries
    renders a humanised key tail on the agent's phone while /health stays
    green; one known to the registries but missing here is a 400 at the door
    for a message the backend believes it sent.
    """

    @pytest.mark.parametrize("event", SALES_EVENTS)
    def test_every_event_in_the_constant_reaches_the_agent(self, server, event):
        text = _send_generic(server, event)["text"]
        # Compensation spec §10.4: the pay pushes put their glyph in front of the headline in
        # code (§8.5), so the event's row is IN the first line rather than at its start.
        assert f"staff.sales.notify.{event}|" in text.splitlines()[0]

    @pytest.mark.parametrize("event", SALES_EVENTS)
    def test_only_the_digest_and_the_pay_pushes_make_a_sound(self, server, event):
        """The digest starts the agent's day, and pay news arrives while the phone is in a
        pocket (§7.5). Every other sales event arrives while the agent is already looking at
        the phone and stays silent."""
        loud = {"morning_digest", "pay_penalty_confirmed", "pay_statement_approved"}
        assert _send_generic(server, event)["disable_notification"] is (event not in loud)

    def test_an_event_outside_the_constant_is_refused_at_the_door(self, server):
        payload = {"telegram_id": 777000111, "event": "outlet_visited", "payload": {"outlet_id": 5}}
        with patch("staff_bot.webhook_server.verify_webhook_signature", AsyncMock(return_value=True)), \
             patch.object(server, "_check_rate_limit", AsyncMock(return_value=None)):
            response = asyncio.run(server.sales_event_handler(make_request(payload)))
        assert response.status == 400
        server.bot_app.bot.send_message.assert_not_awaited()


# The producer's own shape, imported rather than described. `digest_service`
# is backend code and this is a backend-suite file, so the import costs
# nothing and buys the one thing a hand-written fixture cannot: if Task 3
# renames a row key, this file goes RED instead of quietly rendering a
# payload the backend stopped sending.
from business_app.services.sales.digest_service import (  # noqa: E402
    DIGEST_ROW_KEYS,
    DIGEST_SECTION_KEYS,
)

DIGEST_PAYLOAD = {
    "date": "2026-09-15",
    "due_today": [{"outlet_id": 7, "outlet_name": "Bahor market", "overdue_days": 0}],
    "overdue": [{"outlet_id": 9, "outlet_name": "Chorsu do'kon", "overdue_days": 4}],
    "more_count": 0,
    "unvisited": [{"outlet_id": 12, "outlet_name": "Yunusobod 4", "days": 25}],
    "open_visit": {"visit_id": 31, "outlet_id": 7, "outlet_name": "Bahor market"},
}


class TestTheMorningDigest:
    """The one sales event that is a LIST rather than a line.

    Everything in it was decided server-side (`AgentDigestService.build`):
    which outlets are due, how late each is, which five unvisited ones made
    the cut, and whether a visit is still open. The handler renders; it never
    re-queries, re-sorts or re-counts.
    """

    def _send(self, server, payload, language="en"):
        body = {
            "event_id": "sales:digest:1",
            "telegram_id": 777000111,
            "event": "morning_digest",
            "payload": payload,
        }
        with patch("staff_bot.webhook_server.verify_webhook_signature", AsyncMock(return_value=True)), \
             patch.object(server, "_check_rate_limit", AsyncMock(return_value=None)), \
             patch.object(server, "_is_duplicate_event", AsyncMock(return_value=False)), \
             patch("staff_bot.webhook_server.i18n.get_user_language", AsyncMock(return_value=language)), \
             patch("staff_bot.webhook_server.i18n.get", side_effect=_fake_i18n_get):
            response = asyncio.run(server.sales_event_handler(make_request(body)))
        assert response.status == 200
        return server.bot_app.bot.send_message.await_args.kwargs

    def test_the_fixture_is_exactly_what_the_backend_publishes(self):
        """The renderer's fixture, pinned to the producer's own tuples.

        Every other test in this class reads `DIGEST_PAYLOAD`, so the whole
        class is only as true as that dict. A renamed row key on the backend
        would leave all of them green and ship a digest of blank names — the
        bot reads `outlet_name`, finds nothing, and prints a bullet with a
        button and no shop. Asserting `tuple(row)`, not a subset, because a
        row that gained a field the renderer ignores is drift too: the
        backend publishes the answer, and this is the list of answers.
        """
        assert set(DIGEST_SECTION_KEYS) <= set(DIGEST_PAYLOAD)
        for section in DIGEST_SECTION_KEYS:
            assert DIGEST_PAYLOAD[section], f"{section} must carry a row or it pins nothing"
            for row in DIGEST_PAYLOAD[section]:
                assert tuple(row) == DIGEST_ROW_KEYS[section]

    def test_it_renders_every_section_the_backend_published(self, server):
        kwargs = self._send(server, DIGEST_PAYLOAD)
        lines = kwargs["text"].split("\n")

        assert lines[0].startswith("staff.sales.notify.morning_digest||")
        assert lines[0].endswith("|15.09.2026")
        assert "staff.sales.notify.digest_open_visit" in lines[1]
        assert "Bahor market" in lines[1]
        assert "staff.sales.notify.digest_due_today" in lines[2]
        assert lines[3].strip() == "• Bahor market"
        assert "staff.sales.notify.digest_overdue" in lines[4]
        # The lateness suffix is the DUE LIST's own key, rendered with the
        # backend's `overdue_days`: "how late" has one expression in this bot.
        assert lines[5].strip() == "• Chorsu do'kon · staff.sales.list.overdue_suffix|||4"
        assert "staff.sales.notify.digest_unvisited" in lines[6]
        assert lines[7].strip() == "• Yunusobod 4 · staff.sales.notify.digest_days|||25"

    def test_the_digest_makes_a_sound(self, server):
        """A silent 08:30 message is a digest nobody reads. (The two pay pushes are the only
        other loud sales events; `test_only_the_digest_and_the_pay_pushes_make_a_sound` pins
        the whole list.)"""
        assert self._send(server, DIGEST_PAYLOAD)["disable_notification"] is False

    def test_every_row_carries_a_button_that_opens_that_outlet(self, server):
        """Named by the OUTLET, not "Open outlet" four times over."""
        markup = self._send(server, DIGEST_PAYLOAD)["reply_markup"]
        assert [row[0].callback_data for row in markup.inline_keyboard] == [
            "staff_sales_outlet_7",   # the open visit
            "staff_sales_outlet_7",   # due today
            "staff_sales_outlet_9",   # overdue
            "staff_sales_outlet_12",  # unvisited
        ]
        assert [row[0].text for row in markup.inline_keyboard] == [
            "Bahor market", "Bahor market", "Chorsu do'kon", "Yunusobod 4"
        ]

    def test_a_shop_nobody_has_ever_visited_says_so(self, server):
        """`days: null` is NEVER, and it must not look like a zero.

        A falsy check eats both, so an outlet nobody has walked into would
        render exactly like one visited this morning — under a heading that
        says "not visited for a while". The backend refuses to invent an age
        from `created_at` (it would print "2 d" for a shop approved
        yesterday), so the word is the bot's job.
        """
        kwargs = self._send(server, {
            **DIGEST_PAYLOAD,
            "due_today": [], "overdue": [], "open_visit": None,
            "unvisited": [
                {"outlet_id": 12, "outlet_name": "Hech qachon", "days": None},
                {"outlet_id": 13, "outlet_name": "Yunusobod 4", "days": 25},
            ],
        })
        lines = [line.strip() for line in kwargs["text"].split("\n")]

        assert "• Hech qachon · staff.sales.notify.digest_never||" in lines
        assert "• Yunusobod 4 · staff.sales.notify.digest_days|||25" in lines
        # ...and the never row is not silently bare, which is the failure mode.
        assert "• Hech qachon" not in lines

    def test_the_backends_cut_is_announced_rather_than_hidden(self, server):
        """`more_count` is Task 3's figure: the due sections are capped at ten
        because an uncapped digest is a `send_message` that raises and three
        retries that deliver nothing.

        The bot prints what it was handed and counts nothing — it cannot, it
        was never sent the rows. A digest that showed ten of twenty-five with
        no line saying so would read as a complete day's work.
        """
        kwargs = self._send(server, {**DIGEST_PAYLOAD, "more_count": 15})
        assert "staff.sales.notify.digest_more|||15" in kwargs["text"]

        complete = self._send(server, {**DIGEST_PAYLOAD, "more_count": 0})
        assert "staff.sales.notify.digest_more" not in complete["text"]
        # A payload from before the cap existed must not print "+0 more" either.
        missing = self._send(server, {k: v for k, v in DIGEST_PAYLOAD.items() if k != "more_count"})
        assert "staff.sales.notify.digest_more" not in missing["text"]

    def test_a_section_the_backend_left_empty_is_not_drawn(self, server):
        """Membership is the backend's answer, and an empty list is an answer.

        The bot must not invent an "Overdue — none" line: R6 says an agent
        with nothing to report gets no digest at all, so a digest that
        arrives has something in it and every heading it prints has rows.
        """
        kwargs = self._send(server, {
            "date": "2026-09-15",
            "due_today": [{"outlet_id": 7, "outlet_name": "Bahor market", "overdue_days": 0}],
            "overdue": [], "more_count": 0, "unvisited": [], "open_visit": None,
        })
        assert "staff.sales.notify.digest_overdue" not in kwargs["text"]
        assert "staff.sales.notify.digest_unvisited" not in kwargs["text"]
        assert "staff.sales.notify.digest_open_visit" not in kwargs["text"]
        assert [row[0].callback_data for row in kwargs["reply_markup"].inline_keyboard] == [
            "staff_sales_outlet_7"
        ]

    def test_an_overdue_row_without_a_figure_prints_no_suffix(self, server):
        """`overdue_days` of 0 means "due today", which is not late.

        The due list already refuses to print "+0d" for the same reason; a
        digest that did would tell an agent a call is late on the morning it
        became due.
        """
        kwargs = self._send(server, {
            "date": "2026-09-15", "due_today": [], "more_count": 0,
            "unvisited": [], "open_visit": None,
            "overdue": [{"outlet_id": 9, "outlet_name": "Chorsu do'kon", "overdue_days": 0}],
        })
        assert "staff.sales.list.overdue_suffix" not in kwargs["text"]
        assert "• Chorsu do'kon" in kwargs["text"]

    def test_two_digests_on_different_days_are_not_one_event(self, server):
        """The fallback dedup key has to carry the DAY.

        It keys on `order_id` or `outlet_id`, and a digest has neither. With
        a constant key and a 24-hour dedup TTL, a producer that sent no
        `event_id` would have tomorrow's digest swallowed as a duplicate of
        today's — the one event where that is a whole day of work unseen.
        """
        dedup = AsyncMock(return_value=False)
        body = {"telegram_id": 777000111, "event": "morning_digest", "payload": DIGEST_PAYLOAD}
        with patch("staff_bot.webhook_server.verify_webhook_signature", AsyncMock(return_value=True)), \
             patch.object(server, "_check_rate_limit", AsyncMock(return_value=None)), \
             patch.object(server, "_is_duplicate_event", dedup), \
             patch("staff_bot.webhook_server.i18n.get_user_language", AsyncMock(return_value="en")), \
             patch("staff_bot.webhook_server.i18n.get", side_effect=_fake_i18n_get):
            response = asyncio.run(server.sales_event_handler(make_request(body)))

        assert response.status == 200
        assert dedup.await_args.args[1] == "sales:morning_digest:777000111:2026-09-15"

    def test_the_agents_language_decides_every_line(self, server):
        """One `get_user_language` call feeds the header, the sections and
        the suffixes alike — a digest half in Russian is the English-leak
        class this bot keeps rediscovering.

        Asserted PER SECTION rather than as one total. A bare
        `count("@ru") == N` is a number any future localized string breaks for
        a reason unrelated to language, and it cannot say WHICH line leaked —
        the only thing this test exists to find.
        """
        payload = {**DIGEST_PAYLOAD, "more_count": 15}
        with patch("staff_bot.webhook_server.verify_webhook_signature", AsyncMock(return_value=True)), \
             patch.object(server, "_check_rate_limit", AsyncMock(return_value=None)), \
             patch.object(server, "_is_duplicate_event", AsyncMock(return_value=False)), \
             patch("staff_bot.webhook_server.i18n.get_user_language", AsyncMock(return_value="ru")), \
             patch("staff_bot.webhook_server.i18n.get") as getter:
            getter.side_effect = lambda key, lang, **kw: f"{key}@{lang}"
            asyncio.run(server.sales_event_handler(make_request({
                "event_id": "sales:digest:ru", "telegram_id": 777000111,
                "event": "morning_digest", "payload": payload,
            })))
        rendered = server.bot_app.bot.send_message.await_args.kwargs["text"]

        # Every string the screen draws, named, each in the agent's language.
        for key in (
            "staff.sales.notify.morning_digest",
            "staff.sales.notify.digest_open_visit",
            "staff.sales.notify.digest_due_today",
            "staff.sales.notify.digest_overdue",
            "staff.sales.notify.digest_unvisited",
            "staff.sales.list.overdue_suffix",
            "staff.sales.notify.digest_days",
            "staff.sales.notify.digest_more",
        ):
            assert f"{key}@ru" in rendered, f"{key} did not follow the agent's language"
        assert "@en" not in rendered
        # One message for the whole day, not a section per send.
        assert server.bot_app.bot.send_message.await_count == 1

    # Telegram refuses a `sendMessage` over 4096 characters with a 400, and the
    # push has three retries that would all raise the same way — an agent's
    # whole morning, delivered nowhere. The cap that makes this safe is the
    # BACKEND's (Task 3 trims each due section to ten and publishes the
    # remainder as `more_count`), so the number asserted here is the worst case
    # that cap allows, not a guess.
    TELEGRAM_TEXT_LIMIT = 4096
    DIGEST_SECTION_CAP = 10
    DIGEST_UNVISITED_CAP = 5

    def test_a_digest_too_long_to_send_sheds_sections_instead_of_being_lost(self, server):
        """The backstop for a payload no cap anticipated.

        The bot renders what it is SENT. If that still composes past Telegram's
        limit, the send raises and all three retries raise with it — the agent's
        whole morning delivered nowhere. So the lowest-priority sections are shed
        (unvisited first, then due today; what is LATE is today's work) and the
        rows they carried are counted into the same "+N more" line the backend's
        own cut prints on.
        """
        def _rows(prefix, base, count, **extra):
            return [
                {"outlet_id": base + i, "outlet_name": f"{prefix}-{i} ".ljust(60, "ё"), **extra}
                for i in range(count)
            ]

        payload = {
            "date": "2026-09-15",
            "due_today": _rows("due", 100, 30, overdue_days=0),
            "overdue": _rows("late", 200, 30, overdue_days=99),
            "unvisited": _rows("cold", 300, 30, days=365),
            "more_count": 0,
            "open_visit": None,
        }
        kwargs = self._send(server, payload)

        assert len(kwargs["text"]) < self.TELEGRAM_TEXT_LIMIT
        # Late work survives; the two lower-priority sections are gone.
        assert "late-0" in kwargs["text"]
        assert "cold-0" not in kwargs["text"] and "due-0" not in kwargs["text"]
        # ...and every shed row is counted, not silently dropped.
        assert "staff.sales.notify.digest_more|||60" in kwargs["text"]
        assert len(kwargs["reply_markup"].inline_keyboard) == 30

    def test_a_digest_still_too_long_after_shedding_says_so_in_the_log(self, server):
        """The end of the backstop, driven once.

        Only `unvisited` and `due_today` are droppable (what is LATE is today's
        work), so an overdue-only payload past the limit survives the shedding
        loop and still reaches `send_message`. It is NOT truncated — a cut through
        an HTML tag turns a long message into a parse-mode 400, the same lost
        morning by another route — so the one thing that must happen is a log line
        naming the agent and the length. Unreachable under today's caps, which is
        exactly why the limit is monkeypatched to reach it.
        """
        payload = {
            "date": "2026-09-15",
            "due_today": [],
            "unvisited": [],
            "overdue": [
                {"outlet_id": 200 + i, "outlet_name": f"late-{i}", "overdue_days": 99}
                for i in range(3)
            ],
            "more_count": 0,
            "open_visit": None,
        }
        with patch("staff_bot.webhook_server.DIGEST_TEXT_LIMIT", 50), \
             patch("staff_bot.webhook_server.logger") as log:
            kwargs = self._send(server, payload)

        shouted = [
            call for call in log.warning.call_args_list
            if "after shedding" in call.args[0]
        ]
        assert len(shouted) == 1, log.warning.call_args_list
        assert shouted[0].args[1] == 777000111, "the log must name the agent who got nothing"
        assert shouted[0].args[2] == len(kwargs["text"])
        # Sent anyway, whole: every row is still there and no tag was cut.
        assert "late-2" in kwargs["text"]

    def test_the_fullest_digest_the_cap_allows_still_fits_in_one_message(self, server):
        """Ten due, ten overdue, five unvisited, an open visit and a cut line.

        Outlet names are built at `Outlet.name`'s REAL ceiling, read off the
        column itself: 200 characters, all of which an agent can type at the
        new-outlet step and all of which the backend publishes. `LABEL_MAX` is
        the BUTTON's cap, so a test that padded to it measured a worst case the
        payload is not bounded by — 26 rows of 200 is ≈5.6k characters, a
        `sendMessage` that raises and three retries that deliver nothing.
        """
        from business_app.models.sales import Outlet

        name_max = Outlet.__table__.c.name.type.length

        def _name(prefix, index):
            return f"{prefix}-{index} ".ljust(name_max, "ё")

        payload = {
            "date": "2026-09-15",
            "due_today": [
                {"outlet_id": 100 + i, "outlet_name": _name("due", i), "overdue_days": 0}
                for i in range(self.DIGEST_SECTION_CAP)
            ],
            "overdue": [
                {"outlet_id": 200 + i, "outlet_name": _name("late", i), "overdue_days": 99}
                for i in range(self.DIGEST_SECTION_CAP)
            ],
            "unvisited": [
                {"outlet_id": 300 + i, "outlet_name": _name("cold", i), "days": 365}
                for i in range(self.DIGEST_UNVISITED_CAP)
            ],
            "more_count": 15,
            "open_visit": {"visit_id": 31, "outlet_id": 100, "outlet_name": _name("due", 0)},
        }
        kwargs = self._send(server, payload)

        assert len(kwargs["text"]) < self.TELEGRAM_TEXT_LIMIT, (
            f"the fullest digest is {len(kwargs['text'])} characters, past Telegram's "
            f"{self.TELEGRAM_TEXT_LIMIT}: a send that raises and three retries that deliver nothing"
        )
        # The TRUNCATION itself, pinned independently of the length above: with the
        # shedding backstop in place a lost cut still composes under the limit (the
        # sections are simply dropped instead), so the only thing that catches it is
        # the rendered NAMES. Text lines and button labels alike, one ceiling.
        # The fixture names are self-delimiting (`<prefix>-<n> ` then padding), so
        # each one can be measured on its own rather than with its line's furniture.
        import re

        from staff_bot.keyboards.sales import LABEL_MAX

        printed = re.findall(r"(?:due|late|cold)-\d+ ё+", kwargs["text"])
        assert len(printed) == (
            1 + self.DIGEST_SECTION_CAP * 2 + self.DIGEST_UNVISITED_CAP
        ), "fixture: every row and the open visit should print its name"
        over = [name for name in printed if len(name) > LABEL_MAX]
        assert not over, (
            f"{len(over)} digest name(s) print past LABEL_MAX ({len(over[0])} characters): "
            "the text is bounded only by the shedding backstop, which drops whole sections"
        )
        labels = [
            button.text
            for row in kwargs["reply_markup"].inline_keyboard for button in row
        ]
        assert labels and all(len(label) <= LABEL_MAX for label in labels)
        # One button per row, and every row still has one.
        assert len(kwargs["reply_markup"].inline_keyboard) == (
            1 + self.DIGEST_SECTION_CAP * 2 + self.DIGEST_UNVISITED_CAP
        )


# ---- the two pay pushes (compensation spec §7.5, §10.4) ----------------------------------------

MINUS = "−"  # what `signed_currency` prints; a hyphen-minus here would pass against a bug

# Every row the two renderers read. The pushes are asserted byte for byte, so they are rendered
# through production's own `i18n.get` over the seed's rows, never through a sentinel.
PAY_COPY_KEYS = (
    "staff.currency.uzs",
    "staff.sales.notify.pay_penalty_confirmed", "staff.sales.notify.pay_statement_approved",
    "staff.sales.notify.pay_statement_approved_shadow", "staff.sales.notify.penalty_type",
    "staff.sales.notify.penalty_date", "staff.sales.notify.penalty_amount",
    "staff.sales.notify.counts_in", "staff.sales.notify.late", "staff.sales.notify.open_earnings",
    "staff.sales.notify.open_statement", "staff.sales.earnings.shadow_note",
    "staff.sales.earnings.reason", "staff.sales.earnings.base",
    # D-BANDS: the Commission row and its tier block (`format_pay_tiers`).
    "staff.sales.earnings.commission", "staff.sales.earnings.units",
    "staff.sales.earnings.tier_percent", "staff.sales.earnings.tier_scaled",
    "staff.sales.earnings.per_unit", "staff.sales.earnings.next_tier",
    "staff.sales.earnings.commission_after_gate", "staff.sales.earnings.late",
    "staff.sales.earnings.new_outlets", "staff.sales.earnings.adjustments",
    "staff.sales.earnings.penalties", "staff.sales.earnings.total",
    "staff.sales.earnings.carry_in", "staff.sales.earnings.owed_in",
    "staff.sales.earnings.carry_note", "staff.sales.earnings.owed_note",
)

# C24's keys exactly; money is whole UZS, months "YYYY-MM", dates "YYYY-MM-DD".
PENALTY = {
    "penalty_id": 17, "month": "2026-10", "incident_date": "2026-10-14",
    "type_names": {"en": "Missed planned visits", "uz": "Rejadagi tashriflar qoldirilgan",
                   "ru": "Пропущенные плановые визиты"},
    "reason": "Did not visit 5 planned outlets", "amount": 150000, "is_late": False,
}
# Illustration B (§1.2), as §7.5 prints it: D-DAYS's base and total (E.3 M4), and the commission as
# A8's `summary.commission` with `next_tier` null, as `_pushed_product` publishes a frozen
# statement's (it has none, §7.5).
NAMES_19L = {"en": "19L", "uz": "19 l", "ru": "19 л"}


def _single_tier_19l(units, amount, net):
    """19L on one per-unit tier at 1,000, in A8's product shape."""
    return {"product_id": 3, "product_name": NAMES_19L, "uses_default_tiers": False, "units": units,
            "total": amount,
            "tiers": [{"from_unit": 1, "to_unit": None, "units": units, "mode": "per_unit", "value": 1000.0,
                       "net": net, "amount_full": amount, "amount": amount}],
            "next_tier": None}


STATEMENT = {
    "statement_id": 55, "month": "2026-10", "is_shadow": False, "base": 2800000,
    "commission": {"gross": 850000, "products": [
        {"product_id": 3, "product_name": NAMES_19L, "uses_default_tiers": False, "units": 500, "total": 600000,
         "tiers": [
             {"from_unit": 1, "to_unit": 300, "units": 300, "mode": "per_unit", "value": 1000.0,
              "net": 6000000.0, "amount_full": 300000, "amount": 300000},
             {"from_unit": 301, "to_unit": None, "units": 200, "mode": "per_unit", "value": 1500.0,
              "net": 4000000.0, "amount_full": 300000, "amount": 300000},
         ],
         "next_tier": None},
        {"product_id": 9, "product_name": {"en": "Juice 1 L", "uz": "Sharbat 1 l", "ru": "Сок 1 л"},
         "uses_default_tiers": True, "units": 500, "total": 250000,
         "tiers": [{"from_unit": 1, "to_unit": None, "units": 500, "mode": "percent", "value": 2.0,
                    "net": 12500000.0, "amount_full": 250000, "amount": 250000}],
         "next_tier": None},
    ]},
    "commission_after_gate": 680000, "late": -20000, "new_outlets": 200000,
    "adjustments": 50000, "penalties": 150000,
    "carry_in": {"amount": 0, "from_month": None, "source": None},
    "total": 3560000, "carry_out": 0, "owed": 0,
}


@pytest.fixture
def seeded_copy(monkeypatch):
    from staff_bot.i18n import i18n

    monkeypatch.setattr(i18n, "translations", {
        language: {key: _SEED._curated_value(key, language) for key in PAY_COPY_KEYS}
        for language in ("en", "uz", "ru")
    })


class _FakeRedis:
    """The two calls the dedup makes, under redis-py's own parameter names (the fake
    `tests/staff_bot/test_webhook_delivery_failed.py` drives the same helpers with)."""

    def __init__(self):
        self.store = {}

    async def set(self, name, value, ex=None, px=None, nx=False, xx=False):
        if nx and name in self.store:
            return None
        self.store[name] = value
        return True

    async def delete(self, *names):
        return sum(1 for name in names if self.store.pop(name, None) is not None)


@pytest.mark.usefixtures("seeded_copy")
class TestThePayPushes:
    """Both render through their own branch, every figure the backend's (C24), each through
    the formatters' pay block the My earnings screens share."""

    def _push(self, server, event, payload, language="en"):
        body = {"event_id": f"sales-event:{event}:{language}", "telegram_id": 777000111,
                "event": event, "payload": payload}
        with patch("staff_bot.webhook_server.verify_webhook_signature", AsyncMock(return_value=True)), \
             patch.object(server, "_check_rate_limit", AsyncMock(return_value=None)), \
             patch.object(server, "_is_duplicate_event", AsyncMock(return_value=False)), \
             patch("staff_bot.webhook_server.i18n.get_user_language", AsyncMock(return_value=language)):
            response = asyncio.run(server.sales_event_handler(make_request(body)))
        assert response.status == 200
        return server.bot_app.bot.send_message.await_args.kwargs

    def test_the_penalty_push_reads_as_the_spec_prints_it(self, server):
        sent = self._push(server, "pay_penalty_confirmed", PENALTY)

        assert sent["text"] == "\n".join((
            "⚠️ <b>Penalty confirmed</b>",
            "Type: Missed planned visits",
            "Date: 14.10.2026",
            f"Amount: {MINUS}150,000 UZS",
            "Counts in 10.2026",
            "Reason: Did not visit 5 planned outlets",
        ))
        (button,) = [b for row in sent["reply_markup"].inline_keyboard for b in row]
        assert (button.text, button.callback_data) == ("💰 Open my earnings", "staff_sales_earn_xn")
        assert sent["parse_mode"] == "HTML"
        assert sent["disable_notification"] is False

    def test_a_late_penalty_names_the_later_month_it_counts_in(self, server):
        late = {**PENALTY, "penalty_id": 18, "month": "2026-11", "incident_date": "2026-10-29",
                "amount": 90000, "is_late": True}

        lines = self._push(server, "pay_penalty_confirmed", late)["text"].split("\n")

        assert lines[2:5] == ["Date: 29.10.2026", f"Amount: {MINUS}90,000 UZS", "Counts in 11.2026 (late)"]

    def test_the_penalty_push_in_russian(self, server):
        sent = self._push(server, "pay_penalty_confirmed", PENALTY, language="ru")

        # The reason is the admin's free text and is echoed as written, escaped.
        assert sent["text"] == "\n".join((
            "⚠️ <b>Штраф подтверждён</b>",
            "Тип: Пропущенные плановые визиты",
            "Дата: 14.10.2026",
            f"Сумма: {MINUS}150,000 сум",
            "Учитывается в 10.2026",
            "Причина: Did not visit 5 planned outlets",
        ))
        assert sent["reply_markup"].inline_keyboard[0][0].text == "💰 Открыть мой заработок"

    def test_a_reason_is_html_escaped(self, server):
        """T-HTML-1: the reason is typed by an admin, and the push is sent with parse_mode=HTML.
        One raw '<' and Telegram refuses the whole message."""
        hostile = {**PENALTY, "reason": '<a href="https://x">Open</a> & <b>'}

        text = self._push(server, "pay_penalty_confirmed", hostile)["text"]

        assert text.split("\n")[-1] == 'Reason: &lt;a href="https://x"&gt;Open&lt;/a&gt; &amp; &lt;b&gt;'

    def test_the_statement_push_reads_as_the_spec_prints_it(self, server):
        sent = self._push(server, "pay_statement_approved", STATEMENT)

        assert sent["text"] == "\n".join((
            "✅ <b>Pay for 10.2026 is approved</b>",
            "Base: 2,800,000 UZS",
            "Commission: 850,000 UZS",
            "   19L: 500 units · 600,000 UZS",
            "      1–300: 300 × 1,000 = 300,000",
            "      301+: 200 × 1,500 = 300,000",
            "   Juice 1 L: 500 units · 250,000 UZS",
            "      1+: 2% of 12,500,000 = 250,000",
            "Commission after discipline: 680,000 UZS",
            f"Corrections from earlier months: {MINUS}20,000 UZS",
            "New outlets: +200,000 UZS",
            "Adjustments: +50,000 UZS",
            f"Penalties: {MINUS}150,000 UZS",
            "<b>Total: 3,560,000 UZS</b>",
        ))
        (button,) = [b for row in sent["reply_markup"].inline_keyboard for b in row]
        # That month's Statement, as a new message under the push (`_sn_`, M9).
        assert (button.text, button.callback_data) == ("📄 Open statement", "staff_sales_earn_sn_202610")
        assert sent["disable_notification"] is False

    def test_a_trial_statement_says_it_is_not_paid(self, server):
        """The trial September (I-16): the headline says "(not paid)", and the push ends with the
        trial month's own note, the words My earnings prints for it. Its button opens that month."""
        shadow = {**STATEMENT, "statement_id": 56, "month": "2026-09", "is_shadow": True}

        sent = self._push(server, "pay_statement_approved", shadow)
        lines = sent["text"].split("\n")

        assert lines[0] == "✅ <b>Trial statement for 09.2026 is approved (not paid)</b>"
        assert lines[-2:] == [
            "<b>Total: 3,560,000 UZS</b>",
            "Trial month: these numbers are shown for review; pay is made the old way.",
        ]
        (button,) = [b for row in sent["reply_markup"].inline_keyboard for b in row]
        assert (button.text, button.callback_data) == ("📄 Open statement", "staff_sales_earn_sn_202609")

    def test_a_trial_statement_in_russian_ends_with_its_note(self, server):
        shadow = {**STATEMENT, "statement_id": 62, "month": "2026-09", "is_shadow": True}

        sent = self._push(server, "pay_statement_approved", shadow, language="ru")

        assert sent["text"].split("\n")[-1] == _SEED._curated_value("staff.sales.earnings.shadow_note", "ru")
        assert sent["reply_markup"].inline_keyboard[0][0].text == "📄 Открыть расчётный лист"

    def test_the_statement_push_in_russian_with_distinct_amounts(self, server):
        """Every figure distinct, so a field printed on the wrong row cannot pass. The zero
        corrections, new-outlet and penalty rows are skipped, as on the summary; the commission
        prints its tier block in Russian."""
        product = {"product_id": 3, "product_name": NAMES_19L, "uses_default_tiers": False,
                   "units": 443, "total": 514500,
                   "tiers": [
                       {"from_unit": 1, "to_unit": 300, "units": 300, "mode": "per_unit", "value": 1000.0,
                        "net": 6000000.0, "amount_full": 300000, "amount": 300000},
                       {"from_unit": 301, "to_unit": None, "units": 143, "mode": "per_unit", "value": 1500.0,
                        "net": 2860000.0, "amount_full": 214500, "amount": 214500},
                   ],
                   "next_tier": None}
        statement = {**STATEMENT, "statement_id": 57, "base": 2500000,
                     "commission": {"gross": 514500, "products": [product]},
                     "commission_after_gate": 412345, "late": 0, "new_outlets": 0, "adjustments": -30000,
                     "penalties": 0, "total": 2882345}

        text = self._push(server, "pay_statement_approved", statement, language="ru")["text"]

        assert text == "\n".join((
            "✅ <b>Оплата за 10.2026 утверждена</b>",
            "Оклад: 2,500,000 сум",
            "Комиссия: 514,500 сум",
            "   19 л: 443 шт. · 514,500 сум",
            "      1–300: 300 × 1,000 = 300,000",
            "      301+: 143 × 1,500 = 214,500",
            "Комиссия с учётом дисциплины: 412,345 сум",
            f"Корректировки: {MINUS}30,000 сум",
            "<b>Итого: 2,882,345 сум</b>",
        ))

    def test_a_carried_shortfall_reads_before_and_after_the_total(self, server):
        """§7.5: a negative `carry_in` from `carry_forward` adds its row before the total, and
        a negative `carry_out` adds `carry_note` after it (the "below 0" month)."""
        statement = {**STATEMENT, "statement_id": 58, "base": 400000,
                     "commission": {"gross": 50000, "products": [_single_tier_19l(50, 50000, 1000000.0)]},
                     "commission_after_gate": 50000,
                     "late": 0, "new_outlets": 0, "adjustments": 0, "penalties": 600000,
                     "carry_in": {"amount": -120000, "from_month": "2026-09", "source": "carry_forward"},
                     "total": 0, "carry_out": -270000, "owed": 0}

        text = self._push(server, "pay_statement_approved", statement)["text"]

        assert text == "\n".join((
            "✅ <b>Pay for 10.2026 is approved</b>",
            "Base: 400,000 UZS",
            "Commission: 50,000 UZS",
            "   19L: 50 units · 50,000 UZS",
            "      1+: 50 × 1,000 = 50,000",
            "Commission after discipline: 50,000 UZS",
            f"Penalties: {MINUS}600,000 UZS",
            f"Shortfall from 09.2026: {MINUS}120,000 UZS",
            "<b>Total: 0 UZS</b>",
            "Shortfall of 270,000 UZS will be deducted next month.",
        ))

    def test_an_owed_balance_reads_before_and_after_the_total(self, server):
        """I-28: a negative `carry_in` whose `source` is `owed` is labelled as owed, and a
        positive `owed` is the agent's new outstanding balance, said under the total."""
        statement = {**STATEMENT, "statement_id": 59, "month": "2026-11", "base": 900000,
                     "commission": {"gross": 100000, "products": [_single_tier_19l(100, 100000, 2000000.0)]},
                     "commission_after_gate": 100000, "late": 0, "new_outlets": 0, "adjustments": 0,
                     "penalties": 1200000,
                     "carry_in": {"amount": -80000, "from_month": "2026-08", "source": "owed"},
                     "total": 0, "carry_out": 0, "owed": 280000}

        text = self._push(server, "pay_statement_approved", statement)["text"]

        assert text == "\n".join((
            "✅ <b>Pay for 11.2026 is approved</b>",
            "Base: 900,000 UZS",
            "Commission: 100,000 UZS",
            "   19L: 100 units · 100,000 UZS",
            "      1+: 100 × 1,000 = 100,000",
            "Commission after discipline: 100,000 UZS",
            f"Penalties: {MINUS}1,200,000 UZS",
            f"Owed from 08.2026: {MINUS}80,000 UZS",
            "<b>Total: 0 UZS</b>",
            "Owed: 280,000 UZS. It will be deducted from your later earnings.",
        ))

    def test_a_frozen_statement_never_prints_a_next_tier(self, server):
        """§7.5: the push draws its tiers through `format_pay_tiers` without the next tier, which
        only an open estimate has, so a product that carried one anyway prints none. A tier paid
        on less money says what it counted, as on the summary (T-TIER-5's October, frozen)."""
        half_paid = {"product_id": 3, "product_name": NAMES_19L, "uses_default_tiers": False,
                     "units": 700, "total": 687500,
                     "tiers": [
                         {"from_unit": 1, "to_unit": 500, "units": 500, "mode": "per_unit", "value": 1000.0,
                          "net": 10000000.0, "amount_full": 500000, "amount": 500000},
                         {"from_unit": 501, "to_unit": 1000, "units": 200, "mode": "per_unit", "value": 1500.0,
                          "net": 4000000.0, "amount_full": 300000, "amount": 187500},
                     ],
                     "next_tier": {"from_unit": 1001, "units_to_go": 301, "mode": "per_unit", "value": 2000.0}}
        statement = {**STATEMENT, "statement_id": 60, "commission": {"gross": 687500, "products": [half_paid]}}

        lines = self._push(server, "pay_statement_approved", statement)["text"].split("\n")

        start = lines.index("Commission: 687,500 UZS")
        assert lines[start:start + 5] == [
            "Commission: 687,500 UZS",
            "   19L: 700 units · 687,500 UZS",
            "      1–500: 500 × 1,000 = 500,000",
            "      501–1,000: 200 × 1,500 = 300,000 · counted 187,500, less money received",
            "Commission after discipline: 680,000 UZS",
        ]
        assert "🎯" not in "\n".join(lines)

    def test_a_push_past_telegrams_limit_keeps_every_product_line_and_drops_its_tiers(self, server):
        """V5-T6-R1: Telegram refuses a message over 4,096 characters, the handler answers 502 and
        every retry is refused the same way, so the agent never hears the month was approved. Here
        25 products on three tiers each put the tier block alone past the limit, so the push
        collapses each product to its product line, the summary's first "Length" step (§7.2), and
        keeps every other row: the commission, the corrections and the paid total. A push that
        fits is untouched (`test_the_statement_push_reads_as_the_spec_prints_it`)."""
        from staff_bot.utils.formatters import format_pay_tiers

        products = [
            {"product_id": index, "product_name": {"en": f"Product {index} " + "Ö" * 40},
             "uses_default_tiers": False, "units": 300, "total": 450000,
             "tiers": [
                 {"from_unit": 1, "to_unit": 100, "units": 100, "mode": "per_unit", "value": 1000.0,
                  "net": 2000000.0, "amount_full": 100000, "amount": 100000},
                 {"from_unit": 101, "to_unit": 200, "units": 100, "mode": "per_unit", "value": 1500.0,
                  "net": 2000000.0, "amount_full": 150000, "amount": 150000},
                 {"from_unit": 201, "to_unit": None, "units": 100, "mode": "per_unit", "value": 2000.0,
                  "net": 2000000.0, "amount_full": 200000, "amount": 200000},
             ],
             "next_tier": None}
            for index in range(1, 26)
        ]
        statement = {**STATEMENT, "statement_id": 61, "commission": {"gross": 11250000, "products": products}}
        full = [row for product in products for row in format_pay_tiers(product, "en", with_next=False)]
        assert len("\n".join(full)) > 4096, "the fixture must need the collapse"

        sent = self._push(server, "pay_statement_approved", statement)

        lines = sent["text"].split("\n")
        assert len(sent["text"]) <= 4096
        assert lines[:3] == ["✅ <b>Pay for 10.2026 is approved</b>", "Base: 2,800,000 UZS", "Commission: 11,250,000 UZS"]
        assert lines[3:28] == [f"   Product {index} {'Ö' * 40}: 300 units · 450,000 UZS" for index in range(1, 26)]
        assert lines[28:] == [
            "Commission after discipline: 680,000 UZS",
            f"Corrections from earlier months: {MINUS}20,000 UZS",
            "New outlets: +200,000 UZS",
            "Adjustments: +50,000 UZS",
            f"Penalties: {MINUS}150,000 UZS",
            "<b>Total: 3,560,000 UZS</b>",
        ]
        (button,) = [b for row in sent["reply_markup"].inline_keyboard for b in row]
        assert button.callback_data == "staff_sales_earn_sn_202610"


class TestTheSalesEventDedupAndDelivery:
    """Compensation spec §7.5 points 1 and 3 (S-18, S-19)."""

    def _post(self, server, body):
        """The real dedup (in-memory, or the fake Redis a test connects), sentinel copy."""
        with patch("staff_bot.webhook_server.verify_webhook_signature", AsyncMock(return_value=True)), \
             patch.object(server, "_check_rate_limit", AsyncMock(return_value=None)), \
             patch("staff_bot.webhook_server.i18n.get_user_language", AsyncMock(return_value="en")), \
             patch("staff_bot.webhook_server.i18n.get", side_effect=_fake_i18n_get):
            response = asyncio.run(server.sales_event_handler(make_request(body)))
        return response.status, json.loads(response.text)

    @pytest.mark.parametrize("event, payload, key", [
        ("pay_penalty_confirmed", PENALTY, "sales:pay_penalty_confirmed:777000111:17"),
        ("pay_statement_approved", STATEMENT, "sales:pay_statement_approved:777000111:55"),
    ])
    def test_a_pay_push_dedups_on_its_own_row(self, server, event, payload, key):
        dedup = AsyncMock(return_value=False)
        body = {"telegram_id": 777000111, "event": event, "payload": payload}
        with patch.object(server, "_is_duplicate_event", dedup):
            status, _answer = self._post(server, body)

        assert status == 200
        assert dedup.await_args.args[1] == key

    def test_two_penalties_confirmed_on_one_day_are_two_pushes_with_redis_down(self, server):
        """Point 1: neither pay payload carries an order, an outlet or a `date`, so a key
        without `penalty_id` collapsed the second penalty into the first."""
        first = {"telegram_id": 777000111, "event": "pay_penalty_confirmed", "payload": PENALTY}
        second = {**first, "payload": {**PENALTY, "penalty_id": 18, "amount": 40000}}

        assert self._post(server, first) == (200, {"success": True, "message": "Notification sent"})
        assert self._post(server, second) == (200, {"success": True, "message": "Notification sent"})
        assert server.bot_app.bot.send_message.await_count == 2

    @pytest.mark.parametrize("redis_up", [False, True], ids=["in-memory", "redis"])
    @pytest.mark.parametrize("event", SALES_EVENTS)
    def test_a_failed_send_releases_the_slot_so_the_retry_delivers(self, server, event, redis_up):
        """Point 3, for EVERY sales event: the slot is claimed before the send, and
        `push_sales_event` retries only on a non-200. A failed send that kept the slot would
        answer the retry "Already processed": the message lost, and reported delivered."""
        redis = _FakeRedis() if redis_up else None
        server._redis, server._redis_connected = redis, redis_up
        server.bot_app.bot.send_message = AsyncMock(side_effect=[RuntimeError("Bad Gateway"), None])
        body = {"event_id": f"sales-event:{event}:1", "telegram_id": 777000111,
                "event": event, "payload": dict(GENERIC_PAYLOAD)}

        first = self._post(server, body)
        after_failure = dict(redis.store) if redis_up else None
        retry = self._post(server, body)
        replay = self._post(server, body)

        assert first == (502, {"success": False, "message": "Send failed"})
        assert retry == (200, {"success": True, "message": "Notification sent"})
        assert replay == (200, {"success": True, "message": "Already processed"})
        assert server.bot_app.bot.send_message.await_count == 2
        if redis_up:
            assert after_failure == {}
            assert list(redis.store) == [RedisKeyspace.staff_bot_webhook_event(body["event_id"])]

    def test_an_unreachable_recipient_is_a_200_that_keeps_the_slot(self, server):
        """Nobody can reach an agent who blocked the bot, so the backend must not retry."""
        server.bot_app.bot.send_message = AsyncMock(
            side_effect=Exception("Forbidden: bot was blocked by the user")
        )
        body = {"event_id": "sales-event:statement-approved:55", "telegram_id": 777000111,
                "event": "pay_statement_approved", "payload": STATEMENT}

        first = self._post(server, body)
        replay = self._post(server, body)

        assert first == (200, {"success": False, "message": "Recipient unreachable"})
        assert replay == (200, {"success": True, "message": "Already processed"})
        assert server.bot_app.bot.send_message.await_count == 1
