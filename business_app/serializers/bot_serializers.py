"""Request shapes of the bot-to-backend endpoints in business_app/api/bot.py."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class TelegramReachabilityReport(BaseModel):
    """`POST /api/v1/bot/telegram-reachability` (spec 2026-10-09 §7.2 a, b).

    Shapes only: which statuses and sources exist is `TelegramReachabilityService`'s question, so
    they are checked there once, not restated here.
    """

    telegram_id: str = Field(..., min_length=1, max_length=50)
    status: str = Field(..., min_length=1, max_length=16)
    at: datetime
    source: str = Field("my_chat_member", min_length=1, max_length=16)

    @field_validator("at")
    @classmethod
    def _at_carries_an_offset(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("at must carry a UTC offset")
        return value


class WebLinkPreviewRequest(BaseModel):
    """`POST /api/v1/bot/web-link/preview` (spec 2026-10-19 D4).

    Shapes only: whether the token is alive is `CustomerTelegramLinkService`'s question, so an
    empty or truncated token reaches it and is answered WEB_LINK_EXPIRED like any dead link.
    """

    token: str = Field(..., max_length=64)
    telegram_id: int = Field(..., gt=0)


class WebLinkConfirmRequest(WebLinkPreviewRequest):
    """`POST /api/v1/bot/web-link/confirm`: the preview's fields plus what Telegram says about the user."""

    username: Optional[str] = Field(None, max_length=255)
    first_name: Optional[str] = Field(None, max_length=100)
    last_name: Optional[str] = Field(None, max_length=100)
