#!/usr/bin/env python3
"""
Seed Backend Translation Keys

This script populates the database with core translation keys for:
- API response messages (auth, orders, products, payments, etc.)
- Common error messages
- Validation messages
- Success messages

Based on the I18N Implementation Plan Phase 2
"""

import sys
import os

# Add parent directory to path
sys.path.insert(0, '/app')

from business_app import create_app, db
from business_app.models.translation import Translation

# Translation key structure: {category}.{subcategory}.{identifier}
# Categories: api.*, error.*, success.*, validation.*

BACKEND_TRANSLATIONS = {
    # ============================================================================
    # API - Authentication (api.auth.*)
    # ============================================================================
    'api.auth.registration_successful': {
        'en': 'Registration successful',
        'uz': 'Ro\'yxatdan o\'tish muvaffaqiyatli',
        'ru': 'Регистрация успешна'
    },
    'api.auth.login_successful': {
        'en': 'Login successful',
        'uz': 'Kirish muvaffaqiyatli',
        'ru': 'Вход выполнен успешно'
    },
    'api.auth.logout_successful': {
        'en': 'Logout successful',
        'uz': 'Chiqish muvaffaqiyatli',
        'ru': 'Выход выполнен успешно'
    },
    'api.auth.invalid_credentials': {
        'en': 'Invalid email or password',
        'uz': 'Noto\'g\'ri email yoki parol',
        'ru': 'Неверный email или пароль'
    },
    'api.auth.account_locked': {
        'en': 'Account has been locked due to too many failed login attempts',
        'uz': 'Hisob ko\'p marta noto\'g\'ri kirishlar tufayli bloklangan',
        'ru': 'Аккаунт заблокирован из-за множества неудачных попыток входа'
    },
    'api.auth.email_already_exists': {
        'en': 'Email already registered',
        'uz': 'Email allaqachon ro\'yxatdan o\'tgan',
        'ru': 'Email уже зарегистрирован'
    },
    'api.auth.token_expired': {
        'en': 'Session expired, please login again',
        'uz': 'Sessiya tugadi, qaytadan kiring',
        'ru': 'Сессия истекла, пожалуйста, войдите снова'
    },
    'api.auth.token_invalid': {
        'en': 'Invalid authentication token',
        'uz': 'Noto\'g\'ri autentifikatsiya tokeni',
        'ru': 'Недействительный токен аутентификации'
    },
    'api.auth.unauthorized': {
        'en': 'Authentication required',
        'uz': 'Autentifikatsiya talab qilinadi',
        'ru': 'Требуется аутентификация'
    },
    'api.auth.phone_verified': {
        'en': 'Phone number verified successfully',
        'uz': 'Telefon raqami muvaffaqiyatli tasdiqlandi',
        'ru': 'Номер телефона успешно подтвержден'
    },

    # ============================================================================
    # API - Orders (api.orders.*)
    # ============================================================================
    'api.orders.created': {
        'en': 'Order created successfully',
        'uz': 'Buyurtma muvaffaqiyatli yaratildi',
        'ru': 'Заказ успешно создан'
    },
    'api.orders.updated': {
        'en': 'Order updated successfully',
        'uz': 'Buyurtma muvaffaqiyatli yangilandi',
        'ru': 'Заказ успешно обновлен'
    },
    'api.orders.cancelled': {
        'en': 'Order cancelled successfully',
        'uz': 'Buyurtma muvaffaqiyatli bekor qilindi',
        'ru': 'Заказ успешно отменен'
    },
    'api.orders.not_found': {
        'en': 'Order not found',
        'uz': 'Buyurtma topilmadi',
        'ru': 'Заказ не найден'
    },
    'api.orders.cannot_cancel': {
        'en': 'Order cannot be cancelled at this stage',
        'uz': 'Bu bosqichda buyurtmani bekor qilib bo\'lmaydi',
        'ru': 'Заказ не может быть отменен на этом этапе'
    },
    'api.orders.retrieved': {
        'en': 'Order retrieved successfully',
        'uz': 'Buyurtma muvaffaqiyatli olindi',
        'ru': 'Заказ успешно получен'
    },
    'api.orders.list_retrieved': {
        'en': 'Orders retrieved successfully',
        'uz': 'Buyurtmalar muvaffaqiyatli olindi',
        'ru': 'Заказы успешно получены'
    },
    'api.orders.confirmed': {
        'en': 'Order confirmed',
        'uz': 'Buyurtma tasdiqlandi',
        'ru': 'Заказ подтвержден'
    },
    'api.orders.delivered': {
        'en': 'Order delivered',
        'uz': 'Buyurtma yetkazildi',
        'ru': 'Заказ доставлен'
    },

    # ============================================================================
    # API - Products (api.products.*)
    # ============================================================================
    'api.products.retrieved': {
        'en': 'Products retrieved successfully',
        'uz': 'Mahsulotlar muvaffaqiyatli olindi',
        'ru': 'Продукты успешно получены'
    },
    'api.products.not_found': {
        'en': 'Product not found',
        'uz': 'Mahsulot topilmadi',
        'ru': 'Продукт не найден'
    },
    'api.products.out_of_stock': {
        'en': 'Product is out of stock',
        'uz': 'Mahsulot tugagan',
        'ru': 'Товар закончился'
    },
    'api.products.added_to_cart': {
        'en': 'Product added to cart',
        'uz': 'Mahsulot savatchaga qo\'shildi',
        'ru': 'Товар добавлен в корзину'
    },
    'api.products.insufficient_stock': {
        'en': 'Insufficient stock available',
        'uz': 'Yetarli miqdorda mahsulot yo\'q',
        'ru': 'Недостаточно товара на складе'
    },

    # ============================================================================
    # API - Payments (api.payments.*)
    # ============================================================================
    'api.payments.initiated': {
        'en': 'Payment initiated',
        'uz': 'To\'lov boshlandi',
        'ru': 'Платеж инициирован'
    },
    'api.payments.completed': {
        'en': 'Payment completed successfully',
        'uz': 'To\'lov muvaffaqiyatli yakunlandi',
        'ru': 'Платеж успешно завершен'
    },
    'api.payments.failed': {
        'en': 'Payment failed',
        'uz': 'To\'lov bajarilmadi',
        'ru': 'Платеж не выполнен'
    },
    'api.payments.refunded': {
        'en': 'Payment refunded',
        'uz': 'To\'lov qaytarildi',
        'ru': 'Платеж возвращен'
    },
    'api.payments.pending': {
        'en': 'Payment is pending',
        'uz': 'To\'lov kutilmoqda',
        'ru': 'Платеж ожидается'
    },

    # ============================================================================
    # API - Delivery (api.delivery.*)
    # ============================================================================
    'api.delivery.assigned': {
        'en': 'Delivery assigned to driver',
        'uz': 'Yetkazib berish haydovchiga tayinlandi',
        'ru': 'Доставка назначена водителю'
    },
    'api.delivery.in_transit': {
        'en': 'Order is in transit',
        'uz': 'Buyurtma yo\'lda',
        'ru': 'Заказ в пути'
    },
    'api.delivery.arrived': {
        'en': 'Order has arrived',
        'uz': 'Buyurtma yetib keldi',
        'ru': 'Заказ прибыл'
    },
    'notification.delivery_status.in_transit': {
        'en': 'In Transit',
        'uz': 'Yo\'lda',
        'ru': 'В пути'
    },
    'notification.delivery_status.arrived': {
        'en': 'Arrived',
        'uz': 'Yetib keldi',
        'ru': 'Прибыл'
    },
    'api.delivery.completed': {
        'en': 'Delivery completed',
        'uz': 'Yetkazib berish yakunlandi',
        'ru': 'Доставка завершена'
    },
    'api.delivery.address_updated': {
        'en': 'Delivery address updated',
        'uz': 'Yetkazib berish manzili yangilandi',
        'ru': 'Адрес доставки обновлен'
    },

    # ============================================================================
    # API - Subscriptions (api.subscriptions.*)
    # ============================================================================
    'api.subscriptions.created': {
        'en': 'Subscription created successfully',
        'uz': 'Obuna muvaffaqiyatli yaratildi',
        'ru': 'Подписка успешно создана'
    },
    'api.subscriptions.cancelled': {
        'en': 'Subscription cancelled',
        'uz': 'Obuna bekor qilindi',
        'ru': 'Подписка отменена'
    },
    'api.subscriptions.paused': {
        'en': 'Subscription paused',
        'uz': 'Obuna to\'xtatildi',
        'ru': 'Подписка приостановлена'
    },
    'api.subscriptions.resumed': {
        'en': 'Subscription resumed',
        'uz': 'Obuna davom ettirildi',
        'ru': 'Подписка возобновлена'
    },

    # ============================================================================
    # API - Loyalty (api.loyalty.*)
    # ============================================================================
    'api.loyalty.points_earned': {
        'en': 'Loyalty points earned',
        'uz': 'Sodiqlik ballari olindi',
        'ru': 'Баллы лояльности начислены'
    },
    'api.loyalty.points_redeemed': {
        'en': 'Points redeemed successfully',
        'uz': 'Ballar muvaffaqiyatli ishlatildi',
        'ru': 'Баллы успешно использованы'
    },
    'api.loyalty.insufficient_points': {
        'en': 'Insufficient loyalty points',
        'uz': 'Yetarli sodiqlik ballari yo\'q',
        'ru': 'Недостаточно баллов лояльности'
    },

    # ============================================================================
    # Validation Errors (error.validation.*)
    # ============================================================================
    'error.validation.required_field': {
        'en': 'This field is required',
        'uz': 'Bu maydon to\'ldirilishi shart',
        'ru': 'Это поле обязательно для заполнения'
    },
    'error.validation.invalid_email': {
        'en': 'Invalid email format',
        'uz': 'Noto\'g\'ri email formati',
        'ru': 'Неверный формат email'
    },
    'error.validation.invalid_phone': {
        'en': 'Invalid phone number format',
        'uz': 'Noto\'g\'ri telefon raqami formati',
        'ru': 'Неверный формат номера телефона'
    },
    'error.validation.password_too_short': {
        'en': 'Password must be at least 8 characters',
        'uz': 'Parol kamida 8 ta belgidan iborat bo\'lishi kerak',
        'ru': 'Пароль должен содержать не менее 8 символов'
    },
    'error.validation.passwords_dont_match': {
        'en': 'Passwords do not match',
        'uz': 'Parollar mos kelmaydi',
        'ru': 'Пароли не совпадают'
    },
    'error.validation.invalid_date': {
        'en': 'Invalid date format',
        'uz': 'Noto\'g\'ri sana formati',
        'ru': 'Неверный формат даты'
    },
    'error.validation.min_value': {
        'en': 'Value must be at least {min}',
        'uz': 'Qiymat kamida {min} bo\'lishi kerak',
        'ru': 'Значение должно быть не менее {min}'
    },
    'error.validation.max_value': {
        'en': 'Value must not exceed {max}',
        'uz': 'Qiymat {max} dan oshmasligi kerak',
        'ru': 'Значение не должно превышать {max}'
    },

    # ============================================================================
    # General Errors (error.*)
    # ============================================================================
    'error.not_found': {
        'en': 'Resource not found',
        'uz': 'Resurs topilmadi',
        'ru': 'Ресурс не найден'
    },
    'error.unauthorized': {
        'en': 'Unauthorized access',
        'uz': 'Ruxsatsiz kirish',
        'ru': 'Несанкционированный доступ'
    },
    'error.forbidden': {
        'en': 'Access forbidden',
        'uz': 'Kirish taqiqlangan',
        'ru': 'Доступ запрещен'
    },
    'error.server_error': {
        'en': 'Internal server error',
        'uz': 'Ichki server xatosi',
        'ru': 'Внутренняя ошибка сервера'
    },
    'error.network_error': {
        'en': 'Network error, please try again',
        'uz': 'Tarmoq xatosi, qaytadan urinib ko\'ring',
        'ru': 'Ошибка сети, попробуйте снова'
    },

    # ============================================================================
    # Success Messages (success.*)
    # ============================================================================
    'success.saved': {
        'en': 'Saved successfully',
        'uz': 'Muvaffaqiyatli saqlandi',
        'ru': 'Успешно сохранено'
    },
    'success.updated': {
        'en': 'Updated successfully',
        'uz': 'Muvaffaqiyatli yangilandi',
        'ru': 'Успешно обновлено'
    },
    'success.deleted': {
        'en': 'Deleted successfully',
        'uz': 'Muvaffaqiyatli o\'chirildi',
        'ru': 'Успешно удалено'
    },
    'success.sent': {
        'en': 'Sent successfully',
        'uz': 'Muvaffaqiyatli yuborildi',
        'ru': 'Успешно отправлено'
    },

    # ============================================================================
    # Common UI (common.*)
    # ============================================================================
    'common.yes': {
        'en': 'Yes',
        'uz': 'Ha',
        'ru': 'Да'
    },
    'common.no': {
        'en': 'No',
        'uz': 'Yo\'q',
        'ru': 'Нет'
    },
    'common.cancel': {
        'en': 'Cancel',
        'uz': 'Bekor qilish',
        'ru': 'Отмена'
    },
    'common.confirm': {
        'en': 'Confirm',
        'uz': 'Tasdiqlash',
        'ru': 'Подтвердить'
    },
    'common.save': {
        'en': 'Save',
        'uz': 'Saqlash',
        'ru': 'Сохранить'
    },
    'common.delete': {
        'en': 'Delete',
        'uz': 'O\'chirish',
        'ru': 'Удалить'
    },
    'common.edit': {
        'en': 'Edit',
        'uz': 'Tahrirlash',
        'ru': 'Редактировать'
    },
    'common.back': {
        'en': 'Back',
        'uz': 'Orqaga',
        'ru': 'Назад'
    },
    'common.next': {
        'en': 'Next',
        'uz': 'Keyingi',
        'ru': 'Далее'
    },
    'common.loading': {
        'en': 'Loading...',
        'uz': 'Yuklanmoqda...',
        'ru': 'Загрузка...'
    },
    'common.please_wait': {
        'en': 'Please wait',
        'uz': 'Iltimos, kuting',
        'ru': 'Пожалуйста, подождите'
    },
}


