"""The morning digest (spec §9.2, plan Task 22b): one email per admin and manager with new live
escalations, the top five in worklist order, sent once even when a run is redelivered, and only in
mode `on` (F5).

Two layers, like the registration tests of Task 22a. Most tests spy on the digest's own sends
(`digest_calls`): the exact arguments of every call, bound to the real method's signature. The
brace, language and wiring tests drive the REAL send path (`live_send_path`: only `requests.post`
is faked), because the leftover-placeholder guard that braces trip lives there.
"""

import ast
import inspect
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from business_app.models.notification import Notification
from business_app.models.reorder_reminder import ESCALATION_FLAGS, ReorderReminderEpisode
from business_app.models.translation import Translation
from business_app.models.user import User
from business_app.services.notification_service import NotificationService
from business_app.services.reorder_reminders import digest as digest_module
from business_app.services.reorder_reminders.digest import send_escalation_digest
from business_app.services.reorder_reminders.worklist import ReorderReminderWorklist
from business_app.tasks import reorder_reminder_tasks
from business_app.utils.constants import NotificationChannel, NotificationType
from business_app.utils.timezone_utils import ensure_utc
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS
from shared.enums import UserRole, UserType
from tests.integration.reorder_reminder_builders import (  # noqa: F401 - the `*_fixture` names are pytest fixtures
    WEEKLY,
    WORKLIST_NOW,
    captured_logs,
    clock_fixture,
    customer_with_deliveries,
    day,
    frozen_fixture,
    lapses,
    lock_fixture,
    mode_fixture,
    seed_translations,
    sends_fixture,
    staff_episode,
)
from tests.notification_send_fakes import BREVO_URL, live_send_path

pytestmark = pytest.mark.integration

BOT_URL = "https://t.me/aqua_digest_test_bot"
RUN = reorder_reminder_tasks.run_daily_reminders


class _Calls(list):
    """The recorded digest sends, plus the outcome every send reports (a test can flip it, or set
    one for a single recipient in `per_user`: a dict to report, or an exception to raise).

    `on_send(user_id)` runs inside each send (something that happens while the mail is going out);
    `probe()` is asked at each send and its answers are kept in `probed` (a fact about the moment
    of sending)."""

    outcome = None
    per_user = None
    on_send = None
    probe = None
    probed = None


@pytest.fixture(autouse=True)
def digest_mode(mode):
    """The digest is a mode `on` feature (F5): every test runs there unless it says otherwise."""
    mode("on")


@pytest.fixture
def digest_calls(monkeypatch, block_external_side_effects):
    """Record every digest send with its exact arguments.

    Each call is bound (`inspect.signature(...).bind`) to the signature of the autouse conftest
    stub that `block_external_side_effects` has patched onto `NotificationService.send_notification`.
    That stub mirrors the real method, `email_headers` (Task 18) included, so a call the real
    method would refuse raises TypeError here too instead of being silently accepted. Every other
    notification type goes on to the stub.
    """
    stub = NotificationService.send_notification
    signature = inspect.signature(stub)
    calls = _Calls()
    calls.outcome = {"success": True}
    calls.per_user = {}
    calls.probed = []

    def _spy(self, *args, **kwargs):
        bound = signature.bind(self, *args, **kwargs)
        bound.apply_defaults()
        sent = bound.arguments
        if sent["notification_type"] != NotificationType.REORDER_ESCALATION_DIGEST:
            return stub(self, *args, **kwargs)
        calls.append(
            {
                "user_id": sent["user_id"],
                "channels": sent["channels"],
                "template_data": sent["template_data"],
                "email_headers": sent["email_headers"],
            }
        )
        if calls.probe:
            calls.probed.append(calls.probe())
        if calls.on_send:
            calls.on_send(sent["user_id"])
        outcome = calls.per_user.get(sent["user_id"], calls.outcome)
        if isinstance(outcome, Exception):
            raise outcome
        return {NotificationChannel.EMAIL.value: dict(outcome)}

    monkeypatch.setattr(NotificationService, "send_notification", _spy)
    return calls


