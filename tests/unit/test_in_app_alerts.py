"""The IN_APP alert fan-out (`in_app_alerts.send_in_app_alert`) through the REAL notification path.

Its one caller is the sales tasks' admin-and-manager fan-out (`sales_agent_tasks._send_manager_in_app`), so each test
runs a real sales task, `notify_managers_activation_requested`, and reads the `Notification` rows the real
`NotificationService.send_notification` stores. The in-app channel makes no outbound call, so nothing on the send path
is faked; the operator push is a signature-enforcing spy on its Celery `.delay`. The copy rows are the ones
scripts/seed_backend_translations.py writes.
"""

import inspect

import pytest

from business_app.models.notification import Notification
from business_app.models.sales import SalesAgentProfile
from business_app.models.translation import Translation
from business_app.services.notification_service import NotificationService
from business_app.services.sales.outlet_service import OutletService
from business_app.tasks import sales_agent_tasks
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS
from tests.integration.test_outlet_create import GROCERY
from tests.notification_send_fakes import REAL_SEND_NOTIFICATION
from tests.unit.test_sales_agent_role import make_sales_agent_user
from tests.unit.test_sales_agent_tasks import _spy

SUBJECT = "staff.notification.subject.outlet_activation_requested"
CONTENT = "staff.notification.content.outlet_activation_requested"
ALERT_EN = ("Outlet activation requested", "🆕 New outlet awaiting activation: Bahor market")
ALERT_RU = ("Заявка на активацию точки", "🆕 Новая торговая точка ждёт активации: Bahor market")


@pytest.fixture
def outlet(db):
    """An outlet whose activation request is the alert, with the alert's copy seeded. Its onboarder is a sales agent,
    neither admin nor manager, so the recipients are exactly the admin and manager a test asks for."""
    agent = make_sales_agent_user(db, staff_roles=["sales_agent"])
    db.session.add(SalesAgentProfile(user_id=agent.id, districts=["chilanzar"]))
    for key in (SUBJECT, CONTENT):
        for language, value in BACKEND_TRANSLATIONS[key].items():
            db.session.add(Translation(key=key, language=language, value=value, category="staff_notification"))
    db.session.commit()
    return OutletService.create(agent.id, dict(GROCERY))


@pytest.fixture
def pushes(monkeypatch):
    return _spy(monkeypatch, sales_agent_tasks.push_sales_event, "delay")


@pytest.fixture
def sends(monkeypatch):
    """The real send path (conftest stubs it class-wide), recording each recipient and what the channels answered."""
    signature = inspect.signature(REAL_SEND_NOTIFICATION)
    sent = []

    def send(self, *args, **kwargs):
        user_id = signature.bind(self, *args, **kwargs).arguments["user_id"]
        results = REAL_SEND_NOTIFICATION(self, *args, **kwargs)
        sent.append((user_id, results))
        return results

    monkeypatch.setattr(NotificationService, "send_notification", send)
    return sent


def _break_the_session(db):
    """A write that fails at flush (a NOT NULL column left empty). SQLAlchemy then refuses every later use of the
    session, a lazy load included, until it is rolled back: what a database error leaves behind."""
    db.session.add(Translation(key=None, language="en", value="never stored"))
    db.session.flush()


def test_each_recipient_reads_the_alert_in_their_own_language_and_the_stored_row_reads_it_too(
    db, outlet, admin_user, manager_user, sends, pushes
):
    """The admin reads English; the manager, whose stored language is the regional "ru-RU", reads Russian: its base
    code, not the Uzbek a lookup of "ru-RU" itself falls back to. Each stored row carries the alert as its title and
    message, never a generic "System Alert"."""
    manager_user.preferred_language = "ru-RU"
    db.session.commit()

    result = sales_agent_tasks.notify_managers_activation_requested.run(outlet.id)

    assert result == {"success": True, "managers": 2, "operators": 0}
    expected = sorted([(admin_user.id, *ALERT_EN), (manager_user.id, *ALERT_RU)])
    rows = Notification.query.order_by(Notification.user_id).all()
    assert [
        (row.user_id, row.notification_type, row.channel.value, row.is_sent, row.title, row.message) for row in rows
    ] == [(user_id, "system_alert", "in_app", True, title, message) for user_id, title, message in expected]
    assert [row.extra_data for row in rows] == [
        {"outlet_id": outlet.id, "outlet_name": "Bahor market", "title": title, "message": message}
        for _user_id, title, message in expected
    ]
    # What the in-app channel rendered is what the row stores.
    rendered = sorted((user_id, results["in_app"]["title"], results["in_app"]["content"]) for user_id, results in sends)
    assert rendered == expected
    assert pushes == []


def test_a_recipient_whose_send_breaks_the_session_costs_the_others_nothing(
    db, outlet, admin_user, manager_user, sends, pushes, monkeypatch
):
    """The first recipient's send fails on a database error that leaves the session refusing every later use. The
    fan-out rolls it back, so the other recipient still gets the alert through the real path, the count says one,
    and the task still reads its operators afterwards."""
    recorded = NotificationService.send_notification  # the `sends` recorder around the real path
    signature = inspect.signature(REAL_SEND_NOTIFICATION)
    tried = []

    def first_send_breaks(self, *args, **kwargs):
        tried.append(signature.bind(self, *args, **kwargs).arguments["user_id"])
        if len(tried) == 1:
            _break_the_session(db)
        return recorded(self, *args, **kwargs)

    monkeypatch.setattr(NotificationService, "send_notification", first_send_breaks)

    result = sales_agent_tasks.notify_managers_activation_requested.run(outlet.id)

    assert result == {"success": True, "managers": 1, "operators": 0}
    assert sorted(tried) == sorted([admin_user.id, manager_user.id])
    rows = Notification.query.all()
    assert [(row.user_id, row.channel.value, row.is_sent, row.title, row.message) for row in rows] == [
        (tried[1], "in_app", True, *ALERT_EN)
    ]
    assert [user_id for user_id, _results in sends] == [tried[1]]
    assert pushes == []
