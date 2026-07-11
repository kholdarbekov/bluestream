"""Tests that the Tax-Committee token-refresh alert email renders trilingually."""

import pytest

from business_app.services.email_template_service import get_email_template_service


@pytest.mark.unit
@pytest.mark.parametrize("language", ["en", "ru", "uz"])
def test_token_refresh_failed_email_renders(app, language):
    svc = get_email_template_service()
    rendered = svc.render_notification_email(
        "tax_committee_token_refresh_failed",
        language,
        {
            "reason": "http_error",
            "status_code": 401,
            "company_tin": "306522134",
            "timestamp": "2026-07-11 14:00:00 +05",
            "environment": "production",
            "body": "TIN mismatch detail",
        },
    )

    assert rendered is not None
    assert rendered["subject"]  # non-empty subject
    # Context surfaced in the body
    assert "306522134" in rendered["content"]
    assert "401" in rendered["content"]
    assert "http_error" in rendered["content"]
    assert "TIN mismatch detail" in rendered["content"]