@pytest.fixture
def live_send(app, monkeypatch):
    """The production send path with only `requests.post` faked. Yields the fake."""
    with live_send_path(app, monkeypatch, bot_url=BOT_URL) as post:
        yield post


def _escalate(db, index, deliveries, *, flag=None, overdue=None, **customer_fields):
    """One live escalation: a customer with `deliveries` lifetime deliveries, `overdue` days past due
    on 15 October (day 227). The customer is named `Mijoz<index> Test` unless the fields say."""
    customer_fields.setdefault("first_name", f"Mijoz{index}")
    customer, orders = customer_with_deliveries(
        db, [227 - 60 - 20 * back for back in range(deliveries, 0, -1)], **customer_fields
    )
    return staff_episode(
        db,
        customer,
        anchor=orders[-1],
        status="escalated",
        escalation_flag=flag or ("long_lapsed" if index % 2 == 0 else "no_response"),
        # A LOCAL day, as the engine writes `due_on`.
        due_on=day(227 - (10 + index if overdue is None else overdue)),
    )


@pytest.fixture
def escalations(db):
    """Six live escalations on 15 October (day 227): lifetime deliveries 6..1, so worklist order
    is creation order, and customer `index` is 10 + index days overdue."""
    return [_escalate(db, index, deliveries) for index, deliveries in enumerate((6, 5, 4, 3, 2, 1))]


def _stamped(db, episodes):
    """Which of `episodes` carry a `digested_at`, read fresh from the database."""
    db.session.expire_all()
    return [db.session.get(ReorderReminderEpisode, e.id).digested_at is not None for e in episodes]


def test_one_email_per_admin_and_manager_with_the_top_five_in_worklist_order(
    app, db, monkeypatch, admin_user, manager_user, escalations, digest_calls
):
    monkeypatch.setitem(app.config, "ADMIN_UI_BASE_URL", "https://admin.example.test/")
    db.session.add_all(
        [
            Translation(key="ui.lapsed.flag.long_lapsed", language="ru", value="Давно без заказов", category="ui"),
            Translation(key="ui.lapsed.flag.long_lapsed", language="en", value="Long lapsed", category="ui"),
            Translation(key="ui.lapsed.flag.long_lapsed", language="uz", value="Uzoq vaqt buyurtma yo'q", category="ui"),
        ]
    )
    db.session.commit()

    sent = send_escalation_digest(now=WORKLIST_NOW)

    assert sent == 2
    assert [call["user_id"] for call in digest_calls] == sorted([admin_user.id, manager_user.id])
    # The channel is explicit: never a preference lookup that could drop the digest.
    assert all(call["channels"] == [NotificationChannel.EMAIL] for call in digest_calls)
    # A staff summary: no List-Unsubscribe headers (those belong to the customer reminder email).
    assert all(call["email_headers"] is None for call in digest_calls)
    data = digest_calls[0]["template_data"]
    assert data["escalated_count"] == 6
    assert data["worklist_url"] == "https://admin.example.test/lapsed-customers"
    # `outlet_service.user_display_name` (amendment 19): no company, so the full name.
    assert [row["name"] for row in data["top"]] == [f"Mijoz{index} Test" for index in range(5)]
    assert [row["flag"] for row in data["top"]] == ["long_lapsed", "no_response", "long_lapsed", "no_response", "long_lapsed"]
    assert [row["days_overdue"] for row in data["top"]] == [10, 11, 12, 13, 14]
    assert all(type(row["days_overdue"]) is int for row in data["top"])
    assert all(row["name"] for row in data["top"])
    assert data["flag_labels"]["ru"]["long_lapsed"] == "Давно без заказов"
    assert set(data["flag_labels"]) == {"uz", "ru", "en"}
    assert digest_calls[1]["template_data"] == data
    # Every counted row is digested, the one beyond the top five included: the count covered it.
    assert _stamped(db, escalations) == [True] * 6


