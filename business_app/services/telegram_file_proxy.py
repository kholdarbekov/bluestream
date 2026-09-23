"""Stream a Telegram-hosted file on demand, by `file_id`.

We keep Telegram `file_id`s, never bytes: support attachments (customer bot) and visit photos
(staff bot, D27). A `file_id` works only through the bot that received it, so a proxy is built per
bot token. Telegram's `getFile` hands back a `file_path` that expires after roughly an hour, so
paths are cached in Redis just under that, under a per-caller namespace.

SECURITY: callers resolve the `file_id` from their own row, never from the request. Accepting a
caller-supplied file_id would turn a bot token into an open Telegram download proxy.
"""

import logging
import mimetypes
import os
from typing import Optional
from urllib.parse import quote

import requests
from flask import Response, stream_with_context
from werkzeug.http import dump_options_header

from business_app import redis_client
from business_app.utils.exceptions import AttachmentUnavailableError
from business_app.utils.helpers import scrub_bot_token

logger = logging.getLogger(__name__)

# Telegram's own file_path lifetime is ~3600s; stay comfortably inside it.
FILE_PATH_TTL_SECONDS = 2700

_INLINE_PREFIXES = ("image/", "video/", "audio/")


class TelegramFileProxy:
    def __init__(self, token: Optional[str], *, cache_namespace: str):
        self._token = token
        self._cache_namespace = cache_namespace

    @property
    def _bot_token(self) -> str:
        if not self._token:
            raise AttachmentUnavailableError("Telegram bot token is not configured")
        return self._token

    def _scrub_token(self, text: str) -> str:
        """A `requests` connection/DNS/timeout error embeds the full request
        URL, which contains the bot token. Never let that reach a log line."""
        return scrub_bot_token(text, self._token)

    def resolve_file_path(self, file_id: str) -> str:
        cache_key = f"{self._cache_namespace}:tg_file_path:{file_id}"
        try:
            cached = redis_client.get(cache_key)
        except Exception as exc:
            logger.warning("Redis unavailable for Telegram file path cache: %s", exc)
            cached = None
        if cached:
            return cached.decode() if isinstance(cached, bytes) else cached

        # `params=` lets `requests` handle query-string encoding, so a file_id
        # containing `&`/`#`/etc. can never split the query string.
        url = f"https://api.telegram.org/bot{self._bot_token}/getFile"
        try:
            response = requests.get(url, params={"file_id": file_id}, timeout=15)
            body = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise AttachmentUnavailableError(f"Telegram getFile failed: {self._scrub_token(str(exc))}")

        if not body.get("ok"):
            # A file id from another bot, or one Telegram has forgotten, lands here.
            raise AttachmentUnavailableError(body.get("description") or "Telegram rejected the file id")

        file_path = body["result"]["file_path"]
        try:
            redis_client.setex(cache_key, FILE_PATH_TTL_SECONDS, file_path)
        except Exception as exc:
            logger.warning("Could not cache Telegram file path: %s", exc)
        return file_path

    def stream(self, file_id: str, *, mime: Optional[str] = None, filename: Optional[str] = None) -> Response:
        file_path = self.resolve_file_path(file_id)
        download_url = f"https://api.telegram.org/file/bot{self._bot_token}/{file_path}"

        try:
            upstream = requests.get(download_url, stream=True, timeout=30)
        except requests.RequestException as exc:
            raise AttachmentUnavailableError(f"Telegram download failed: {self._scrub_token(str(exc))}")
        if upstream.status_code >= 400:
            raise AttachmentUnavailableError(f"Telegram download returned {upstream.status_code}")

        mime = mime or mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        filename = filename or os.path.basename(file_path)
        disposition = "inline" if mime.startswith(_INLINE_PREFIXES) else "attachment"

        response = Response(stream_with_context(upstream.iter_content(chunk_size=8192)), mimetype=mime)
        response.headers["Content-Disposition"] = self._content_disposition_header(disposition, filename)
        response.headers["Cache-Control"] = "private, max-age=3600"
        content_length = upstream.headers.get("Content-Length")
        if content_length:
            response.headers["Content-Length"] = content_length
        # Without this, a client disconnect mid-stream abandons the generator
        # and the pooled upstream connection is only released at GC.
        response.call_on_close(upstream.close)
        return response

    @staticmethod
    def _content_disposition_header(disposition: str, filename: str) -> str:
        """Build a `Content-Disposition` header that survives Werkzeug's
        `latin-1` header encoding even for a non-ASCII filename or one containing
        a double quote. Uses the RFC 5987 two-form `filename` / `filename*` pair,
        the same approach Werkzeug's own `send_file` uses."""
        opts = {"filename": filename}
        try:
            filename.encode("ascii")
        except UnicodeEncodeError:
            opts = {
                "filename": filename.encode("ascii", "replace").decode(),
                "filename*": f"UTF-8''{quote(filename, safe='')}",
            }
        return dump_options_header(disposition, opts)
