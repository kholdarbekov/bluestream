from decimal import Decimal

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
from shared.enums import SupportMessageType


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


@pytest.mark.unit
def test_photo_message_serialises_attachment_fields(app, db):
    msg = SupportMessage(
        conversation_id=1,
        direction=SupportMessageDirection.INBOUND,
        content=None,
        message_type=SupportMessageType.PHOTO,
        telegram_file_id="AgACAgIAAxkBAAI",
        attachment_mime_type="image/jpeg",
        attachment_size=204800,
    )
    data = msg.to_dict()

    assert data["message_type"] == "photo"
    assert data["telegram_file_id"] == "AgACAgIAAxkBAAI"
    assert data["attachment_mime_type"] == "image/jpeg"
    assert data["attachment_size"] == 204800
    assert data["has_attachment"] is True
    assert data["content"] is None


@pytest.mark.unit
def test_attachment_too_large_is_published_by_the_backend(app, db):
    """FIX 3: the 20 MB rule must be decided in exactly one place. The
    backend computes `attachment_too_large` from its own
    `TELEGRAM_MAX_DOWNLOAD_BYTES` constant so the frontend never has to
    restate the threshold."""
    from business_app.services.support_attachment_service import TELEGRAM_MAX_DOWNLOAD_BYTES

    small = SupportMessage(
        conversation_id=1,
        direction=SupportMessageDirection.INBOUND,
        content=None,
        message_type=SupportMessageType.DOCUMENT,
        telegram_file_id="doc-small",
        attachment_size=TELEGRAM_MAX_DOWNLOAD_BYTES - 1,
    )
    assert small.to_dict()["attachment_too_large"] is False

    big = SupportMessage(
        conversation_id=1,
        direction=SupportMessageDirection.INBOUND,
        content=None,
        message_type=SupportMessageType.DOCUMENT,
        telegram_file_id="doc-big",
        attachment_size=TELEGRAM_MAX_DOWNLOAD_BYTES + 1,
    )
    assert big.to_dict()["attachment_too_large"] is True

    unknown_size = SupportMessage(
        conversation_id=1,
        direction=SupportMessageDirection.INBOUND,
        content=None,
        message_type=SupportMessageType.DOCUMENT,
        telegram_file_id="doc-unknown",
        attachment_size=None,
    )
    assert unknown_size.to_dict()["attachment_too_large"] is False


@pytest.mark.unit
def test_location_message_serialises_coordinates_as_floats(app, db):
    msg = SupportMessage(
        conversation_id=1,
        direction=SupportMessageDirection.INBOUND,
        content=None,
        message_type=SupportMessageType.LOCATION,
        latitude=Decimal("41.3235400"),
        longitude=Decimal("69.2410360"),
    )
    data = msg.to_dict()

    # The admin UI feeds these straight to react-leaflet, which cannot take a
    # Decimal or a string.
    assert data["latitude"] == pytest.approx(41.32354)
    assert data["longitude"] == pytest.approx(69.241036)
    assert isinstance(data["latitude"], float)
    assert data["has_attachment"] is False


@pytest.mark.unit
def test_forwarded_message_keeps_origin_type(app, db):
    msg = SupportMessage(
        conversation_id=1,
        direction=SupportMessageDirection.INBOUND,
        content="fwd body",
        message_type=SupportMessageType.TEXT,
        forwarded_from="Aqua Element News",
        forwarded_origin_type="channel",
    )
    data = msg.to_dict()

    assert data["forwarded_from"] == "Aqua Element News"
    assert data["forwarded_origin_type"] == "channel"


@pytest.mark.unit
def test_plain_text_message_defaults_to_text_type(app, db):
    msg = SupportMessage(
        conversation_id=1,
        direction=SupportMessageDirection.INBOUND,
        content="just words",
    )
    db.session.add(msg)
    db.session.flush()

    assert msg.to_dict()["message_type"] == "text"