def test_the_top_five_are_the_first_five_rows_of_the_worklist_page(db, admin_user, digest_calls, frozen):
    """Created fewest deliveries first, so creation order is the REVERSE of worklist order; and two
    customers tie on deliveries, so days overdue decides between them. The digest names exactly the
    rows, in the order, of the real worklist's first page."""
    for index, (deliveries, overdue) in enumerate([(1, 40), (2, 12), (3, 12), (3, 30), (4, 5), (5, 5), (6, 5)]):
        _escalate(db, index, deliveries, overdue=overdue)

    assert send_escalation_digest(now=WORKLIST_NOW) == 1

    page, total = ReorderReminderWorklist.list_rows(tab="follow_up", flag=None, search=None, page=1, per_page=5)
    top = digest_calls[0]["template_data"]["top"]
    assert total == digest_calls[0]["template_data"]["escalated_count"] == 7
    assert [(row["name"], row["days_overdue"]) for row in top] == [
        (row["recipient"]["name"], row["days_overdue"]) for row in page
    ]
    assert [row["name"] for row in top] == [f"Mijoz{index} Test" for index in (6, 5, 4, 3, 2)]


def test_a_second_digest_sends_nothing_new_until_a_reescalation_clears_the_stamp(
    db, admin_user, escalations, digest_calls
):
    first = send_escalation_digest(now=WORKLIST_NOW)
    second = send_escalation_digest(now=WORKLIST_NOW + timedelta(minutes=5))
    assert (first, second) == (1, 0)
    assert len(digest_calls) == 1

    # Amendment 18: an outlet-dormant re-escalation clears `digested_at` (Task 11a), so it is new.
    reescalated = db.session.get(ReorderReminderEpisode, escalations[2].id)
    reescalated.digested_at = None
    db.session.commit()
    third = send_escalation_digest(now=WORKLIST_NOW + timedelta(days=1))

    assert third == 1
    assert [call["template_data"]["escalated_count"] for call in digest_calls] == [6, 1]
    assert [row["name"] for row in digest_calls[1]["template_data"]["top"]] == ["Mijoz2 Test"]


def test_dry_run_rows_never_reach_the_digest(db, admin_user, digest_calls):
    simulated = _escalate(db, 0, 1, flag="no_response")
    simulated.is_dry_run = True
    db.session.commit()

    assert send_escalation_digest(now=WORKLIST_NOW) == 0
    assert digest_calls == []
    assert _stamped(db, [simulated]) == [False]


def test_dry_run_rows_are_left_alone_beside_live_ones(db, admin_user, digest_calls):
    live = _escalate(db, 0, 3)
    simulated = _escalate(db, 1, 2)
    simulated.is_dry_run = True
    db.session.commit()

    assert send_escalation_digest(now=WORKLIST_NOW) == 1

    data = digest_calls[0]["template_data"]
    assert data["escalated_count"] == 1
    assert [row["name"] for row in data["top"]] == ["Mijoz0 Test"]
    assert _stamped(db, [live, simulated]) == [True, False]


@pytest.mark.parametrize("mode_name", ["dry_run", "off"])
def test_nothing_goes_out_outside_mode_on_and_no_row_is_touched(
    db, admin_user, escalations, digest_calls, mode, mode_name
):
    """F5, on `send_escalation_digest` itself (not only through the daily run): in `dry_run` and
    `off` it returns 0, sends nothing and stamps nothing, live rows and simulated rows alike."""
    simulated = _escalate(db, 7, 2)
    simulated.is_dry_run = True
    db.session.commit()
    mode(mode_name)

    assert send_escalation_digest(now=WORKLIST_NOW) == 0

    assert digest_calls == []
    assert _stamped(db, [*escalations, simulated]) == [False] * 7


def test_nothing_is_stamped_when_every_recipient_failed(db, admin_user, manager_user, escalations, digest_calls):
    """Marking rule: a row is stamped only when at least one successful send carried it. Every
    recipient refusing (Brevo down, a skipped send) stamps nothing, so tomorrow's digest has it."""
    digest_calls.outcome = {"success": False, "skipped": True, "reason": "unrendered_placeholders"}

    assert send_escalation_digest(now=WORKLIST_NOW) == 0

    assert len(digest_calls) == 2
    assert _stamped(db, escalations) == [False] * 6


