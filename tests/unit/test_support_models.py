import pytest
from business_app import db
from business_app.models.user import User
from business_app.models.support import (
    SupportConversation,
    SupportMessage,
    SupportMessageDirection,
    SupportMessageDeliveryStatus,
    SupportConversationStatus,
)
from business_app.utils.password_security import hash_password


@pytest.mark.unit
def test_conversation_and_messages_persist(db):
    user = User(
        email="cust_models@example.com",
        password_hash=hash_password("Passw0rd!"),
        first_name="Cust",
        last_name="Omer",
    )
    db.session.add(user)
    db.session.flush()

    conv = SupportConversation(user_id=user.id, status=SupportConversationStatus.OPEN)
    db.session.add(conv)
    db.session.flush()

    inbound = SupportMessage(
        conversation_id=conv.id,
        direction=SupportMessageDirection.INBOUND,
        content="I need 19L bottles",
        is_read=False,
    )
    outbound = SupportMessage(
        conversation_id=conv.id,
        direction=SupportMessageDirection.OUTBOUND,
        content="Sure, where to deliver?",
        sender_admin_id=user.id,
        delivery_status=SupportMessageDeliveryStatus.SENT,
        telegram_message_id="555",
        is_read=True,
    )
    db.session.add_all([inbound, outbound])
    db.session.commit()

    reloaded = SupportConversation.query.filter_by(user_id=user.id).one()
    assert reloaded.status == SupportConversationStatus.OPEN
    assert len(reloaded.messages) == 2
    d = inbound.to_dict()
    assert d["direction"] == "inbound"
    assert d["is_read"] is False
    assert reloaded.to_dict()["user_id"] == user.id
