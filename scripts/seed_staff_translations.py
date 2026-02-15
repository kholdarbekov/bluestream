#!/usr/bin/env python3
"""
Seed staff bot translations into the database.

This script:
1. Collects literal i18n keys used in staff_bot code.
2. Adds dynamic key families used via f-strings.
3. Upserts translations with category='staff_bot' for en/uz/ru.
"""

import re
import sys
from pathlib import Path
from typing import Dict, Set

# Match existing project seeding scripts.
sys.path.insert(0, '/app')

from business_app import create_app, db  # noqa: E402
from business_app.models.translation import Translation  # noqa: E402
from shared.staff_constants import FAILED_DELIVERY_REASONS, STAFF_BOT_ROLES  # noqa: E402
from shared.enums import OrderStatus, PaymentMethod  # noqa: E402


LANGUAGES = ("en", "uz", "ru")


# High-value user-facing strings with curated text.
STAFF_TRANSLATIONS: Dict[str, Dict[str, str]] = {
    "staff.menu.title": {
        "en": "Staff Bot - Main Menu",
        "uz": "Xodim Bot - Asosiy menyu",
        "ru": "Staff Bot - Glavnoe menyu",
    },
    "staff.menu.new_orders": {
        "en": "New Orders",
        "uz": "Yangi buyurtmalar",
        "ru": "Novye zakazy",
    },
    "staff.menu.new_orders_view": {
        "en": "New Orders (View)",
        "uz": "Yangi buyurtmalar (Korish)",
        "ru": "Novye zakazy (Prosmotr)",
    },
    "staff.menu.active_deliveries": {
        "en": "My Active Deliveries",
        "uz": "Faol yetkazishlarim",
        "ru": "Moi aktivnye dostavki",
    },
    "staff.menu.delivery_history": {
        "en": "Delivery History",
        "uz": "Yetkazish tarixi",
        "ru": "Istoriya dostavok",
    },
    "staff.menu.my_stats": {
        "en": "My Stats",
        "uz": "Mening statistikam",
        "ru": "Moya statistika",
    },
    "staff.menu.create_client": {
        "en": "Create Client",
        "uz": "Mijoz yaratish",
        "ru": "Sozdat klienta",
    },
    "staff.menu.create_order": {
        "en": "Create Order",
        "uz": "Buyurtma yaratish",
        "ru": "Sozdat zakaz",
    },
    "staff.menu.search_client": {
        "en": "Search Client",
        "uz": "Mijoz qidirish",
        "ru": "Poisk klienta",
    },
    "staff.menu.recent_orders": {
        "en": "Recent Orders",
        "uz": "So'nggi buyurtmalar",
        "ru": "Poslednie zakazy",
    },
    "staff.menu.profile": {
        "en": "Profile",
        "uz": "Profil",
        "ru": "Profil",
    },
    "staff.menu.settings": {
        "en": "Settings",
        "uz": "Sozlamalar",
        "ru": "Nastroiki",
    },
    "staff.menu.help": {
        "en": "Help",
        "uz": "Yordam",
        "ru": "Pomosh",
    },
    "staff.operator.pool_title": {
        "en": "Order Pool (View Only)",
        "uz": "Buyurtmalar havzasi (Faqat korish)",
        "ru": "Pul zakazov (Tolko prosmotr)",
    },
    "staff.operator.assigned_to": {
        "en": "Assigned To",
        "uz": "Biriktirilgan",
        "ru": "Naznachen",
    },
    "staff.back": {"en": "Back", "uz": "Orqaga", "ru": "Nazad"},
    "staff.confirm": {"en": "Confirm", "uz": "Tasdiqlash", "ru": "Podtverdit"},
    "staff.cancel": {"en": "Cancel", "uz": "Bekor qilish", "ru": "Otmena"},
    "staff.cancelled": {"en": "Cancelled", "uz": "Bekor qilindi", "ru": "Otmeneno"},
    "staff.yes": {"en": "Yes", "uz": "Ha", "ru": "Da"},
    "staff.no": {"en": "No", "uz": "Yoq", "ru": "Net"},
    "staff.page": {"en": "Page", "uz": "Sahifa", "ru": "Stranitsa"},
    "staff.error_occurred": {
        "en": "An error occurred. Please try again.",
        "uz": "Xatolik yuz berdi. Qayta urinib koring.",
        "ru": "Proizoshla oshibka. Poprobuyte snova.",
    },
    "staff.session_expired": {
        "en": "Session expired. Please login again.",
        "uz": "Sessiya tugadi. Qayta kiring.",
        "ru": "Sessiya istekla. Voydite snova.",
    },
    "staff.unauthorized": {
        "en": "You are not allowed to perform this action.",
        "uz": "Bu amal uchun sizda ruxsat yoq.",
        "ru": "U vas net prav dlya etogo deystviya.",
    },
    "staff.select_language": {
        "en": "Select language",
        "uz": "Tilni tanlang",
        "ru": "Vyberite yazyk",
    },
    "staff.language_changed": {
        "en": "Language updated.",
        "uz": "Til yangilandi.",
        "ru": "Yazyk obnovlen.",
    },
    "staff.notification.new_order": {
        "en": "New order available!",
        "uz": "Yangi buyurtma mavjud!",
        "ru": "Novyy zakaz dostupen!",
    },
    "staff.notification.order_assigned": {
        "en": "Order #{number} has been assigned to you.",
        "uz": "#{number} buyurtma sizga biriktirildi.",
        "ru": "Zakaz #{number} naznachen vam.",
    },
    "staff.notification.order_reassigned_from": {
        "en": "Order #{number} was reassigned.",
        "uz": "#{number} buyurtma qayta biriktirildi.",
        "ru": "Zakaz #{number} byl perenaznachen.",
    },
    "staff.notification.order_cancelled": {
        "en": "Order #{number} was cancelled.",
        "uz": "#{number} buyurtma bekor qilindi.",
        "ru": "Zakaz #{number} otmenen.",
    },
}


