"""Unit tests for error handler mapper and sanitization helpers."""

from flask import g
import pytest
from flask_jwt_extended.exceptions import (
    CSRFError,
    FreshTokenRequired,
    InvalidHeaderError,
    InvalidQueryParamError,
    JWTDecodeError,
    NoAuthorizationError,
    RevokedTokenError,
    UserClaimsVerificationError,
    UserLookupError,
    WrongTokenError,
)
from jwt.exceptions import DecodeError

from business_app.utils.error_handlers import ErrorResponse, ExceptionMapper, sanitize_request_data
from business_app.utils.exceptions import (
    ConflictError,
    DeliveryError,
    ExternalServiceError,
    ForbiddenError,
    NotFoundError,
    RateLimitError,
    UnauthorizedError,
    ValidationError,
)


@pytest.mark.unit
class TestExceptionMapper:
    @pytest.mark.parametrize(
        "exception,expected_status,expected_type,expected_message",
        [
            pytest.param(ValidationError("validation failed"), 400, "VALIDATION_ERROR", "validation failed", id="validation"),
            pytest.param(NotFoundError("missing"), 404, "NOT_FOUND", "missing", id="not-found"),
            pytest.param(UnauthorizedError("unauth"), 401, "UNAUTHORIZED", "unauth", id="unauthorized"),
            pytest.param(ForbiddenError("forbidden"), 403, "FORBIDDEN", "forbidden", id="forbidden"),
            pytest.param(ConflictError("conflict"), 409, "CONFLICT", "conflict", id="conflict"),
            pytest.param(DeliveryError("delivery error"), 422, "DELIVERY_ERROR", "delivery error", id="delivery"),
            pytest.param(ExternalServiceError("service down"), 503, "SERVICE_UNAVAILABLE", "service down", id="service-unavailable"),
            pytest.param(RateLimitError("too many"), 429, "RATE_LIMIT_EXCEEDED", "too many", id="rate-limit"),
        ],
    )
    def test_get_error_info_for_business_exceptions(
        self,
        exception,
        expected_status,
        expected_type,
        expected_message,
    ):
        status_code, error_type, message = ExceptionMapper.get_error_info(exception)

        assert status_code == expected_status
        assert error_type == expected_type
        assert message == expected_message

    @pytest.mark.parametrize(
        "exception,expected_status,expected_type",
        [
            pytest.param(ValueError("bad value"), 400, "INVALID_VALUE", id="value-error"),
            pytest.param(TypeError("bad type"), 400, "TYPE_ERROR", id="type-error"),
            pytest.param(KeyError("missing_key"), 400, "MISSING_KEY", id="key-error"),
            pytest.param(ConnectionError("network down"), 503, "CONNECTION_ERROR", id="connection-error"),
            pytest.param(TimeoutError("timed out"), 504, "TIMEOUT", id="timeout-error"),
        ],
    )
    def test_get_error_info_for_standard_exceptions(self, exception, expected_status, expected_type):
        status_code, error_type, _ = ExceptionMapper.get_error_info(exception)
        assert status_code == expected_status
        assert error_type == expected_type

    def test_get_error_info_for_unmapped_exception(self):
        status_code, error_type, message = ExceptionMapper.get_error_info(RuntimeError("unexpected"))

        assert status_code == 500
        assert error_type == "INTERNAL_ERROR"
        assert message == "An unexpected error occurred"

    def test_jwt_exception_mapping_is_explicit_and_non_500(self):
        expected = {
            NoAuthorizationError: (401, "UNAUTHORIZED"),
            FreshTokenRequired: (401, "UNAUTHORIZED"),
            RevokedTokenError: (401, "UNAUTHORIZED"),
            UserLookupError: (401, "UNAUTHORIZED"),
            InvalidHeaderError: (401, "UNAUTHORIZED"),
            InvalidQueryParamError: (401, "UNAUTHORIZED"),
            JWTDecodeError: (401, "UNAUTHORIZED"),
            WrongTokenError: (401, "UNAUTHORIZED"),
            UserClaimsVerificationError: (403, "FORBIDDEN"),
            CSRFError: (401, "UNAUTHORIZED"),
            DecodeError: (401, "UNAUTHORIZED"),
        }

        for exception_class, mapping in expected.items():
            assert exception_class in ExceptionMapper.EXCEPTION_MAPPING
            assert ExceptionMapper.EXCEPTION_MAPPING[exception_class] == mapping
            assert mapping[0] < 500


@pytest.mark.unit
class TestErrorResponseAndSanitizer:
    def test_build_error_response_includes_request_metadata_and_trace_id(self, app):
        with app.test_request_context("/api/v1/orders", method="POST"):
            g.trace_id = "trace-123"
            body, status_code = ErrorResponse.build_error_response(
                error_type="VALIDATION_ERROR",
                message="invalid payload",
                details={"field": ["required"]},
                request_id="req-1",
                status_code=400,
            )

        assert status_code == 400
        assert body["error"] == "VALIDATION_ERROR"
        assert body["message"] == "invalid payload"
        assert body["details"] == {"field": ["required"]}
        assert body["path"] == "/api/v1/orders"
        assert body["method"] == "POST"
        assert body["request_id"] == "req-1"
        assert body["trace_id"] == "trace-123"
        assert "timestamp" in body

    def test_sanitize_request_data_redacts_nested_sensitive_fields(self):
        payload = {
            "email": "user@example.com",
            "password": "secret",
            "nested": {
                "access_token": "abc",
                "metadata": {"api_key": "xyz", "safe": "value"},
            },
            "cards": [
                {"card_number": "4111111111111111", "holder": "User"},
                {"safe": "ok"},
            ],
        }

        sanitized = sanitize_request_data(payload)
        assert sanitized["email"] == "user@example.com"
        assert sanitized["password"] == "[REDACTED]"
        assert sanitized["nested"]["access_token"] == "[REDACTED]"
        assert sanitized["nested"]["metadata"]["api_key"] == "[REDACTED]"
        assert sanitized["nested"]["metadata"]["safe"] == "value"
        assert sanitized["cards"][0]["card_number"] == "[REDACTED]"
        assert sanitized["cards"][0]["holder"] == "User"
        assert sanitized["cards"][1]["safe"] == "ok"
