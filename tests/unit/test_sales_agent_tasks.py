"""After-commit dispatch of sales events, and the task bodies."""

import inspect
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from celery.exceptions import Retry

from business_app.models.sales import OUTLET_STAGES, Outlet, SalesAgentProfile
from business_app.services.sales import agent_order_confirmation_service  # noqa: F401
from business_app.services.sales import notifications
from business_app.services.sales.digest_service import AgentDigestService
from business_app.services.sales.outlet_service import OutletService
from business_app.tasks import sales_agent_tasks
from shared.staff_constants import SALES_EVENT_MORNING_DIGEST, SALES_EVENTS
from tests.integration.test_outlet_create import GROCERY
from tests.unit.test_sales_agent_role import make_sales_agent_user


def _spy(monkeypatch, obj, attr, result=None):
    """Signature-enforcing spy: a wrong-arity or wrong-keyword call raises here
    instead of being silently accepted.

    For a Celery task's `.delay` the signature that carries the payload is the TASK's:
    `Task.delay(self, *args, **kwargs)` binds anything at all, so enforcing IT enforced
    nothing. `run` is the task's own function with `self` already bound on a `bind=True`
    task — which is what `push_sales_event` and `push_agent_order_confirmation` are.
    """
    real = getattr(obj, attr)
    signature = inspect.signature(obj.run if attr == "delay" else real)
    calls = []

    def fake(*args, **kwargs):
        signature.bind(*args, **kwargs)
        calls.append((args, kwargs))
        return result

    monkeypatch.setattr(obj, attr, fake)
    return calls


@pytest.fixture
def agent(db):
    user = make_sales_agent_user(db, staff_roles=["sales_agent"])
    user.telegram_id = "777000111"
    db.session.add(SalesAgentProfile(user_id=user.id, districts=["chilanzar"]))
    db.session.commit()
    return user


def test_request_activation_notifies_approvers_after_commit(db, agent, monkeypatch):
    calls = _spy(monkeypatch, notifications, "notify_activation_requested")
    outlet = OutletService.create(agent.id, dict(GROCERY))
    OutletService.request_activation(outlet, agent.id)
    assert [c[0][0].id for c in calls] == [outlet.id]


def test_approve_and_reject_notify_the_agent(db, agent, admin_user, monkeypatch):
    calls = _spy(monkeypatch, notifications, "notify_agent_event")
    outlet = OutletService.create(agent.id, dict(GROCERY))
    OutletService.request_activation(outlet, agent.id)
    OutletService.reject(outlet.id, actor_id=admin_user.id, reason="Incomplete")
    OutletService.request_activation(outlet, agent.id)
    OutletService.approve(outlet.id, actor_id=admin_user.id)
    assert [(c[0][0].id, c[0][1]) for c in calls] == [(outlet.id, "outlet_rejected"), (outlet.id, "outlet_approved")]


def test_notify_agent_event_enqueues_a_push_with_exact_payload(db, agent, monkeypatch):
    delays = _spy(monkeypatch, sales_agent_tasks.push_sales_event, "delay")
    outlet = OutletService.create(agent.id, dict(GROCERY))
    outlet.rejected_reason = "Incomplete"
    notifications.notify_agent_event(outlet, "outlet_rejected")
    assert delays == [
        (
            (777000111, "outlet_rejected", {"outlet_id": outlet.id, "outlet_name": "Bahor market", "reason": "Incomplete"}),
            {},
        )
    ]


def test_notify_agent_event_skips_agents_without_telegram(db, agent, monkeypatch):
    agent.telegram_id = None
    db.session.commit()
    delays = _spy(monkeypatch, sales_agent_tasks.push_sales_event, "delay")
    outlet = OutletService.create(agent.id, dict(GROCERY))
    notifications.notify_agent_event(outlet, "outlet_approved")
    assert delays == []


def test_push_sales_event_posts_to_the_staff_bot(monkeypatch):
    # `result=True` because a falsy answer now MEANS "the webhook failed" and retries (M29);
    # this test is about the payload, not the failure path.
    sent = _spy(monkeypatch, sales_agent_tasks, "_send_staff_webhook", result=True)
    sales_agent_tasks.push_sales_event.run(777000111, "outlet_approved", {"outlet_id": 5, "outlet_name": "X", "reason": None})
    assert sent == [
        (
            (
                "/internal/sales-event",
                {
                    "telegram_id": 777000111,
                    "event": "outlet_approved",
                    "payload": {"outlet_id": 5, "outlet_name": "X", "reason": None},
                },
            ),
            {},
        )
    ]