def test_nothing_is_stamped_when_every_send_raises(db, admin_user, manager_user, escalations, digest_calls):
    digest_calls.outcome = RuntimeError("provider exploded")

    assert send_escalation_digest(now=WORKLIST_NOW) == 0

    assert len(digest_calls) == 2
    assert _stamped(db, escalations) == [False] * 6


@pytest.mark.parametrize(
    "failure",
    [{"success": False, "error": "brevo 503"}, RuntimeError("provider exploded")],
    ids=["refused", "raised"],
)
def test_one_failed_recipient_costs_the_others_nothing_and_the_rows_are_stamped(
    db, admin_user, manager_user, escalations, digest_calls, failure
):
    """Partial failure: the first recipient's send fails, the second's goes out. The digest reached
    somebody, so every row it counted is stamped; the failed recipient is not retried tomorrow
    (one `digested_at` per row cannot say who has read it), and the count is the emails sent."""
    digest_calls.per_user[admin_user.id] = failure

    assert send_escalation_digest(now=WORKLIST_NOW) == 1

    assert [call["user_id"] for call in digest_calls] == [admin_user.id, manager_user.id]
    assert _stamped(db, escalations) == [True] * 6


def test_no_escalations_means_no_email(db, admin_user, digest_calls):
    assert send_escalation_digest(now=WORKLIST_NOW) == 0
    assert digest_calls == []


def test_escalations_without_a_recipient_stay_undigested_for_the_day_one_exists(db, escalations, digest_calls):
    """No admin or manager has a deliverable email: nothing is sent and nothing is stamped, so the
    first morning after somebody does, the backlog goes out."""
    assert send_escalation_digest(now=WORKLIST_NOW) == 0
    assert _stamped(db, escalations) == [False] * 6

    db.session.add(
        User(
            email="late-admin@example.com",
            phone="+998901239111",
            password_hash="x",
            first_name="Late",
            last_name="Admin",
            role=UserRole.ADMIN,
            user_type=UserType.STAFF,
            is_verified=True,
        )
    )
    db.session.commit()

    assert send_escalation_digest(now=WORKLIST_NOW + timedelta(days=1)) == 1
    assert digest_calls[0]["template_data"]["escalated_count"] == 6


def test_an_account_without_any_name_is_still_a_named_row(db, admin_user, digest_calls):
    """`user_display_name` ends at the phone, so a nameless account is never a blank cell."""
    episode = _escalate(db, 0, 2, first_name="Nobody")
    customer = db.session.get(User, episode.user_id)
    customer.first_name, customer.last_name, customer.company_name = None, None, None
    db.session.commit()

    send_escalation_digest(now=WORKLIST_NOW)

    (row,) = digest_calls[0]["template_data"]["top"]
    assert row["name"] == customer.phone


def test_an_episode_whose_account_row_is_gone_is_named_by_id_and_does_not_stop_the_digest(
    db, admin_user, digest_calls
):
    """A dangling `user_id` cannot exist on Postgres (RESTRICT), but the test database has foreign
    keys off, and one bad row must not cost every admin the morning's digest."""
    orphan = _escalate(db, 0, 3)
    healthy = _escalate(db, 1, 2)
    user_id = orphan.user_id
    # A bulk delete, so the ORM does not null the account's orders on the way (it would only fail).
    User.query.filter_by(id=user_id).delete(synchronize_session=False)
    db.session.commit()

    assert send_escalation_digest(now=WORKLIST_NOW) == 1

    names = [row["name"] for row in digest_calls[0]["template_data"]["top"]]
    # The orphan has no cluster left, hence no deliveries, so the worklist order puts it last.
    assert names == ["Mijoz1 Test", f"#{user_id}"]
    assert _stamped(db, [orphan, healthy]) == [True, True]


