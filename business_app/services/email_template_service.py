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

from shared.i18n_rendering import base_language

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
        "delivery_rescheduled": "delivery_rescheduled",
        "subscription_created": "subscription_created",
        "subscription_reminder": "subscription_reminder",
        "subscription_cancelled": "subscription_cancelled",
        "reward_redeemed": "reward_redeemed",
        "agent_performance": "agent_performance",
        "reorder_reminder": "reorder_reminder",
        "reorder_escalation_digest": "reorder_escalation_digest",
        # The customer notices of spec 2026-10-23 §9, for a customer with no Telegram
        # (business_app/services/customer_notices.py).
        "subscription_order_created": "subscription_order_created",
        "subscription_delivery_skipped": "subscription_delivery_skipped",
        "subscription_paid_order_kept": "subscription_paid_order_kept",
        "order_payment_expired": "order_payment_expired",
        "subscription_schedule_changed": "subscription_schedule_changed",
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
        "delivery_rescheduled": {
            "uz": "Yetkazib berish sanasi o'zgardi: buyurtma #{order_number} - {company_name}",
            "en": "Delivery rescheduled: order #{order_number} - {company_name}",
            "ru": "Доставка перенесена: заказ #{order_number} - {company_name}",
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
        "agent_performance": {
            "uz": "Agentlar samaradorligi ({week_start} haftasi) - {company_name}",
            "en": "Agent Performance (week of {week_start}) - {company_name}",
            "ru": "Эффективность агентов (неделя от {week_start}) - {company_name}",
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
        # Reorder reminder (spec 2026-10-09 §7.4). `{greeting}` is the ONLY placeholder: it
        # arrives pre-rendered in template_data (a raw `{name}` is never used), and the
        # placeholder-strict guard skips a send that still carries it.
        "reorder_reminder": {
            "uz": "{greeting} Suv buyurtma qilish vaqti keldimi?",
            "en": "{greeting} Time for fresh water?",
            "ru": "{greeting} Пора заказать свежую воду?",
        },
        # Lapsed-customers morning digest to admins and managers (spec 2026-10-09 §9.2). The
        # placeholders are `{escalated_count}` and the common `{company_name}`; the
        # placeholder-strict guard skips a send whose count never arrived. The ru subject is a
        # label ("новых: N"), not "N новых": a word that follows the number would have to agree
        # with it (1 новый, 2 новых), and one escalation is the commonest morning.
        "reorder_escalation_digest": {
            "uz": "Yo'qolgan mijozlar: {escalated_count} ta yangi - {company_name}",
            "en": "Lapsed customers: {escalated_count} new to follow up - {company_name}",
            "ru": "Пропавшие клиенты (новых: {escalated_count}) - {company_name}",
        },
        # The customer notices (spec 2026-10-23 §9). uz/ru need a native read before prod.
        "subscription_order_created": {
            "uz": "Obuna buyurtmasi #{order_number} yaratildi - {company_name}",
            "en": "Subscription order #{order_number} placed - {company_name}",
            "ru": "Заказ по подписке #{order_number} оформлен - {company_name}",
        },
        "subscription_delivery_skipped": {
            "uz": "Obuna bo'yicha yetkazib berish o'tkazib yuborildi - {company_name}",
            "en": "Subscription delivery skipped - {company_name}",
            "ru": "Доставка по подписке пропущена - {company_name}",
        },
        "subscription_paid_order_kept": {
            "uz": "To'langan #{order_number} buyurtmangiz yetkazib beriladi - {company_name}",
            "en": "Your paid order #{order_number} will be delivered - {company_name}",
            "ru": "Оплаченный заказ #{order_number} будет доставлен - {company_name}",
        },
        "order_payment_expired": {
            "uz": "#{order_number} buyurtmasi bekor qilindi: to'lanmagan - {company_name}",
            "en": "Order #{order_number} cancelled: not paid - {company_name}",
            "ru": "Заказ #{order_number} отменён: не оплачен - {company_name}",
        },
        # A slot write changed the delivery time or day (spec 2026-10-27 §6.5). uz/ru: NATIVE-READ.
        "subscription_schedule_changed": {
            "uz": "Yetkazib berish jadvalingiz o'zgardi - {company_name}",
            "en": "Your delivery schedule changed - {company_name}",
            "ru": "Расписание доставки изменилось - {company_name}",
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
            company_telegram_bot_url = current_app.config["COMPANY_TELEGRAM_BOT_URL"]
        except RuntimeError:
            # Outside Flask context
            company_name = os.environ.get("COMPANY_NAME", "BlueStream")
            company_phone = os.environ.get("COMPANY_PHONE", "")
            company_email = os.environ.get("COMPANY_EMAIL", "")
            company_address = os.environ.get("COMPANY_ADDRESS", "")
            company_website = os.environ.get("COMPANY_WEBSITE", "")
            # The config class's own value, so the link is never built a second way.
            from business_app.config.base import BaseConfig

            company_telegram_bot_url = BaseConfig.COMPANY_TELEGRAM_BOT_URL

        return {
            "company_name": company_name,
            "company_phone": company_phone,
            "company_email": company_email,
            "company_address": company_address,
            "company_website": company_website,
            # Where a customer email sends a question (F16): the bot chat, whose replies land in
            # the admin Support Inbox. In the common context, so every render has it, hand-built
            # data included.
            "company_telegram_bot_url": company_telegram_bot_url,
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

    def resolve_language(self, language: Optional[str]) -> str:
        """The ONE language an email renders in: a supported code, else DEFAULT_LANGUAGE.

        A regional or upper-case code reads as its base language ("ru-RU", "ru_RU" and "RU" are
        ru) by `shared.i18n_rendering.base_language`, the rule the plural choice and the reminder
        composer's item names use too. Shared by `get_subject`, `render_template` and callers
        that pre-render copy for a template (the reorder reminder, spec §7.4/D15), so the
        subject, the body and the copy inside it can never be in different languages.
        """
        code = base_language(language)
        return code if code in self.SUPPORTED_LANGUAGES else self.DEFAULT_LANGUAGE

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
        language = self.resolve_language(language)

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
        language = self.resolve_language(language)

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
