import pytest
from unittest.mock import MagicMock
from business_app import db
from business_app.models.user import User
from business_app.models.support import SupportConversation, SupportMessageDirection, SupportMessageDeliveryStatus
from business_app.services.support_conversation_service import SupportConversationService
from business_app.utils.exceptions import NotFoundError, ValidationError as _VE
from business_app.utils.password_security import hash_password


def _make_user(**kw):
    u = User(
        email=kw.pop("email", "c@example.com"),
        password_hash=hash_password("Passw0rd!"),
        first_name=kw.pop("first_name", "C"),
        last_name=kw.pop("last_name", "X"),
        **kw,
    )
    db.session.add(u)
    db.session.flush()
    return u


@pytest.mark.unit
def test_record_inbound_creates_conversation_and_message(db):
    user = _make_user(email="inbound1@example.com")
    svc = SupportConversationService()

    msg = svc.record_inbound_message(user.id, "I need water", telegram_message_id="42")

    assert msg.direction == SupportMessageDirection.INBOUND
    assert msg.is_read is False
    assert msg.telegram_message_id == "42"
    conv = SupportConversation.query.filter_by(user_id=user.id).one()
    assert conv.last_message_preview == "I need water"
    assert conv.last_message_direction == SupportMessageDirection.INBOUND
    assert conv.last_message_at is not None


@pytest.mark.unit
def test_record_inbound_reuses_conversation(db):
    user = _make_user(email="inbound2@example.com")
    svc = SupportConversationService()
    svc.record_inbound_message(user.id, "first")
    svc.record_inbound_message(user.id, "second")
    assert SupportConversation.query.filter_by(user_id=user.id).count() == 1


@pytest.mark.unit
def test_record_inbound_unknown_user_raises(db):
    with pytest.raises(NotFoundError):
        SupportConversationService().record_inbound_message(99999999, "hi")


@pytest.mark.unit
def test_send_message_records_outbound_and_marks_sent(db, monkeypatch):
    user = _make_user(email="out1@example.com", telegram_id="tg-out-1")
    admin = _make_user(email="admin1@example.com")
    svc = SupportConversationService()

    fake_ns = MagicMock()
    fake_ns.send_user_telegram_message.return_value = {"success": True, "message_id": 7}
    monkeypatch.setattr(
        "business_app.services.support_conversation_service.get_notification_service",
        lambda: fake_ns,
    )

    result = svc.send_message_to_user(user.id, admin.id, "Hi from support")

    msg = result["message"]
    assert msg.direction == SupportMessageDirection.OUTBOUND
    assert msg.delivery_status == SupportMessageDeliveryStatus.SENT
    assert msg.telegram_message_id == "7"
    assert msg.sender_admin_id == admin.id
    fake_ns.send_user_telegram_message.assert_called_once_with(user, "Hi from support")


@pytest.mark.unit
def test_send_message_failed_delivery_is_persisted(db, monkeypatch):
    user = _make_user(email="out2@example.com", telegram_id="tg-out-2")
    admin = _make_user(email="admin2@example.com")
    svc = SupportConversationService()
    fake_ns = MagicMock()
    fake_ns.send_user_telegram_message.return_value = {"success": False, "error": "bot blocked"}
    monkeypatch.setattr(
        "business_app.services.support_conversation_service.get_notification_service",
        lambda: fake_ns,
    )

    result = svc.send_message_to_user(user.id, admin.id, "Hello")
    assert result["message"].delivery_status == SupportMessageDeliveryStatus.FAILED
    assert result["message"].delivery_error == "bot blocked"


@pytest.mark.unit
def test_send_message_rejects_user_without_telegram(db):
    user = _make_user(email="notg@example.com")  # no telegram_id
    admin = _make_user(email="admin3@example.com")
    with pytest.raises(_VE):
        SupportConversationService().send_message_to_user(user.id, admin.id, "Hello")


@pytest.mark.unit
def test_list_thread_and_mark_read(db):
    user = _make_user(email="list1@example.com", first_name="Ann", phone="+998900000001", telegram_id="tg-l1")
    svc = SupportConversationService()
    svc.record_inbound_message(user.id, "msg one")
    svc.record_inbound_message(user.id, "msg two")

    listing = svc.list_conversations(page=1, per_page=20)
    assert listing["total"] == 1
    item = listing["items"][0]
    assert item["unread_count"] == 2
    assert item["user"]["name"].startswith("Ann")
    assert listing["total_unread"] == 2

    conv_id = item["id"]
    thread = svc.get_thread(conv_id, page=1, per_page=50)
    assert thread["total"] == 2
    assert thread["items"][0]["content"] == "msg one"  # chronological

    marked = svc.mark_read(conv_id)
    assert marked == 2
    assert svc.list_conversations()["total_unread"] == 0


@pytest.mark.unit
def test_list_unread_only_and_search(db):
    u1 = _make_user(email="s1@example.com", first_name="Zed", phone="+998901112233", telegram_id="tg-s1")
    u2 = _make_user(email="s2@example.com", first_name="Bob", phone="+998904445566", telegram_id="tg-s2")
    svc = SupportConversationService()
    svc.record_inbound_message(u1.id, "hello")
    svc.record_inbound_message(u2.id, "hi")
    svc.mark_read(SupportConversation.query.filter_by(user_id=u2.id).one().id)

    assert svc.list_conversations(unread_only=True)["total"] == 1
    assert svc.list_conversations(search="Zed")["total"] == 1
    assert svc.list_conversations(search="998904445566")["total"] == 1


@pytest.mark.unit
def test_get_thread_missing_raises(db):
    with pytest.raises(NotFoundError):
        SupportConversationService().get_thread(123456)
