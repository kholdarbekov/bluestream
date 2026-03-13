"""Compatibility wrapper for the Click provider service module."""

from business_app.services.click_payment_provider_service import ClickPaymentProviderService


class ClickProviderService(ClickPaymentProviderService):
    """Stable import path for Click provider workflows."""

