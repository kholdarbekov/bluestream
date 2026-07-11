"""Tests for the best-effort Slack incoming-webhook alert util."""

from unittest.mock import Mock, patch

import pytest

from business_app.utils.slack_alerts import send_slack_alert


@pytest.mark.unit
class TestSendSlackAlert:
    def test_posts_text_to_configured_webhook(self, app):
        app.config["SLACK_ALERTS_WEBHOOK_URL"] = "https://hooks.slack.test/T/B/xyz"
        mock_resp = Mock()
        mock_resp.raise_for_status.return_value = None
        with patch("business_app.utils.slack_alerts.requests.post", return_value=mock_resp) as mock_post:
            result = send_slack_alert("hello world")

        assert result is True
        assert mock_post.call_count == 1
        called = mock_post.call_args
        assert called.args[0] == "https://hooks.slack.test/T/B/xyz"
        assert called.kwargs["json"] == {"text": "hello world"}
        assert called.kwargs["timeout"] == 5

    def test_returns_false_and_skips_when_unconfigured(self, app):
        app.config["SLACK_ALERTS_WEBHOOK_URL"] = None
        with patch("business_app.utils.slack_alerts.requests.post") as mock_post:
            result = send_slack_alert("hello")

        assert result is False
        mock_post.assert_not_called()

    def test_returns_false_and_never_raises_on_http_error(self, app):
        app.config["SLACK_ALERTS_WEBHOOK_URL"] = "https://hooks.slack.test/T/B/xyz"
        with patch(
            "business_app.utils.slack_alerts.requests.post",
            side_effect=RuntimeError("connection refused"),
        ):
            result = send_slack_alert("hello")

        assert result is False

    def test_http_failure_does_not_log_webhook_url(self, app):
        app.config["SLACK_ALERTS_WEBHOOK_URL"] = "https://hooks.slack.test/T/B/SECRETTOKEN"
        import requests as _requests
        resp = Mock()
        resp.raise_for_status.side_effect = _requests.exceptions.HTTPError(
            response=Mock(status_code=500)
        )
        with patch("business_app.utils.slack_alerts.requests.post", return_value=resp), \
             patch.object(app.logger, "warning") as mock_warn, \
             patch.object(app.logger, "exception") as mock_exc:
            result = send_slack_alert("hi")
        assert result is False
        mock_exc.assert_not_called()
        logged = " ".join(str(c) for c in mock_warn.call_args_list)
        assert "SECRETTOKEN" not in logged
        assert "hooks.slack.test" not in logged