def test_push_sales_event_retries_a_failed_webhook(monkeypatch):
    """M29: this task is the ONLY channel that carries the store's answer back to the agent.

    Its sibling `push_agent_order_confirmation` is declared `max_retries=3,
    default_retry_delay=60` and retries a failed webhook; this one returned `{"success": False}`
    and dropped the answer on the first unreachable moment, so the agent standing at the counter
    simply never heard that the shop had confirmed or declined.
    """
    _spy(monkeypatch, sales_agent_tasks, "_send_staff_webhook", result=False)
    retries = []

    def fake_retry(*args, **kwargs):
        inspect.signature(sales_agent_tasks.push_sales_event.retry).bind(*args, **kwargs)
        retries.append((args, kwargs))
        raise Retry()

    monkeypatch.setattr(sales_agent_tasks.push_sales_event, "retry", fake_retry)

    with pytest.raises(Retry):
        sales_agent_tasks.push_sales_event.run(777000111, "agent_order_confirmed", {"order_id": 9})

    assert len(retries) == 1
    assert isinstance(retries[0][1]["exc"], RuntimeError)
    assert "agent_order_confirmed" in str(retries[0][1]["exc"])
    assert sales_agent_tasks.push_sales_event.max_retries == 3
    assert sales_agent_tasks.push_sales_event.default_retry_delay == 60


def test_a_retried_sales_event_carries_the_same_event_id_so_the_chat_gets_one_message(monkeypatch):
    """A retry must not put the same answer in the agent's chat twice.

    `_send_staff_webhook` mints a FRESH uuid `event_id` per call (staff_tasks.py:32), and the
    staff bot's dedup is a Redis `SET NX` on that id (`webhook_server.py::_is_duplicate_event`,
    pinned by tests/unit/test_staff_sales_webhook.py::test_is_idempotent_on_a_repeated_event_id)
    -- it can only collapse replays carrying the SAME id. Celery keeps `request.id` across
    `self.retry()`, so the task's own id is that key. Called directly there is no id and the
    sender's uuid stands, exactly as before.
    """
    sent = _spy(monkeypatch, sales_agent_tasks, "_send_staff_webhook", result=True)
    payload = {"outlet_id": 5, "outlet_name": "X", "order_id": 9, "order_number": "SA_1", "reason": None}

    sales_agent_tasks.push_sales_event.push_request(id="task-abc")
    try:
        sales_agent_tasks.push_sales_event.run(777000111, "agent_order_confirmed", payload)
        sales_agent_tasks.push_sales_event.run(777000111, "agent_order_confirmed", payload)
    finally:
        sales_agent_tasks.push_sales_event.pop_request()
    sales_agent_tasks.push_sales_event.run(777000111, "agent_order_confirmed", payload)

    assert [call[0][1].get("event_id") for call in sent] == [
        "sales-event:task-abc",
        "sales-event:task-abc",
        None,
    ]
    assert [call[0][1]["payload"] for call in sent] == [payload, payload, payload]


def test_managers_get_in_app_and_operators_get_a_push(db, agent, admin_user, operator_user, monkeypatch):
    from business_app.services.notification_service import NotificationService

    operator_user.telegram_id = "777000222"
    db.session.commit()
    in_app = _spy(monkeypatch, NotificationService, "send_notification")
    delays = _spy(monkeypatch, sales_agent_tasks.push_sales_event, "delay")
    outlet = OutletService.create(agent.id, dict(GROCERY))

    sales_agent_tasks.notify_managers_activation_requested.run(outlet.id)

    assert [c[1]["user_id"] for c in in_app] == [admin_user.id]
    # The admin UI renders notification `content` as PLAIN TEXT, so the IN_APP body must come from
    # the markup-free key -- `staff.sales.notify.activation_requested` carries <b>...</b> and stays
    # reserved for the staff-bot push, which sends parse_mode='HTML'. Unseeded keys render as
    # themselves, which is exactly what makes the choice of key visible here.
    override = in_app[0][1]["template_override"]
    assert override.subject == "staff.notification.subject.outlet_activation_requested"
    assert override.content == "staff.notification.content.outlet_activation_requested"
    assert override.get_translated("content", "ru") == "staff.notification.content.outlet_activation_requested"
    assert delays == [
        ((777000222, "activation_requested", {"outlet_id": outlet.id, "outlet_name": "Bahor market", "reason": None}), {})
    ]


