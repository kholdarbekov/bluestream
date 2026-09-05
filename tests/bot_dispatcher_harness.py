"""Bot-agnostic pieces of the PTB dispatcher harness.

Lives at ``tests/`` root rather than inside either bot's test package because
both bots need it and NEITHER bot's sys.path tricks may leak into the other.
``tests/telegram_bot/conftest.py`` puts ``telegram_bot/`` first on sys.path so
its modules resolve by bare name (``import i18n``); ``tests/staff_bot/`` must
never inherit that, or a staff test would silently exercise the customer bot's
i18n. So everything in here imports nothing from either bot.

What it provides:

* :class:`FakeTelegramTransport` — a real ``telegram.request.BaseRequest``, so
  every ``Bot`` method above it is the production one. ``send_message`` really
  serialises its keyboard, the reply is really parsed into a ``Message``, and a
  scripted 400 really raises :class:`telegram.error.BadRequest`.
* :class:`TelegramCall` — one recorded Bot API call, with helpers that answer
  the question tests actually ask: what did the user SEE, and which buttons
  could they tap?
* :class:`UpdateFactory` — real :class:`telegram.Update` objects, including the
  ``bot_command`` entity without which ``CommandHandler`` never matches.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Optional

from telegram import Update
from telegram.request import BaseRequest, RequestData


def _is_catch_all(handler) -> bool:
    """True for a handler that claims EVERY update and processes none.

    Shared by both bots' harnesses because both register the same shape of
    middleware, and a wiring assertion that counts them proves nothing.
    """
    from telegram.ext import CallbackQueryHandler, TypeHandler

    if isinstance(handler, TypeHandler):
        return True
    if isinstance(handler, CallbackQueryHandler) and handler.pattern is None:
        return True
    return False


DEFAULT_USER_ID = 700100200
DEFAULT_CHAT_ID = 700100200


@dataclass
class TelegramCall:
    """One Bot API call the bot made, as Telegram would have received it."""

    method: str
    params: dict

    @property
    def text(self) -> str:
        return self.params.get("text", "")

    @property
    def reply_markup(self) -> dict:
        raw = self.params.get("reply_markup")
        if raw is None:
            return {}
        return json.loads(raw) if isinstance(raw, str) else raw

    def callback_data(self) -> list[str]:
        """Every callback_data the keyboard on this message can emit."""
        rows = self.reply_markup.get("inline_keyboard") or []
        return [
            button["callback_data"]
            for row in rows
            for button in row
            if "callback_data" in button
        ]

    def button_labels(self) -> list[str]:
        markup = self.reply_markup
        rows = markup.get("inline_keyboard") or markup.get("keyboard") or []
        labels = []
        for row in rows:
            for button in row:
                labels.append(button["text"] if isinstance(button, dict) else str(button))
        return labels


class ScriptedTelegramError(Exception):
    """Marker for a failure scripted onto the transport."""


class FakeTelegramTransport(BaseRequest):
    """The Bot API, answered from memory.

    Records what the bot SENT (which is what the customer would see) and can be
    scripted to fail the way Telegram really fails — ``Message is not
    modified``, ``Message to edit not found``, ``Can't parse entities`` — none
    of which any existing test simulates, and all of which appear in this
    project's production logs.
    """

    def __init__(self):
        self.calls: list[TelegramCall] = []
        self._next_message_id = 5000
        # method -> callable(params) -> (status_code, payload_dict)
        self.failures: dict[str, Callable[[dict], tuple[int, dict]]] = {}

    # -- BaseRequest contract -------------------------------------------------

    async def initialize(self) -> None:  # pragma: no cover - trivial
        return None

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        return None

    @property
    def read_timeout(self) -> Optional[float]:
        return 5.0

    async def do_request(self, url, method, request_data: RequestData = None, **_kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data is not None else {}
        self.calls.append(TelegramCall(endpoint, params))

        failure = self.failures.get(endpoint)
        if failure is not None:
            status, payload = failure(params)
            return status, json.dumps(payload).encode("utf-8")

        return 200, json.dumps({"ok": True, "result": self._result_for(endpoint, params)}).encode(
            "utf-8"
        )

    # -- scripted failures ----------------------------------------------------

    def fail(self, endpoint: str, description: str, status: int = 400):
        """Make ``endpoint`` answer like a real Telegram rejection."""
        self.failures[endpoint] = lambda _params: (
            status,
            {"ok": False, "error_code": status, "description": description},
        )

    def clear_failures(self):
        self.failures.clear()

    # -- canned results -------------------------------------------------------

    def _message(self, params: dict) -> dict:
        self._next_message_id += 1
        return {
            "message_id": params.get("message_id", self._next_message_id),
            "date": 1_700_000_000,
            "chat": {"id": int(params.get("chat_id", DEFAULT_CHAT_ID)), "type": "private"},
            "from": {"id": 42, "is_bot": True, "first_name": "BlueStream"},
            "text": params.get("text", params.get("caption", "")),
        }

    def _result_for(self, endpoint: str, params: dict) -> Any:
        if endpoint == "getMe":
            return {
                "id": 42,
                "is_bot": True,
                "first_name": "BlueStream",
                "username": "bluestream_test_bot",
            }
        if endpoint in {"sendMessage", "sendPhoto", "sendInvoice", "sendDocument"}:
            return self._message(params)
        if endpoint in {"editMessageText", "editMessageCaption", "editMessageReplyMarkup"}:
            return self._message(params)
        return True

    # -- assertions -----------------------------------------------------------

    def of(self, *methods: str) -> list[TelegramCall]:
        return [call for call in self.calls if call.method in methods]

    @property
    def shown(self) -> list[TelegramCall]:
        """Everything the customer would actually see, in order."""
        return self.of("sendMessage", "sendPhoto", "editMessageText")

    def last_shown(self) -> TelegramCall:
        shown = self.shown
        assert shown, "the bot showed the customer nothing at all"
        return shown[-1]

    def texts(self) -> list[str]:
        return [call.text for call in self.shown]

    def reset(self):
        self.calls.clear()


class UpdateFactory:
    """Real :class:`telegram.Update` objects, numbered like a real poll.

    Bound to a bot on purpose: `Message.reply_text` and the other shortcuts
    every handler here uses resolve the bot off the object itself, so an
    Update built with `de_json(..., None)` blows up with "This object has no
    bot associated with it" the first time a handler replies.
    """

    def __init__(self, bot=None, user_id=DEFAULT_USER_ID, chat_id=DEFAULT_CHAT_ID,
                 language_code="uz"):
        self.bot = bot
        self.user_id = user_id
        self.chat_id = chat_id
        self.language_code = language_code
        self._update_id = 10_000
        self._message_id = 200

    def _build(self, payload: dict) -> Update:
        return Update.de_json(payload, self.bot)

    def _next_update_id(self):
        self._update_id += 1
        return self._update_id

    def _next_message_id(self):
        self._message_id += 1
        return self._message_id

    @property
    def _user(self):
        return {
            "id": self.user_id,
            "is_bot": False,
            "first_name": "Kamola",
            "username": "kamola_test",
            "language_code": self.language_code,
        }

    def _message_envelope(self, **extra):
        envelope = {
            "message_id": self._next_message_id(),
            "date": 1_700_000_000,
            "chat": {"id": self.chat_id, "type": "private"},
            "from": self._user,
        }
        envelope.update(extra)
        return envelope

    def text(self, text: str) -> Update:
        return self._build({
            "update_id": self._next_update_id(),
            "message": self._message_envelope(text=text),
        })

    def edited_text(self, text: str) -> Update:
        """A message the customer went back and EDITED.

        Worth having because `filters.TEXT` MATCHES one of these: PTB's
        `MessageFilter.check_update` tests `update.effective_message`, which
        resolves to `edited_message` — and `allowed_updates=None` (bot.py) means
        both bots actually receive them. But `update.message` is `None` on an
        edit, so any handler reaching through `update.message` raises
        AttributeError on an entirely ordinary customer action.
        """
        return self._build({
            "update_id": self._next_update_id(),
            "edited_message": self._message_envelope(
                text=text, edit_date=1_700_000_100,
            ),
        })

    def command(self, command: str) -> Update:
        """A slash command. Without the bot_command entity PTB's CommandHandler
        does not match, so a hand-rolled text update silently tests nothing."""
        body = command if command.startswith("/") else f"/{command}"
        # The entity covers the COMMAND TOKEN ONLY, never the whole text.
        # PTB reads the command as `text[1:entity.length]`, so spanning the
        # arguments too made "/start ref_ABC123" parse as the command
        # "start ref_ABC123" — matching no CommandHandler at all. Deep links
        # were therefore inexpressible here, which is part of why the dropped
        # referral (tests/telegram_bot/test_signup_journey_after_restart.py)
        # had no test. Unchanged for an argument-less command.
        return self._build({
            "update_id": self._next_update_id(),
            "message": self._message_envelope(
                text=body,
                entities=[{
                    "type": "bot_command",
                    "offset": 0,
                    "length": len(body.split(" ", 1)[0]),
                }],
            ),
        })

    def location(self, latitude: float, longitude: float, horizontal_accuracy=None) -> Update:
        location = {"latitude": latitude, "longitude": longitude}
        if horizontal_accuracy is not None:
            location["horizontal_accuracy"] = horizontal_accuracy
        return self._build({
            "update_id": self._next_update_id(),
            "message": self._message_envelope(location=location),
        })

    def contact(self, phone_number: str) -> Update:
        return self._build({
            "update_id": self._next_update_id(),
            "message": self._message_envelope(
                contact={
                    "phone_number": phone_number,
                    "first_name": "Kamola",
                    "user_id": self.user_id,
                }
            ),
        })

    def photo(self, caption: str = None, file_id: str = "photo-file-id") -> Update:
        """A photo, in Telegram's real ascending-size form."""
        extra = {
            "photo": [
                {"file_id": f"{file_id}-s", "file_unique_id": "u-s",
                 "width": 90, "height": 90, "file_size": 1234},
                {"file_id": file_id, "file_unique_id": "u-l",
                 "width": 1280, "height": 1280, "file_size": 98765},
            ]
        }
        if caption is not None:
            extra["caption"] = caption
        return self._build({
            "update_id": self._next_update_id(),
            "message": self._message_envelope(**extra),
        })

    def document(self, file_name: str = "receipt.pdf", mime_type: str = "application/pdf",
                 file_id: str = "doc-file-id", caption: str = None) -> Update:
        extra = {
            "document": {
                "file_id": file_id,
                "file_unique_id": "u-doc",
                "file_name": file_name,
                "mime_type": mime_type,
                "file_size": 20480,
            }
        }
        if caption is not None:
            extra["caption"] = caption
        return self._build({
            "update_id": self._next_update_id(),
            "message": self._message_envelope(**extra),
        })

    def voice(self, file_id: str = "voice-file-id") -> Update:
        return self._build({
            "update_id": self._next_update_id(),
            "message": self._message_envelope(
                voice={
                    "file_id": file_id,
                    "file_unique_id": "u-voice",
                    "duration": 7,
                    "mime_type": "audio/ogg",
                    "file_size": 8192,
                }
            ),
        })

    def video(self, file_id: str = "video-file-id", caption: str = None) -> Update:
        extra = {
            "video": {
                "file_id": file_id,
                "file_unique_id": "u-video",
                "width": 640,
                "height": 480,
                "duration": 5,
                "mime_type": "video/mp4",
                "file_size": 51200,
            }
        }
        if caption is not None:
            extra["caption"] = caption
        return self._build({
            "update_id": self._next_update_id(),
            "message": self._message_envelope(**extra),
        })

    def sticker(self, file_id: str = "sticker-file-id") -> Update:
        """A sticker — matches none of PHOTO/Document/VIDEO/etc, so this is
        what exercises the UNSUPPORTED branch of `build_support_payload`."""
        return self._build({
            "update_id": self._next_update_id(),
            "message": self._message_envelope(
                sticker={
                    "file_id": file_id,
                    "file_unique_id": "u-sticker",
                    "type": "regular",
                    "width": 512,
                    "height": 512,
                    "is_animated": False,
                    "is_video": False,
                }
            ),
        })

    def forwarded_text(self, text: str, sender_name: str = "Dilnoza K") -> Update:
        """Text forwarded from a named user (`MessageOriginUser`)."""
        return self._build({
            "update_id": self._next_update_id(),
            "message": self._message_envelope(
                text=text,
                forward_origin={
                    "type": "user",
                    "date": 1_699_000_000,
                    "sender_user": {"id": 55_001, "is_bot": False, "first_name": sender_name},
                },
            ),
        })

    def tap(self, callback_data: str, message_id: int = None) -> Update:
        """An inline-button tap on the message the customer is looking at."""
        return self._build({
            "update_id": self._next_update_id(),
            "callback_query": {
                "id": f"cb{self._update_id}",
                "from": self._user,
                "chat_instance": "test-chat-instance",
                "data": callback_data,
                "message": {
                    "message_id": message_id or self._message_id,
                    "date": 1_700_000_000,
                    "chat": {"id": self.chat_id, "type": "private"},
                    "from": {"id": 42, "is_bot": True, "first_name": "BlueStream"},
                    "text": "previous bot message",
                },
            },
        })
