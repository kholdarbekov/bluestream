"""`StaffService.active_admin_manager_recipients` (spec §9.2): every ACTIVE admin and manager,
by role or by the staff_roles list, with a deliverable email; nobody else.

Real rows in the real (test) database, one per way an account can fail to be a recipient. The
deliverability rule is `shared.validators.is_deliverable_email`, the role rule is
`StaffService.staff_role_member_filter`, "active" is `users.status`; this file only supplies the
accounts. The language test then sends the digest to each returned recipient through the real
`NotificationService` path (only `requests.post` faked), so a recipient reads it in their own
`preferred_language`.
"""

import pytest

from business_app.models.notification import Notification
from business_app.models.user import User
from business_app.services.notification_service import NotificationService
from business_app.services.staff_service import StaffService
from business_app.utils.constants import NotificationType
from shared.enums import UserRole, UserStatus, UserType
from tests.integration.reorder_reminder_builders import (  # noqa: F401 - `store_fixture` is the `store` fixture
    make_customer,
    store_fixture,
)
from tests.notification_send_fakes import BREVO_URL, live_send_path

pytestmark = pytest.mark.integration

STAFF = {"user_type": UserType.STAFF}


def _staff(db, email, role, **fields):
    return make_customer(db, first_name="Staff", email=email, role=role, **STAFF, **fields)


def test_only_active_admins_and_managers_with_a_real_email_receive_the_digest(
    db, admin_user, manager_user, sample_user
):
    # Recipients: a plain admin and manager (the conftest fixtures), a mixed-case real address, an
    # operator who is also a manager by the staff_roles list, and an admin who holds BOTH roles
    # (one row, not two).
    mixed_case = _staff(db, "Dilnoza.K@Example.COM", UserRole.MANAGER)
    operator_manager = _staff(db, "ops-manager@example.com", UserRole.OPERATOR, staff_roles=["manager"])
    both = _staff(db, "both@example.com", UserRole.ADMIN, staff_roles=["admin", "manager"])
    driver_admin = _staff(db, "driver-admin@example.com", UserRole.DELIVERY_DRIVER, staff_roles=["admin"])

    # Not active.
    _staff(db, "away-manager@example.com", UserRole.MANAGER, status=UserStatus.INACTIVE)
    _staff(db, "banned-admin@example.com", UserRole.ADMIN, status=UserStatus.BANNED)
    _staff(db, "new-admin@example.com", UserRole.ADMIN, status=UserStatus.PENDING_VERIFICATION)
    # No mailbox behind the address: the two placeholder domains, a merge tombstone, nothing at all.
    _staff(db, "telegram_5550001@bot.internal", UserRole.MANAGER)
    _staff(db, "telegram_5550002@bluestream.local", UserRole.ADMIN)
    _staff(db, "deleted_77@bluestream.local", UserRole.ADMIN)
    _staff(db, None, UserRole.ADMIN)
    _staff(db, "   ", UserRole.MANAGER)
    # Other roles, however real their address: a driver, an operator, a sales agent, a customer, and
    # a staff_roles entry that merely CONTAINS "manager" (the predicate matches the quoted role).
    _staff(db, "driver@example.org", UserRole.DELIVERY_DRIVER, staff_roles=["delivery_driver"])
    _staff(db, "operator@example.org", UserRole.OPERATOR, staff_roles=["operator"])
    _staff(db, "agent@example.org", UserRole.SALES_AGENT)
    _staff(db, "lookalike@example.org", UserRole.OPERATOR, staff_roles=["sales_manager"])
    make_customer(db, email="customer@example.org")
    # An admin whose account was deleted (a merge deletes the secondary row).
    deleted = _staff(db, "deleted-admin@example.com", UserRole.ADMIN)
    deleted_id = deleted.id
    db.session.delete(deleted)
    db.session.commit()

    recipients = StaffService.active_admin_manager_recipients()

    assert all(isinstance(user, User) for user in recipients)
    expected = [admin_user, manager_user, mixed_case, operator_manager, both, driver_admin]
    assert [user.id for user in recipients] == sorted(user.id for user in expected)
    assert sample_user.id not in {user.id for user in recipients}
    assert deleted_id not in {user.id for user in recipients}