def _manager(db):
    """A MANAGER, so the fan-out is proved to reach both roles and not just admins.

    +998901234581 is outside the conftest sequence (568-577) and outside the sales modules'
    (573-576); `users.phone` is UNIQUE, and a collision fails at setup rather than in an
    assertion.
    """
    from business_app.models.user import User
    from business_app.utils.password_security import hash_password
    from shared.enums import UserRole, UserType

    user = User(
        phone="+998901234581",
        password_hash=hash_password("ManagerPassword123!"),
        first_name="Malika",
        last_name="Manager",
        user_type=UserType.STAFF,
        role=UserRole.MANAGER,
        is_verified=True,
        created_at=datetime.now(UTC),
    )
    db.session.add(user)
    db.session.commit()
    return user


def test_the_exception_summary_counts_yesterday_and_speaks_once_per_manager(
    db, agent, admin_user, monkeypatch
):
    """One line per admin/manager, counting the PREVIOUS local day's DATED exceptions.

    Yesterday, not today: the job runs at 08:00 and a day that is still happening is not a
    day a supervisor can act on. The third visit below happened this morning and must not be
    in the number.

    The two AS-OF-NOW types are seeded here too and must NOT be counted (R9, V02/V07). They
    describe a state, not an event: a shop nobody has been to for two months is still
    unvisited tomorrow morning, so counting it would push the same number every day until
    somebody drove there — and the count would never fall back to zero, which is the one
    thing that makes the silent-at-zero branch meaningful. `DATED_EXCEPTION_TYPES` is the
    only expression of that membership, and this is the only test that can fail when it moves.
    """
    from business_app.models.sales_visits import Visit
    from business_app.models.tryout import ProductTryout, TrialContact
    from business_app.services.notification_service import NotificationService
    from business_app.services.sales.exception_feed_service import ExceptionFeedService
    from business_app.utils.constants import NotificationChannel, NotificationType
    from business_app.utils.local_windows import local_date, local_day_bounds
    from shared.enums import TryoutStatus

    manager = _manager(db)
    yesterday = local_date() - timedelta(days=1)
    yesterday_start, _ = local_day_bounds(yesterday)
    today_start, _ = local_day_bounds(local_date())

    outlet = Outlet(
        name="Bahor market",
        outlet_type="grocery_store",
        stage="active",
        assigned_agent_user_id=agent.id,
        last_visit_at=datetime.now(UTC),
    )
    db.session.add(outlet)
    # `unvisited`: an ACTIVE shop nobody has been to since the spring. True today, true
    # tomorrow — and therefore not yesterday's news.
    stale = Outlet(
        name="Chorsu counter",
        outlet_type="grocery_store",
        stage="active",
        assigned_agent_user_id=agent.id,
        last_visit_at=datetime.now(UTC) - timedelta(days=120),
    )
    db.session.add(stale)
    db.session.flush()
    # `duplicate_open_tryout`: two NON-TERMINAL try-outs at one counter, both created
    # yesterday so that a dated reading of this type would land them squarely in the window.
    contact = TrialContact(first_name="Olim", phone="+998901112277")
    db.session.add(contact)
    db.session.flush()
    db.session.add_all(
        [
            ProductTryout(
                trial_contact_id=contact.id,
                created_by_user_id=agent.id,
                source="sales_agent",
                outlet_id=stale.id,
                status=TryoutStatus.DRAFT,
                created_at=yesterday_start + timedelta(hours=13),
            ),
            ProductTryout(
                trial_contact_id=contact.id,
                created_by_user_id=agent.id,
                source="sales_agent",
                outlet_id=stale.id,
                status=TryoutStatus.ACTIVE,
                created_at=yesterday_start + timedelta(hours=14),
            ),
        ]
    )
    db.session.add_all(
        [
            Visit(
                outlet_id=outlet.id,
                agent_user_id=agent.id,
                status="completed",
                started_at=yesterday_start + timedelta(hours=9),
                checkin_at=yesterday_start + timedelta(hours=9, minutes=5),
                ended_at=yesterday_start + timedelta(hours=9, minutes=40),
                distance_m=640.0,
                in_radius=False,
            ),
            Visit(
                outlet_id=outlet.id,
                agent_user_id=agent.id,
                status="completed",
                started_at=yesterday_start + timedelta(hours=11),
                checkin_at=yesterday_start + timedelta(hours=11, minutes=2),
                ended_at=yesterday_start + timedelta(hours=11, minutes=30),
                checkin_skipped=True,
            ),
            Visit(
                outlet_id=outlet.id,
                agent_user_id=agent.id,
                status="completed",
                started_at=today_start + timedelta(hours=9),
                checkin_at=today_start + timedelta(hours=9, minutes=5),
                ended_at=today_start + timedelta(hours=9, minutes=40),
                distance_m=900.0,
                in_radius=False,
            ),
        ]
    )
    db.session.commit()

    in_app = _spy(monkeypatch, NotificationService, "send_notification")

    result = sales_agent_tasks.notify_managers_exception_summary.run()

    assert result == {"success": True, "day": yesterday.isoformat(), "count": 2, "managers": 2}
    # ...and the two as-of-now rows ARE in the estate: the count omits them by RULE, not
    # because the fixture quietly failed to create them. Without this the assertion above
    # would keep passing on an empty seed.
    feed_types = {row["type"] for row in ExceptionFeedService.list(yesterday, yesterday)[0]}
    assert {"unvisited", "duplicate_open_tryout"} <= feed_types
    assert sorted(call[1]["user_id"] for call in in_app) == sorted([admin_user.id, manager.id])
    # IN_APP and nothing else. This is a SCHEDULED fan-out to every admin and manager, so an
    # extra channel here is an email (or a Telegram push) to the whole supervisory roster every
    # single morning — the one failure mode of this task that costs more than being wrong.
    for _, kwargs in in_app:
        assert kwargs["channels"] == [NotificationChannel.IN_APP]
        assert kwargs["notification_type"] == NotificationType.SYSTEM_ALERT
    assert in_app[0][1]["template_data"] == {
        "day": yesterday.isoformat(),
        "count": 2,
        "path": "/sales/visits?tab=exceptions",
    }
    # Markup-FREE keys: the admin UI prints notification `content` as plain text. Unseeded
    # keys render as themselves, which is exactly what makes the choice of key visible here.
    override = in_app[0][1]["template_override"]
    assert override.subject == "staff.notification.subject.sales_exception_summary"
    assert override.content == "staff.notification.content.sales_exception_summary"
    assert override.get_translated("content", "ru") == "staff.notification.content.sales_exception_summary"


