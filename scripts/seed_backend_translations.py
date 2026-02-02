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
    'api.auth.otp_sent': {
        'en': 'Verification code sent',
        'uz': 'Tasdiqlash kodi yuborildi',
        'ru': 'Код подтверждения отправлен'
    },
    'api.auth.otp_resent': {
        'en': 'Verification code resent',
        'uz': 'Tasdiqlash kodi qayta yuborildi',
        'ru': 'Код подтверждения отправлен повторно'
    },
    'api.auth.otp_invalid': {
        'en': 'Invalid verification code',
        'uz': 'Noto\'g\'ri tasdiqlash kodi',
        'ru': 'Неверный код подтверждения'
    },
    'api.auth.otp_expired': {
        'en': 'Verification code has expired. Please request a new one.',
        'uz': 'Tasdiqlash kodi muddati tugagan. Iltimos, yangisini so\'rang.',
        'ru': 'Срок действия кода подтверждения истёк. Пожалуйста, запросите новый.'
    },

    # ============================================================================
    # SMS Templates (sms.*)
    # ============================================================================
    'sms.registration.otp': {
        'en': 'Bluestream: Your registration code: {otp_code}. Valid for 3 minutes.',
        'uz': 'Bluestream: Ro\'yxatdan o\'tish kodi: {otp_code}. Kod 3 daqiqa amal qiladi.',
        'ru': 'Bluestream: Код регистрации: {otp_code}. Код действителен 3 минуты.'
    },
    'sms.welcome': {
        'en': 'Welcome to Bluestream, {first_name}! Use our app to place orders.',
        'uz': 'Bluestream\'ga xush kelibsiz, {first_name}! Buyurtma berish uchun ilovamizdan foydalaning.',
        'ru': 'Добро пожаловать в Bluestream, {first_name}! Используйте наше приложение для заказов.'
    },
    'sms.verification.otp': {
        'en': 'Bluestream: Your verification code: {otp_code}. Valid for 5 minutes.',
        'uz': 'Bluestream: Tasdiqlash kodi: {otp_code}. Kod 5 daqiqa amal qiladi.',
        'ru': 'Bluestream: Код подтверждения: {otp_code}. Код действителен 5 минут.'
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

    # ============================================================================
    # Cart and Checkout (cart.*, checkout.*)
    # ============================================================================
    'Delivery fee will be calculated at checkout': {
        'en': 'Delivery fee will be calculated at checkout',
        'uz': 'Yetkazib berish narxi to\'lov sahifasida hisoblanadi',
        'ru': 'Стоимость доставки будет рассчитана при оформлении заказа'
    },
    'Select address': {
        'en': 'Select address',
        'uz': 'Manzilni tanlang',
        'ru': 'Выберите адрес'
    },
    'Calculating...': {
        'en': 'Calculating...',
        'uz': 'Hisoblanmoqda...',
        'ru': 'Расчёт...'
    },

    # ============================================================================
    # Payme Card Verification (checkout.payme.*)
    # ============================================================================
    'Card Verification': {
        'en': 'Card Verification',
        'uz': 'Kartani tasdiqlash',
        'ru': 'Подтверждение карты'
    },
    'We sent a verification code to': {
        'en': 'We sent a verification code to',
        'uz': 'Tasdiqlash kodini yubordik',
        'ru': 'Мы отправили код подтверждения на'
    },
    'Enter verification code': {
        'en': 'Enter verification code',
        'uz': 'Tasdiqlash kodini kiriting',
        'ru': 'Введите код подтверждения'
    },
    'Code expires in': {
        'en': 'Code expires in',
        'uz': 'Kod amal qilish muddati',
        'ru': 'Срок действия кода'
    },
    'Code expired.': {
        'en': 'Code expired.',
        'uz': 'Kod muddati tugadi.',
        'ru': 'Срок действия кода истек.'
    },
    'Request new code': {
        'en': 'Request new code',
        'uz': 'Yangi kod so\'rash',
        'ru': 'Запросить новый код'
    },
    'Resend code': {
        'en': 'Resend code',
        'uz': 'Kodni qayta yuborish',
        'ru': 'Отправить код повторно'
    },
    'Sending...': {
        'en': 'Sending...',
        'uz': 'Yuborilmoqda...',
        'ru': 'Отправка...'
    },
    'Verify': {
        'en': 'Verify',
        'uz': 'Tasdiqlash',
        'ru': 'Подтвердить'
    },
    'Verifying...': {
        'en': 'Verifying...',
        'uz': 'Tasdiqlanmoqda...',
        'ru': 'Проверка...'
    },
    'attempts remaining': {
        'en': 'attempts remaining',
        'uz': 'urinishlar qoldi',
        'ru': 'попыток осталось'
    },
    'New code sent': {
        'en': 'New code sent',
        'uz': 'Yangi kod yuborildi',
        'ru': 'Новый код отправлен'
    },
    'Failed to resend code': {
        'en': 'Failed to resend code',
        'uz': 'Kodni qayta yuborishda xatolik',
        'ru': 'Не удалось отправить код повторно'
    },
    'Please enter the verification code': {
        'en': 'Please enter the verification code',
        'uz': 'Iltimos, tasdiqlash kodini kiriting',
        'ru': 'Пожалуйста, введите код подтверждения'
    },
    'Too many failed attempts. Please request a new code.': {
        'en': 'Too many failed attempts. Please request a new code.',
        'uz': 'Juda ko\'p noto\'g\'ri urinishlar. Iltimos, yangi kod so\'rang.',
        'ru': 'Слишком много неудачных попыток. Пожалуйста, запросите новый код.'
    },
    'Invalid verification code': {
        'en': 'Invalid verification code',
        'uz': 'Noto\'g\'ri tasdiqlash kodi',
        'ru': 'Неверный код подтверждения'
    },
    'Verification failed. Please try again.': {
        'en': 'Verification failed. Please try again.',
        'uz': 'Tasdiqlash muvaffaqiyatsiz. Iltimos, qaytadan urinib ko\'ring.',
        'ru': 'Проверка не удалась. Пожалуйста, попробуйте снова.'
    },
    'Processing payment...': {
        'en': 'Processing payment...',
        'uz': 'To\'lov amalga oshirilmoqda...',
        'ru': 'Обработка платежа...'
    },
    'Please do not close this window': {
        'en': 'Please do not close this window',
        'uz': 'Iltimos, bu oynani yopmang',
        'ru': 'Пожалуйста, не закрывайте это окно'
    },
    'Payment failed. Please try again.': {
        'en': 'Payment failed. Please try again.',
        'uz': 'To\'lov muvaffaqiyatsiz. Iltimos, qaytadan urinib ko\'ring.',
        'ru': 'Платеж не удался. Пожалуйста, попробуйте снова.'
    },
    'Invalid card number': {
        'en': 'Invalid card number',
        'uz': 'Noto\'g\'ri karta raqami',
        'ru': 'Неверный номер карты'
    },
    'Invalid expiry date': {
        'en': 'Invalid expiry date',
        'uz': 'Noto\'g\'ri amal qilish muddati',
        'ru': 'Неверный срок действия'
    },
    'Cardholder name required': {
        'en': 'Cardholder name required',
        'uz': 'Karta egasining ismi talab qilinadi',
        'ru': 'Требуется имя владельца карты'
    },
    'Failed to process card': {
        'en': 'Failed to process card',
        'uz': 'Kartani qayta ishlashda xatolik',
        'ru': 'Не удалось обработать карту'
    },
    'Failed to create order': {
        'en': 'Failed to create order',
        'uz': 'Buyurtma yaratishda xatolik',
        'ru': 'Не удалось создать заказ'
    },
    'Failed to place order': {
        'en': 'Failed to place order',
        'uz': 'Buyurtma berishda xatolik',
        'ru': 'Не удалось оформить заказ'
    },

    # ============================================================================
    # Telegram Address Flow (telegram.address.*)
    # ============================================================================
    'telegram.address.location_prompt_enhanced': {
        'en': '📍 *Add New Address*\n\nPlease share your location for accurate delivery, or enter your address manually.\n\nSharing location is recommended for precise delivery.',
        'uz': '📍 *Yangi manzil qo\'shish*\n\nAniq yetkazib berish uchun joylashuvingizni ulashing yoki manzilni qo\'lda kiriting.\n\nAniq yetkazib berish uchun joylashuvni ulashish tavsiya etiladi.',
        'ru': '📍 *Добавить новый адрес*\n\nПоделитесь своим местоположением для точной доставки или введите адрес вручную.\n\nРекомендуется поделиться местоположением для точной доставки.'
    },
    'telegram.address.share_location_button': {
        'en': '📍 Share Location',
        'uz': '📍 Joylashuvni ulashish',
        'ru': '📍 Поделиться местоположением'
    },
    'telegram.address.enter_manually_button': {
        'en': '✏️ Enter Manually',
        'uz': '✏️ Qo\'lda kiritish',
        'ru': '✏️ Ввести вручную'
    },
    'telegram.address.select_region': {
        'en': 'Please select your region:',
        'uz': 'Iltimos, viloyatingizni tanlang:',
        'ru': 'Пожалуйста, выберите ваш регион:'
    },
    'telegram.address.select_district': {
        'en': 'Please select your district:',
        'uz': 'Iltimos, tumaningizni tanlang:',
        'ru': 'Пожалуйста, выберите ваш район:'
    },
    'telegram.address.enter_street': {
        'en': 'Please enter your street name, or skip if you don\'t want to specify:',
        'uz': 'Iltimos, ko\'changiz nomini kiriting yoki o\'tkazib yuboring:',
        'ru': 'Пожалуйста, введите название улицы или пропустите:'
    },
    'telegram.address.enter_building': {
        'en': 'Please enter your building/house number, or skip:',
        'uz': 'Iltimos, bino/uy raqamini kiriting yoki o\'tkazib yuboring:',
        'ru': 'Пожалуйста, введите номер дома или пропустите:'
    },
    'telegram.address.enter_apartment': {
        'en': 'Please enter your apartment number, or skip:',
        'uz': 'Iltimos, kvartira raqamini kiriting yoki o\'tkazib yuboring:',
        'ru': 'Пожалуйста, введите номер квартиры или пропустите:'
    },
    'telegram.address.enter_floor': {
        'en': 'Please enter your floor number, or skip:',
        'uz': 'Iltimos, qavat raqamini kiriting yoki o\'tkazib yuboring:',
        'ru': 'Пожалуйста, введите номер этажа или пропустите:'
    },
    'telegram.address.enter_entrance': {
        'en': 'Please enter your entrance/podyezd number, or skip:',
        'uz': 'Iltimos, kirish/podyezd raqamini kiriting yoki o\'tkazib yuboring:',
        'ru': 'Пожалуйста, введите номер подъезда или пропустите:'
    },
    'telegram.address.enter_delivery_instructions': {
        'en': 'Any special delivery instructions?\n(e.g., door code, call before arriving, preferred delivery times)\n\nOr skip if none:',
        'uz': 'Yetkazib berish bo\'yicha maxsus ko\'rsatmalar bormi?\n(masalan, eshik kodi, kelishdan oldin qo\'ng\'iroq qiling)\n\nYoki o\'tkazib yuboring:',
        'ru': 'Есть особые инструкции по доставке?\n(например, код домофона, позвонить перед приездом)\n\nИли пропустите:'
    },
    'telegram.address.skip_field': {
        'en': '⏭️ Skip',
        'uz': '⏭️ O\'tkazib yuborish',
        'ru': '⏭️ Пропустить'
    },
    'telegram.address.skip_instructions': {
        'en': '⏭️ Skip (No special instructions)',
        'uz': '⏭️ O\'tkazib yuborish (maxsus ko\'rsatmalar yo\'q)',
        'ru': '⏭️ Пропустить (без особых инструкций)'
    },
    'telegram.address.geocode_found': {
        'en': '📍 *Location Found*\n\nIs this location correct?',
        'uz': '📍 *Joylashuv topildi*\n\nBu joylashuv to\'g\'rimi?',
        'ru': '📍 *Местоположение найдено*\n\nЭто местоположение правильное?'
    },
    'telegram.address.geocode_failed': {
        'en': '⚠️ Could not find exact location. Using approximate district center.',
        'uz': '⚠️ Aniq joylashuvni topib bo\'lmadi. Taxminiy tuman markazi ishlatilmoqda.',
        'ru': '⚠️ Не удалось найти точное местоположение. Используется приблизительный центр района.'
    },
    'telegram.address.location_correct': {
        'en': '✅ Yes, Correct',
        'uz': '✅ Ha, to\'g\'ri',
        'ru': '✅ Да, верно'
    },
    'telegram.address.location_wrong': {
        'en': '❌ No, Re-enter',
        'uz': '❌ Yo\'q, qayta kiriting',
        'ru': '❌ Нет, ввести заново'
    },
    'telegram.address.edit_details': {
        'en': '✏️ Edit Details',
        'uz': '✏️ Ma\'lumotlarni tahrirlash',
        'ru': '✏️ Редактировать данные'
    },
    'telegram.address.title_prompt': {
        'en': 'Great! Now give this address a name.\n\nYou can choose from the suggestions below or type your own:',
        'uz': 'Ajoyib! Endi bu manzilga nom bering.\n\nQuyidagi variantlardan tanlashingiz yoki o\'zingiz yozishingiz mumkin:',
        'ru': 'Отлично! Теперь дайте этому адресу название.\n\nВы можете выбрать из предложенных вариантов или ввести свое:'
    },
    'telegram.address.saved_successfully': {
        'en': '✅ Address saved successfully!',
        'uz': '✅ Manzil muvaffaqiyatli saqlandi!',
        'ru': '✅ Адрес успешно сохранен!'
    },
    'telegram.address.save_failed': {
        'en': '❌ Failed to save address. Please try again.',
        'uz': '❌ Manzilni saqlashda xatolik. Iltimos, qaytadan urinib ko\'ring.',
        'ru': '❌ Не удалось сохранить адрес. Пожалуйста, попробуйте снова.'
    },

    # ============================================================================
    # Admin UI - Users Page (ui.users.*)
    # ============================================================================
    'ui.users.reg_method_phone': {
        'en': 'Phone',
        'uz': 'Telefon',
        'ru': 'Телефон'
    },
    'ui.users.reg_method_email': {
        'en': 'Email',
        'uz': 'Email',
        'ru': 'Email'
    },
    'ui.users.reg_method_telegram': {
        'en': 'Telegram',
        'uz': 'Telegram',
        'ru': 'Telegram'
    },
    'ui.users.filter_by_registration': {
        'en': 'Registration',
        'uz': 'Ro\'yxatdan o\'tish',
        'ru': 'Регистрация'
    },
    'ui.users.registration_method': {
        'en': 'Registration Method',
        'uz': 'Ro\'yxatdan o\'tish usuli',
        'ru': 'Способ регистрации'
    },

    # ============================================================================
    # Telegram Payment Buttons (telegram.payment.*)
    # ============================================================================
    'telegram.payment.pay_now': {
        'en': '💳 Pay Now',
        'uz': '💳 Hozir to\'lash',
        'ru': '💳 Оплатить'
    },
    'telegram.payment.cancel_order': {
        'en': '❌ Cancel Order',
        'uz': '❌ Buyurtmani bekor qilish',
        'ru': '❌ Отменить заказ'
    },
    'telegram.payment.cancel': {
        'en': 'Cancel Payment',
        'uz': 'To\'lovni bekor qilish',
        'ru': 'Отменить платеж'
    },
    'telegram.payment.retry': {
        'en': 'Retry Payment',
        'uz': 'To\'lovni qaytadan amalga oshirish',
        'ru': 'Повторить платеж'
    },
    'telegram.payment.switch_method': {
        'en': 'Choose Different Method',
        'uz': 'Boshqa usulni tanlash',
        'ru': 'Выбрать другой способ'
    },
    'telegram.payment.view_order': {
        'en': 'View Order',
        'uz': 'Buyurtmani ko\'rish',
        'ru': 'Посмотреть заказ'
    },
    'telegram.payment.back_to_menu': {
        'en': 'Back to Menu',
        'uz': 'Menyuga qaytish',
        'ru': 'Вернуться в меню'
    },
    'telegram.payment.pay_message': {
        'en': 'Click the button below to pay for your order',
        'uz': 'Buyurtmangiz uchun to\'lov qilish uchun quyidagi tugmani bosing',
        'ru': 'Нажмите кнопку ниже, чтобы оплатить заказ'
    },
    'telegram.payment.pay_btn': {
        'en': '💳 Pay',
        'uz': '💳 To\'lash',
        'ru': '💳 Оплатить'
    },

    # ============================================================================
    # Telegram Common Buttons (telegram.*)
    # ============================================================================
    'telegram.cancel': {
        'en': '❌ Cancel',
        'uz': '❌ Bekor qilish',
        'ru': '❌ Отмена'
    },
    'telegram.done': {
        'en': '✅ Done',
        'uz': '✅ Tayyor',
        'ru': '✅ Готово'
    },
    'telegram.edit': {
        'en': '✏️ Edit',
        'uz': '✏️ Tahrirlash',
        'ru': '✏️ Редактировать'
    },
    'telegram.delete': {
        'en': '🗑️ Delete',
        'uz': '🗑️ O\'chirish',
        'ru': '🗑️ Удалить'
    },

    # ============================================================================
    # Telegram Pagination (telegram.pagination.*)
    # ============================================================================
    'telegram.pagination.previous': {
        'en': '⬅️ Previous',
        'uz': '⬅️ Oldingi',
        'ru': '⬅️ Назад'
    },
    'telegram.pagination.next': {
        'en': 'Next ➡️',
        'uz': 'Keyingi ➡️',
        'ru': 'Далее ➡️'
    },

    # ============================================================================
    # Telegram Cart Buttons (telegram.cart.*)
    # ============================================================================
    'telegram.cart.continue_shopping': {
        'en': '🛍️ Continue Shopping',
        'uz': '🛍️ Xarid qilishda davom etish',
        'ru': '🛍️ Продолжить покупки'
    },
    'telegram.cart.clear': {
        'en': '🗑️ Clear Cart',
        'uz': '🗑️ Savatni tozalash',
        'ru': '🗑️ Очистить корзину'
    },

    # ============================================================================
    # Telegram Order Buttons (telegram.order.*)
    # ============================================================================
    'telegram.order.confirm': {
        'en': '✅ Confirm Order',
        'uz': '✅ Buyurtmani tasdiqlash',
        'ru': '✅ Подтвердить заказ'
    },
    'telegram.order.edit': {
        'en': '✏️ Edit Order',
        'uz': '✏️ Buyurtmani tahrirlash',
        'ru': '✏️ Редактировать заказ'
    },
    'telegram.order.track': {
        'en': '📍 Track Order',
        'uz': '📍 Buyurtmani kuzatish',
        'ru': '📍 Отследить заказ'
    },
    'telegram.order.reorder': {
        'en': '🔄 Reorder',
        'uz': '🔄 Qayta buyurtma berish',
        'ru': '🔄 Повторить заказ'
    },

    # ============================================================================
    # Telegram Subscription Buttons (telegram.subscription.*)
    # ============================================================================
    'telegram.subscription.create': {
        'en': '➕ Create Subscription',
        'uz': '➕ Obuna yaratish',
        'ru': '➕ Создать подписку'
    },
    'telegram.subscription.statistics': {
        'en': '📊 My Statistics',
        'uz': '📊 Mening statistikam',
        'ru': '📊 Моя статистика'
    },
    'telegram.subscription.pause': {
        'en': '⏸️ Pause',
        'uz': '⏸️ To\'xtatish',
        'ru': '⏸️ Приостановить'
    },
    'telegram.subscription.skip_next': {
        'en': '⏭️ Skip Next Delivery',
        'uz': '⏭️ Keyingisini o\'tkazib yuborish',
        'ru': '⏭️ Пропустить следующую'
    },
    'telegram.subscription.resume': {
        'en': '▶️ Resume',
        'uz': '▶️ Davom ettirish',
        'ru': '▶️ Возобновить'
    },
    'telegram.subscription.edit': {
        'en': '✏️ Edit Subscription',
        'uz': '✏️ Obunani tahrirlash',
        'ru': '✏️ Редактировать подписку'
    },
    'telegram.subscription.manage_items': {
        'en': '📦 Manage Items',
        'uz': '📦 Mahsulotlarni boshqarish',
        'ru': '📦 Управление товарами'
    },
    'telegram.subscription.billing': {
        'en': '💳 Billing',
        'uz': '💳 To\'lovlar',
        'ru': '💳 Платежи'
    },
    'telegram.subscription.logs': {
        'en': '📋 Logs',
        'uz': '📋 Loglar',
        'ru': '📋 Журнал'
    },
    'telegram.subscription.use_template': {
        'en': '📋 Use Template',
        'uz': '📋 Shablondan foydalanish',
        'ru': '📋 Использовать шаблон'
    },
    'telegram.subscription.create_custom': {
        'en': '✨ Create Custom',
        'uz': '✨ Maxsus yaratish',
        'ru': '✨ Создать свою'
    },
    'telegram.subscription.add_item': {
        'en': '➕ Add Item',
        'uz': '➕ Mahsulot qo\'shish',
        'ru': '➕ Добавить товар'
    },
    'telegram.subscription.add_more_items': {
        'en': '➕ Add More Items',
        'uz': '➕ Yana qo\'shish',
        'ru': '➕ Добавить ещё'
    },
    'telegram.subscription.change_frequency': {
        'en': '📅 Change Frequency',
        'uz': '📅 Chastotani o\'zgartirish',
        'ru': '📅 Изменить частоту'
    },
    'telegram.subscription.change_payment': {
        'en': '💳 Change Payment Method',
        'uz': '💳 To\'lov usulini o\'zgartirish',
        'ru': '💳 Изменить способ оплаты'
    },
    'telegram.subscription.view_logs': {
        'en': '📋 View Activity Log',
        'uz': '📋 Faoliyat jurnali',
        'ru': '📋 Журнал активности'
    },

    # ============================================================================
    # Telegram Profile Buttons (telegram.profile.*)
    # ============================================================================
    'telegram.profile.edit': {
        'en': '✏️ Edit Profile',
        'uz': '✏️ Profilni tahrirlash',
        'ru': '✏️ Редактировать профиль'
    },
    'telegram.profile.addresses': {
        'en': '📍 Addresses',
        'uz': '📍 Manzillar',
        'ru': '📍 Адреса'
    },
    'telegram.profile.phone_verification': {
        'en': '📱 Phone Verification',
        'uz': '📱 Telefonni tasdiqlash',
        'ru': '📱 Подтверждение телефона'
    },
    'telegram.profile.notifications': {
        'en': '🔔 Notifications',
        'uz': '🔔 Bildirishnomalar',
        'ru': '🔔 Уведомления'
    },
    'telegram.profile.payment_methods': {
        'en': '💳 Payment Methods',
        'uz': '💳 To\'lov usullari',
        'ru': '💳 Способы оплаты'
    },
    'telegram.profile.share_phone': {
        'en': '📱 Share Phone Number',
        'uz': '📱 Telefon raqamini ulashish',
        'ru': '📱 Поделиться номером'
    },

    # ============================================================================
    # Telegram Address Buttons (telegram.address.*)
    # ============================================================================
    'telegram.address.add_new': {
        'en': '➕ Add New Address',
        'uz': '➕ Yangi manzil qo\'shish',
        'ru': '➕ Добавить новый адрес'
    },
    'telegram.address.edit': {
        'en': '✏️ Edit Address',
        'uz': '✏️ Manzilni tahrirlash',
        'ru': '✏️ Редактировать адрес'
    },
    'telegram.address.delete': {
        'en': '🗑️ Delete Address',
        'uz': '🗑️ Manzilni o\'chirish',
        'ru': '🗑️ Удалить адрес'
    },
    'telegram.address.add_first': {
        'en': '➕ Add Your First Address',
        'uz': '➕ Birinchi manzilingizni qo\'shing',
        'ru': '➕ Добавьте первый адрес'
    },
    'telegram.address.set_default': {
        'en': '⭐ Set as Default',
        'uz': '⭐ Asosiy qilib belgilash',
        'ru': '⭐ Сделать основным'
    },

    # ============================================================================
    # Telegram Payment Method Buttons (telegram.payment_*)
    # ============================================================================
    'telegram.payment_card': {
        'en': '💳 Card',
        'uz': '💳 Karta',
        'ru': '💳 Карта'
    },
    'telegram.payment_cash': {
        'en': '💰 Cash on Delivery',
        'uz': '💰 Naqd pul',
        'ru': '💰 Наличными'
    },
    'telegram.payment_payme': {
        'en': '📱 Payme',
        'uz': '📱 Payme',
        'ru': '📱 Payme'
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
