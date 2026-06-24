from pydantic import BaseModel, Field


class InboundSupportMessageRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=4096)


class AdminSupportReplyRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=4096)


class AdminStartConversationRequest(BaseModel):
    user_id: int = Field(..., gt=0)
    content: str = Field(..., min_length=1, max_length=4096)