def test_the_exception_summary_is_silent_when_yesterday_was_clean(db, admin_user, monkeypatch):
    """A zero that arrives every morning is how a manager learns to ignore the one that
    does not (spec § *Notifications*)."""
    from business_app.services.notification_service import NotificationService
    from business_app.utils.local_windows import local_date

    in_app = _spy(monkeypatch, NotificationService, "send_notification")

    result = sales_agent_tasks.notify_managers_exception_summary.run()

    assert result == {
        "success": True,
        "day": (local_date() - timedelta(days=1)).isoformat(),
        "count": 0,
        "managers": 0,
    }
    assert in_app == []


def test_the_exception_summary_copy_is_seeded_trilingually_and_markup_free():
    """Both keys, three languages, identical placeholders, genuine Cyrillic ru.

    `get_translation` falls back to the KEY when a row is missing, so an unseeded summary
    ships `staff.notification.content.sales_exception_summary` into a manager's inbox and
    nothing fails anywhere else.
    """
    from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

    subject = BACKEND_TRANSLATIONS["staff.notification.subject.sales_exception_summary"]
    content = BACKEND_TRANSLATIONS["staff.notification.content.sales_exception_summary"]

    for row in (subject, content):
        assert sorted(row) == ["en", "ru", "uz"]
        assert all(value.strip() for value in row.values())
        # The admin UI renders notification content as PLAIN TEXT.
        assert all("<" not in value for value in row.values())
        assert any("Ѐ" <= char <= "ӿ" for char in row["ru"])

    placeholders = {lang: sorted(re.findall(r"\{(\w+)\}", value)) for lang, value in content.items()}
    assert placeholders == {
        "en": ["count", "day", "path"],
        "ru": ["count", "day", "path"],
        "uz": ["count", "day", "path"],
    }
    assert re.findall(r"\{(\w+)\}", subject["en"]) == []


