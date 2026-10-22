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
* :func:`telegram_entity_error` — a port of tdlib's two Markdown parsers, so
  a transport built with ``strict_entities=True`` refuses a message Telegram
  would refuse (``Can't parse entities``) instead of accepting any text.
* :class:`UpdateFactory` — real :class:`telegram.Update` objects, including the
  ``bot_command`` entity without which ``CommandHandler`` never matches.
* :class:`FlaskBackend` — a bot's HTTP client answered by the REAL Flask app, for
  the journeys that need the real backend behind the bot's own request funnel.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

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


# Port of tdlib's parse_markdown / parse_markdown_v2 (td/telegram/MessageEntity.cpp). It
# scans UTF-8 bytes, so offsets are byte offsets. One deliberate deviation: MarkdownV2 ">"
# block quotes and "![" custom emoji are always refused here (stricter than Telegram; the
# bot sends neither).
_LEGACY_OPENERS = (b"_", b"*", b"`", b"[")
_V2_RESERVED = b"_*[]()~`>#+-=|{}.!"
# MarkdownV2 opening tokens, longest first; each entity closes with its own token (TextUrl with "]").
_V2_OPENERS = ((b"```", "Pre"), (b"__", "Underline"), (b"||", "Spoiler"), (b"`", "Code"),
               (b"_", "Italic"), (b"*", "Bold"), (b"~", "Strikethrough"), (b"[", "TextUrl"))
_V2_CLOSERS = {"Pre": b"```", "Underline": b"__", "Spoiler": b"||", "Code": b"`",
               "Italic": b"_", "Bold": b"*", "Strikethrough": b"~", "TextUrl": b"]"}


def telegram_entity_error(text: str, parse_mode: Optional[str]) -> Optional[str]:
    if parse_mode == "Markdown":
        detail = _legacy_markdown_error(text.encode("utf-8"))
    elif parse_mode == "MarkdownV2":
        detail = _markdown_v2_error(text.encode("utf-8"))
    else:
        return None
    return None if detail is None else f"Bad Request: can't parse entities: {detail}"


def _legacy_markdown_error(data: bytes) -> Optional[str]:
    i, size = 0, len(data)
    while i < size:
        c = data[i:i + 1]
        if c == b"\\" and data[i + 1:i + 2] in _LEGACY_OPENERS:  # escapes count only OUTSIDE an entity
            i += 2
            continue
        if c not in _LEGACY_OPENERS:
            i += 1
            continue
        begin = i
        closer = b"```" if data.startswith(b"```", i) else (b"]" if c == b"[" else c)
        i += 3 if closer == b"```" else 1
        while i < size and not data.startswith(closer, i):  # no nesting, no escapes: first closer wins
            i += 1
        if i >= size:
            return f"Can't find end of the entity starting at byte offset {begin}"
        i += len(closer)
        if c == b"[" and data.startswith(b"(", i):  # [text](url); an unclosed url is not an error
            end = data.find(b")", i)
            i = size if end == -1 else end + 1
    return None


def _markdown_v2_error(data: bytes) -> Optional[str]:
    stack: list[tuple[str, int]] = []  # open entities: (type, opener byte offset)
    i, size = 0, len(data)
    while i < size:
        c, nxt = data[i:i + 1], data[i + 1:i + 2]
        if c == b"\\" and nxt and 0 < nxt[0] <= 126:
            i += 2
            continue
        top = stack[-1][0] if stack else None
        if c not in (b"`" if top in ("Code", "Pre") else _V2_RESERVED):
            i += 1
            continue
        if top and data.startswith(_V2_CLOSERS[top], i) and not (top == "Italic" and nxt == b"_"):
            stack.pop()
            i += len(_V2_CLOSERS[top])
            if top == "TextUrl" and data.startswith(b"(", i):
                url_begin = j = i + 1
                while j < size and data[j:j + 1] != b")":
                    j += 2 if data[j:j + 1] == b"\\" else 1
                if j >= size:
                    return f"Can't find end of a URL at byte offset {url_begin}"
                i = j + 1
            continue
        match = next(((tok, kind) for tok, kind in _V2_OPENERS if data.startswith(tok, i)), None)
        if match is None:
            return f"Character '{c.decode()}' is reserved and must be escaped with the preceding '\\'"
        stack.append((match[1], i))
        i += len(match[0])
    if stack:
        kind, begin = stack[-1]
        return f"Can't find end of {kind} entity at byte offset {begin}"
    return None


_ENTITY_ENDPOINTS = frozenset({"sendMessage", "editMessageText", "editMessageCaption"})
_EDIT_ENDPOINTS = frozenset({"editMessageText", "editMessageCaption", "editMessageReplyMarkup"})


def _reply_markup_of(params: dict) -> dict:
    raw = params.get("reply_markup")
    if raw is None:
        return {}
    return json.loads(raw) if isinstance(raw, str) else raw


DEFAULT_USER_ID = 700100200
DEFAULT_CHAT_ID = 700100200


@dataclass
class TelegramCall:
    """One Bot API call the bot made, as Telegram would have received it."""

    method: str
    params: dict
    # Description of the 4xx Telegram answered (scripted failure or strict-entity refusal).
    refused: Optional[str] = None

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


# Two-byte JPEG magic plus filler: enough that a test can assert the exact
# bytes reached the backend, and recognisable as an image if anything ever
# sniffs it.
DEFAULT_FILE_BYTES = b"\xff\xd8\xff\xe0JPEGBYTES"


class FakeTelegramTransport(BaseRequest):
    """The Bot API, answered from memory.

    Records what the bot SENT (which is what the customer would see) and can be
    scripted to fail the way Telegram really fails — ``Message is not
    modified``, ``Message to edit not found``, ``Can't parse entities`` — none
    of which any existing test simulates, and all of which appear in this
    project's production logs.
    """

    def __init__(self, *, strict_entities: bool = False):
        # When on, text/caption of send/edit calls is parsed like Telegram does and a
        # malformed parse_mode message is refused with a 400.
        self.strict_entities = strict_entities
        self.calls: list[TelegramCall] = []
        self._next_message_id = 5000
        # method -> callable(params) -> (status_code, payload_dict)
        self.failures: dict[str, Callable[[dict], tuple[int, dict]]] = {}
        # File DOWNLOADS, keyed by the name `getFile` handed out
        # (`<file_id>.jpg`). The Bot API answers these with raw bytes rather
        # than a JSON envelope, which is why `do_request` branches on the
        # HTTP verb below.
        self.downloads: dict[str, bytes] = {}
        # ...and every download the bot really MADE, in order. A burst of three
        # photos is three fetches, and without this record a test cannot tell
        # that from one picture downloaded and filed three times -- which is the
        # exact bug the burst queue exists to prevent.
        self.downloaded: list[str] = []

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
        call = TelegramCall(endpoint, params)
        self.calls.append(call)

        if method == "GET":
            # `File.download_as_bytearray` -> `BaseRequest.retrieve` is the ONE
            # call that is not a Bot API method: it GETs the file URL and the
            # body is the file itself. Every Bot API method goes out as POST
            # (`BaseRequest.post`), so the verb separates them exactly.
            self.downloaded.append(endpoint)
            return 200, self.downloads.get(endpoint, DEFAULT_FILE_BYTES)

        failure = self.failures.get(endpoint)
        if failure is not None:
            status, payload = failure(params)
            if status >= 400:
                call.refused = payload.get("description")
            return status, json.dumps(payload).encode("utf-8")

        if endpoint in _EDIT_ENDPOINTS and "keyboard" in _reply_markup_of(params):
            # Telegram always refuses an edit that carries a reply keyboard: only inline buttons can be edited.
            error = "Bad Request: inline keyboard expected"
            call.refused = error
            body = {"ok": False, "error_code": 400, "description": error}
            return 400, json.dumps(body).encode("utf-8")

        if self.strict_entities and endpoint in _ENTITY_ENDPOINTS:
            error = telegram_entity_error(
                params.get("text", params.get("caption", "")), params.get("parse_mode")
            )
            if error is not None:
                call.refused = error
                body = {"ok": False, "error_code": 400, "description": error}
                return 400, json.dumps(body).encode("utf-8")

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
        if endpoint == "getFile":
            file_id = str(params.get("file_id", ""))
            return {
                "file_id": file_id,
                "file_unique_id": f"u-{file_id}",
                "file_size": len(self.downloads.get(f"{file_id}.jpg", DEFAULT_FILE_BYTES)),
                "file_path": f"photos/{file_id}.jpg",
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

    @property
    def delivered(self) -> list[TelegramCall]:
        """What the customer actually received: ``shown`` minus the refused calls."""
        return [call for call in self.shown if call.refused is None]

    @property
    def visible_answers(self) -> list[TelegramCall]:
        """The answerCallbackQuery calls a user could SEE: the first per tap.

        Telegram accepts a second answer to the same callback query (HTTP 200,
        ``True``) and displays nothing — measured in the staff bot's prod logs on
        2026-09-10. Counting every recorded answer as "shown" is how popups that
        production drops stayed green here.
        """
        seen: set = set()
        visible: list[TelegramCall] = []
        for call in self.of("answerCallbackQuery"):
            query_id = call.params.get("callback_query_id")
            if query_id in seen:
                continue
            seen.add(query_id)
            visible.append(call)
        return visible

    def last_shown(self) -> TelegramCall:
        shown = self.shown
        assert shown, "the bot showed the customer nothing at all"
        return shown[-1]

    def texts(self) -> list[str]:
        return [call.text for call in self.shown]

    def reset(self):
        self.calls.clear()
        self.downloaded.clear()


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

    # The Bot API 7.0 forward marker both `photo(forwarded=)` and `location(forwarded=)` add:
    # the staff bot refuses a picture or a check-in pin that somebody else sent.
    _FORWARD_ORIGIN = {
        "type": "user",
        "date": 1_699_000_000,
        "sender_user": {"id": 55_001, "is_bot": False, "first_name": "Dilnoza K"},
    }

    def location(self, latitude: float, longitude: float, horizontal_accuracy=None,
                 forwarded: bool = False) -> Update:
        location = {"latitude": latitude, "longitude": longitude}
        if horizontal_accuracy is not None:
            location["horizontal_accuracy"] = horizontal_accuracy
        extra = {"location": location}
        if forwarded:
            extra["forward_origin"] = dict(self._FORWARD_ORIGIN)
        return self._build({
            "update_id": self._next_update_id(),
            "message": self._message_envelope(**extra),
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

    def photo(self, caption: str = None, file_id: str = "photo-file-id",
              file_unique_id: str = "u-l", forwarded: bool = False) -> Update:
        """A photo, in Telegram's real ascending-size form.

        `file_unique_id` is a parameter because a BURST is three photos with
        three different ones, and the sales visit files every one of them --
        a fixed id would let a test pass while the bot uploaded the same
        picture three times. `forwarded` adds the Bot API 7.0 marker the
        staff bot refuses on.
        """
        extra = {
            "photo": [
                {"file_id": f"{file_id}-s", "file_unique_id": f"{file_unique_id}-s",
                 "width": 90, "height": 90, "file_size": 1234},
                {"file_id": file_id, "file_unique_id": file_unique_id,
                 "width": 1280, "height": 1280, "file_size": 98765},
            ]
        }
        if caption is not None:
            extra["caption"] = caption
        if forwarded:
            extra["forward_origin"] = dict(self._FORWARD_ORIGIN)
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

    def bot_membership(self, new_status: str, old_status: str = "member", *, date: int = 1_700_000_100,
                       chat_type: str = "private") -> Update:
        """Telegram's `my_chat_member` update: this customer blocked the bot (`"kicked"`) or came
        back to it (`"member"`). `date` defaults to 100 s after every message this factory builds."""
        bot_user = {"id": 424242, "is_bot": True, "first_name": "BlueStream"}

        def member(status):
            payload = {"status": status, "user": bot_user}
            if status == "kicked":
                payload["until_date"] = 0
            return payload

        chat = {"id": self.chat_id, "type": chat_type}
        if chat_type != "private":
            chat["title"] = "Office"
        return self._build({
            "update_id": self._next_update_id(),
            "my_chat_member": {
                "chat": chat,
                "from": self._user,
                "date": date,
                "old_chat_member": member(old_status),
                "new_chat_member": member(new_status),
            },
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


class FlaskBackend:
    """A bot's HTTP client, answered by the REAL Flask app: every request the bot's own
    `_make_request` builds (headers, signature, body bytes) reaches the real routes.

    Speaks both clients' httpx calls: the customer bot's `get` / `post` and the staff
    bot's `request`. `seen` records (method, path, status, Authorization header).
    """

    def __init__(self, app):
        self.app = app
        self.seen = []

    async def request(self, method, url, headers=None, params=None, cookies=None, json=None, content=None):
        path = urlsplit(url).path
        body = {"json": json} if json is not None else {"data": content}
        resp = self.app.test_client().open(path, method=method, headers=headers, query_string=params, **body)
        self.seen.append((method, path, resp.status_code, (headers or {}).get("Authorization")))
        return _FlaskResponse(resp, method)

    async def get(self, url, **kwargs):
        return await self.request("GET", url, **kwargs)

    async def post(self, url, **kwargs):
        return await self.request("POST", url, **kwargs)


class _FlaskResponse:
    """The parts of an `httpx.Response` the bots read."""

    def __init__(self, resp, method):
        self.status_code = resp.status_code
        self.headers = dict(resp.headers)
        self.content = resp.get_data()
        self.text = resp.get_data(as_text=True)
        self.request = SimpleNamespace(method=method)
        self._json = resp.get_json(silent=True)

    def json(self):
        if self._json is None:
            raise ValueError("not JSON")
        return self._json