def test_flag_labels_are_the_seeded_rows_and_never_a_raw_key_when_unseeded(db, admin_user, digest_calls):
    """With the `ui.lapsed.flag.*` rows seeded the labels are the seed's text, in each language.
    Unseeded, a lookup returns the KEY itself; the digest never mails that: it falls back to the
    humanised key (`shared.i18n_rendering.humanise_key`, the rule the reminder composer degrades
    by), so a missed seed costs a plain English label, not `ui.lapsed.flag.long_lapsed`."""
    _escalate(db, 0, 2, flag="long_lapsed")
    send_escalation_digest(now=WORKLIST_NOW)
    unseeded = digest_calls[0]["template_data"]["flag_labels"]

    assert set(unseeded) == {"uz", "ru", "en"}
    for labels in unseeded.values():
        assert set(labels) == set(ESCALATION_FLAGS)
        assert all(label and "ui.lapsed" not in label and "." not in label for label in labels.values())
    assert unseeded["en"]["long_lapsed"] == "Long lapsed"
    assert unseeded["ru"]["no_response"] == "No response"
    assert unseeded["uz"]["outlet_dormant"] == "Outlet dormant"

    seed_translations(db, [f"ui.lapsed.flag.{flag}" for flag in ESCALATION_FLAGS])
    again = _escalate(db, 1, 2, flag="stopped")
    assert send_escalation_digest(now=WORKLIST_NOW + timedelta(days=1)) == 1
    seeded = digest_calls[1]["template_data"]["flag_labels"]

    assert seeded == {
        language: {flag: BACKEND_TRANSLATIONS[f"ui.lapsed.flag.{flag}"][language] for flag in ESCALATION_FLAGS}
        for language in ("uz", "ru", "en")
    }
    assert _stamped(db, [again]) == [True]


def test_a_row_stamped_while_the_mail_was_going_out_keeps_its_first_stamp(db, admin_user, escalations, digest_calls):
    """The stamp is conditional on `digested_at IS NULL`: a row another digest stamped meanwhile
    keeps that first timestamp; the rest get this digest's."""
    earlier = datetime(2026, 10, 15, 4, 0, tzinfo=timezone.utc)  # an hour before WORKLIST_NOW
    rival = escalations[1].id

    def stamped_elsewhere(_user_id):
        ReorderReminderEpisode.query.filter_by(id=rival).update({"digested_at": earlier})
        db.session.commit()

    digest_calls.on_send = stamped_elsewhere

    assert send_escalation_digest(now=WORKLIST_NOW) == 1

    db.session.expire_all()
    stamps = {e.id: ensure_utc(db.session.get(ReorderReminderEpisode, e.id).digested_at) for e in escalations}
    assert stamps[rival] == earlier
    assert {stamp for episode_id, stamp in stamps.items() if episode_id != rival} == {ensure_utc(WORKLIST_NOW)}


def test_a_stamp_that_fails_after_the_mail_went_out_is_logged_with_the_email_count(
    db, admin_user, manager_user, escalations, digest_calls, monkeypatch, caplog
):
    def stamp_exploded(_ids, _stamp):
        raise RuntimeError("stamp exploded")

    monkeypatch.setattr(digest_module, "_stamp", stamp_exploded)

    with captured_logs(caplog, digest_module.logger), pytest.raises(RuntimeError, match="stamp exploded"):
        send_escalation_digest(now=WORKLIST_NOW)

    assert [r.getMessage() for r in caplog.records if "already went out" in r.getMessage()] == [
        "Reorder escalation digest: 2 emails already went out, but stamping 6 escalations failed; "
        "they will be digested again"
    ]
    assert _stamped(db, escalations) == [False] * 6


# --- the real send path -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "fields, shown",
    [
        ({"first_name": "{evil}"}, "｛evil｝ Test"),
        ({"first_name": "Ali", "last_name": "{x}"}, "Ali ｛x｝"),
        ({"company_name": "{Acme} LLC"}, "｛Acme｝ LLC"),
    ],
    ids=["first-name", "last-name", "company"],
)
def test_a_customer_name_with_braces_still_mails_with_the_braces_neutralised(
    db, admin_user, live_send, fields, shown
):
    """Ruling T22a: the digest type is placeholder-strict, so a literal `{evil}` in a customer's
    name would make the guard skip the WHOLE digest, and again every morning while that row stays in
    the top five. The braces reach the email as the full-width ones, and the digest goes out."""
    episode = _escalate(db, 0, 2, **fields)

    assert send_escalation_digest(now=WORKLIST_NOW) == 1

    live_send.assert_called_once()
    assert live_send.call_args.args == (BREVO_URL,)
    html = live_send.call_args.kwargs["json"]["htmlContent"]
    cells = [cell.get_text(strip=True) for cell in BeautifulSoup(html, "html.parser").select("table.details-table td")]
    assert cells[0] == shown
    assert "{" not in "".join(cells) and "}" not in "".join(cells)
    assert _stamped(db, [episode]) == [True]
    assert Notification.query.filter_by(notification_type="reorder_escalation_digest").count() == 1


