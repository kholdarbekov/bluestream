"""
Reusable Telegram request client with retry/backoff for transient network failures.
"""
import asyncio
import logging

from telegram.error import NetworkError, TimedOut
from telegram.request import HTTPXRequest

logger = logging.getLogger(__name__)


class ResilientHTTPXRequest(HTTPXRequest):
    """HTTPXRequest wrapper that retries transient Telegram transport errors."""

    def __init__(
        self,
        *args,
        max_retries: int = 3,
        retry_backoff_seconds: float = 0.75,
        retry_max_backoff_seconds: float = 5.0,
        **kwargs,
    ):
        # Retry count includes the initial attempt, so clamp to at least one try.
        self.max_retries = max(1, int(max_retries))
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))
        self.retry_max_backoff_seconds = max(0.0, float(retry_max_backoff_seconds))
        super().__init__(*args, **kwargs)

    async def do_request(self, *args, **kwargs):
        """Retry transient network/timeout failures with exponential backoff."""
        for attempt in range(1, self.max_retries + 1):
            try:
                return await super().do_request(*args, **kwargs)
            except (TimedOut, NetworkError) as exc:
                if attempt >= self.max_retries:
                    raise

                delay = min(
                    self.retry_backoff_seconds * (2 ** (attempt - 1)),
                    self.retry_max_backoff_seconds,
                )
                logger.warning(
                    "Telegram request failed (%s). Retrying %d/%d in %.2fs.",
                    type(exc).__name__,
                    attempt + 1,
                    self.max_retries,
                    delay,
                )
                if delay > 0:
                    await asyncio.sleep(delay)