REPO_ROOT = Path(__file__).resolve().parents[2]

# Which event names, spelled as a bare string literal in THIS file, would be the
# producer-side copy ruling 66 removes. `activation_requested` is filtered out of
# `outlet_service.py` because it is also an outlet STAGE there (`PROSPECT_STAGES`, the
# approve/reject guards) — a different vocabulary that happens to share a word.
SALES_EVENT_PRODUCERS = {
    "business_app/services/sales/notifications.py": SALES_EVENTS,
    "business_app/services/sales/agent_order_confirmation_service.py": SALES_EVENTS,
    "business_app/services/sales/outlet_service.py": tuple(e for e in SALES_EVENTS if e not in OUTLET_STAGES),
    "business_app/tasks/sales_agent_tasks.py": SALES_EVENTS,
}


@pytest.mark.parametrize("relative_path", sorted(SALES_EVENT_PRODUCERS))
def test_no_sales_producer_spells_an_event_name_as_a_literal(relative_path):
    """The bot side of this vocabulary has been one constant since M27; the BACKEND side was
    five bare literals across three modules, which is the same drift in the other direction —
    a renamed event ships a humanised key tail to an agent, or is refused at the door for a
    message the backend believes it delivered."""
    source = (REPO_ROOT / relative_path).read_text(encoding="utf-8")

    # A SUBSTRING scan, deliberately: it cannot tell a quoted event name in code from one
    # inside a comment or a docstring, so the remedy for a future trip is to stop quoting the
    # name in prose (say `SALES_EVENT_OUTLET_APPROVED` instead) — never to rename the event.
    offenders = sorted(
        event
        for event in SALES_EVENT_PRODUCERS[relative_path]
        if f'"{event}"' in source or f"'{event}'" in source
    )

    assert offenders == [], (
        f"{relative_path} still retypes {offenders} — pass shared.staff_constants.SALES_EVENT_* instead"
    )


def _due_outlet(db, agent, name, *, days_overdue):
    """An active outlet due `days_overdue` days ago, visited on the same day so it never
    also qualifies for the 21-day unvisited section."""
    now = datetime.now(UTC)
    outlet = Outlet(
        name=name,
        outlet_type="grocery_store",
        stage="active",
        assigned_agent_user_id=agent.id,
        next_visit_due_at=now - timedelta(days=days_overdue),
        last_visit_at=now - timedelta(days=days_overdue),
    )
    db.session.add(outlet)
    db.session.commit()
    return outlet


def test_the_digest_event_is_part_of_the_shared_vocabulary():
    """`SALES_EVENTS` is the door the staff bot opens (`webhook_server.py:615`) and the tuple
    both translation registries build `staff.sales.notify.<event>` from. A producer that
    spells the name itself is how a message gets minted, delivered and then refused."""
    assert SALES_EVENT_MORNING_DIGEST == "morning_digest"
    assert SALES_EVENT_MORNING_DIGEST in SALES_EVENTS


