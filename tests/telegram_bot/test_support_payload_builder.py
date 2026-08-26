"""`build_support_payload` is pure, so it is tested directly.

Everything it produces has to survive Task 3's pydantic serializer unchanged —
a key this builder invents that the serializer ignores is a silent data loss.
"""
from types import SimpleNamespace

import pytest

from support_capture import build_support_payload

pytestmark = pytest.mark.unit


def _message(**kw):
    base = dict(
        text=None, caption=None, photo=(), document=None, location=None, venue=None,
        voice=None, video=None, video_note=None, audio=None, forward_origin=None,
        message_id=42,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_plain_text():
    payload = build_support_payload(_message(text="hello"))

    assert payload["message_type"] == "text"
    assert payload["content"] == "hello"
    assert "telegram_file_id" not in payload


def test_photo_uses_the_largest_size_and_carries_the_caption():
    small = SimpleNamespace(file_id="small", file_size=100, width=90, height=90)
    large = SimpleNamespace(file_id="large", file_size=90000, width=1280, height=1280)

    payload = build_support_payload(_message(photo=(small, large), caption="cracked cap"))

    assert payload["message_type"] == "photo"
    # Telegram orders PhotoSize ascending; the last entry is the one worth showing.
    assert payload["telegram_file_id"] == "large"
    assert payload["attachment_size"] == 90000
    assert payload["attachment_mime_type"] == "image/jpeg"
    assert payload["content"] == "cracked cap"


def test_document_carries_name_and_mime():
    doc = SimpleNamespace(
        file_id="doc-1", file_name="receipt.pdf", mime_type="application/pdf", file_size=2048
    )

    payload = build_support_payload(_message(document=doc))

    assert payload["message_type"] == "document"
    assert payload["attachment_file_name"] == "receipt.pdf"
    assert payload["attachment_mime_type"] == "application/pdf"


def test_an_overlong_attachment_file_name_is_truncated_to_the_column_width():
    """Pydantic REJECTS an over-length field rather than truncating it, so an
    untruncated name here would 400 the whole message — on the silent capture
    path the customer's upload would then vanish with only a log line to show
    for it (MINOR 3, task-4 review)."""
    doc = SimpleNamespace(
        file_id="doc-1", file_name="x" * 300 + ".pdf", mime_type="application/pdf", file_size=2048
    )

    payload = build_support_payload(_message(document=doc))

    assert len(payload["attachment_file_name"]) == 255


def test_location():
    payload = build_support_payload(
        _message(location=SimpleNamespace(latitude=41.32354, longitude=69.241036))
    )

    assert payload["message_type"] == "location"
    assert payload["latitude"] == pytest.approx(41.32354)
    assert payload["longitude"] == pytest.approx(69.241036)


def test_venue_is_a_location_with_its_title_as_text():
    venue = SimpleNamespace(
        location=SimpleNamespace(latitude=41.1, longitude=69.2),
        title="Chorsu Bazaar",
        address="Tashkent",
    )

    payload = build_support_payload(_message(venue=venue))

    assert payload["message_type"] == "location"
    assert payload["latitude"] == pytest.approx(41.1)
    assert "Chorsu Bazaar" in payload["content"]


def test_voice():
    payload = build_support_payload(
        _message(voice=SimpleNamespace(file_id="v-1", mime_type="audio/ogg", file_size=8000))
    )

    assert payload["message_type"] == "voice"
    assert payload["telegram_file_id"] == "v-1"


def test_sticker_becomes_unsupported_and_carries_no_payload():
    """A sticker must be RECORDED, not dropped — otherwise the admin reads the
    gap as the customer going quiet."""
    payload = build_support_payload(_message())

    assert payload["message_type"] == "unsupported"
    assert "telegram_file_id" not in payload


def test_forward_from_a_named_user():
    origin = SimpleNamespace(
        type="user",
        sender_user=SimpleNamespace(full_name="Dilnoza K"),
        date=None,
    )

    payload = build_support_payload(_message(text="see this", forward_origin=origin))

    assert payload["forwarded_from"] == "Dilnoza K"
    assert payload["forwarded_origin_type"] == "user"


def test_forward_from_a_hidden_user_never_claims_an_identity_we_lack():
    origin = SimpleNamespace(type="hidden_user", sender_user_name="Someone", date=None)

    payload = build_support_payload(_message(text="fwd", forward_origin=origin))

    assert payload["forwarded_origin_type"] == "hidden_user"
    assert payload["forwarded_from"] == "Someone"


def test_prefix_is_applied_to_the_caption():
    payload = build_support_payload(
        _message(photo=(SimpleNamespace(file_id="p", file_size=1),), caption="leak"),
        prefix="[Order #TG_1] ",
    )

    assert payload["content"] == "[Order #TG_1] leak"


def test_prefix_alone_is_not_treated_as_content_for_a_bare_photo():
    payload = build_support_payload(
        _message(photo=(SimpleNamespace(file_id="p", file_size=1),)),
        prefix="[Order #TG_1] ",
    )

    assert payload["content"] == "[Order #TG_1]"


def test_payload_survives_the_backend_serializer_unchanged():
    """Guard against the builder inventing keys the API silently drops."""
    from business_app.serializers.support_serializers import InboundSupportMessageRequest

    payload = build_support_payload(
        _message(document=SimpleNamespace(
            file_id="d", file_name="a.pdf", mime_type="application/pdf", file_size=5
        ))
    )
    parsed = InboundSupportMessageRequest(**payload)

    assert parsed.message_type.value == payload["message_type"]
    assert parsed.telegram_file_id == payload["telegram_file_id"]
    assert set(payload) <= set(InboundSupportMessageRequest.model_fields)


def test_harness_photo_update_maps_to_a_photo_payload():
    """The harness must build a real telegram.Message the builder understands —
    a hand-rolled dict that PTB parses differently would test nothing."""
    from tests.bot_dispatcher_harness import UpdateFactory

    update = UpdateFactory().photo(caption="leaking", file_id="harness-photo")
    payload = build_support_payload(update.message)

    assert payload["message_type"] == "photo"
    assert payload["telegram_file_id"] == "harness-photo"
    assert payload["content"] == "leaking"


def test_harness_document_update_maps_to_a_document_payload():
    from tests.bot_dispatcher_harness import UpdateFactory

    update = UpdateFactory().document(
        file_name="test.pdf", mime_type="application/pdf", file_id="doc-123", caption="receipt"
    )
    payload = build_support_payload(update.message)

    assert payload["message_type"] == "document"
    assert payload["telegram_file_id"] == "doc-123"
    assert payload["attachment_file_name"] == "test.pdf"
    assert payload["attachment_mime_type"] == "application/pdf"
    assert payload["content"] == "receipt"


def test_harness_voice_update_maps_to_a_voice_payload():
    from tests.bot_dispatcher_harness import UpdateFactory

    update = UpdateFactory().voice(file_id="voice-xyz")
    payload = build_support_payload(update.message)

    assert payload["message_type"] == "voice"
    assert payload["telegram_file_id"] == "voice-xyz"


def test_harness_video_update_maps_to_a_video_payload():
    from tests.bot_dispatcher_harness import UpdateFactory

    update = UpdateFactory().video(file_id="video-abc", caption="slow motion")
    payload = build_support_payload(update.message)

    assert payload["message_type"] == "video"
    assert payload["telegram_file_id"] == "video-abc"
    assert payload["content"] == "slow motion"


def test_harness_forwarded_text_update_carries_forward_origin():
    from tests.bot_dispatcher_harness import UpdateFactory

    update = UpdateFactory().forwarded_text(text="forwarded msg", sender_name="Ahmed")
    payload = build_support_payload(update.message)

    assert payload["message_type"] == "text"
    assert payload["content"] == "forwarded msg"
    assert payload["forwarded_from"] == "Ahmed"
    assert payload["forwarded_origin_type"] == "user"
