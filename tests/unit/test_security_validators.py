"""Unit tests for security validators and API decorators."""

import re

import pytest
from flask import Flask, jsonify

from business_app.utils.security_validators import (
    SecurityValidator,
    validate_password_strength,
    validate_user_data,
)


@pytest.mark.unit
@pytest.mark.security
class TestSecurityValidatorCore:
    def test_password_strength_rules(self):
        assert SecurityValidator.validate_password_strength("") == (False, "Password is required")
        assert SecurityValidator.validate_password_strength("S1!x7") == (False, "Password must be at least 8 characters long")
        assert SecurityValidator.validate_password_strength("alllowercase1!") == (False, "Password must contain at least one uppercase letter")
        assert SecurityValidator.validate_password_strength("ALLUPPERCASE1!") == (False, "Password must contain at least one lowercase letter")
        assert SecurityValidator.validate_password_strength("NoDigits!!") == (False, "Password must contain at least one digit")
        assert SecurityValidator.validate_password_strength("NoSpecial123") == (False, "Password must contain at least one special character")

        weak_ok, weak_msg = SecurityValidator.validate_password_strength("Password123!")
        assert weak_ok is False
        assert "weak pattern" in weak_msg

        seq_ok, seq_msg = SecurityValidator.validate_password_strength("Abcdefg1!")
        assert seq_ok is False
        assert "sequential letters" in seq_msg

        rep_ok, rep_msg = SecurityValidator.validate_password_strength("AAAstrong1!")
        assert rep_ok is False
        assert "repeated characters" in rep_msg

        assert SecurityValidator.validate_password_strength("Strong!9xZ")[0] is True

    def test_email_validation(self):
        assert SecurityValidator.validate_email("") == (False, "Email is required")
        assert SecurityValidator.validate_email("bad-format")[0] is False
        assert SecurityValidator.validate_email("UPPER@example.com") == (False, "Email must be lowercase")
        assert SecurityValidator.validate_email("user..name@example.com")[0] is False
        assert SecurityValidator.validate_email("user@mailinator.com")[0] is False
        assert SecurityValidator.validate_email("valid.user@example.com") == (True, "Email is valid")

    def test_phone_and_telegram_validation(self):
        assert SecurityValidator.validate_phone("") == (True, "Phone is optional")
        assert SecurityValidator.validate_phone("+998901234567") == (True, "Phone is valid")
        assert SecurityValidator.validate_phone("998901234567")[0] is False
        assert SecurityValidator.validate_phone("+" + "1" * 25)[0] is False

        assert SecurityValidator.validate_telegram_id("") == (True, "Telegram ID is optional")
        assert SecurityValidator.validate_telegram_id("abc")[0] is False
        assert SecurityValidator.validate_telegram_id("1234")[0] is False
        assert SecurityValidator.validate_telegram_id("1234567890")[0] is True

    def test_role_status_tax_business_validation(self):
        assert SecurityValidator.validate_role("admin")[0] is True
        assert SecurityValidator.validate_role("unknown")[0] is False

        assert SecurityValidator.validate_status("active")[0] is True
        assert SecurityValidator.validate_status("wrong")[0] is False

        assert SecurityValidator.validate_tax_id("") == (True, "Tax ID is optional")
        assert SecurityValidator.validate_tax_id("ab-123")[0] is False
        assert SecurityValidator.validate_tax_id("AB-12345")[0] is True

        assert SecurityValidator.validate_business_type("") == (True, "Business type is optional")
        assert SecurityValidator.validate_business_type("corporation")[0] is True
        assert SecurityValidator.validate_business_type("invalid-type")[0] is False

    def test_sanitize_hash_token_and_hash_validation(self):
        sanitized = SecurityValidator.sanitize_user_input(" <script>alert(1)</script> ")
        assert sanitized == "scriptalert1/script"
        assert SecurityValidator.sanitize_user_input("x" * 10, max_length=5) is None
        assert SecurityValidator.sanitize_user_input("   ") is None

        token_a = SecurityValidator.generate_secure_token(24)
        token_b = SecurityValidator.generate_secure_token(24)
        assert len(token_a) == 24
        assert re.fullmatch(r"[A-Za-z0-9]{24}", token_a)
        assert token_a != token_b

        digest = SecurityValidator.hash_sensitive_data("secret-value")
        assert digest == SecurityValidator.hash_sensitive_data("secret-value")
        assert len(digest) == 64

        valid_hash = "$2b$12$" + ("a" * 53)
        assert SecurityValidator.validate_password_hash(valid_hash)[0] is True
        assert SecurityValidator.validate_password_hash("$1$bad")[0] is False
        assert SecurityValidator.validate_password_hash("")[0] is False

    def test_jwt_and_bulk_user_validation(self):
        valid_token = ("a" * 34) + "." + ("b" * 34) + "." + ("c" * 34)
        assert SecurityValidator.validate_jwt_token(valid_token)[0] is True
        assert SecurityValidator.validate_jwt_token("short.token")[0] is False
        assert SecurityValidator.validate_jwt_token(("a" * 40) + "." + ("b" * 40) + "." + ("c*" * 20))[0] is False

        payload = {
            "email": "bad_email",
            "phone": "not-a-phone",
            "role": "wrong",
            "status": "wrong",
            "telegram_id": "bad",
            "tax_id": "bad lower",
            "business_type": "wrong",
            "first_name": "  Jane<script> ",
            "company_name": "Acme Inc",
        }
        errors = SecurityValidator.validate_all_user_fields(payload)
        assert len(errors) >= 6
        assert payload["first_name"] == "Janescript"


@pytest.mark.unit
@pytest.mark.security
class TestSecurityValidatorDecorators:
    @pytest.fixture
    def decorator_app(self):
        app = Flask(__name__)
        app.config["TESTING"] = True

        @app.post("/password-check")
        @validate_password_strength
        def password_check():
            return jsonify({"ok": True}), 200

        @app.post("/user-check")
        @validate_user_data
        def user_check():
            return jsonify({"ok": True}), 200

        return app

    def test_validate_password_strength_decorator(self, decorator_app):
        client = decorator_app.test_client()

        weak = client.post("/password-check", json={"password": "weak"})
        assert weak.status_code == 400
        assert "Password validation failed" in weak.get_json()["error"]

        strong = client.post("/password-check", json={"password": "G7!xQz9Lm"})
        assert strong.status_code == 200
        assert strong.get_json()["ok"] is True

    def test_validate_user_data_decorator(self, decorator_app):
        client = decorator_app.test_client()

        invalid = client.post("/user-check", json={"email": "bad", "role": "invalid"})
        assert invalid.status_code == 400
        body = invalid.get_json()
        assert body["error"] == "Validation failed"
        assert len(body["details"]) >= 1

        valid = client.post(
            "/user-check",
            json={"email": "valid@example.com", "role": "customer", "status": "active"},
        )
        assert valid.status_code == 200