def test_each_recipient_reads_the_digest_in_their_own_language_through_the_real_send_path(
    db, admin_user, manager_user, live_send
):
    admin_user.preferred_language, manager_user.preferred_language = "ru", "en"
    db.session.commit()
    seed_translations(db, [f"ui.lapsed.flag.{flag}" for flag in ESCALATION_FLAGS])
    _escalate(db, 0, 3, flag="long_lapsed")

    assert send_escalation_digest(now=WORKLIST_NOW) == 2

    sent = {call.kwargs["json"]["to"][0]["email"]: call.kwargs["json"] for call in live_send.call_args_list}
    assert sent["admin@example.com"]["subject"] == "Пропавшие клиенты (новых: 1) - Aqua Element"
    assert sent["sales-manager@example.com"]["subject"] == "Lapsed customers: 1 new to follow up - Aqua Element"
    reasons = {
        email: BeautifulSoup(body["htmlContent"], "html.parser").select("table.details-table td")[2].get_text(strip=True)
        for email, body in sent.items()
    }
    assert reasons == {
        "admin@example.com": BACKEND_TRANSLATIONS["ui.lapsed.flag.long_lapsed"]["ru"],
        "sales-manager@example.com": BACKEND_TRANSLATIONS["ui.lapsed.flag.long_lapsed"]["en"],
    }
    hrefs = {a["href"] for a in BeautifulSoup(sent["admin@example.com"]["htmlContent"], "html.parser").find_all("a")}
    assert "https://admin.aqua-element.uz/lapsed-customers" in hrefs


@pytest.mark.parametrize("edited", ["Long {lapsed}", "{{lapsed}}"], ids=["placeholder", "escaped-braces"])
def test_an_admin_edited_label_with_a_placeholder_never_skips_the_digest(db, admin_user, live_send, edited):
    """The labels are admin-editable translations. One edited into `Long {lapsed}` (or `{{lapsed}}`,
    which `.format()` turns into a literal `{lapsed}`) would trip the placeholder guard for every
    recipient, every morning: a label that still holds a placeholder reads as the humanised key."""
    db.session.add(Translation(key="ui.lapsed.flag.long_lapsed", language="en", value=edited, category="ui"))
    db.session.commit()
    episode = _escalate(db, 0, 2, flag="long_lapsed")

    assert send_escalation_digest(now=WORKLIST_NOW) == 1

    html = live_send.call_args.kwargs["json"]["htmlContent"]
    cells = [cell.get_text(strip=True) for cell in BeautifulSoup(html, "html.parser").select("table.details-table td")]
    assert cells[2] == "Long lapsed"
    assert _stamped(db, [episode]) == [True]


# --- the daily run's wiring (amendment 14) ----------------------------------------------------


def test_the_daily_run_in_mode_on_sends_the_digest_once_even_when_redelivered(
    db, lock, clock, sends, admin_user, escalations, digest_calls
):
    clock(WORKLIST_NOW)
    digest_calls.probe = lambda: bool(lock.store)

    first = RUN.run()
    second = RUN.run()

    assert (first["digest_emails"], second["digest_emails"]) == (1, 0)
    assert (first["digest_failed"], second["digest_failed"]) == (0, 0)
    assert "digest_error" not in first
    assert [call["user_id"] for call in digest_calls] == [admin_user.id]
    # The digest went out WHILE the run's lock was held (not merely before it was released)...
    assert digest_calls.probed == [True]
    assert lock.store == {}  # ...and the lock was released afterwards.


