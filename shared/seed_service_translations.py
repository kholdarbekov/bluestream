#!/usr/bin/env python3
"""
Seed Service Layer Translation Keys

This script adds missing translation keys discovered from service layer implementation.
Run after seed_backend_translations.py
"""

import sys
import os

sys.path.insert(0, '/app')

from business_app import create_app, db
from business_app.models.translation import Translation

# Additional translation keys for service layer
SERVICE_TRANSLATIONS = {
    # Additional Auth Messages
    'api.auth.account_disabled': {
        'en': 'Account has been disabled',
        'uz': 'Hisob o\'chirilgan',
        'ru': 'Аккаунт отключен'
    },
    'api.auth.admin_already_exists': {
        'en': 'Admin account already exists',
        'uz': 'Admin hisobi allaqachon mavjud',
        'ru': 'Учетная запись администратора уже существует'
    },
    'api.auth.authentication_failed': {
        'en': 'Authentication failed',
        'uz': 'Autentifikatsiya muvaffaqiyatsiz',
        'ru': 'Ошибка аутентификации'
    },
    'api.auth.current_password_incorrect': {
        'en': 'Current password is incorrect',
        'uz': 'Joriy parol noto\'g\'ri',
        'ru': 'Текущий пароль неверен'
    },
    'api.auth.invalid_user': {
        'en': 'Invalid user',
        'uz': 'Noto\'g\'ri foydalanuvchi',
        'ru': 'Недействительный пользователь'
    },
    'api.auth.telegram_account_password_required': {
        'en': 'Password is required for Telegram accounts',
        'uz': 'Telegram hisobi uchun parol talab qilinadi',
        'ru': 'Для учетных записей Telegram требуется пароль'
    },

    # Payment Errors
    'error.payment.cannot_refund': {
        'en': 'Cannot refund this payment',
        'uz': 'Bu to\'lovni qaytarib bo\'lmaydi',
        'ru': 'Невозможно вернуть этот платеж'
    },
    'error.payment.card_save_failed': {
        'en': 'Failed to save card',
        'uz': 'Kartani saqlash muvaffaqiyatsiz',
        'ru': 'Не удалось сохранить карту'
    },
    'error.payment.invalid_method': {
        'en': 'Invalid payment method',
        'uz': 'Noto\'g\'ri to\'lov usuli',
        'ru': 'Недействительный метод оплаты'
    },
    'error.payment.invalid_signature': {
        'en': 'Invalid payment signature',
        'uz': 'Noto\'g\'ri to\'lov imzosi',
        'ru': 'Недействительная подпись платежа'
    },
    'error.payment.provider_unavailable': {
        'en': 'Payment provider is unavailable',
        'uz': 'To\'lov provayderi mavjud emas',
        'ru': 'Платежный провайдер недоступен'
    },
    'error.payment.unknown_action': {
        'en': 'Unknown payment action',
        'uz': 'Noma\'lum to\'lov harakati',
        'ru': 'Неизвестное действие платежа'
    },
    'error.payment.unknown_method': {
        'en': 'Unknown payment method',
        'uz': 'Noma\'lum to\'lov usuli',
        'ru': 'Неизвестный метод оплаты'
    },
    'error.payment.unsupported_method': {
        'en': 'Unsupported payment method',
        'uz': 'Qo\'llab-quvvatlanmaydigan to\'lov usuli',
        'ru': 'Неподдерживаемый метод оплаты'
    },

    # Validation Errors - Card
    'error.validation.amount_exceeds_total': {
        'en': 'Amount exceeds order total',
        'uz': 'Miqdor buyurtma summasidan oshib ketdi',
        'ru': 'Сумма превышает общую сумму заказа'
    },
    'error.validation.cannot_delete_last_card': {
        'en': 'Cannot delete the last payment card',
        'uz': 'Oxirgi to\'lov kartasini o\'chirib bo\'lmaydi',
        'ru': 'Невозможно удалить последнюю платежную карту'
    },
    'error.validation.card_already_saved': {
        'en': 'Card already saved',
        'uz': 'Karta allaqachon saqlangan',
        'ru': 'Карта уже сохранена'
    },
    'error.validation.card_expired': {
        'en': 'Card has expired',
        'uz': 'Karta muddati tugagan',
        'ru': 'Срок действия карты истек'
    },
    'error.validation.card_invalid': {
        'en': 'Invalid card details',
        'uz': 'Noto\'g\'ri karta ma\'lumotlari',
        'ru': 'Неверные данные карты'
    },
    'error.validation.card_not_verified': {
        'en': 'Card not verified',
        'uz': 'Karta tasdiqlanmagan',
        'ru': 'Карта не подтверждена'
    },
    'error.validation.test_card_not_allowed': {
        'en': 'Test cards are not allowed in production',
        'uz': 'Test kartalari ishlab chiqarishda ruxsat etilmagan',
        'ru': 'Тестовые карты не разрешены в производстве'
    },

    # Validation Errors - General
    'error.validation.failed': {
        'en': 'Validation failed',
        'uz': 'Tekshiruv muvaffaqiyatsiz',
        'ru': 'Ошибка валидации'
    },
    'error.validation.invalid_amount': {
        'en': 'Invalid amount',
        'uz': 'Noto\'g\'ri miqdor',
        'ru': 'Неверная сумма'
    },
    'error.validation.invalid_password': {
        'en': 'Invalid password',
        'uz': 'Noto\'g\'ri parol',
        'ru': 'Неверный пароль'
    },
    'error.validation.no_email_address': {
        'en': 'No email address found',
        'uz': 'Email manzil topilmadi',
        'ru': 'Адрес электронной почты не найден'
    },
    'error.validation.no_phone_number': {
        'en': 'No phone number found',
        'uz': 'Telefon raqami topilmadi',
        'ru': 'Номер телефона не найден'
    },
    'error.validation.no_telegram_chat_id': {
        'en': 'No Telegram chat ID found',
        'uz': 'Telegram chat ID topilmadi',
        'ru': 'ID чата Telegram не найден'
    },
    'error.validation.phone_already_exists': {
        'en': 'Phone number already registered',
        'uz': 'Telefon raqami allaqachon ro\'yxatdan o\'tgan',
        'ru': 'Номер телефона уже зарегистрирован'
    },

    # Configuration Errors
    'error.configuration.sendgrid_not_configured': {
        'en': 'Email service not configured',
        'uz': 'Email xizmati sozlanmagan',
        'ru': 'Служба электронной почты не настроена'
    },
    'error.configuration.sms_not_configured': {
        'en': 'SMS service not configured',
        'uz': 'SMS xizmati sozlanmagan',
        'ru': 'SMS-служба не настроена'
    },
    'error.configuration.telegram_not_configured': {
        'en': 'Telegram service not configured',
        'uz': 'Telegram xizmati sozlanmagan',
        'ru': 'Служба Telegram не настроена'
    },

    # Other Errors
    'error.template_not_found': {
        'en': 'Template not found',
        'uz': 'Shablon topilmadi',
        'ru': 'Шаблон не найден'
    },
    'error.push_not_implemented': {
        'en': 'Push notifications not implemented',
        'uz': 'Push bildirishnomalar amalga oshirilmagan',
        'ru': 'Push-уведомления не реализованы'
    },
}


