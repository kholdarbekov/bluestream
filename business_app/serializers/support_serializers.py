from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, model_validator

from shared.enums import SupportMessageType


class InboundSupportMessageRequest(BaseModel):
    content: Optional[str] = Field(None, max_length=4096)
    message_type: SupportMessageType = SupportMessageType.TEXT
    telegram_file_id: Optional[str] = Field(None, max_length=256)
    attachment_mime_type: Optional[str] = Field(None, max_length=128)
    attachment_file_name: Optional[str] = Field(None, max_length=255)
    attachment_size: Optional[int] = Field(None, ge=0)
    latitude: Optional[float] = Field(None, ge=-90, le=90)
    longitude: Optional[float] = Field(None, ge=-180, le=180)
    forwarded_from: Optional[str] = Field(None, max_length=255)
    forwarded_origin_type: Optional[str] = Field(None, max_length=32)
    forwarded_date: Optional[datetime] = None

    @model_validator(mode="after")
    def _require_a_payload(self):
        """Text, a file, or coordinates — `unsupported` alone may be empty.

        The service re-checks this; keeping it here too turns a malformed bot
        payload into a rejection naming the field, rather than a generic one.
        """
        has_text = bool((self.content or "").strip())
        has_file = bool(self.telegram_file_id)
        has_coords = self.latitude is not None and self.longitude is not None
        if not (has_text or has_file or has_coords) and self.message_type != SupportMessageType.UNSUPPORTED:
            raise ValueError("content, telegram_file_id, or latitude/longitude is required")
        return self


class AdminSupportReplyRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=4096)


class AdminStartConversationRequest(BaseModel):
    user_id: int = Field(..., gt=0)
    content: str = Field(..., min_length=1, max_length=4096)


class AdminSupportLocationRequest(BaseModel):
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
