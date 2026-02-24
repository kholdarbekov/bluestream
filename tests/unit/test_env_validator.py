"""Unit tests for environment validation utilities."""

import pytest

from business_app.utils.env_validator import (
    EnvironmentValidator,
    check_security_issues,
    get_missing_vars,
    validate_environment_startup,
)


def _unset(monkeypatch, names):
    for name in names:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.unit
class TestEnvironmentValidator:
    def test_validate_all_production_reports_missing_and_invalid_values(self, monkeypatch):
        _unset(
            monkeypatch,
            [
                "DATABASE_URL",
                "DB_PASSWORD",
                "REDIS_URL",
                "JWT_SECRET_KEY",
                "SENTRY_DSN",
                "SENDGRID_API_KEY",
                "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY",
                "AWS_S3_BUCKET",
                "PAYME_TEST_MODE",
                "CLICK_TEST_MODE",
            ],
        )
        monkeypatch.setenv("SECRET_KEY", "short")
        monkeypatch.setenv("DEBUG", "true")

        validator = EnvironmentValidator("production")
        is_valid, errors, warnings = validator.validate_all()

        assert is_valid is False
        assert any("SECRET_KEY must be at least 32 characters" in e for e in errors)
        assert any("JWT_SECRET_KEY is required" in e for e in errors)
        assert any("REDIS_URL is required" in e for e in errors)
        assert any("DEBUG mode must not be enabled" in e for e in errors)
        assert any("PAYME_TEST_MODE is enabled in production" in w for w in warnings)

    def test_validate_all_development_allows_missing_db_password_with_warning(self, monkeypatch):
        _unset(monkeypatch, ["DATABASE_URL", "DB_PASSWORD", "REDIS_URL", "GOOGLE_MAPS_API_KEY"])
        monkeypatch.setenv("SECRET_KEY", "a" * 32)
        monkeypatch.setenv("FLASK_ENV", "development")

        validator = EnvironmentValidator("development")
        is_valid, errors, warnings = validator.validate_all()

        assert is_valid is True
        assert errors == []
        assert any("DB_PASSWORD not set for development" in w for w in warnings)

    def test_validate_specific_var(self):
        validator = EnvironmentValidator("testing")

        assert validator.validate_specific_var("SECRET_KEY", "x" * 40)[0] is True
        assert validator.validate_specific_var("SECRET_KEY", "short")[0] is False

        assert validator.validate_specific_var("DATABASE_URL", "postgresql://u:p@db:5432/app")[0] is True
        assert validator.validate_specific_var("DATABASE_URL", "invalid-url")[0] is False

        assert validator.validate_specific_var("REDIS_URL", "redis://cache:6379/0")[0] is True
        assert validator.validate_specific_var("REDIS_URL", "http://cache:6379/0")[0] is False

        assert validator.validate_specific_var("SENTRY_DSN", "https://example.ingest.sentry.io/1")[0] is True
        assert validator.validate_specific_var("SENTRY_DSN", "http://example.com/1")[0] is False

        assert validator.validate_specific_var("DEBUG", "true")[0] is True
        assert validator.validate_specific_var("DEBUG", "maybe")[0] is False

    def test_suggest_fixes_includes_expected_guidance(self, monkeypatch):
        _unset(monkeypatch, ["SECRET_KEY", "JWT_SECRET_KEY", "SENTRY_DSN", "SENDGRID_API_KEY"])
        validator = EnvironmentValidator("production")

        suggestions = validator.suggest_fixes()

        assert any("Generate SECRET_KEY" in s for s in suggestions)
        assert any("Generate JWT_SECRET_KEY" in s for s in suggestions)
        assert any("Sentry" in s for s in suggestions)
        assert any("SendGrid" in s for s in suggestions)


@pytest.mark.unit
class TestEnvironmentHelpers:
    def test_validate_environment_startup_fails_hard_in_production(self, monkeypatch):
        class _Logger:
            def __init__(self):
                self.messages = []

            def warning(self, msg):
                self.messages.append(("warning", msg))

            def error(self, msg):
                self.messages.append(("error", msg))

            def critical(self, msg):
                self.messages.append(("critical", msg))

            def info(self, msg):
                self.messages.append(("info", msg))

        class _App:
            logger = _Logger()

        _unset(monkeypatch, ["SECRET_KEY", "JWT_SECRET_KEY", "REDIS_URL", "DB_PASSWORD", "DATABASE_URL"])
        monkeypatch.setenv("FLASK_ENV", "production")
        result = validate_environment_startup(_App())

        assert result is False

    def test_get_missing_vars_extracts_required_variable_names(self, monkeypatch):
        _unset(monkeypatch, ["SECRET_KEY", "JWT_SECRET_KEY", "REDIS_URL", "DB_PASSWORD", "DATABASE_URL"])
        missing = get_missing_vars("production")

        assert "SECRET_KEY" in missing
        assert "JWT_SECRET_KEY" in missing
        assert "REDIS_URL" in missing

    def test_check_security_issues_detects_by_severity(self, monkeypatch):
        monkeypatch.setenv("DEBUG", "true")
        monkeypatch.setenv("SECRET_KEY", "dev-secret-key-change")
        monkeypatch.setenv("CORS_ORIGINS", "http://localhost:3000,https://app.example.com")

        issues = check_security_issues("production")

        assert any("DEBUG mode enabled" in issue for issue in issues["critical"])
        assert any("placeholder" in issue.lower() for issue in issues["high"])
        assert any("localhost" in issue for issue in issues["medium"])
