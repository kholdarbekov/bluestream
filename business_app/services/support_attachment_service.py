"""Stream a support attachment from Telegram on demand.

We store `file_id`s, never bytes (spec decision D1). Telegram's `getFile` hands
back a `file_path` that expires after roughly an hour, so paths are cached in
Redis just under that. The `file_id` itself stays valid indefinitely — for as
long as the bot token does.
"""

import logging
import mimetypes
import os
from urllib.parse import quote

import requests
from flask import Response, current_app, stream_with_context
from werkzeug.http import dump_options_header

from business_app import redis_client
from business_app.models.support import SupportMessage
from business_app.utils.exceptions import (
    AttachmentTooLargeError,
    AttachmentUnavailableError,
    NotFoundError,
)
from business_app.utils.helpers import scrub_bot_token

logger = logging.getLogger(__name__)

# The Bot API refuses to download anything larger, though a customer can send
# up to 2 GB. Spec D1.2.
TELEGRAM_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024

# Telegram's own file_path lifetime is ~3600s; stay comfortably inside it.
FILE_PATH_TTL_SECONDS = 2700

_INLINE_PREFIXES = ("image/", "video/", "audio/")


class SupportAttachmentService:
    @property
    def _bot_token(self):
        token = current_app.config.get("TELEGRAM_BOT_TOKEN")
        if not token:
            raise AttachmentUnavailableError("Telegram bot token is not configured")
        return token

    def _scrub_token(self, text: str) -> str:
        """A `requests` connection/DNS/timeout error embeds the full request
        URL, which contains the bot token. Never let that reach a log line."""
        return scrub_bot_token(text, current_app.config.get("TELEGRAM_BOT_TOKEN"))

    def resolve_file_path(self, file_id: str) -> str:
        cache_key = f"support:tg_file_path:{file_id}"
        try:
            cached = redis_client.get(cache_key)
        except Exception as exc:
            logger.warning("Redis unavailable for attachment path cache: %s", exc)
            cached = None
        if cached:
            return cached.decode() if isinstance(cached, bytes) else cached

        # `params=` lets `requests` handle query-string encoding, so a file_id
        # containing `&`/`#`/etc. can never split the query string — that
        # guarantee lives in the library, not in a `safe=` argument someone
        # could later drop or "simplify" away.
        url = f"https://api.telegram.org/bot{self._bot_token}/getFile"
        try:
            response = requests.get(url, params={"file_id": file_id}, timeout=15)
            body = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise AttachmentUnavailableError(f"Telegram getFile failed: {self._scrub_token(str(exc))}")

        if not body.get("ok"):
            # A rotated bot token lands here for every historical file_id.
            raise AttachmentUnavailableError(body.get("description") or "Telegram rejected the file id")

        file_path = body["result"]["file_path"]
        try:
            redis_client.setex(cache_key, FILE_PATH_TTL_SECONDS, file_path)
        except Exception as exc:
            logger.warning("Could not cache attachment path: %s", exc)
        return file_path

    def stream_attachment(self, message_id: int) -> Response:
        """SECURITY: the file_id is resolved from the message row, never from the
        request. Accepting a caller-supplied file_id would turn our bot token
        into an open Telegram download proxy."""
        message = SupportMessage.query.get(message_id)
        if not message or not message.telegram_file_id:
            raise NotFoundError(f"Message {message_id} has no attachment")

        size = int(message.attachment_size or 0)
        if size > TELEGRAM_MAX_DOWNLOAD_BYTES:
            raise AttachmentTooLargeError(
                f"Attachment is {size} bytes; Telegram will not serve over {TELEGRAM_MAX_DOWNLOAD_BYTES}"
            )

        file_path = self.resolve_file_path(message.telegram_file_id)
        download_url = f"https://api.telegram.org/file/bot{self._bot_token}/{file_path}"

        try:
            upstream = requests.get(download_url, stream=True, timeout=30)
        except requests.RequestException as exc:
            raise AttachmentUnavailableError(f"Telegram download failed: {self._scrub_token(str(exc))}")
        if upstream.status_code >= 400:
            raise AttachmentUnavailableError(f"Telegram download returned {upstream.status_code}")

        mime = message.attachment_mime_type or mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        filename = message.attachment_file_name or os.path.basename(file_path)
        disposition = "inline" if mime.startswith(_INLINE_PREFIXES) else "attachment"

        response = Response(
            stream_with_context(upstream.iter_content(chunk_size=8192)),
            mimetype=mime,
        )
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
        `latin-1` header encoding even for a non-ASCII filename (routine here —
        our customers are Uzbek/Russian-speaking) or one containing a double
        quote. Uses the RFC 5987 two-form `filename` / `filename*` pair, the
        same approach Werkzeug's own `send_file` uses."""
        opts = {"filename": filename}
        try:
            filename.encode("ascii")
        except UnicodeEncodeError:
            opts = {
                "filename": filename.encode("ascii", "replace").decode(),
                "filename*": f"UTF-8''{quote(filename, safe='')}",
            }
        return dump_options_header(disposition, opts)