def test_the_run_result_counts_the_recipients_the_digest_could_not_reach(
    db, lock, clock, sends, admin_user, manager_user, escalations, digest_calls
):
    """Partial failure, as the run reports it: one email out, one recipient not reached."""
    digest_calls.per_user[admin_user.id] = {"success": False, "error": "brevo 503"}
    clock(WORKLIST_NOW)

    result = RUN.run()

    assert (result["digest_emails"], result["digest_failed"]) == (1, 1)
    assert "digest_error" not in result
    assert _stamped(db, escalations) == [True] * 6


@pytest.mark.parametrize("mode_name", ["dry_run", "off"])
def test_the_daily_run_sends_no_digest_outside_mode_on(
    db, lock, clock, sends, admin_user, escalations, digest_calls, mode, mode_name
):
    mode(mode_name)
    clock(WORKLIST_NOW)

    result = RUN.run()

    assert (result["digest_emails"], result["digest_failed"]) == (0, 0)
    assert digest_calls == []
    assert _stamped(db, escalations) == [False] * 6


def _newly_due_customer(db):
    """A weekly customer (last delivery on day 216) who is 11 days since it on day 227: the run
    opens her lapse and queues the first reminder, so the engine has a transition of its own to
    commit in the same run."""
    return customer_with_deliveries(db, [216 + offset for offset in WEEKLY], telegram=True)


def test_a_failing_digest_never_fails_or_undoes_the_run_it_follows(
    db, lock, clock, sends, admin_user, escalations, digest_calls, monkeypatch, caplog
):
    clock(WORKLIST_NOW)
    customer, orders = _newly_due_customer(db)

    def explode(*_args, **_kwargs):
        raise RuntimeError("digest exploded")

    with monkeypatch.context() as broken:
        broken.setattr(digest_module, "_undigested_escalations", explode)
        with captured_logs(caplog, reorder_reminder_tasks.logger):
            result = RUN.run()

    assert (result["mode"], result["digest_emails"], result["digest_failed"], result["digest_error"]) == (
        "on",
        0,
        0,
        True,
    )
    assert result["errors"] == 0  # the engine's own error count is not the digest's
    assert lock.store == {}
    assert any("escalation digest failed" in record.getMessage() for record in caplog.records)
    # The run's own work survives the fault: its lapse was opened and its reminder queued, and it is
    # still there after the session is rolled back as the app context's teardown would.
    assert result["opened"] == 1
    db.session.rollback()
    (opened,) = lapses(orders[-1])
    assert opened.status == "open" and opened.reminders_sent == 0
    assert sends == [(opened.id, 1)]

    # Fixed the next morning: the same rows are still undigested and go out.
    assert RUN.run()["digest_emails"] == 1


def test_a_digest_module_that_cannot_be_imported_never_fails_the_run(
    db, lock, clock, sends, admin_user, escalations, digest_calls, monkeypatch, caplog
):
    """The import sits inside the same guard as the call: a broken deploy of the digest module
    (an ImportError) costs the morning's digest, never the run it follows."""
    clock(WORKLIST_NOW)
    customer, orders = _newly_due_customer(db)
    monkeypatch.setitem(sys.modules, "business_app.services.reorder_reminders.digest", None)

    with captured_logs(caplog, reorder_reminder_tasks.logger):
        result = RUN.run()

    assert (result["mode"], result["opened"], result["digest_emails"], result["digest_error"]) == ("on", 1, 0, True)
    assert lock.store == {}
    assert any("escalation digest failed" in record.getMessage() for record in caplog.records)
    assert digest_calls == []
    db.session.rollback()
    assert [row.status for row in lapses(orders[-1])] == ["open"]


def test_the_task_module_imports_the_digest_lazily_inside_the_run():
    """Amendment 14: no module-level import of the digest, so the task module stays light."""
    tree = ast.parse(Path(reorder_reminder_tasks.__file__).read_text(encoding="utf-8"))
    imported = [
        node.module if isinstance(node, ast.ImportFrom) else ",".join(alias.name for alias in node.names)
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert not [name for name in imported if name and "reorder_reminders.digest" in name]
    run_source = inspect.getsource(RUN.run)
    assert "from business_app.services.reorder_reminders.digest import send_digest_after_run" in run_source
