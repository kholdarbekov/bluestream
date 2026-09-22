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
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
        payload = {
            "event_id": f"sales:{event}",
            "telegram_id": 777000111,
            "event": event,
            "payload": {"outlet_id": 5, "outlet_name": "Bahor", "order_id": 9,
                        "order_number": "SA_000413_26", "reason": "Incomplete"},
        }
        with patch("staff_bot.webhook_server.verify_webhook_signature", AsyncMock(return_value=True)), \
             patch.object(server, "_check_rate_limit", AsyncMock(return_value=None)), \
             patch.object(server, "_is_duplicate_event", AsyncMock(return_value=False)), \
             patch("staff_bot.webhook_server.i18n.get_user_language", AsyncMock(return_value="en")), \
             patch("staff_bot.webhook_server.i18n.get", side_effect=_fake_i18n_get):
            response = asyncio.run(server.sales_event_handler(make_request(payload)))

        assert response.status == 200
        text = server.bot_app.bot.send_message.await_args.kwargs["text"]
        assert text.startswith(f"staff.sales.notify.{event}|")

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

    def test_the_digest_is_the_one_sales_push_that_makes_a_sound(self, server):
        """A silent 08:30 message is a digest nobody reads.

        Every other sales event is `disable_notification=True` — they arrive
        while the agent is already looking at the phone.
        """
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