def main():
    """Seed service layer translation keys"""
    app = create_app()

    with app.app_context():
        print("=" * 70)
        print("SERVICE LAYER TRANSLATION SEEDING")
        print("=" * 70)
        print()

        total_keys = len(SERVICE_TRANSLATIONS)
        total_translations = total_keys * 3
        added_count = 0
        updated_count = 0
        skipped_count = 0

        print(f"Processing {total_keys} translation keys ({total_translations} total translations)...")
        print()

        for key, translations in SERVICE_TRANSLATIONS.items():
            print(f"  Key: {key}")

            for language, value in translations.items():
                existing = Translation.query.filter_by(
                    key=key,
                    language=language
                ).first()

                if existing:
                    if existing.value != value:
                        print(f"    [{language}] Updating: '{existing.value}' → '{value}'")
                        existing.value = value
                        existing.is_active = True
                        existing.category = key.split('.')[0]
                        updated_count += 1
                    else:
                        skipped_count += 1
                else:
                    print(f"    [{language}] Creating: '{value}'")
                    translation = Translation(
                        key=key,
                        language=language,
                        value=value,
                        category=key.split('.')[0],
                        description=f"Service layer translation for {key}",
                        is_active=True
                    )
                    db.session.add(translation)
                    added_count += 1

        db.session.commit()

        print()
        print("=" * 70)
        print("✓ SERVICE LAYER TRANSLATION SEEDING COMPLETED")
        print("=" * 70)
        print()
        print("Summary:")
        print(f"  - Total keys processed: {total_keys}")
        print(f"  - New translations added: {added_count}")
        print(f"  - Existing translations updated: {updated_count}")
        print(f"  - Unchanged translations: {skipped_count}")
        print(f"  - Total new translations in database: {added_count + updated_count}")
        print()
        print("Combined with backend translations:")
        print(f"  - Previous: 68 keys (204 translations)")
        print(f"  - New: {total_keys} keys ({total_translations} translations)")
        print(f"  - Total: {68 + total_keys} keys ({204 + total_translations} translations)")
        print()


if __name__ == '__main__':
    main()
