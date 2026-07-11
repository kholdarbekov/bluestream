"""
Email Template Service
Provides file-based email template rendering with multi-language support.
Uses Jinja2 templates stored in business_app/templates/emails/
"""

import os
import logging
from typing import Dict, Any, Optional
from datetime import datetime, UTC
from flask import current_app
from jinja2 import Environment, FileSystemLoader, TemplateNotFound

logger = logging.getLogger(__name__)


class EmailTemplateService:
    """
    Service for loading and rendering email templates from files.

    Templates are organized by language:
    - templates/emails/base.html (shared base template)
    - templates/emails/{language}/{template_name}.html

    Supported languages: uz (Uzbek), en (English), ru (Russian)
    Default language: uz (falls back to en if not found)
    """

    # Template name to notification type mapping
    # Maps notification_type values to template file names
    TEMPLATE_MAPPING = {
        "email_verification": "email_verification",
        "password_reset": "password_reset",
        "order_confirmation": "order_confirmation",
        "payment_confirmation": "payment_confirmation",
        "loyalty_reward": "loyalty_reward",
        "delivery_update": "delivery_update",
        "subscription_created": "subscription_created",
        "subscription_reminder": "subscription_reminder",
        "subscription_renewal": "subscription_renewal",
        "subscription_cancelled": "subscription_cancelled",
        "reward_redeemed": "reward_redeemed",
    }

    # Email subjects by template and language
    EMAIL_SUBJECTS = {
        "email_verification": {
            "uz": "Elektron pochtangizni tasdiqlang - {company_name}",
            "en": "Verify Your Email - {company_name}",
            "ru": "Подтвердите вашу почту - {company_name}",
        },
        "password_reset": {
            "uz": "Parolni tiklash - {company_name}",
            "en": "Reset Your Password - {company_name}",
            "ru": "Сброс пароля - {company_name}",
        },
        "order_confirmation": {
            "uz": "Buyurtma #{order_number} tasdiqlandi - {company_name}",
            "en": "Order #{order_number} Confirmed - {company_name}",
            "ru": "Заказ #{order_number} подтвержден - {company_name}",
        },
        "order_update": {
            "uz": "Buyurtma #{order_number} yangilanishi - {company_name}",
            "en": "Order #{order_number} Update - {company_name}",
            "ru": "Обновление заказа #{order_number} - {company_name}",
        },
        "delivery_reminder": {
            "uz": "Yetkazib berish eslatmasi: buyurtma #{order_number} - {company_name}",
            "en": "Delivery Reminder: Order #{order_number} - {company_name}",
            "ru": "Напоминание о доставке: заказ #{order_number} - {company_name}",
        },
        "payment_confirmation": {
            "uz": "To'lov qabul qilindi - {company_name}",
            "en": "Payment Confirmed - {company_name}",
            "ru": "Оплата подтверждена - {company_name}",
        },
        "loyalty_reward": {
            "uz": "AquaCoins qo'shildi - {company_name}",
            "en": "AquaCoins Earned - {company_name}",
            "ru": "AquaCoins начислены - {company_name}",
        },
        "delivery_update": {
            "uz": "Yetkazib berish yangiligi - {company_name}",
            "en": "Delivery Update - {company_name}",
            "ru": "Обновление доставки - {company_name}",
        },
        "subscription_created": {
            "uz": "Obuna faollashtirildi - {company_name}",
            "en": "Subscription Activated - {company_name}",
            "ru": "Подписка активирована - {company_name}",
        },
        "subscription_reminder": {
            "uz": "Obuna eslatmasi - {company_name}",
            "en": "Subscription Reminder - {company_name}",
            "ru": "Напоминание о подписке - {company_name}",
        },
        "subscription_renewal": {
            "uz": "Obuna yangilandi - {company_name}",
            "en": "Subscription Renewed - {company_name}",
            "ru": "Подписка продлена - {company_name}",
        },
        "subscription_cancelled": {
            "uz": "Obuna bekor qilindi - {company_name}",
            "en": "Subscription Cancelled - {company_name}",
            "ru": "Подписка отменена - {company_name}",
        },
        "reward_redeemed": {
            "uz": "Mukofot olindi - {company_name}",
            "en": "Reward Redeemed - {company_name}",
            "ru": "Награда получена - {company_name}",
        },
        # Admin analytics report emails
        "daily_report": {
            "uz": "Kunlik hisobot {report_date} - {company_name}",
            "en": "Daily Report {report_date} - {company_name}",
            "ru": "Ежедневный отчёт {report_date} - {company_name}",
        },
        "weekly_business_report": {
            "uz": "Haftalik biznes hisoboti ({week_ending}) - {company_name}",
            "en": "Weekly Business Report ({week_ending}) - {company_name}",
            "ru": "Еженедельный бизнес-отчёт ({week_ending}) - {company_name}",
        },
        "churn_alert": {
            "uz": "Mijozlar ketishi xavfi: {high_risk_count} ta yuqori xavf - {company_name}",
            "en": "Customer Churn Alert: {high_risk_count} high-risk - {company_name}",
            "ru": "Отток клиентов: {high_risk_count} в зоне высокого риска - {company_name}",
        },
        "demand_forecast": {
            "uz": "Talab prognozi ({forecast_period}) - {company_name}",
            "en": "Demand Forecast ({forecast_period}) - {company_name}",
            "ru": "Прогноз спроса ({forecast_period}) - {company_name}",
        },
        "kpi_alert": {
            "uz": "KPI ogohlantirishi {date} - {company_name}",
            "en": "KPI Alert {date} - {company_name}",
            "ru": "KPI оповещение {date} - {company_name}",
        },
        # Admin inventory management emails
        "low_stock_alert": {
            "uz": "Kam zaxira ogohlantirishi: {product_name} - {company_name}",
            "en": "Low Stock Alert: {product_name} - {company_name}",
            "ru": "Предупреждение о низком запасе: {product_name} - {company_name}",
        },
        "inventory_report": {
            "uz": "Inventar hisoboti ({report_type}) - {company_name}",
            "en": "Inventory Report ({report_type}) - {company_name}",
            "ru": "Отчёт по инвентарю ({report_type}) - {company_name}",
        },
        "reorder_suggestions": {
            "uz": "Qayta buyurtma takliflari: {total_products} ta mahsulot - {company_name}",
            "en": "Reorder Suggestions: {total_products} products - {company_name}",
            "ru": "Предложения по дозаказу: {total_products} товаров - {company_name}",
        },
        # Fiscalization (Asl Belgisi / Tax Committee) critical alert
        "tax_committee_token_refresh_failed": {
            "uz": "⛔ Asl Belgisi tokenini yangilash muvaffaqiyatsiz — fiskalizatsiya bloklandi - {company_name}",
            "en": "⛔ Asl Belgisi token refresh FAILED — fiscalization blocked - {company_name}",
            "ru": "⛔ Не удалось обновить токен Asl Belgisi — фискализация заблокирована - {company_name}",
        },
    }

    SUPPORTED_LANGUAGES = ["uz", "en", "ru"]
    DEFAULT_LANGUAGE = "en"
    FALLBACK_CHAIN = ["uz", "en", "ru"]

    def __init__(self):
        """Initialize the email template service."""
        self._jinja_env = None
        self._templates_dir = None

    @property
    def templates_dir(self) -> str:
        """Get the templates directory path."""
        if self._templates_dir is None:
            # Get the templates directory from Flask app or construct it
            try:
                base_dir = current_app.root_path
            except RuntimeError:
                # Outside Flask context, use relative path
                base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

            self._templates_dir = os.path.join(base_dir, "templates", "emails")

        return self._templates_dir

    @property
    def jinja_env(self) -> Environment:
        """Get or create Jinja2 environment for email templates."""
        if self._jinja_env is None:
            self._jinja_env = Environment(
                loader=FileSystemLoader(
                    [self.templates_dir, os.path.dirname(self.templates_dir)]  # For base.html access
                ),
                autoescape=True,
            )
        return self._jinja_env

    def get_common_context(self) -> Dict[str, Any]:
        """
        Get common template context variables.
        These are available in all email templates.
        """
        try:
            company_name = current_app.config.get("COMPANY_NAME", "BlueStream")
            company_phone = current_app.config.get("COMPANY_PHONE", "")
            company_email = current_app.config.get("COMPANY_EMAIL", "")
            company_address = current_app.config.get("COMPANY_ADDRESS", "")
            company_website = current_app.config.get("COMPANY_WEBSITE", "")
        except RuntimeError:
            # Outside Flask context
            company_name = os.environ.get("COMPANY_NAME", "BlueStream")
            company_phone = os.environ.get("COMPANY_PHONE", "")
            company_email = os.environ.get("COMPANY_EMAIL", "")
            company_address = os.environ.get("COMPANY_ADDRESS", "")
            company_website = os.environ.get("COMPANY_WEBSITE", "")

        return {
            "company_name": company_name,
            "company_phone": company_phone,
            "company_email": company_email,
            "company_address": company_address,
            "company_website": company_website,
            "current_year": datetime.now(UTC).year,
        }

    def get_template_name_for_notification_type(self, notification_type: str) -> str:
        """
        Map notification type to template file name.

        Args:
            notification_type: The notification type (e.g., 'system_alert', 'order_confirmation')

        Returns:
            Template file name without extension (e.g., 'email_verification')
        """
        return self.TEMPLATE_MAPPING.get(notification_type, notification_type)

    def get_subject(self, template_name: str, language: str, template_data: Dict[str, Any]) -> str:
        """
        Get the email subject for a template.

        Args:
            template_name: Name of the template (e.g., 'email_verification')
            language: Language code (uz, en, ru)
            template_data: Data to interpolate into subject

        Returns:
            Rendered email subject string
        """
        if language not in self.SUPPORTED_LANGUAGES:
            language = self.DEFAULT_LANGUAGE

        subjects = self.EMAIL_SUBJECTS.get(template_name, {})
        subject_template = subjects.get(language) or subjects.get(self.DEFAULT_LANGUAGE, "")

        # Merge with common context for company_name
        context = {**self.get_common_context(), **template_data}

        try:
            return subject_template.format(**context)
        except KeyError as e:
            logger.warning(f"Missing subject variable {e} for template {template_name}")
            return subject_template

    def render_template(self, template_name: str, language: str, template_data: Dict[str, Any]) -> Optional[str]:
        """
        Render an email template with the given data.

        Args:
            template_name: Name of the template (e.g., 'email_verification', 'order_confirmation')
            language: Language code (uz, en, ru)
            template_data: Dictionary of template variables

        Returns:
            Rendered HTML content or None if template not found
        """
        if language not in self.SUPPORTED_LANGUAGES:
            language = self.DEFAULT_LANGUAGE

        # Build template path
        template_path = f"{language}/{template_name}.html"

        # Merge common context with template-specific data
        context = {**self.get_common_context(), "language": language, **template_data}

        try:
            template = self.jinja_env.get_template(template_path)
            return template.render(**context)
        except TemplateNotFound:
            logger.warning(f"Template not found: {template_path}, trying fallback chain")

            # Try fallback languages
            for fallback_lang in self.FALLBACK_CHAIN:
                if fallback_lang == language:
                    continue

                fallback_path = f"{fallback_lang}/{template_name}.html"
                try:
                    template = self.jinja_env.get_template(fallback_path)
                    context["language"] = fallback_lang
                    logger.info(f"Using fallback template: {fallback_path}")
                    return template.render(**context)
                except TemplateNotFound:
                    continue

            logger.error(f"No template found for {template_name} in any language")
            return None
        except Exception:
            logger.exception("Error rendering template %s", template_path)
            return None

    def render_notification_email(
        self, notification_type: str, language: str, template_data: Dict[str, Any]
    ) -> Optional[Dict[str, str]]:
        """
        Render a notification email by notification type.

        Args:
            notification_type: The notification type (e.g., 'system_alert', 'order_confirmation')
            language: Language code (uz, en, ru)
            template_data: Dictionary of template variables

        Returns:
            Dictionary with 'subject' and 'content' keys, or None if failed
        """
        template_name = self.get_template_name_for_notification_type(notification_type)

        subject = self.get_subject(template_name, language, template_data)
        content = self.render_template(template_name, language, template_data)

        if content is None:
            return None

        return {"subject": subject, "content": content}

    def template_exists(self, template_name: str, language: str = None) -> bool:
        """
        Check if a template exists.

        Args:
            template_name: Name of the template
            language: Optional language code. If None, checks all languages.

        Returns:
            True if template exists
        """
        if language:
            template_path = os.path.join(self.templates_dir, language, f"{template_name}.html")
            return os.path.exists(template_path)

        # Check all languages
        for lang in self.SUPPORTED_LANGUAGES:
            template_path = os.path.join(self.templates_dir, lang, f"{template_name}.html")
            if os.path.exists(template_path):
                return True

        return False

    def list_templates(self) -> Dict[str, list]:
        """
        List all available templates by language.

        Returns:
            Dictionary mapping language codes to list of template names
        """
        templates = {}

        for lang in self.SUPPORTED_LANGUAGES:
            lang_dir = os.path.join(self.templates_dir, lang)
            if os.path.exists(lang_dir):
                templates[lang] = [f.replace(".html", "") for f in os.listdir(lang_dir) if f.endswith(".html")]
            else:
                templates[lang] = []

        return templates


# Singleton instance
_email_template_service = None


def get_email_template_service() -> EmailTemplateService:
    """Get or create the EmailTemplateService singleton."""
    global _email_template_service
    if _email_template_service is None:
        _email_template_service = EmailTemplateService()
    return _email_template_service
