"""Admin UI translation query helpers."""

from collections.abc import Iterable

from sqlalchemy import distinct, or_

from business_app import db
from business_app.models.translation import Translation


class AdminUiTranslationService:
    """Query admin UI translations in the shapes expected by i18next."""

    SHARED_UI_CATEGORY = "ui"
    SCOPED_UI_CATEGORY_PREFIX = "ui_"

    NAMESPACE_CATEGORY_MAPPING = {
        "common": SHARED_UI_CATEGORY,
        "navigation": "ui_navigation",
        "dashboard": "ui_dashboard",
        "orders": "ui_orders",
        "products": "ui_products",
        "users": "ui_users",
        "settings": "ui_settings",
        "profile": "ui_profile",
        "analytics": "ui_analytics",
        "blog": "ui_blog",
        "delivery": "ui_delivery",
        "loyalty": "ui_loyalty",
        "login": "ui_login",
        "staff": "ui_staff",
    }

    LEGACY_NAMESPACE_PREFIXES = {
        "navigation": ("ui.nav.", "ui.user_menu.", "ui.app_name_"),
        "dashboard": ("ui.dashboard.",),
        "orders": ("ui.orders.",),
        "products": ("ui.products.",),
        "users": ("ui.users.",),
        "settings": ("ui.settings.",),
        "profile": ("ui.profile.",),
        "analytics": ("ui.analytics.",),
        "blog": ("ui.blog.",),
        "delivery": ("ui.delivery.",),
        "loyalty": ("ui.loyalty.",),
        "login": ("ui.login.",),
        "staff": ("ui.staff.",),
    }

    @classmethod
    def get_translations(cls, language: str, namespace: str) -> dict[str, str]:
        """Return i18next-compatible flat key/value translations."""

        if namespace == "common":
            shared_records = cls._get_shared_ui_records(language)
            scoped_records = cls._get_all_scoped_ui_records(language)
            return cls._to_translation_map(shared_records, scoped_records)

        shared_records = cls._get_legacy_namespace_records(language, namespace)
        scoped_records = cls._get_scoped_namespace_records(language, namespace)
        return cls._to_translation_map(shared_records, scoped_records)

    @classmethod
    def get_namespaces(cls) -> list[str]:
        """Return available i18next namespace names for admin UI translations."""

        categories = (
            db.session.query(distinct(Translation.category))
            .filter(
                Translation.is_active.is_(True),
                or_(
                    Translation.category == cls.SHARED_UI_CATEGORY,
                    Translation.category.like(f"{cls.SCOPED_UI_CATEGORY_PREFIX}%"),
                ),
            )
            .all()
        )

        namespaces = {"common"}
        for (category,) in categories:
            if category and category.startswith(cls.SCOPED_UI_CATEGORY_PREFIX):
                namespaces.add(category[len(cls.SCOPED_UI_CATEGORY_PREFIX) :])

        return sorted(namespaces)

    @classmethod
    def _get_shared_ui_records(cls, language: str) -> list[Translation]:
        return (
            cls._base_query(language)
            .filter(
                Translation.category == cls.SHARED_UI_CATEGORY,
            )
            .all()
        )

    @classmethod
    def _get_all_scoped_ui_records(cls, language: str) -> list[Translation]:
        return (
            cls._base_query(language)
            .filter(
                Translation.category.like(f"{cls.SCOPED_UI_CATEGORY_PREFIX}%"),
            )
            .all()
        )

    @classmethod
    def _get_legacy_namespace_records(
        cls,
        language: str,
        namespace: str,
    ) -> list[Translation]:
        prefixes = cls.LEGACY_NAMESPACE_PREFIXES.get(namespace, ())
        if not prefixes:
            return []

        prefix_filters = [Translation.key.like(f"{prefix}%") for prefix in prefixes]
        return (
            cls._base_query(language)
            .filter(
                Translation.category == cls.SHARED_UI_CATEGORY,
                or_(*prefix_filters),
            )
            .all()
        )

    @classmethod
    def _get_scoped_namespace_records(
        cls,
        language: str,
        namespace: str,
    ) -> list[Translation]:
        category = cls.NAMESPACE_CATEGORY_MAPPING.get(
            namespace,
            f"{cls.SCOPED_UI_CATEGORY_PREFIX}{namespace}",
        )
        if category == cls.SHARED_UI_CATEGORY:
            return []

        return (
            cls._base_query(language)
            .filter(
                Translation.category == category,
            )
            .all()
        )

    @classmethod
    def _base_query(cls, language: str):
        return Translation.query.filter(
            Translation.language == language,
            Translation.is_active.is_(True),
        )

    @staticmethod
    def _to_translation_map(*record_groups: Iterable[Translation]) -> dict[str, str]:
        result: dict[str, str] = {}
        for records in record_groups:
            for translation in records:
                result[translation.key] = translation.value
        return result
