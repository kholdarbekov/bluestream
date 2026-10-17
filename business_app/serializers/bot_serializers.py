"""Request shapes of the bot-to-backend endpoints in business_app/api/bot.py."""

from datetime import datetime

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