def _extract_literal_keys(repo_root: Path) -> Set[str]:
    """Collect literal keys from i18n.get('...') calls in staff bot files."""
    pattern = re.compile(r"i18n\.get\(\s*'([^']+)'\s*[,)]")
    keys: Set[str] = set()

    staff_root = repo_root / "staff_bot"
    for path in staff_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in pattern.findall(text):
            if match.startswith("staff."):
                keys.add(match)
    return keys


def _add_dynamic_keys(keys: Set[str]) -> None:
    """Add f-string based key families that static regex cannot enumerate."""
    # Role labels
    for role in STAFF_BOT_ROLES:
        keys.add(f"staff.role.{role}")

    # Delivery statuses
    for status in ("assigned", "picked_up", "in_transit", "arrived", "delivered", "failed"):
        keys.add(f"staff.delivery.status.{status}")

    # Failure reasons
    for reason in FAILED_DELIVERY_REASONS:
        keys.add(f"staff.delivery.reason.{reason}")

    # Payment labels
    for payment in PaymentMethod:
        keys.add(f"staff.delivery.payment.{payment.value}")
        keys.add(f"staff.operator.payment_{payment.value}")

    # Order status labels (operator order-pool details)
    for status in OrderStatus:
        keys.add(f"staff.order.status.{status.value}")


def _humanize_key(key: str) -> str:
    """Convert key tail into a readable fallback phrase."""
    tail = key.split(".")[-1]
    return tail.replace("_", " ").strip().capitalize()


def _resolve_value(key: str, language: str) -> str:
    """Resolve translation value from curated map or generated fallback."""
    curated = STAFF_TRANSLATIONS.get(key, {})
    if language in curated:
        return curated[language]
    if "en" in curated:
        return curated["en"]
    return _humanize_key(key)


def main() -> int:
    app = create_app()
    repo_root = Path(__file__).resolve().parents[1]

    with app.app_context():
        keys = _extract_literal_keys(repo_root)
        _add_dynamic_keys(keys)

        total_keys = len(keys)
        created = 0
        updated = 0

        for key in sorted(keys):
            for lang in LANGUAGES:
                value = _resolve_value(key, lang)
                existing = Translation.query.filter_by(key=key, language=lang).first()
                if existing:
                    existing.value = value
                    existing.category = "staff_bot"
                    existing.is_active = True
                    updated += 1
                else:
                    db.session.add(
                        Translation(
                            key=key,
                            language=lang,
                            value=value,
                            category="staff_bot",
                            is_active=True,
                        )
                    )
                    created += 1

        db.session.commit()

        print(
            f"Staff translations seeded: keys={total_keys}, "
            f"created={created}, updated={updated}"
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
