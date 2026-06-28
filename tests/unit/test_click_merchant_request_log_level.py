"""merchant_request log-level classification for non-JSON 200 responses.

Click's supplementary `ofd_data` endpoint intermittently returns HTTP 200 with
a non-JSON body. The fiscalization caller already tolerates this (the receipt is
still submitted; only the QR URL is skipped), so the inner parse failure should
log at WARNING for ofd_data — not ERROR — to avoid false fiscalization alarms.
A parse failure on a critical endpoint (e.g. payment_status) stays ERROR.
"""

from unittest.mock import Mock, patch

import pytest

from business_app.services.click_payment_provider_service import ClickPaymentProviderService
from business_app.utils.exceptions import PaymentError


def _configure(app):
    app.config["CLICK_TEST_MODE"] = False
    app.config["CLICK_MERCHANT_ID"] = "58228"
    app.config["CLICK_MERCHANT_API_USER_ID"] = "merchant-user"
    app.config["CLICK_MERCHANT_API_SECRET_KEY"] = "merchant-secret"


def _non_json_200_response():
    resp = Mock()
    resp.raise_for_status = Mock(return_value=None)
    resp.json = Mock(side_effect=ValueError("Expecting value: line 1 column 1 (char 0)"))
    resp.status_code = 200
    resp.text = ""
    return resp


@pytest.mark.unit
def test_ofd_data_non_json_200_logs_warning_not_error(app):
    _configure(app)
    with app.app_context():
        provider = ClickPaymentProviderService()
        with patch(
            "business_app.services.click_payment_provider_service.request_with_retry",
            return_value=_non_json_200_response(),
        ), patch.object(provider, "_log_flow_step", wraps=provider._log_flow_step) as spy:
            with pytest.raises(PaymentError):
                provider.merchant_request(
                    method="GET",
                    fallback_path="/payment/ofd_data/98060/123",
                    endpoint_label="ofd_data",
                    expect_error_code=False,
                )

    parse_calls = [c for c in spy.call_args_list if c.args and c.args[0] == "merchant_request_json_parse_failed"]
    assert parse_calls, "expected a merchant_request_json_parse_failed log entry"
    assert parse_calls[0].kwargs.get("level") == "warning"


@pytest.mark.unit
def test_critical_endpoint_non_json_200_stays_error(app):
    _configure(app)
    with app.app_context():
        provider = ClickPaymentProviderService()
        with patch(
            "business_app.services.click_payment_provider_service.request_with_retry",
            return_value=_non_json_200_response(),
        ), patch.object(provider, "_log_flow_step", wraps=provider._log_flow_step) as spy:
            with pytest.raises(PaymentError):
                provider.merchant_request(
                    method="GET",
                    fallback_path="/payment/status/98060/123",
                    endpoint_label="payment_status",
                    expect_error_code=False,
                )

    parse_calls = [c for c in spy.call_args_list if c.args and c.args[0] == "merchant_request_json_parse_failed"]
    assert parse_calls, "expected a merchant_request_json_parse_failed log entry"
    assert parse_calls[0].kwargs.get("level") == "error"