def test_the_roster_query_runs_on_both_databases(store):
    """The role predicate casts the JSON `staff_roles` list to text and matches the quoted role;
    that is dialect-specific SQL, so it runs on SQLite AND on a migrated Postgres (`store`)."""
    admin = _staff(store, "pg-admin@example.com", UserRole.ADMIN)
    manager_by_list = _staff(store, "pg-ops-manager@example.com", UserRole.OPERATOR, staff_roles=["manager"])
    _staff(store, "pg-away-manager@example.com", UserRole.MANAGER, status=UserStatus.INACTIVE)
    _staff(store, "pg-operator@example.com", UserRole.OPERATOR, staff_roles=["operator"])

    recipients = StaffService.active_admin_manager_recipients()

    assert [user.id for user in recipients] == [admin.id, manager_by_list.id]


def test_nobody_is_a_recipient_when_there_is_no_admin_or_manager(db, sample_user, delivery_driver):
    assert StaffService.active_admin_manager_recipients() == []


def test_each_recipient_reads_the_digest_in_their_own_language(app, db, admin_user, manager_user, monkeypatch):
    """Spec §9.2 / D15, driven the way Task 22b sends: for each recipient the helper returns, the
    real `send_notification`. A stored regional code reads as its base language; an unsupported or
    empty one as English."""
    admin_user.preferred_language = "ru-RU"
    manager_user.preferred_language = "uz"
    unsupported = _staff(db, "de-manager@example.com", UserRole.MANAGER, language="de")
    empty = _staff(db, "empty-lang@example.com", UserRole.ADMIN, language=None)
    db.session.commit()
    data = {
        "escalated_count": 2,
        "top": [{"name": "Alisher Mijoz", "days_overdue": 44, "flag": "long_lapsed"}],
        "flag_labels": {"en": {"long_lapsed": "Long lapsed"}},
        "worklist_url": "https://admin.aqua-element.uz/lapsed-customers",
    }

    recipients = StaffService.active_admin_manager_recipients()
    with live_send_path(app, monkeypatch, bot_url="https://t.me/aqua_digest_test_bot") as post:
        for user in recipients:
            result = NotificationService().send_notification(
                user.id, NotificationType.REORDER_ESCALATION_DIGEST, template_data=data
            )
            assert result["email"]["success"] is True, (user.email, result)

    sent = {call.kwargs["json"]["to"][0]["email"]: call.kwargs["json"] for call in post.call_args_list}
    assert all(call.args == (BREVO_URL,) for call in post.call_args_list)
    assert {email: body["subject"] for email, body in sent.items()} == {
        "admin@example.com": "Пропавшие клиенты (новых: 2) - Aqua Element",
        "sales-manager@example.com": "Yo'qolgan mijozlar: 2 ta yangi - Aqua Element",
        unsupported.email: "Lapsed customers: 2 new to follow up - Aqua Element",
        empty.email: "Lapsed customers: 2 new to follow up - Aqua Element",
    }
    # The body is in the subject's language too (the heading of that language's file).
    assert "<h1>Пропавшие клиенты</h1>" in sent["admin@example.com"]["htmlContent"]
    assert "<h1>Yo'qolgan mijozlar</h1>" in sent["sales-manager@example.com"]["htmlContent"]
    assert "<h1>Lapsed customers</h1>" in sent[unsupported.email]["htmlContent"]
    assert "<h1>Lapsed customers</h1>" in sent[empty.email]["htmlContent"]
    assert Notification.query.filter_by(notification_type="reorder_escalation_digest").count() == 4