def main():
    """Seed backend translation keys"""
    app = create_app()

    with app.app_context():
        print("=" * 70)
        print("BACKEND TRANSLATION SEEDING")
        print("=" * 70)
        print()

        total_keys = len(BACKEND_TRANSLATIONS)
        total_translations = total_keys * 3  # 3 languages per key
        added_count = 0
        updated_count = 0
        skipped_count = 0

        print(f"Processing {total_keys} translation keys ({total_translations} total translations)...")
        print()

        for key, translations in BACKEND_TRANSLATIONS.items():
            print(f"  Key: {key}")

            for language, value in translations.items():
                # Check if translation already exists
                existing = Translation.query.filter_by(
                    key=key,
                    language=language
                ).first()

                if existing:
                    if existing.value != value:
                        print(f"    [{language}] Updating: '{existing.value}' → '{value}'")
                        existing.value = value
                        existing.is_active = True
                        existing.category = key.split('.')[0]  # Extract category from key
                        updated_count += 1
                    else:
                        skipped_count += 1
                else:
                    print(f"    [{language}] Creating: '{value}'")
                    translation = Translation(
                        key=key,
                        language=language,
                        value=value,
                        category=key.split('.')[0],  # e.g., 'api', 'error', 'success'
                        description=f"Backend translation for {key}",
                        is_active=True
                    )
                    db.session.add(translation)
                    added_count += 1

        # Commit all changes
        db.session.commit()

        print()
        print("=" * 70)
        print("✓ BACKEND TRANSLATION SEEDING COMPLETED")
        print("=" * 70)
        print()
        print("Summary:")
        print(f"  - Total keys processed: {total_keys}")
        print(f"  - New translations added: {added_count}")
        print(f"  - Existing translations updated: {updated_count}")
        print(f"  - Unchanged translations: {skipped_count}")
        print(f"  - Total translations in database: {added_count + updated_count + skipped_count}")
        print()
        print("Next steps:")
        print("  1. Update API endpoints to use get_translation() for messages")
        print("  2. Update service layer to use translations")
        print("  3. Update exception classes to support i18n")
        print("  4. Clear Redis cache: docker compose exec redis redis-cli FLUSHDB")
        print()


if __name__ == '__main__':
    main()
