"""Turn a Telegram message into a support-inbox payload, and post it.

WHY THIS MODULE EXISTS
----------------------
`bot._capture_support_message` and `handlers.support._silent_capture` were the
same function written twice, with a comment in the latter explaining that reuse
"would be circular". Per-type extraction would have tripled that duplication.
This module imports neither, so both can depend on it.

`build_support_payload` is deliberately pure: no network, no auth, no PTB
context. It is the piece with all the branching, so it is the piece worth
testing directly.
"""
import logging

from api_client import api_client
from shared.enums import SupportMessageType
from utils import get_auth_token

logger = logging.getLogger('handlers')

# Column widths on `support_messages`, mirrored from Task 3's serializer caps —
# pydantic REJECTS an over-length field rather than truncating it, and a 400
# here would make the whole message unpostable (see MINOR 3 in the task-4
# review). `content` has its own cap below; forwarded_from was always capped.
_TELEGRAM_FILE_ID_MAX = 256
_ATTACHMENT_MIME_TYPE_MAX = 128
_ATTACHMENT_FILE_NAME_MAX = 255
_FORWARDED_ORIGIN_TYPE_MAX = 32

# The serializer caps content at 4096, and so does Telegram.
MAX_SUPPORT_CONTENT = 4096

# Telegram photos are always JPEG and PhotoSize carries no mime_type.
_PHOTO_MIME = 'image/jpeg'


def _forward_fields(message) -> dict:
    """Flatten `Message.forward_origin` into display fields.

    The origin TYPE travels with the name on purpose: Telegram lets a user hide
    their account on forward, and without the discriminator the admin UI would
    assert an identity we do not actually have.
    """
    origin = getattr(message, 'forward_origin', None)
    if origin is None:
        return {}

    origin_type = getattr(origin, 'type', None)
    origin_type = getattr(origin_type, 'value', origin_type)

    name = None
    sender_user = getattr(origin, 'sender_user', None)
    if sender_user is not None:
        name = getattr(sender_user, 'full_name', None) or getattr(sender_user, 'first_name', None)
    if name is None:
        name = getattr(origin, 'sender_user_name', None)
    if name is None:
        chat = getattr(origin, 'sender_chat', None) or getattr(origin, 'chat', None)
        if chat is not None:
            name = getattr(chat, 'title', None) or getattr(chat, 'username', None)
    if name is None:
        name = getattr(origin, 'author_signature', None)

    fields = {
        'forwarded_origin_type': str(origin_type)[:_FORWARDED_ORIGIN_TYPE_MAX] if origin_type else None
    }
    if name:
        fields['forwarded_from'] = str(name)[:255]
    date = getattr(origin, 'date', None)
    if date is not None:
        fields['forwarded_date'] = date.isoformat()
    return {k: v for k, v in fields.items() if v is not None}


def _file_fields(file_obj, *, mime_default=None, with_name=True) -> dict:
    mime_type = getattr(file_obj, 'mime_type', None) or mime_default
    fields = {
        'telegram_file_id': str(file_obj.file_id)[:_TELEGRAM_FILE_ID_MAX],
        'attachment_mime_type': str(mime_type)[:_ATTACHMENT_MIME_TYPE_MAX] if mime_type else None,
        'attachment_size': getattr(file_obj, 'file_size', None),
    }
    if with_name:
        file_name = getattr(file_obj, 'file_name', None)
        fields['attachment_file_name'] = str(file_name)[:_ATTACHMENT_FILE_NAME_MAX] if file_name else None
    return {k: v for k, v in fields.items() if v is not None}


def build_support_payload(message, prefix: str = '') -> dict:
    """Map a `telegram.Message` to the `/support/messages` request body.

    Pure. Returns only keys the backend serializer declares — the contract test
    in tests/telegram_bot/test_support_payload_builder.py enforces that.
    """
    body = getattr(message, 'text', None) or getattr(message, 'caption', None) or ''
    payload = {}

    photo = getattr(message, 'photo', None)
    document = getattr(message, 'document', None)
    location = getattr(message, 'location', None)
    venue = getattr(message, 'venue', None)
    voice = getattr(message, 'voice', None)
    video = getattr(message, 'video', None)
    video_note = getattr(message, 'video_note', None)
    audio = getattr(message, 'audio', None)

    if photo:
        # Telegram orders PhotoSize ascending; the last one is the full image.
        payload['message_type'] = SupportMessageType.PHOTO.value
        payload.update(_file_fields(photo[-1], mime_default=_PHOTO_MIME, with_name=False))
    elif document:
        payload['message_type'] = SupportMessageType.DOCUMENT.value
        payload.update(_file_fields(document))
    elif venue is not None:
        payload['message_type'] = SupportMessageType.LOCATION.value
        payload['latitude'] = venue.location.latitude
        payload['longitude'] = venue.location.longitude
        body = body or ' '.join(p for p in (venue.title, venue.address) if p)
    elif location is not None:
        payload['message_type'] = SupportMessageType.LOCATION.value
        payload['latitude'] = location.latitude
        payload['longitude'] = location.longitude
    elif voice is not None:
        payload['message_type'] = SupportMessageType.VOICE.value
        payload.update(_file_fields(voice, mime_default='audio/ogg', with_name=False))
    elif video_note is not None:
        payload['message_type'] = SupportMessageType.VIDEO_NOTE.value
        payload.update(_file_fields(video_note, mime_default='video/mp4', with_name=False))
    elif video is not None:
        payload['message_type'] = SupportMessageType.VIDEO.value
        payload.update(_file_fields(video, mime_default='video/mp4'))
    elif audio is not None:
        payload['message_type'] = SupportMessageType.AUDIO.value
        payload.update(_file_fields(audio, mime_default='audio/mpeg'))
    elif body:
        payload['message_type'] = SupportMessageType.TEXT.value
    else:
        # A sticker, an animation, a poll — record that SOMETHING arrived. A
        # silent drop reads to the admin as the customer going quiet.
        payload['message_type'] = SupportMessageType.UNSUPPORTED.value

    content = f"{prefix}{body}".strip() if (prefix or body) else ''
    if content:
        payload['content'] = content[:MAX_SUPPORT_CONTENT]

    payload.update(_forward_fields(message))
    return payload


async def capture_support_message(update, context, prefix: str = '') -> bool:
    """Persist an inbound message so an admin can reply. Returns True on success.

    Silent by design: no acknowledgement is sent from here. The concern flow
    adds its own ack on a True return.
    """
    message = update.message
    try:
        payload = build_support_payload(message, prefix=prefix)
        async with api_client as client:
            user_token = await get_auth_token(update, context, client)
            if not user_token:
                logger.warning(
                    "Support capture skipped: no auth token for user %s",
                    update.effective_user.id,
                )
                return False
            response = await client.record_support_message(user_token, **payload)
            return bool(getattr(response, 'success', False))
    except Exception as exc:
        logger.error(f"Failed to record support message: {exc}")
        return False
