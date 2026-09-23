"""Stream a support attachment from Telegram on demand.

We store `file_id`s, never bytes (spec decision D1). The fetching is `TelegramFileProxy`'s, shared
with visit photos; this service owns only what is specific to a support message -- finding it, and
the Bot API's 20 MB download ceiling.
"""

from flask import Response, current_app

from business_app.models.support import SupportMessage
from business_app.services.telegram_file_proxy import TelegramFileProxy
from business_app.utils.exceptions import AttachmentTooLargeError, NotFoundError

# The Bot API refuses to download anything larger, though a customer can send
# up to 2 GB. Spec D1.2.
TELEGRAM_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024


class SupportAttachmentService:
    def __init__(self):
        # The CUSTOMER bot's token: support attachments arrive through telegram_bot. The "support"
        # namespace keeps the Redis key this cache has always used.
        self._proxy = TelegramFileProxy(current_app.config.get("TELEGRAM_BOT_TOKEN"), cache_namespace="support")

    def stream_attachment(self, message_id: int) -> Response:
        """SECURITY: the file_id is resolved from the message row, never from the request."""
        message = SupportMessage.query.get(message_id)
        if not message or not message.telegram_file_id:
            raise NotFoundError(f"Message {message_id} has no attachment")

        size = int(message.attachment_size or 0)
        if size > TELEGRAM_MAX_DOWNLOAD_BYTES:
            raise AttachmentTooLargeError(
                f"Attachment is {size} bytes; Telegram will not serve over {TELEGRAM_MAX_DOWNLOAD_BYTES}"
            )

        return self._proxy.stream(
            message.telegram_file_id,
            mime=message.attachment_mime_type,
            filename=message.attachment_file_name,
        )