def test_the_morning_digest_goes_only_to_agents_with_something_to_say(db, agent, monkeypatch):
    """The 08:30 fan-out: one push per active agent whose digest is not empty.

    Dates are relative to the real clock because the task owns its own `now` — it is beat's
    job, and ONE `now` per run is what stops two agents disagreeing about which day this
    digest is for. The numbers are still exact: three days ago is three days overdue.
    """
    busy = _due_outlet(db, agent, "Bahor market", days_overdue=3)
    quiet = make_sales_agent_user(db, phone="+998901234583", staff_roles=["sales_agent"])
    quiet.telegram_id = "777000333"
    db.session.add(SalesAgentProfile(user_id=quiet.id, districts=[]))
    db.session.commit()

    # The instant the RUN chose, captured as it is handed down: asserting against a second
    # `local_now()` read is a test that disagrees with the job whenever the two straddle
    # midnight — the one boundary this field exists to get right.
    built_from = []
    real_build = AgentDigestService.build

    def capturing(agent_user_id, *, now):
        built_from.append(now)
        return real_build(agent_user_id, now=now)

    monkeypatch.setattr(AgentDigestService, "build", capturing)
    delays = _spy(monkeypatch, sales_agent_tasks.push_sales_event, "delay")
    started = datetime.now(UTC)
    result = sales_agent_tasks.send_agent_morning_digest.run()

    assert result == {"success": True, "agents": 2, "sent": 1}
    assert len(delays) == 1
    (telegram_id, event, payload), kwargs = delays[0]
    assert (telegram_id, event, kwargs) == (777000111, SALES_EVENT_MORNING_DIGEST, {})
    assert payload["overdue"] == [{"outlet_id": busy.id, "outlet_name": "Bahor market", "overdue_days": 3}]
    assert payload["due_today"] == []
    assert payload["unvisited"] == []
    assert payload["open_visit"] is None
    # ONE `now` for the whole run, it is the wall clock, and the date is ITS day.
    assert set(built_from) == {built_from[0]}
    assert started <= built_from[0] <= datetime.now(UTC)
    assert payload["date"] == AgentDigestService._local_date(built_from[0])


def test_one_agents_broken_estate_does_not_cost_the_others_their_morning(db, agent, monkeypatch):
    """The fan-out's own rule ("One bad agent must not cost the rest their morning").

    It runs once a day at 08:30 with nothing but a Celery traceback as evidence, so a refactor
    that hoists `build` out of the try — or narrows the `except` — turns one broken estate into
    a silent morning for every agent.
    """
    other = make_sales_agent_user(db, phone="+998901234583", staff_roles=["sales_agent"])
    other.telegram_id = "777000444"
    db.session.add(SalesAgentProfile(user_id=other.id, districts=[]))
    db.session.commit()
    _due_outlet(db, agent, "Buzuq", days_overdue=2)
    healthy = _due_outlet(db, other, "Sog'lom", days_overdue=2)
    real_build = AgentDigestService.build

    def exploding(agent_user_id, *, now):
        if agent_user_id == agent.id:
            raise RuntimeError("this agent's estate blew up")
        return real_build(agent_user_id, now=now)

    monkeypatch.setattr(AgentDigestService, "build", exploding)
    delays = _spy(monkeypatch, sales_agent_tasks.push_sales_event, "delay")

    result = sales_agent_tasks.send_agent_morning_digest.run()

    assert result == {"success": True, "agents": 2, "sent": 1}
    assert len(delays) == 1
    (telegram_id, event, payload), _kwargs = delays[0]
    assert (telegram_id, event) == (777000444, SALES_EVENT_MORNING_DIGEST)
    assert payload["overdue"] == [{"outlet_id": healthy.id, "outlet_name": "Sog'lom", "overdue_days": 2}]


def test_a_deactivated_or_unreachable_agent_gets_no_digest(db, agent, monkeypatch):
    """The staff bot's own door decides who is reachable.

    A deactivated agent is refused by every /staff/sales route (`assert_staff_active` →
    `STAFF_ACCOUNT_DEACTIVATED`), so a digest they cannot act on is noise with a sound on
    it; an agent who has never opened the staff bot has no chat to push into at all.
    """
    SalesAgentProfile.query.filter_by(user_id=agent.id).one().is_active = False
    silent = make_sales_agent_user(db, phone="+998901234583", staff_roles=["sales_agent"])
    silent.telegram_id = None
    db.session.add(SalesAgentProfile(user_id=silent.id, districts=[]))
    db.session.commit()
    _due_outlet(db, agent, "Deaktiv", days_overdue=2)
    _due_outlet(db, silent, "Chatsiz", days_overdue=2)

    delays = _spy(monkeypatch, sales_agent_tasks.push_sales_event, "delay")
    result = sales_agent_tasks.send_agent_morning_digest.run()

    assert delays == []
    assert result == {"success": True, "agents": 1, "sent": 0}
