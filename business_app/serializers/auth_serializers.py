"""
Authentication Serializers for the Water Business Platform using Pydantic v2
This file contains Pydantic models for authentication-related data serialization
"""
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field, field_validator, ConfigDict
from pydantic.alias_generators import to_camel

from business_app.utils.validators import (
    validate_uzbekistan_phone,
    normalize_uzbekistan_phone,
    mask_phone_number
)


class PhoneRegistrationInitRequest(BaseModel):
    """Schema for initiating phone registration - Step 1: Request OTP"""
    model_config = ConfigDict(str_strip_whitespace=True)

    phone: str = Field(..., description="Uzbekistan phone number (+998 XX XXX XX XX)")
    preferred_language: str = Field(default='uz', description="Preferred language code")

    @field_validator('phone')
    @classmethod
    def validate_phone(cls, v: str) -> str:
        """Validate and normalize Uzbekistan phone number"""
        is_valid, error_msg, normalized = validate_uzbekistan_phone(v)
        if not is_valid:
            raise ValueError(error_msg)
        return normalized

    @field_validator('preferred_language')
    @classmethod
    def validate_language(cls, v: str) -> str:
        """Validate language code"""
        allowed_languages = ['uz', 'ru', 'en']
        if v not in allowed_languages:
            return 'uz'  # Default to Uzbek
        return v


class PhoneRegistrationInitResponse(BaseModel):
    """Response schema for phone registration init"""
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    phone_masked: str = Field(..., description="Masked phone number for display")
    expires_in: int = Field(default=180, description="OTP expiry time in seconds")
    resend_available_in: int = Field(default=60, description="Seconds until resend is available")


class PhoneRegistrationVerifyRequest(BaseModel):
    """Schema for verifying phone and completing registration - Step 2"""
    model_config = ConfigDict(str_strip_whitespace=True)

    phone: str = Field(..., description="Uzbekistan phone number (+998 XX XXX XX XX)")
    otp_code: str = Field(..., min_length=6, max_length=6, description="6-digit OTP code")
    first_name: str = Field(..., min_length=1, max_length=100, description="User first name")
    last_name: Optional[str] = Field(default=None, max_length=100, description="User last name")
    password: str = Field(..., min_length=8, description="User password (min 8 chars)")
    referral_code: Optional[str] = Field(default=None, description="Optional referral code")

    @field_validator('phone')
    @classmethod
    def validate_phone(cls, v: str) -> str:
        """Validate and normalize Uzbekistan phone number"""
        is_valid, error_msg, normalized = validate_uzbekistan_phone(v)
        if not is_valid:
            raise ValueError(error_msg)
        return normalized

    @field_validator('otp_code')
    @classmethod
    def validate_otp(cls, v: str) -> str:
        """Validate OTP code format"""
        if not v.isdigit():
            raise ValueError("OTP must contain only digits")
        if len(v) != 6:
            raise ValueError("OTP must be exactly 6 digits")
        return v

    @field_validator('first_name')
    @classmethod
    def validate_first_name(cls, v: str) -> str:
        """Validate and sanitize first name"""
        if not v or not v.strip():
            raise ValueError("First name is required")
        # Remove potentially dangerous characters
        import re
        sanitized = re.sub(r'[<>"\'\`&;|$(){}[\]\\]', '', v)
        return sanitized.strip()

    @field_validator('last_name')
    @classmethod
    def validate_last_name(cls, v: Optional[str]) -> Optional[str]:
        """Validate and sanitize last name"""
        if v is None:
            return None
        import re
        sanitized = re.sub(r'[<>"\'\`&;|$(){}[\]\\]', '', v)
        return sanitized.strip() if sanitized.strip() else None


class PhoneResendOtpRequest(BaseModel):
    """Schema for resending OTP"""
    model_config = ConfigDict(str_strip_whitespace=True)

    phone: str = Field(..., description="Uzbekistan phone number (+998 XX XXX XX XX)")

    @field_validator('phone')
    @classmethod
    def validate_phone(cls, v: str) -> str:
        """Validate and normalize Uzbekistan phone number"""
        is_valid, error_msg, normalized = validate_uzbekistan_phone(v)
        if not is_valid:
            raise ValueError(error_msg)
        return normalized


class UserResponse(BaseModel):
    """User response schema"""
    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel, populate_by_name=True)

    id: int
    phone: str
    email: Optional[str] = None
    first_name: str
    last_name: Optional[str] = None
    is_verified: bool
    registration_method: str
    registration_source: str
    preferred_language: str


class TokensResponse(BaseModel):
    """JWT tokens response schema"""
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    access_token: str
    refresh_token: str
    expires_in: int = Field(default=3600, description="Access token expiry in seconds")


class PhoneRegistrationVerifyResponse(BaseModel):
    """Response schema for successful phone registration"""
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    user: UserResponse
    tokens: TokensResponse
