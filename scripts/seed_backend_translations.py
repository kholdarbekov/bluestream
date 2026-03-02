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
    'api.auth.email_required_for_registration': {
        'en': 'Email is required for email registration. Use phone registration endpoints for phone-only signup.',
        'uz': 'Email orqali ro\'yxatdan o\'tish uchun email majburiy. Faqat telefon bilan ro\'yxatdan o\'tish uchun phone ro\'yxat endpointlaridan foydalaning.',
        'ru': 'Для регистрации по email требуется email. Для регистрации только по телефону используйте phone endpoint-ы.'
    },
    'api.auth.token_refreshed_successfully': {
        'en': 'Token refreshed successfully',
        'uz': 'Token muvaffaqiyatli yangilandi',
        'ru': 'Токен успешно обновлен'
    },
    'api.auth.address_not_found': {
        'en': 'Address not found',
        'uz': 'Manzil topilmadi',
        'ru': 'Адрес не найден'
    },
    'api.auth.address_updated_successfully': {
        'en': 'Address updated successfully',
        'uz': 'Manzil muvaffaqiyatli yangilandi',
        'ru': 'Адрес успешно обновлен'
    },
    'api.auth.phone_available': {
        'en': 'Phone number is available',
        'uz': 'Telefon raqami bo\'sh',
        'ru': 'Номер телефона доступен'
    },
    'api.auth.phone_already_registered': {
        'en': 'Phone number already registered',
        'uz': 'Telefon raqami allaqachon ro\'yxatdan o\'tgan',
        'ru': 'Номер телефона уже зарегистрирован'
    },
    'api.auth.telegram_user_not_found': {
        'en': 'Telegram user not found',
        'uz': 'Telegram foydalanuvchisi topilmadi',
        'ru': 'Пользователь Telegram не найден'
    },
    'api.auth.phone_account_not_found': {
        'en': 'No account found with this phone',
        'uz': 'Bu telefon raqami bilan akkaunt topilmadi',
        'ru': 'Аккаунт с этим номером телефона не найден'
    },
    'api.auth.phone_already_linked_to_telegram': {
        'en': 'This phone is already linked to another Telegram account',
        'uz': 'Bu telefon raqami boshqa Telegram akkauntiga allaqachon bog\'langan',
        'ru': 'Этот номер телефона уже привязан к другому Telegram-аккаунту'
    },
    'api.auth.phone_account_inactive': {
        'en': 'The account with this phone is not active',
        'uz': 'Bu telefon raqami bilan akkaunt faol emas',
        'ru': 'Аккаунт с этим номером телефона не активен'
    },
    'api.auth.otp_sent_success': {
        'en': 'OTP sent successfully',
        'uz': 'OTP muvaffaqiyatli yuborildi',
        'ru': 'OTP успешно отправлен'
    },
    'api.auth.otp_send_failed': {
        'en': 'Failed to send OTP',
        'uz': 'OTP yuborib bo\'lmadi',
        'ru': 'Не удалось отправить OTP'
    },
    'api.auth.pending_link_not_found': {
        'en': 'No pending link request found. Please start again.',
        'uz': 'Kutilayotgan bog\'lash so\'rovi topilmadi. Iltimos, qaytadan boshlang.',
        'ru': 'Ожидающий запрос на привязку не найден. Пожалуйста, начните заново.'
    },
    'api.auth.link_otp_invalid': {
        'en': 'Invalid OTP. Please try again.',
        'uz': 'Noto\'g\'ri OTP. Iltimos, qayta urinib ko\'ring.',
        'ru': 'Неверный OTP. Пожалуйста, попробуйте снова.'
    },
    'api.auth.web_account_not_found': {
        'en': 'Web account not found',
        'uz': 'Veb akkaunt topilmadi',
        'ru': 'Веб-аккаунт не найден'
    },
    'api.auth.accounts_linking_failed': {
        'en': 'Failed to link accounts',
        'uz': 'Akkauntlarni bog\'lab bo\'lmadi',
        'ru': 'Не удалось связать аккаунты'
    },
    'api.auth.accounts_linked_successfully': {
        'en': 'Accounts linked successfully!',
        'uz': 'Akkauntlar muvaffaqiyatli bog\'landi!',
        'ru': 'Аккаунты успешно связаны!'
    },
    'api.auth.link_with_existing_description': {
        'en': 'Link Telegram with existing account',
        'uz': 'Telegramni mavjud akkaunt bilan bog\'lash',
        'ru': 'Связать Telegram с существующим аккаунтом'
    },
    'api.auth.telegram_auth_instruction_open': {
        'en': 'Open Telegram using this link: {link}',
        'uz': 'Telegramni ushbu havola orqali oching: {link}',
        'ru': 'Откройте Telegram по этой ссылке: {link}'
    },
    'api.auth.telegram_auth_instruction_manual': {
        'en': 'Or open @{bot_username} and send: /start auth_{auth_code}',
        'uz': 'Yoki @{bot_username} ni ochib quyidagini yuboring: /start auth_{auth_code}',
        'ru': 'Или откройте @{bot_username} и отправьте: /start auth_{auth_code}'
    },
    'api.auth.telegram_auth_instruction_auto_link': {
        'en': 'Your accounts will be linked automatically.',
        'uz': 'Akkauntlaringiz avtomatik ravishda bog\'lanadi.',
        'ru': 'Ваши аккаунты будут связаны автоматически.'
    },
    'api.auth.web_auth_instruction_open': {
        'en': 'Open the web app using this link: {link}',
        'uz': 'Veb ilovani ushbu havola orqali oching: {link}',
        'ru': 'Откройте веб-приложение по этой ссылке: {link}'
    },
    'api.auth.web_auth_instruction_auto_login': {
        'en': 'You will be logged in automatically.',
        'uz': 'Siz avtomatik tizimga kirasiz.',
        'ru': 'Вы будете автоматически авторизованы.'
    },
    'api.auth.web_auth_instruction_expiry': {
        'en': 'This link expires in {minutes} minutes for security.',
        'uz': 'Xavfsizlik uchun ushbu havola {minutes} daqiqada tugaydi.',
        'ru': 'По соображениям безопасности эта ссылка истекает через {minutes} минут.'
    },
    'api.auth.error.auth_code_failed': {
        'en': 'Failed to generate authentication code',
        'uz': 'Autentifikatsiya kodini yaratib bo\'lmadi',
        'ru': 'Не удалось создать код аутентификации'
    },
    'api.auth.error.web_token_failed': {
        'en': 'Failed to generate web authentication token',
        'uz': 'Veb autentifikatsiya tokenini yaratib bo\'lmadi',
        'ru': 'Не удалось создать токен веб-аутентификации'
    },
    'address_added_successfully': {
        'en': 'Address added successfully',
        'uz': 'Manzil muvaffaqiyatli qo\'shildi',
        'ru': 'Адрес успешно добавлен'
    },
    'profile_updated_successfully': {
        'en': 'Profile updated successfully',
        'uz': 'Profil muvaffaqiyatli yangilandi',
        'ru': 'Профиль успешно обновлен'
    },
    'use_change_phone_endpoint': {
        'en': 'To update your phone number, use the /change-phone endpoint.',
        'uz': 'Telefon raqamingizni yangilash uchun /change-phone endpointidan foydalaning.',
        'ru': 'Чтобы обновить номер телефона, используйте endpoint /change-phone.'
    },
    'user_not_found': {
        'en': 'User not found',
        'uz': 'Foydalanuvchi topilmadi',
        'ru': 'Пользователь не найден'
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
    'api.orders.statuses_retrieved': {
        'en': 'Order statuses retrieved successfully',
        'uz': 'Buyurtma holatlari muvaffaqiyatli olindi',
        'ru': 'Статусы заказов успешно получены'
    },
    'api.orders.error.invalid_request_data': {
        'en': 'Invalid order request data',
        'uz': 'Buyurtma so\'rovi ma\'lumotlari noto\'g\'ri',
        'ru': 'Некорректные данные запроса заказа'
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
    'api.orders.error.business_account_entity_only': {
        'en': 'Business Account payment is only available for entity customers.',
        'uz': 'Business Account to\'lov usuli faqat yuridik mijozlar uchun mavjud.',
        'ru': 'Способ оплаты Business Account доступен только для юридических клиентов.'
    },
    'api.orders.error.business_account_contract_required': {
        'en': 'Business Account payment requires an active corporate contract that covers at least one order item.',
        'uz': 'Business Account to\'lovi uchun buyurtmadagi kamida bitta mahsulotni qamrab oluvchi faol korporativ shartnoma kerak.',
        'ru': 'Для оплаты через Business Account нужен активный корпоративный договор, покрывающий хотя бы одну позицию заказа.'
    },
    'api.orders.error.business_account_all_items_must_be_contract_backed': {
        'en': 'Every order item must be covered by an active corporate contract for Business Account payment.',
        'uz': 'Business Account to\'lovi uchun buyurtmadagi barcha mahsulotlar faol korporativ shartnoma bilan qamrab olinishi kerak.',
        'ru': 'Для оплаты через Business Account все позиции заказа должны быть покрыты активным корпоративным договором.'
    },
    'api.orders.error.business_account_contract_line_invalid': {
        'en': 'One or more contract-backed order lines are invalid for Business Account payment.',
        'uz': 'Business Account to\'lovi uchun shartnomaga bog\'langan ayrim buyurtma satrlari yaroqsiz.',
        'ru': 'Одна или несколько контрактных позиций заказа недействительны для оплаты через Business Account.'
    },
    'api.orders.error.business_account_insufficient_prepayment': {
        'en': 'Corporate prepayment balance is insufficient for one or more order items.',
        'uz': 'Bir yoki bir nechta buyurtma mahsuloti uchun korporativ oldindan to\'lov qoldig\'i yetarli emas.',
        'ru': 'Корпоративного предоплаченного остатка недостаточно для одной или нескольких позиций заказа.'
    },
    'api.orders.error.ambiguous_contract_pricing': {
        'en': 'Ambiguous contract pricing for product {product_id}. Multiple active contracts match: {contract_numbers}',
        'uz': '{product_id} mahsulot uchun shartnoma narxi noaniq. Bir nechta faol shartnoma mos keldi: {contract_numbers}',
        'ru': 'Неоднозначная контрактная цена для товара {product_id}. Подходят несколько активных договоров: {contract_numbers}'
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

    # ============================================================================
    # API - Cart (api.cart.*)
    # ============================================================================
    'api.cart.cleared': {
        'en': 'Cart cleared successfully',
        'uz': 'Savat muvaffaqiyatli tozalandi',
        'ru': 'Корзина успешно очищена'
    },
    'api.cart.synchronized': {
        'en': 'Cart synchronized successfully',
        'uz': 'Savat muvaffaqiyatli sinxronlashtirildi',
        'ru': 'Корзина успешно синхронизирована'
    },
    'api.cart.error.items_must_be_list': {
        'en': 'Cart items must be a list',
        'uz': 'Savat elementlari ro\'yxat bo\'lishi kerak',
        'ru': 'Элементы корзины должны быть списком'
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
    'api.payments.retrieved': {
        'en': 'Payments retrieved successfully',
        'uz': 'To\'lovlar muvaffaqiyatli olindi',
        'ru': 'Платежи успешно получены'
    },
    'api.payments.cancelled': {
        'en': 'Payment cancelled successfully',
        'uz': 'To\'lov muvaffaqiyatli bekor qilindi',
        'ru': 'Платеж успешно отменен'
    },
    'api.payments.cash_payment_created': {
        'en': 'Cash payment created. Pay on delivery.',
        'uz': 'Naqd to\'lov yaratildi. Yetkazilganda to\'lang.',
        'ru': 'Наличный платеж создан. Оплатите при доставке.'
    },
    'api.payments.card_saved': {
        'en': 'Card saved successfully',
        'uz': 'Karta muvaffaqiyatli saqlandi',
        'ru': 'Карта успешно сохранена'
    },
    'api.payments.card_deleted': {
        'en': 'Card deleted successfully',
        'uz': 'Karta muvaffaqiyatli o\'chirildi',
        'ru': 'Карта успешно удалена'
    },
    'api.payments.verification_initiated': {
        'en': 'Payment verification initiated',
        'uz': 'To\'lovni tekshirish boshlandi',
        'ru': 'Проверка платежа запущена'
    },
    'api.payments.refund_requested': {
        'en': 'Refund request submitted',
        'uz': 'Qaytarish so\'rovi yuborildi',
        'ru': 'Запрос на возврат отправлен'
    },
    'api.payments.refund_reason_customer_request': {
        'en': 'Customer request',
        'uz': 'Mijoz so\'rovi',
        'ru': 'Запрос клиента'
    },
    'api.payments.error.payment_not_found': {
        'en': 'Payment not found',
        'uz': 'To\'lov topilmadi',
        'ru': 'Платеж не найден'
    },
    'api.payments.error.already_paid': {
        'en': 'This order is already paid',
        'uz': 'Bu buyurtma allaqachon to\'langan',
        'ru': 'Этот заказ уже оплачен'
    },
    'api.payments.error.subscription_not_found': {
        'en': 'Subscription not found',
        'uz': 'Obuna topilmadi',
        'ru': 'Подписка не найдена'
    },
    'api.payments.error.get_methods_failed': {
        'en': 'Failed to get payment methods',
        'uz': 'To\'lov usullarini olishda xatolik',
        'ru': 'Не удалось получить способы оплаты'
    },
    'api.payments.error.create_failed': {
        'en': 'Failed to create payment',
        'uz': 'To\'lovni yaratib bo\'lmadi',
        'ru': 'Не удалось создать платеж'
    },
    'api.payments.error.subscription_create_failed': {
        'en': 'Failed to create subscription payment',
        'uz': 'Obuna to\'lovini yaratib bo\'lmadi',
        'ru': 'Не удалось создать платеж подписки'
    },
    'api.payments.error.get_status_failed': {
        'en': 'Failed to get payment status',
        'uz': 'To\'lov holatini olishda xatolik',
        'ru': 'Не удалось получить статус платежа'
    },
    'api.payments.error.only_pending_cancellable': {
        'en': 'Only pending payments can be cancelled',
        'uz': 'Faqat kutilayotgan to\'lovlarni bekor qilish mumkin',
        'ru': 'Отменить можно только ожидающие платежи'
    },
    'api.payments.error.cancel_failed': {
        'en': 'Failed to cancel payment',
        'uz': 'To\'lovni bekor qilib bo\'lmadi',
        'ru': 'Не удалось отменить платеж'
    },
    'api.payments.error.get_cards_failed': {
        'en': 'Failed to get saved cards',
        'uz': 'Saqlangan kartalarni olishda xatolik',
        'ru': 'Не удалось получить сохраненные карты'
    },
    'api.payments.error.save_card_failed': {
        'en': 'Failed to save card',
        'uz': 'Kartani saqlab bo\'lmadi',
        'ru': 'Не удалось сохранить карту'
    },
    'api.payments.error.delete_card_failed': {
        'en': 'Failed to delete card',
        'uz': 'Kartani o\'chirib bo\'lmadi',
        'ru': 'Не удалось удалить карту'
    },
    'api.payments.error.get_stats_failed': {
        'en': 'Failed to get payment statistics',
        'uz': 'To\'lov statistikasini olishda xatolik',
        'ru': 'Не удалось получить статистику платежей'
    },
    'api.payments.error.verify_failed': {
        'en': 'Failed to verify payment',
        'uz': 'To\'lovni tekshirib bo\'lmadi',
        'ru': 'Не удалось проверить платеж'
    },
    'api.payments.error.only_completed_refundable': {
        'en': 'Only completed payments can be refunded',
        'uz': 'Faqat yakunlangan to\'lovlarni qaytarish mumkin',
        'ru': 'Возврат возможен только для завершенных платежей'
    },
    'api.payments.error.refund_failed': {
        'en': 'Failed to request refund',
        'uz': 'Qaytarish so\'rovini yuborib bo\'lmadi',
        'ru': 'Не удалось отправить запрос на возврат'
    },
    'api.payments.error.get_rates_failed': {
        'en': 'Failed to get exchange rates',
        'uz': 'Valyuta kurslarini olishda xatolik',
        'ru': 'Не удалось получить валютные курсы'
    },
    'api.payments.error.create_card_token_failed': {
        'en': 'Failed to create card token',
        'uz': 'Karta tokenini yaratib bo\'lmadi',
        'ru': 'Не удалось создать токен карты'
    },
    'api.payments.error.send_verification_failed': {
        'en': 'Failed to send verification code',
        'uz': 'Tasdiqlash kodini yuborib bo\'lmadi',
        'ru': 'Не удалось отправить код подтверждения'
    },
    'api.payments.error.resend_verification_failed': {
        'en': 'Failed to resend verification code',
        'uz': 'Tasdiqlash kodini qayta yuborib bo\'lmadi',
        'ru': 'Не удалось повторно отправить код подтверждения'
    },
    'api.payments.error.verify_card_failed': {
        'en': 'Failed to verify card',
        'uz': 'Kartani tasdiqlab bo\'lmadi',
        'ru': 'Не удалось подтвердить карту'
    },
    'api.payments.error.process_card_payment_failed': {
        'en': 'Failed to process card payment',
        'uz': 'Karta to\'lovini qayta ishlashda xatolik',
        'ru': 'Не удалось обработать платеж картой'
    },

    # ============================================================================
    # API - Addresses (api.addresses.*)
    # ============================================================================
    'api.addresses.success.created': {
        'en': 'Address created successfully',
        'uz': 'Manzil muvaffaqiyatli yaratildi',
        'ru': 'Адрес успешно создан'
    },
    'api.addresses.success.updated': {
        'en': 'Address updated successfully',
        'uz': 'Manzil muvaffaqiyatli yangilandi',
        'ru': 'Адрес успешно обновлен'
    },
    'api.addresses.success.deleted': {
        'en': 'Address deleted successfully',
        'uz': 'Manzil muvaffaqiyatli o\'chirildi',
        'ru': 'Адрес успешно удален'
    },
    'api.addresses.success.default_updated': {
        'en': 'Default address updated',
        'uz': 'Asosiy manzil yangilandi',
        'ru': 'Адрес по умолчанию обновлен'
    },
    'api.addresses.error.not_found': {
        'en': 'Address not found',
        'uz': 'Manzil topilmadi',
        'ru': 'Адрес не найден'
    },
    'api.addresses.not_found': {
        'en': 'Address not found',
        'uz': 'Manzil topilmadi',
        'ru': 'Адрес не найден'
    },
    'api.addresses.error.full_address_or_coordinates_required': {
        'en': 'Either full_address or coordinates (latitude/longitude) are required',
        'uz': 'full_address yoki koordinatalar (latitude/longitude) talab qilinadi',
        'ru': 'Требуется либо full_address, либо координаты (latitude/longitude)'
    },
    'api.addresses.error.cannot_delete_only_address': {
        'en': 'Cannot delete your only address',
        'uz': 'Yagona manzilingizni o\'chirib bo\'lmaydi',
        'ru': 'Нельзя удалить единственный адрес'
    },
    'api.addresses.error.address_string_required': {
        'en': 'Address string is required',
        'uz': 'Manzil matni majburiy',
        'ru': 'Требуется строка адреса'
    },
    'api.addresses.error.geocode_not_found': {
        'en': 'Could not geocode the address. Please try with more specific details.',
        'uz': 'Manzil geokod qilinmadi. Iltimos, aniqroq ma\'lumot kiriting.',
        'ru': 'Не удалось геокодировать адрес. Пожалуйста, укажите более точные данные.'
    },
    'api.addresses.error.geocoding_service_unavailable': {
        'en': 'Geocoding service temporarily unavailable',
        'uz': 'Geokodlash xizmati vaqtincha mavjud emas',
        'ru': 'Сервис геокодирования временно недоступен'
    },
    'api.addresses.error.coordinates_required': {
        'en': 'Both latitude and longitude are required',
        'uz': 'Latitude va longitude ikkalasi ham talab qilinadi',
        'ru': 'Требуются и latitude, и longitude'
    },
    'api.addresses.error.coordinates_outside_supported_area': {
        'en': 'Coordinates are outside the supported delivery area (Tashkent)',
        'uz': 'Koordinatalar qo\'llab-quvvatlanadigan yetkazib berish hududidan tashqarida (Toshkent)',
        'ru': 'Координаты находятся вне поддерживаемой зоны доставки (Ташкент)'
    },
    'api.addresses.region.tashkent_city': {
        'en': 'Tashkent City',
        'uz': 'Toshkent shahri',
        'ru': 'Город Ташкент'
    },
    'api.addresses.city.tashkent': {
        'en': 'Tashkent',
        'uz': 'Toshkent',
        'ru': 'Ташкент'
    },
    'api.addresses.country.uzbekistan': {
        'en': 'Uzbekistan',
        'uz': 'O\'zbekiston',
        'ru': 'Узбекистан'
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
    'api.delivery.tracking_retrieved': {
        'en': 'Delivery tracking information retrieved successfully',
        'uz': 'Yetkazib berishni kuzatish ma\'lumotlari muvaffaqiyatli olindi',
        'ru': 'Информация об отслеживании доставки успешно получена'
    },
    'api.delivery.list_retrieved': {
        'en': 'Deliveries retrieved successfully',
        'uz': 'Yetkazib berishlar muvaffaqiyatli olindi',
        'ru': 'Доставки успешно получены'
    },
    'api.delivery.fee_calculated': {
        'en': 'Delivery fee calculated successfully',
        'uz': 'Yetkazib berish narxi muvaffaqiyatli hisoblandi',
        'ru': 'Стоимость доставки успешно рассчитана'
    },
    'api.delivery.location_updated_successfully': {
        'en': 'Location updated successfully',
        'uz': 'Joylashuv muvaffaqiyatli yangilandi',
        'ru': 'Местоположение успешно обновлено'
    },
    'api.delivery.started_successfully': {
        'en': 'Delivery started successfully',
        'uz': 'Yetkazib berish muvaffaqiyatli boshlandi',
        'ru': 'Доставка успешно начата'
    },
    'api.delivery.arrived_marked_successfully': {
        'en': 'Marked as arrived successfully',
        'uz': 'Muvaffaqiyatli yetib kelgan deb belgilandi',
        'ru': 'Успешно отмечено как прибытие'
    },
    'api.delivery.completion_processing': {
        'en': 'Delivery completion is being processed',
        'uz': 'Yetkazib berishni yakunlash jarayoni davom etmoqda',
        'ru': 'Завершение доставки обрабатывается'
    },
    'api.delivery.issue_reported_successfully': {
        'en': 'Issue reported successfully. Our team will assist you shortly.',
        'uz': 'Muammo muvaffaqiyatli yuborildi. Jamoamiz tez orada yordam beradi.',
        'ru': 'Проблема успешно отправлена. Наша команда скоро свяжется с вами.'
    },
    'api.delivery.route_optimization_requested': {
        'en': 'Route optimization requested. You will be notified when complete.',
        'uz': 'Marshrut optimallashtirish so\'rovi yuborildi. Tugagach sizga xabar beriladi.',
        'ru': 'Запрос на оптимизацию маршрута отправлен. Вы получите уведомление после завершения.'
    },
    'api.delivery.photo_uploaded_successfully': {
        'en': 'Photo uploaded successfully',
        'uz': 'Rasm muvaffaqiyatli yuklandi',
        'ru': 'Фото успешно загружено'
    },
    'api.delivery.error.tracking_number_required': {
        'en': 'Tracking number is required',
        'uz': 'Kuzatuv raqami majburiy',
        'ru': 'Требуется номер отслеживания'
    },
    'api.delivery.error.not_found': {
        'en': 'Delivery not found',
        'uz': 'Yetkazib berish topilmadi',
        'ru': 'Доставка не найдена'
    },
    'api.delivery.error.not_trackable': {
        'en': 'Delivery is not currently trackable',
        'uz': 'Yetkazib berishni hozircha kuzatib bo\'lmaydi',
        'ru': 'Доставка сейчас недоступна для отслеживания'
    },
    'api.delivery.error.get_live_tracking_failed': {
        'en': 'Failed to get live tracking',
        'uz': 'Jonli kuzatuvni olishda xatolik',
        'ru': 'Не удалось получить данные live-отслеживания'
    },
    'api.delivery.error.date_required': {
        'en': 'date parameter is required',
        'uz': 'date parametri majburiy',
        'ru': 'Требуется параметр date'
    },
    'api.delivery.error.invalid_date_format': {
        'en': 'Invalid date format. Use YYYY-MM-DD',
        'uz': 'Sana formati noto\'g\'ri. YYYY-MM-DD formatidan foydalaning',
        'ru': 'Неверный формат даты. Используйте YYYY-MM-DD'
    },
    'api.delivery.error.cannot_book_past_dates': {
        'en': 'Cannot book delivery for past dates',
        'uz': 'O\'tgan sanalar uchun yetkazib berishni bron qilib bo\'lmaydi',
        'ru': 'Нельзя бронировать доставку на прошедшие даты'
    },
    'api.delivery.error.get_time_slots_failed': {
        'en': 'Failed to get time slots',
        'uz': 'Vaqt oralig\'larini olishda xatolik',
        'ru': 'Не удалось получить временные интервалы'
    },
    'api.delivery.error.address_id_required': {
        'en': 'Address ID is required',
        'uz': 'Manzil ID majburiy',
        'ru': 'Требуется ID адреса'
    },
    'api.delivery.error.address_not_found': {
        'en': 'Address not found',
        'uz': 'Manzil topilmadi',
        'ru': 'Адрес не найден'
    },
    'api.delivery.error.get_zones_failed': {
        'en': 'Failed to get delivery zones',
        'uz': 'Yetkazib berish zonalarini olishda xatolik',
        'ru': 'Не удалось получить зоны доставки'
    },
    'api.delivery.error.estimate_validation_failed': {
        'en': 'Invalid delivery estimate request data',
        'uz': 'Yetkazib berish baholash so\'rovi ma\'lumotlari noto\'g\'ri',
        'ru': 'Некорректные данные запроса оценки доставки'
    },
    'api.delivery.error.estimate_failed': {
        'en': 'Failed to estimate delivery',
        'uz': 'Yetkazib berishni baholashda xatolik',
        'ru': 'Не удалось рассчитать доставку'
    },
    'api.delivery.error.driver_role_required': {
        'en': 'Access denied. Driver role required.',
        'uz': 'Kirish rad etildi. Haydovchi roli talab qilinadi.',
        'ru': 'Доступ запрещен. Требуется роль водителя.'
    },
    'api.delivery.error.get_assignments_failed': {
        'en': 'Failed to get assignments',
        'uz': 'Topshiriqlarni olishda xatolik',
        'ru': 'Не удалось получить назначения'
    },
    'api.delivery.error.invalid_coordinates': {
        'en': 'Invalid coordinates',
        'uz': 'Koordinatalar noto\'g\'ri',
        'ru': 'Неверные координаты'
    },
    'api.delivery.error.update_location_failed': {
        'en': 'Failed to update location',
        'uz': 'Joylashuvni yangilashda xatolik',
        'ru': 'Не удалось обновить местоположение'
    },
    'api.delivery.error.not_found_or_not_assigned': {
        'en': 'Delivery not found or not assigned to you',
        'uz': 'Yetkazib berish topilmadi yoki sizga biriktirilmagan',
        'ru': 'Доставка не найдена или не назначена вам'
    },
    'api.delivery.error.cannot_start_at_stage': {
        'en': 'Delivery cannot be started at this stage',
        'uz': 'Bu bosqichda yetkazib berishni boshlab bo\'lmaydi',
        'ru': 'Доставку нельзя начать на этом этапе'
    },
    'api.delivery.error.start_failed': {
        'en': 'Failed to start delivery',
        'uz': 'Yetkazib berishni boshlashda xatolik',
        'ru': 'Не удалось начать доставку'
    },
    'api.delivery.error.must_be_in_transit_to_arrive': {
        'en': 'Delivery must be in transit to mark as arrived',
        'uz': 'Yetib kelgan deb belgilash uchun yetkazib berish yo\'lda bo\'lishi kerak',
        'ru': 'Доставка должна быть в пути, чтобы отметить прибытие'
    },
    'api.delivery.error.mark_arrived_failed': {
        'en': 'Failed to mark as arrived',
        'uz': 'Yetib kelgan deb belgilashda xatolik',
        'ru': 'Не удалось отметить прибытие'
    },
    'api.delivery.error.must_be_arrived_before_completion': {
        'en': 'Delivery must be marked as arrived before completion',
        'uz': 'Yakunlashdan oldin yetkazib berish yetib kelgan deb belgilanishi kerak',
        'ru': 'Перед завершением доставку нужно отметить как прибывшую'
    },
    'api.delivery.error.complete_failed': {
        'en': 'Failed to complete delivery',
        'uz': 'Yetkazib berishni yakunlashda xatolik',
        'ru': 'Не удалось завершить доставку'
    },
    'api.delivery.error.invalid_issue_type': {
        'en': 'Invalid issue type',
        'uz': 'Muammo turi noto\'g\'ri',
        'ru': 'Недопустимый тип проблемы'
    },
    'api.delivery.error.report_issue_failed': {
        'en': 'Failed to report issue',
        'uz': 'Muammoni yuborishda xatolik',
        'ru': 'Не удалось сообщить о проблеме'
    },
    'api.delivery.error.route_optimization_failed': {
        'en': 'Failed to request route optimization',
        'uz': 'Marshrut optimallashtirish so\'rovini yuborishda xatolik',
        'ru': 'Не удалось отправить запрос на оптимизацию маршрута'
    },
    'api.delivery.error.no_photo_provided': {
        'en': 'No photo file provided',
        'uz': 'Rasm fayli taqdim etilmagan',
        'ru': 'Файл фото не предоставлен'
    },
    'api.delivery.error.no_file_selected': {
        'en': 'No file selected',
        'uz': 'Fayl tanlanmagan',
        'ru': 'Файл не выбран'
    },
    'api.delivery.error.invalid_photo_type': {
        'en': 'Invalid file type for delivery photos. Only JPG, PNG allowed. Got: {file_ext}',
        'uz': 'Yetkazib berish rasmi uchun fayl turi noto\'g\'ri. Faqat JPG, PNG ruxsat etiladi. Olingan: {file_ext}',
        'ru': 'Недопустимый тип файла для фото доставки. Разрешены только JPG, PNG. Получено: {file_ext}'
    },
    'api.delivery.error.photo_too_large': {
        'en': 'File too large for delivery photo. Maximum: 5MB, Got: {size_mb}MB',
        'uz': 'Yetkazib berish rasmi uchun fayl juda katta. Maksimum: 5MB, olingan: {size_mb}MB',
        'ru': 'Файл слишком большой для фото доставки. Максимум: 5MB, получено: {size_mb}MB'
    },
    'api.delivery.error.file_validation_failed': {
        'en': 'File validation failed',
        'uz': 'Fayl tekshiruvi muvaffaqiyatsiz tugadi',
        'ru': 'Проверка файла не пройдена'
    },
    'api.delivery.error.upload_no_result': {
        'en': 'Upload failed - no result returned',
        'uz': 'Yuklash muvaffaqiyatsiz - natija qaytmadi',
        'ru': 'Загрузка не удалась - результат не возвращен'
    },
    'api.delivery.error.upload_failed': {
        'en': 'Failed to upload photo',
        'uz': 'Rasmni yuklashda xatolik',
        'ru': 'Не удалось загрузить фото'
    },

    # ============================================================================
    # API - Subscriptions (api.subscriptions.*)
    # ============================================================================
    'api.subscriptions.created': {
        'en': 'Subscription created successfully',
        'uz': 'Obuna muvaffaqiyatli yaratildi',
        'ru': 'Подписка успешно создана'
    },
    'api.subscriptions.updated': {
        'en': 'Subscription updated successfully',
        'uz': 'Obuna muvaffaqiyatli yangilandi',
        'ru': 'Подписка успешно обновлена'
    },
    'api.subscriptions.cancelled': {
        'en': 'Subscription cancelled',
        'uz': 'Obuna bekor qilindi',
        'ru': 'Подписка отменена'
    },
    'api.subscriptions.cancellation_scheduled': {
        'en': 'Subscription cancellation scheduled',
        'uz': 'Obunani bekor qilish rejalashtirildi',
        'ru': 'Отмена подписки запланирована'
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
    'api.subscriptions.item_added': {
        'en': 'Subscription item added',
        'uz': 'Obuna elementi qo\'shildi',
        'ru': 'Элемент подписки добавлен'
    },
    'api.subscriptions.item_updated': {
        'en': 'Subscription item updated',
        'uz': 'Obuna elementi yangilandi',
        'ru': 'Элемент подписки обновлен'
    },
    'api.subscriptions.item_removed': {
        'en': 'Subscription item removed',
        'uz': 'Obuna elementi olib tashlandi',
        'ru': 'Элемент подписки удален'
    },
    'api.subscriptions.preview_calculated': {
        'en': 'Subscription preview calculated',
        'uz': 'Obuna oldindan ko\'rish hisoblandi',
        'ru': 'Предпросмотр подписки рассчитан'
    },
    'api.subscriptions.next_delivery_skipped': {
        'en': 'Next delivery skipped',
        'uz': 'Keyingi yetkazib berish o\'tkazib yuborildi',
        'ru': 'Следующая доставка пропущена'
    },
    'api.subscriptions.payment_method_updated': {
        'en': 'Subscription payment method updated',
        'uz': 'Obuna to\'lov usuli yangilandi',
        'ru': 'Способ оплаты подписки обновлен'
    },
    'api.subscriptions.billing_retry_initiated': {
        'en': 'Billing retry initiated',
        'uz': 'Hisob-kitobni qayta urinish boshlandi',
        'ru': 'Повторная попытка списания запущена'
    },
    'api.subscriptions.default_reason_customer_request': {
        'en': 'Customer request',
        'uz': 'Mijoz so\'rovi',
        'ru': 'Запрос клиента'
    },
    'api.subscriptions.manual_resume_required': {
        'en': 'Manual resume required',
        'uz': 'Qo\'lda davom ettirish talab etiladi',
        'ru': 'Требуется ручное возобновление'
    },
    'api.subscriptions.immediate': {
        'en': 'Immediate',
        'uz': 'Darhol',
        'ru': 'Немедленно'
    },
    'api.subscriptions.unknown_product': {
        'en': 'Unknown product',
        'uz': 'Noma\'lum mahsulot',
        'ru': 'Неизвестный товар'
    },
    'api.subscriptions.error.not_found': {
        'en': 'Subscription not found',
        'uz': 'Obuna topilmadi',
        'ru': 'Подписка не найдена'
    },
    'api.subscriptions.error.validation_failed': {
        'en': 'Invalid subscription request data',
        'uz': 'Obuna so\'rovi ma\'lumotlari noto\'g\'ri',
        'ru': 'Некорректные данные запроса подписки'
    },
    'api.subscriptions.error.invalid_status_value': {
        'en': 'Invalid subscription status value',
        'uz': 'Obuna holati qiymati noto\'g\'ri',
        'ru': 'Недопустимое значение статуса подписки'
    },
    'api.subscriptions.error.invalid_delivery_address': {
        'en': 'Invalid delivery address',
        'uz': 'Yetkazib berish manzili noto\'g\'ri',
        'ru': 'Некорректный адрес доставки'
    },
    'api.subscriptions.error.invalid_or_inactive_time_slot': {
        'en': 'Invalid or inactive delivery time slot',
        'uz': 'Yetkazib berish vaqt oralig\'i noto\'g\'ri yoki faol emas',
        'ru': 'Некорректный или неактивный интервал доставки'
    },
    'api.subscriptions.error.invalid_payment_method': {
        'en': 'Invalid payment method',
        'uz': 'Noto\'g\'ri to\'lov usuli',
        'ru': 'Некорректный способ оплаты'
    },
    'api.subscriptions.error.already_cancelled': {
        'en': 'Subscription is already cancelled',
        'uz': 'Obuna allaqachon bekor qilingan',
        'ru': 'Подписка уже отменена'
    },
    'api.subscriptions.error.cannot_update_cancelled': {
        'en': 'Cannot update cancelled subscription',
        'uz': 'Bekor qilingan obunani yangilab bo\'lmaydi',
        'ru': 'Нельзя обновить отмененную подписку'
    },
    'api.subscriptions.error.cannot_modify_cancelled': {
        'en': 'Cannot modify cancelled subscription',
        'uz': 'Bekor qilingan obunani o\'zgartirib bo\'lmaydi',
        'ru': 'Нельзя изменять отмененную подписку'
    },
    'api.subscriptions.error.cannot_change_payment_cancelled': {
        'en': 'Cannot change payment method for cancelled subscription',
        'uz': 'Bekor qilingan obuna uchun to\'lov usulini o\'zgartirib bo\'lmaydi',
        'ru': 'Нельзя изменить способ оплаты для отмененной подписки'
    },
    'api.subscriptions.error.cannot_remove_last_item': {
        'en': 'Cannot remove the last subscription item',
        'uz': 'Obunadagi oxirgi elementni olib tashlab bo\'lmaydi',
        'ru': 'Нельзя удалить последний элемент подписки'
    },
    'api.subscriptions.error.only_active_pause': {
        'en': 'Only active subscriptions can be paused',
        'uz': 'Faqat faol obunalarni to\'xtatish mumkin',
        'ru': 'Приостановить можно только активные подписки'
    },
    'api.subscriptions.error.only_paused_resume': {
        'en': 'Only paused subscriptions can be resumed',
        'uz': 'Faqat to\'xtatilgan obunalarni davom ettirish mumkin',
        'ru': 'Возобновить можно только приостановленные подписки'
    },
    'api.subscriptions.error.only_active_skip': {
        'en': 'Only active subscriptions can skip delivery',
        'uz': 'Faqat faol obunalar yetkazib berishni o\'tkazib yuborishi mumkin',
        'ru': 'Только активные подписки могут пропускать доставку'
    },
    'api.subscriptions.error.only_active_retry': {
        'en': 'Only active subscriptions can retry billing',
        'uz': 'Faqat faol obunalar hisob-kitobni qayta urinishi mumkin',
        'ru': 'Только активные подписки могут повторить списание'
    },
    'api.subscriptions.error.resume_date_future': {
        'en': 'Resume date must be in the future',
        'uz': 'Davom ettirish sanasi kelajakda bo\'lishi kerak',
        'ru': 'Дата возобновления должна быть в будущем'
    },
    'api.subscriptions.error.product_not_found': {
        'en': 'Product not found',
        'uz': 'Mahsulot topilmadi',
        'ru': 'Товар не найден'
    },
    'api.subscriptions.error.product_already_exists': {
        'en': 'Product already exists in subscription',
        'uz': 'Mahsulot obunada allaqachon mavjud',
        'ru': 'Товар уже есть в подписке'
    },
    'api.subscriptions.error.item_not_found': {
        'en': 'Subscription item not found',
        'uz': 'Obuna elementi topilmadi',
        'ru': 'Элемент подписки не найден'
    },
    'api.subscriptions.error.no_failed_billing_to_retry': {
        'en': 'No failed billing attempts to retry',
        'uz': 'Qayta urinish uchun muvaffaqiyatsiz hisob-kitob yo\'q',
        'ru': 'Нет неудачных списаний для повторной попытки'
    },
    'api.subscriptions.error.get_failed': {
        'en': 'Failed to retrieve subscriptions',
        'uz': 'Obunalarni olishda xatolik',
        'ru': 'Не удалось получить подписки'
    },
    'api.subscriptions.error.create_failed': {
        'en': 'Failed to create subscription',
        'uz': 'Obuna yaratishda xatolik',
        'ru': 'Не удалось создать подписку'
    },
    'api.subscriptions.error.update_failed': {
        'en': 'Failed to update subscription',
        'uz': 'Obunani yangilashda xatolik',
        'ru': 'Не удалось обновить подписку'
    },
    'api.subscriptions.error.pause_failed': {
        'en': 'Failed to pause subscription',
        'uz': 'Obunani to\'xtatishda xatolik',
        'ru': 'Не удалось приостановить подписку'
    },
    'api.subscriptions.error.resume_failed': {
        'en': 'Failed to resume subscription',
        'uz': 'Obunani davom ettirishda xatolik',
        'ru': 'Не удалось возобновить подписку'
    },
    'api.subscriptions.error.cancel_failed': {
        'en': 'Failed to cancel subscription',
        'uz': 'Obunani bekor qilishda xatolik',
        'ru': 'Не удалось отменить подписку'
    },
    'api.subscriptions.error.get_items_failed': {
        'en': 'Failed to retrieve subscription items',
        'uz': 'Obuna elementlarini olishda xatolik',
        'ru': 'Не удалось получить элементы подписки'
    },
    'api.subscriptions.error.add_item_failed': {
        'en': 'Failed to add subscription item',
        'uz': 'Obunaga element qo\'shishda xatolik',
        'ru': 'Не удалось добавить элемент подписки'
    },
    'api.subscriptions.error.update_item_failed': {
        'en': 'Failed to update subscription item',
        'uz': 'Obuna elementini yangilashda xatolik',
        'ru': 'Не удалось обновить элемент подписки'
    },
    'api.subscriptions.error.remove_item_failed': {
        'en': 'Failed to remove subscription item',
        'uz': 'Obuna elementini olib tashlashda xatolik',
        'ru': 'Не удалось удалить элемент подписки'
    },
    'api.subscriptions.error.get_billing_history_failed': {
        'en': 'Failed to retrieve billing history',
        'uz': 'Hisob-kitob tarixini olishda xatolik',
        'ru': 'Не удалось получить историю списаний'
    },
    'api.subscriptions.error.get_logs_failed': {
        'en': 'Failed to retrieve subscription logs',
        'uz': 'Obuna jurnallarini olishda xatolik',
        'ru': 'Не удалось получить журналы подписки'
    },
    'api.subscriptions.error.get_templates_failed': {
        'en': 'Failed to retrieve subscription templates',
        'uz': 'Obuna shablonlarini olishda xatolik',
        'ru': 'Не удалось получить шаблоны подписок'
    },
    'api.subscriptions.error.preview_failed': {
        'en': 'Failed to calculate subscription preview',
        'uz': 'Obuna oldindan ko\'rishini hisoblashda xatolik',
        'ru': 'Не удалось рассчитать предпросмотр подписки'
    },
    'api.subscriptions.error.get_statistics_failed': {
        'en': 'Failed to retrieve subscription statistics',
        'uz': 'Obuna statistikasini olishda xatolik',
        'ru': 'Не удалось получить статистику подписок'
    },
    'api.subscriptions.error.skip_failed': {
        'en': 'Failed to skip next delivery',
        'uz': 'Keyingi yetkazib berishni o\'tkazib yuborishda xatolik',
        'ru': 'Не удалось пропустить следующую доставку'
    },
    'api.subscriptions.error.change_payment_failed': {
        'en': 'Failed to change subscription payment method',
        'uz': 'Obuna to\'lov usulini o\'zgartirishda xatolik',
        'ru': 'Не удалось изменить способ оплаты подписки'
    },
    'api.subscriptions.error.retry_billing_failed': {
        'en': 'Failed to retry billing',
        'uz': 'Hisob-kitobni qayta urinishda xatolik',
        'ru': 'Не удалось повторить списание'
    },
    'api.subscriptions.log.updated_fields': {
        'en': 'Updated fields: {fields}',
        'uz': 'Yangilangan maydonlar: {fields}',
        'ru': 'Обновленные поля: {fields}'
    },
    'api.subscriptions.log.reason': {
        'en': 'Reason: {reason}',
        'uz': 'Sabab: {reason}',
        'ru': 'Причина: {reason}'
    },
    'api.subscriptions.log.resumed': {
        'en': 'Subscription resumed',
        'uz': 'Obuna davom ettirildi',
        'ru': 'Подписка возобновлена'
    },
    'api.subscriptions.log.cancelled_with_reason': {
        'en': 'Reason: {reason}',
        'uz': 'Sabab: {reason}',
        'ru': 'Причина: {reason}'
    },
    'api.subscriptions.log.cancellation_scheduled': {
        'en': 'Subscription will be cancelled on {date}. Reason: {reason}',
        'uz': 'Obuna {date} da bekor qilinadi. Sabab: {reason}',
        'ru': 'Подписка будет отменена {date}. Причина: {reason}'
    },
    'api.subscriptions.log.item_added': {
        'en': 'Added {quantity}x {product}',
        'uz': '{quantity}x {product} qo\'shildi',
        'ru': 'Добавлено {quantity}x {product}'
    },
    'api.subscriptions.log.item_updated': {
        'en': 'Updated {product} quantity from {old_quantity} to {new_quantity}',
        'uz': '{product} miqdori {old_quantity} dan {new_quantity} ga yangilandi',
        'ru': 'Количество {product} изменено с {old_quantity} на {new_quantity}'
    },
    'api.subscriptions.log.item_removed': {
        'en': 'Removed {product}',
        'uz': '{product} olib tashlandi',
        'ru': 'Удален {product}'
    },
    'api.subscriptions.log.delivery_skipped': {
        'en': 'Skipped delivery scheduled for {date}. Reason: {reason}',
        'uz': '{date} ga rejalashtirilgan yetkazib berish o\'tkazib yuborildi. Sabab: {reason}',
        'ru': 'Доставка, запланированная на {date}, пропущена. Причина: {reason}'
    },
    'api.subscriptions.log.payment_method_changed': {
        'en': 'Payment method changed from {old_method} to {new_method}',
        'uz': 'To\'lov usuli {old_method} dan {new_method} ga o\'zgartirildi',
        'ru': 'Способ оплаты изменен с {old_method} на {new_method}'
    },
    'api.subscriptions.templates.basic_weekly.name': {
        'en': 'Basic Weekly',
        'uz': 'Asosiy haftalik',
        'ru': 'Базовая еженедельная'
    },
    'api.subscriptions.templates.basic_weekly.description': {
        'en': 'Perfect for small families - weekly water delivery',
        'uz': 'Kichik oilalar uchun ideal - haftalik suv yetkazib berish',
        'ru': 'Идеально для небольших семей - еженедельная доставка воды'
    },
    'api.subscriptions.templates.family_monthly.name': {
        'en': 'Family Monthly',
        'uz': 'Oilaviy oylik',
        'ru': 'Семейная ежемесячная'
    },
    'api.subscriptions.templates.family_monthly.description': {
        'en': 'Great value for larger families - monthly bulk delivery',
        'uz': 'Katta oilalar uchun qulay - oyiga yirik yetkazib berish',
        'ru': 'Выгодно для больших семей - ежемесячная доставка большим объемом'
    },
    'api.subscriptions.templates.office_daily.name': {
        'en': 'Office Daily',
        'uz': 'Ofis uchun kunlik',
        'ru': 'Ежедневно в офис'
    },
    'api.subscriptions.templates.office_daily.description': {
        'en': 'Fresh water for your office every day',
        'uz': 'Ofisingiz uchun har kuni toza suv',
        'ru': 'Свежая вода для вашего офиса каждый день'
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
    'api.loyalty.reward_redeemed_successfully': {
        'en': 'Reward redeemed successfully',
        'uz': 'Mukofot muvaffaqiyatli ishlatildi',
        'ru': 'Награда успешно активирована'
    },
    'api.loyalty.points_awarded_successfully': {
        'en': 'Points awarded successfully',
        'uz': 'Ballar muvaffaqiyatli berildi',
        'ru': 'Баллы успешно начислены'
    },
    'api.loyalty.points_gifted_successfully': {
        'en': 'Points gifted successfully',
        'uz': 'Ballar muvaffaqiyatli sovg\'a qilindi',
        'ru': 'Баллы успешно подарены'
    },
    'api.loyalty.error.validation_failed': {
        'en': 'Invalid loyalty request data',
        'uz': 'Sodiqlik so\'rovi ma\'lumotlari noto\'g\'ri',
        'ru': 'Некорректные данные запроса лояльности'
    },
    'api.loyalty.error.reward_not_found': {
        'en': 'Reward not found',
        'uz': 'Mukofot topilmadi',
        'ru': 'Награда не найдена'
    },
    'api.loyalty.error.recipient_not_found': {
        'en': 'Recipient not found',
        'uz': 'Qabul qiluvchi topilmadi',
        'ru': 'Получатель не найден'
    },
    'api.loyalty.error.invalid_transaction_type': {
        'en': 'Invalid transaction type',
        'uz': 'Tranzaksiya turi noto\'g\'ri',
        'ru': 'Недопустимый тип транзакции'
    },
    'api.loyalty.error.invalid_status_value': {
        'en': 'Invalid status value',
        'uz': 'Holat qiymati noto\'g\'ri',
        'ru': 'Недопустимое значение статуса'
    },
    'api.loyalty.error.invalid_action_type': {
        'en': 'Invalid action type',
        'uz': 'Harakat turi noto\'g\'ri',
        'ru': 'Недопустимый тип действия'
    },
    'api.loyalty.error.invalid_start_date_format': {
        'en': 'Invalid start_date format',
        'uz': 'start_date formati noto\'g\'ri',
        'ru': 'Неверный формат start_date'
    },
    'api.loyalty.error.invalid_end_date_format': {
        'en': 'Invalid end_date format',
        'uz': 'end_date formati noto\'g\'ri',
        'ru': 'Неверный формат end_date'
    },
    'api.loyalty.error.insufficient_points': {
        'en': 'Insufficient points balance',
        'uz': 'Ballar balansi yetarli emas',
        'ru': 'Недостаточный баланс баллов'
    },
    'api.loyalty.error.redemption_limit_reached': {
        'en': 'You have reached the redemption limit for this reward',
        'uz': 'Ushbu mukofot uchun ishlatish limitiga yetdingiz',
        'ru': 'Вы достигли лимита активации для этой награды'
    },
    'api.loyalty.error.reward_auto_applied': {
        'en': 'This reward is automatically applied by the system and cannot be manually redeemed',
        'uz': 'Bu mukofot tizim tomonidan avtomatik qo\'llanadi va qo\'lda ishlatib bo\'lmaydi',
        'ru': 'Эта награда применяется системой автоматически и не может быть активирована вручную'
    },
    'api.loyalty.error.reward_no_longer_available': {
        'en': 'Reward is no longer available',
        'uz': 'Mukofot endi mavjud emas',
        'ru': 'Награда больше недоступна'
    },
    'api.loyalty.error.points_amount_must_be_positive': {
        'en': 'Points amount must be positive',
        'uz': 'Ballar miqdori musbat bo\'lishi kerak',
        'ru': 'Количество баллов должно быть положительным'
    },
    'api.loyalty.error.cannot_gift_to_self': {
        'en': 'Cannot gift points to yourself',
        'uz': 'O\'zingizga ballar sovg\'a qilib bo\'lmaydi',
        'ru': 'Нельзя подарить баллы самому себе'
    },
    'api.loyalty.error.get_membership_tiers_failed': {
        'en': 'Failed to get membership tiers',
        'uz': 'A\'zolik darajalarini olishda xatolik',
        'ru': 'Не удалось получить уровни членства'
    },
    'api.loyalty.error.get_points_failed': {
        'en': 'Failed to get loyalty points',
        'uz': 'Sodiqlik ballarini olishda xatolik',
        'ru': 'Не удалось получить баллы лояльности'
    },
    'api.loyalty.error.get_account_failed': {
        'en': 'Failed to get loyalty account',
        'uz': 'Sodiqlik hisobini olishda xatolik',
        'ru': 'Не удалось получить аккаунт лояльности'
    },
    'api.loyalty.error.get_history_failed': {
        'en': 'Failed to get loyalty history',
        'uz': 'Sodiqlik tarixini olishda xatolik',
        'ru': 'Не удалось получить историю лояльности'
    },
    'api.loyalty.error.get_profile_failed': {
        'en': 'Failed to get loyalty profile',
        'uz': 'Sodiqlik profilini olishda xatolik',
        'ru': 'Не удалось получить профиль лояльности'
    },
    'api.loyalty.error.get_points_history_failed': {
        'en': 'Failed to get points history',
        'uz': 'Ballar tarixini olishda xatolik',
        'ru': 'Не удалось получить историю баллов'
    },
    'api.loyalty.error.get_rewards_failed': {
        'en': 'Failed to get rewards',
        'uz': 'Mukofotlarni olishda xatolik',
        'ru': 'Не удалось получить награды'
    },
    'api.loyalty.error.get_reward_details_failed': {
        'en': 'Failed to get reward details',
        'uz': 'Mukofot tafsilotlarini olishda xatolik',
        'ru': 'Не удалось получить детали награды'
    },
    'api.loyalty.error.redeem_reward_failed': {
        'en': 'Failed to redeem reward',
        'uz': 'Mukofotni ishlatishda xatolik',
        'ru': 'Не удалось активировать награду'
    },
    'api.loyalty.error.get_redemption_history_failed': {
        'en': 'Failed to get redemption history',
        'uz': 'Ishlatish tarixini olishda xatolik',
        'ru': 'Не удалось получить историю активаций'
    },
    'api.loyalty.error.get_programs_failed': {
        'en': 'Failed to get loyalty programs',
        'uz': 'Sodiqlik dasturlarini olishda xatolik',
        'ru': 'Не удалось получить программы лояльности'
    },
    'api.loyalty.error.earn_points_failed': {
        'en': 'Failed to earn points',
        'uz': 'Ballar olishda xatolik',
        'ru': 'Не удалось начислить баллы'
    },
    'api.loyalty.error.get_referral_info_failed': {
        'en': 'Failed to get referral info',
        'uz': 'Taklif ma\'lumotlarini olishda xatolik',
        'ru': 'Не удалось получить информацию о рефералах'
    },
    'api.loyalty.error.get_statistics_failed': {
        'en': 'Failed to get loyalty statistics',
        'uz': 'Sodiqlik statistikasini olishda xatolik',
        'ru': 'Не удалось получить статистику лояльности'
    },
    'api.loyalty.error.get_challenges_failed': {
        'en': 'Failed to get loyalty challenges',
        'uz': 'Sodiqlik vazifalarini olishda xatolik',
        'ru': 'Не удалось получить задания лояльности'
    },
    'api.loyalty.error.get_tier_benefits_failed': {
        'en': 'Failed to get tier benefits',
        'uz': 'Daraja imtiyozlarini olishda xatolik',
        'ru': 'Не удалось получить преимущества уровня'
    },
    'api.loyalty.error.gift_points_failed': {
        'en': 'Failed to gift points',
        'uz': 'Ballarni sovg\'a qilishda xatolik',
        'ru': 'Не удалось подарить баллы'
    },

    # ============================================================================
    # API - Admin/Notifications Success Messages
    # ============================================================================
    'api.admin.success.blog_post_deleted': {
        'en': 'Blog post deleted successfully',
        'uz': 'Blog posti muvaffaqiyatli o\'chirildi',
        'ru': 'Пост блога успешно удален'
    },
    'api.admin.success.campaign_deactivated': {
        'en': 'Campaign deactivated successfully',
        'uz': 'Kampaniya muvaffaqiyatli o\'chirildi',
        'ru': 'Кампания успешно деактивирована'
    },
    'api.admin.success.campaign_deleted': {
        'en': 'Campaign deleted successfully',
        'uz': 'Kampaniya muvaffaqiyatli o\'chirildi',
        'ru': 'Кампания успешно удалена'
    },
    'api.admin.success.category_deleted': {
        'en': 'Category deleted successfully',
        'uz': 'Kategoriya muvaffaqiyatli o\'chirildi',
        'ru': 'Категория успешно удалена'
    },
    'api.admin.success.category_order_updated': {
        'en': 'Category order updated successfully',
        'uz': 'Kategoriya tartibi muvaffaqiyatli yangilandi',
        'ru': 'Порядок категорий успешно обновлен'
    },
    'api.admin.success.delivery_route_deleted': {
        'en': 'Delivery route deleted successfully',
        'uz': 'Yetkazib berish marshruti muvaffaqiyatli o\'chirildi',
        'ru': 'Маршрут доставки успешно удален'
    },
    'api.admin.success.loyalty_program_deleted': {
        'en': 'Loyalty program deleted successfully',
        'uz': 'Sodiqlik dasturi muvaffaqiyatli o\'chirildi',
        'ru': 'Программа лояльности успешно удалена'
    },
    'api.admin.success.loyalty_reward_deactivated': {
        'en': 'Loyalty reward deactivated successfully',
        'uz': 'Sodiqlik mukofoti muvaffaqiyatli o\'chirildi',
        'ru': 'Награда лояльности успешно деактивирована'
    },
    'api.admin.success.loyalty_reward_deleted': {
        'en': 'Loyalty reward deleted successfully',
        'uz': 'Sodiqlik mukofoti muvaffaqiyatli o\'chirildi',
        'ru': 'Награда лояльности успешно удалена'
    },
    'api.admin.success.price_rule_deleted': {
        'en': 'Price rule deleted successfully',
        'uz': 'Narx qoidasi muvaffaqiyatli o\'chirildi',
        'ru': 'Правило ценообразования успешно удалено'
    },
    'api.admin.success.review_deleted': {
        'en': 'Review deleted successfully',
        'uz': 'Sharh muvaffaqiyatli o\'chirildi',
        'ru': 'Отзыв успешно удален'
    },
    'api.admin.success.template_deactivated': {
        'en': 'Template deactivated successfully',
        'uz': 'Shablon muvaffaqiyatli o\'chirildi',
        'ru': 'Шаблон успешно деактивирован'
    },
    'api.admin.success.time_slot_deleted': {
        'en': 'Time slot deleted successfully',
        'uz': 'Vaqt oralig\'i muvaffaqiyatli o\'chirildi',
        'ru': 'Временной слот успешно удален'
    },
    'api.admin.success.time_slot_updated': {
        'en': 'Time slot updated successfully',
        'uz': 'Vaqt oralig\'i muvaffaqiyatli yangilandi',
        'ru': 'Временной слот успешно обновлен'
    },
    'api.admin.success.translation_deleted': {
        'en': 'Translation deleted successfully',
        'uz': 'Tarjima muvaffaqiyatli o\'chirildi',
        'ru': 'Перевод успешно удален'
    },
    'api.admin.success.translation_updated': {
        'en': 'Translation updated successfully',
        'uz': 'Tarjima muvaffaqiyatli yangilandi',
        'ru': 'Перевод успешно обновлен'
    },
    'ui.common.cancel': {
        'en': 'Cancel',
        'uz': 'Bekor qilish',
        'ru': 'Отмена'
    },
    'ui.common.edit': {
        'en': 'Edit',
        'uz': 'Tahrirlash',
        'ru': 'Редактировать'
    },
    'ui.common.remove': {
        'en': 'Remove',
        'uz': 'Olib tashlash',
        'ru': 'Удалить'
    },
    'ui.common.save': {
        'en': 'Save',
        'uz': 'Saqlash',
        'ru': 'Сохранить'
    },
    'ui.common.status': {
        'en': 'Status',
        'uz': 'Holat',
        'ru': 'Статус'
    },
    'ui.nav.corporate_contracts': {
        'en': 'Corporate Contracts',
        'uz': 'Korporativ shartnomalar',
        'ru': 'Корпоративные договоры'
    },
    'ui.nav.loyalty': {
        'en': 'Loyalty',
        'uz': 'Sodiqlik dasturi',
        'ru': 'Лояльность'
    },
    'ui.nav.loyalty_members': {
        'en': 'Members',
        'uz': 'A\'zolar',
        'ru': 'Участники'
    },
    'ui.nav.loyalty_programs': {
        'en': 'Programs',
        'uz': 'Dasturlar',
        'ru': 'Программы'
    },
    'ui.nav.loyalty_rewards': {
        'en': 'Rewards',
        'uz': 'Mukofotlar',
        'ru': 'Награды'
    },
    'ui.analytics.avg_redemption_value': {
        'en': 'Average Redemption Value',
        'uz': 'O\'rtacha yechib olish qiymati',
        'ru': 'Средняя стоимость списания'
    },
    'ui.analytics.loyalty': {
        'en': 'Loyalty',
        'uz': 'Sodiqlik dasturi',
        'ru': 'Лояльность'
    },
    'ui.analytics.loyalty_points_earned': {
        'en': 'Points Earned',
        'uz': 'Yig\'ilgan ballar',
        'ru': 'Начисленные баллы'
    },
    'ui.analytics.loyalty_points_redeemed': {
        'en': 'Points Redeemed',
        'uz': 'Ishlatilgan ballar',
        'ru': 'Списанные баллы'
    },
    'ui.analytics.loyalty_points_trend': {
        'en': 'Loyalty Points Trend',
        'uz': 'Sodiqlik ballari dinamikasi',
        'ru': 'Динамика баллов лояльности'
    },
    'ui.analytics.members': {
        'en': 'Members',
        'uz': 'A\'zolar',
        'ru': 'Участники'
    },
    'ui.analytics.points': {
        'en': 'Points',
        'uz': 'Ballar',
        'ru': 'Баллы'
    },
    'ui.analytics.points_in_circulation': {
        'en': 'Points In Circulation',
        'uz': 'Aylanmadagi ballar',
        'ru': 'Баллы в обращении'
    },
    'ui.analytics.program': {
        'en': 'Program',
        'uz': 'Dastur',
        'ru': 'Программа'
    },
    'ui.analytics.program_breakdown': {
        'en': 'Program Breakdown',
        'uz': 'Dasturlar kesimidagi ko\'rsatkichlar',
        'ru': 'Разбивка по программам'
    },
    'ui.analytics.redemptions': {
        'en': 'Redemptions',
        'uz': 'Yechib olishlar',
        'ru': 'Списания'
    },
    'ui.analytics.rewards': {
        'en': 'Rewards',
        'uz': 'Mukofotlar',
        'ru': 'Награды'
    },
    'ui.analytics.tier_distribution': {
        'en': 'Tier Distribution',
        'uz': 'Darajalar taqsimoti',
        'ru': 'Распределение по уровням'
    },
    'ui.analytics.top_rewards': {
        'en': 'Top Rewards',
        'uz': 'Eng ommabop mukofotlar',
        'ru': 'Популярные награды'
    },
    'ui.analytics.total_loyalty_members': {
        'en': 'Total Loyalty Members',
        'uz': 'Jami sodiqlik a\'zolari',
        'ru': 'Всего участников программы'
    },
    'ui.analytics.total_redemptions': {
        'en': 'Total Redemptions',
        'uz': 'Jami yechib olishlar',
        'ru': 'Всего списаний'
    },
    'ui.loyalty.actions': {
        'en': 'Actions',
        'uz': 'Amallar',
        'ru': 'Действия'
    },
    'ui.loyalty.active': {
        'en': 'Active',
        'uz': 'Faol',
        'ru': 'Активно'
    },
    'ui.loyalty.active_members': {
        'en': 'Active Members',
        'uz': 'Faol a\'zolar',
        'ru': 'Активные участники'
    },
    'ui.loyalty.active_programs': {
        'en': 'Active Programs',
        'uz': 'Faol dasturlar',
        'ru': 'Активные программы'
    },
    'ui.loyalty.active_rewards': {
        'en': 'Active Rewards',
        'uz': 'Faol mukofotlar',
        'ru': 'Активные награды'
    },
    'ui.loyalty.avg_points_per_member': {
        'en': 'Average Points per Member',
        'uz': 'Bir a\'zoga o\'rtacha ball',
        'ru': 'Среднее число баллов на участника'
    },
    'ui.loyalty.birthday_bonus': {
        'en': 'Birthday Bonus',
        'uz': 'Tug\'ilgan kun bonusi',
        'ru': 'Бонус ко дню рождения'
    },
    'ui.loyalty.cancel': {
        'en': 'Cancel',
        'uz': 'Bekor qilish',
        'ru': 'Отмена'
    },
    'ui.loyalty.create': {
        'en': 'Create',
        'uz': 'Yaratish',
        'ru': 'Создать'
    },
    'ui.loyalty.create_program': {
        'en': 'Create Program',
        'uz': 'Dastur yaratish',
        'ru': 'Создать программу'
    },
    'ui.loyalty.create_reward': {
        'en': 'Create Reward',
        'uz': 'Mukofot yaratish',
        'ru': 'Создать награду'
    },
    'ui.loyalty.create_success': {
        'en': 'Program created successfully',
        'uz': 'Dastur muvaffaqiyatli yaratildi',
        'ru': 'Программа успешно создана'
    },
    'ui.loyalty.create_tier': {
        'en': 'Create Tier',
        'uz': 'Daraja yaratish',
        'ru': 'Создать уровень'
    },
    'ui.loyalty.created': {
        'en': 'Created',
        'uz': 'Yaratilgan',
        'ru': 'Создано'
    },
    'ui.loyalty.current_points': {
        'en': 'Current Points',
        'uz': 'Joriy ballar',
        'ru': 'Текущие баллы'
    },
    'ui.loyalty.customer': {
        'en': 'Customer',
        'uz': 'Mijoz',
        'ru': 'Клиент'
    },
    'ui.loyalty.default_program': {
        'en': 'Default Program',
        'uz': 'Asosiy dastur',
        'ru': 'Программа по умолчанию'
    },
    'ui.loyalty.default_program_warning': {
        'en': 'Only one loyalty program should be marked as default.',
        'uz': 'Faqat bitta sodiqlik dasturi asosiy sifatida belgilanishi kerak.',
        'ru': 'Только одна программа лояльности должна быть отмечена как основная.'
    },
    'ui.loyalty.delete_confirm_message': {
        'en': 'Are you sure you want to delete this loyalty program?',
        'uz': 'Ushbu sodiqlik dasturini o\'chirishni tasdiqlaysizmi?',
        'ru': 'Вы уверены, что хотите удалить эту программу лояльности?'
    },
    'ui.loyalty.delete_confirm_title': {
        'en': 'Delete Program',
        'uz': 'Dasturni o\'chirish',
        'ru': 'Удалить программу'
    },
    'ui.loyalty.delete_success': {
        'en': 'Program deleted successfully',
        'uz': 'Dastur muvaffaqiyatli o\'chirildi',
        'ru': 'Программа успешно удалена'
    },
    'ui.loyalty.delete_tier_confirm_message': {
        'en': 'Are you sure you want to delete this tier?',
        'uz': 'Ushbu darajani o\'chirishni tasdiqlaysizmi?',
        'ru': 'Вы уверены, что хотите удалить этот уровень?'
    },
    'ui.loyalty.delete_tier_confirm_title': {
        'en': 'Delete Tier',
        'uz': 'Darajani o\'chirish',
        'ru': 'Удалить уровень'
    },
    'ui.loyalty.description': {
        'en': 'Description',
        'uz': 'Tavsif',
        'ru': 'Описание'
    },
    'ui.loyalty.discount': {
        'en': 'Discount',
        'uz': 'Chegirma',
        'ru': 'Скидка'
    },
    'ui.loyalty.discount_type': {
        'en': 'Discount Type',
        'uz': 'Chegirma turi',
        'ru': 'Тип скидки'
    },
    'ui.loyalty.discount_value': {
        'en': 'Discount Value',
        'uz': 'Chegirma qiymati',
        'ru': 'Размер скидки'
    },
    'ui.loyalty.display_order': {
        'en': 'Display Order',
        'uz': 'Ko\'rsatish tartibi',
        'ru': 'Порядок отображения'
    },
    'ui.loyalty.edit_program': {
        'en': 'Edit Program',
        'uz': 'Dasturni tahrirlash',
        'ru': 'Редактировать программу'
    },
    'ui.loyalty.edit_reward': {
        'en': 'Edit Reward',
        'uz': 'Mukofotni tahrirlash',
        'ru': 'Редактировать награду'
    },
    'ui.loyalty.edit_tier': {
        'en': 'Edit Tier',
        'uz': 'Darajani tahrirlash',
        'ru': 'Редактировать уровень'
    },
    'ui.loyalty.export_data': {
        'en': 'Export Programs',
        'uz': 'Dasturlarni eksport qilish',
        'ru': 'Экспорт программ'
    },
    'ui.loyalty.export_members': {
        'en': 'Export Members',
        'uz': 'A\'zolarni eksport qilish',
        'ru': 'Экспорт участников'
    },
    'ui.loyalty.export_rewards': {
        'en': 'Export Rewards',
        'uz': 'Mukofotlarni eksport qilish',
        'ru': 'Экспорт наград'
    },
    'ui.loyalty.featured': {
        'en': 'Featured',
        'uz': 'Ajratilgan',
        'ru': 'Рекомендуемое'
    },
    'ui.loyalty.featured_rewards': {
        'en': 'Featured Rewards',
        'uz': 'Ajratilgan mukofotlar',
        'ru': 'Рекомендуемые награды'
    },
    'ui.loyalty.filter_by_status': {
        'en': 'Filter by status',
        'uz': 'Holat bo\'yicha filtrlash',
        'ru': 'Фильтр по статусу'
    },
    'ui.loyalty.form_description': {
        'en': 'Description',
        'uz': 'Tavsif',
        'ru': 'Описание'
    },
    'ui.loyalty.free_product_id': {
        'en': 'Free Product',
        'uz': 'Bepul mahsulot',
        'ru': 'Бесплатный товар'
    },
    'ui.loyalty.image_url': {
        'en': 'Image URL',
        'uz': 'Rasm URL manzili',
        'ru': 'URL изображения'
    },
    'ui.loyalty.last_activity': {
        'en': 'Last Activity',
        'uz': 'Oxirgi faollik',
        'ru': 'Последняя активность'
    },
    'ui.loyalty.max_points': {
        'en': 'Maximum Points',
        'uz': 'Maksimal ball',
        'ru': 'Максимум баллов'
    },
    'ui.loyalty.max_redemptions': {
        'en': 'Maximum Redemptions',
        'uz': 'Maksimal yechib olishlar',
        'ru': 'Максимум списаний'
    },
    'ui.loyalty.max_uses_per_user': {
        'en': 'Max Uses per User',
        'uz': 'Bir foydalanuvchi uchun maksimal ishlatish',
        'ru': 'Максимум использований на пользователя'
    },
    'ui.loyalty.member_details': {
        'en': 'Member Details',
        'uz': 'A\'zo tafsilotlari',
        'ru': 'Детали участника'
    },
    'ui.loyalty.member_not_found': {
        'en': 'Member not found',
        'uz': 'A\'zo topilmadi',
        'ru': 'Участник не найден'
    },
    'ui.loyalty.member_since': {
        'en': 'Member Since',
        'uz': 'A\'zolikka qo\'shilgan sana',
        'ru': 'Участник с'
    },
    'ui.loyalty.min_order_value': {
        'en': 'Minimum Order Value',
        'uz': 'Minimal buyurtma summasi',
        'ru': 'Минимальная сумма заказа'
    },
    'ui.loyalty.min_points': {
        'en': 'Minimum Points',
        'uz': 'Minimal ball',
        'ru': 'Минимум баллов'
    },
    'ui.loyalty.min_redemption_points': {
        'en': 'Minimum Redemption Points',
        'uz': 'Minimal yechib olish ballari',
        'ru': 'Минимум баллов для списания'
    },
    'ui.loyalty.multiplier': {
        'en': 'Multiplier',
        'uz': 'Ko\'paytirgich',
        'ru': 'Множитель'
    },
    'ui.loyalty.no_members': {
        'en': 'No loyalty members found',
        'uz': 'Sodiqlik a\'zolari topilmadi',
        'ru': 'Участники программы не найдены'
    },
    'ui.loyalty.no_programs': {
        'en': 'No loyalty programs found',
        'uz': 'Sodiqlik dasturlari topilmadi',
        'ru': 'Программы лояльности не найдены'
    },
    'ui.loyalty.no_recent_activity': {
        'en': 'No recent transactions',
        'uz': 'So\'nggi tranzaksiyalar yo\'q',
        'ru': 'Недавних транзакций нет'
    },
    'ui.loyalty.no_recent_redemptions': {
        'en': 'No recent redemptions',
        'uz': 'So\'nggi yechib olishlar yo\'q',
        'ru': 'Недавних списаний нет'
    },
    'ui.loyalty.no_rewards': {
        'en': 'No rewards found',
        'uz': 'Mukofotlar topilmadi',
        'ru': 'Награды не найдены'
    },
    'ui.loyalty.no_tiers': {
        'en': 'No tiers configured',
        'uz': 'Darajalar sozlanmagan',
        'ru': 'Уровни не настроены'
    },
    'ui.loyalty.points_cost': {
        'en': 'Points Cost',
        'uz': 'Ball qiymati',
        'ru': 'Стоимость в баллах'
    },
    'ui.loyalty.points_distributed': {
        'en': 'Points In Circulation',
        'uz': 'Aylanmadagi ballar',
        'ru': 'Баллы в обращении'
    },
    'ui.loyalty.points_expiry_days': {
        'en': 'Points Expiry Days',
        'uz': 'Ball amal qilish kunlari',
        'ru': 'Срок действия баллов в днях'
    },
    'ui.loyalty.points_range': {
        'en': 'Points Range',
        'uz': 'Ball oralig\'i',
        'ru': 'Диапазон баллов'
    },
    'ui.loyalty.program': {
        'en': 'Program',
        'uz': 'Dastur',
        'ru': 'Программа'
    },
    'ui.loyalty.program_name': {
        'en': 'Program Name',
        'uz': 'Dastur nomi',
        'ru': 'Название программы'
    },
    'ui.loyalty.recent_activity': {
        'en': 'Recent Transactions',
        'uz': 'So\'nggi tranzaksiyalar',
        'ru': 'Недавние транзакции'
    },
    'ui.loyalty.recent_redemptions': {
        'en': 'Recent Redemptions',
        'uz': 'So\'nggi yechib olishlar',
        'ru': 'Недавние списания'
    },
    'ui.loyalty.redemptions': {
        'en': 'Redemptions',
        'uz': 'Yechib olishlar',
        'ru': 'Списания'
    },
    'ui.loyalty.referral_bonus': {
        'en': 'Referral Bonus',
        'uz': 'Referal bonusi',
        'ru': 'Реферальный бонус'
    },
    'ui.loyalty.reward_create_success': {
        'en': 'Reward created successfully',
        'uz': 'Mukofot muvaffaqiyatli yaratildi',
        'ru': 'Награда успешно создана'
    },
    'ui.loyalty.reward_delete_confirm_message': {
        'en': 'Are you sure you want to delete this reward?',
        'uz': 'Ushbu mukofotni o\'chirishni tasdiqlaysizmi?',
        'ru': 'Вы уверены, что хотите удалить эту награду?'
    },
    'ui.loyalty.reward_delete_confirm_title': {
        'en': 'Delete Reward',
        'uz': 'Mukofotni o\'chirish',
        'ru': 'Удалить награду'
    },
    'ui.loyalty.reward_delete_success': {
        'en': 'Reward deleted successfully',
        'uz': 'Mukofot muvaffaqiyatli o\'chirildi',
        'ru': 'Награда успешно удалена'
    },
    'ui.loyalty.reward_details': {
        'en': 'Reward Details',
        'uz': 'Mukofot tafsilotlari',
        'ru': 'Детали награды'
    },
    'ui.loyalty.reward_name': {
        'en': 'Reward Name',
        'uz': 'Mukofot nomi',
        'ru': 'Название награды'
    },
    'ui.loyalty.reward_update_success': {
        'en': 'Reward updated successfully',
        'uz': 'Mukofot muvaffaqiyatli yangilandi',
        'ru': 'Награда успешно обновлена'
    },
    'ui.loyalty.search_members': {
        'en': 'Search members',
        'uz': 'A\'zolarni qidirish',
        'ru': 'Поиск участников'
    },
    'ui.loyalty.search_programs': {
        'en': 'Search programs',
        'uz': 'Dasturlarni qidirish',
        'ru': 'Поиск программ'
    },
    'ui.loyalty.search_rewards': {
        'en': 'Search rewards',
        'uz': 'Mukofotlarni qidirish',
        'ru': 'Поиск наград'
    },
    'ui.loyalty.signup_bonus': {
        'en': 'Signup Bonus',
        'uz': 'Ro\'yxatdan o\'tish bonusi',
        'ru': 'Бонус за регистрацию'
    },
    'ui.loyalty.sort_order': {
        'en': 'Sort Order',
        'uz': 'Saralash tartibi',
        'ru': 'Порядок сортировки'
    },
    'ui.loyalty.status': {
        'en': 'Status',
        'uz': 'Holat',
        'ru': 'Статус'
    },
    'ui.loyalty.tab_programs': {
        'en': 'Programs',
        'uz': 'Dasturlar',
        'ru': 'Программы'
    },
    'ui.loyalty.tab_tiers': {
        'en': 'Tiers',
        'uz': 'Darajalar',
        'ru': 'Уровни'
    },
    'ui.loyalty.terms': {
        'en': 'Terms & Conditions',
        'uz': 'Shartlar va qoidalar',
        'ru': 'Условия и правила'
    },
    'ui.loyalty.tier': {
        'en': 'Tier',
        'uz': 'Daraja',
        'ru': 'Уровень'
    },
    'ui.loyalty.tier_color': {
        'en': 'Tier Color',
        'uz': 'Daraja rangi',
        'ru': 'Цвет уровня'
    },
    'ui.loyalty.tier_create_success': {
        'en': 'Tier created successfully',
        'uz': 'Daraja muvaffaqiyatli yaratildi',
        'ru': 'Уровень успешно создан'
    },
    'ui.loyalty.tier_delete_success': {
        'en': 'Tier deleted successfully',
        'uz': 'Daraja muvaffaqiyatli o\'chirildi',
        'ru': 'Уровень успешно удалён'
    },
    'ui.loyalty.tier_icon': {
        'en': 'Tier Icon',
        'uz': 'Daraja belgisi',
        'ru': 'Иконка уровня'
    },
    'ui.loyalty.tier_name': {
        'en': 'Tier Name',
        'uz': 'Daraja nomi',
        'ru': 'Название уровня'
    },
    'ui.loyalty.tier_update_success': {
        'en': 'Tier updated successfully',
        'uz': 'Daraja muvaffaqiyatli yangilandi',
        'ru': 'Уровень успешно обновлён'
    },
    'ui.loyalty.tiers': {
        'en': 'Tiers',
        'uz': 'Darajalar',
        'ru': 'Уровни'
    },
    'ui.loyalty.total_earned': {
        'en': 'Total Earned',
        'uz': 'Jami yig\'ilgan',
        'ru': 'Всего начислено'
    },
    'ui.loyalty.total_members': {
        'en': 'Total Members',
        'uz': 'Jami a\'zolar',
        'ru': 'Всего участников'
    },
    'ui.loyalty.total_programs': {
        'en': 'Total Programs',
        'uz': 'Jami dasturlar',
        'ru': 'Всего программ'
    },
    'ui.loyalty.total_rewards': {
        'en': 'Total Rewards',
        'uz': 'Jami mukofotlar',
        'ru': 'Всего наград'
    },
    'ui.loyalty.type': {
        'en': 'Type',
        'uz': 'Turi',
        'ru': 'Тип'
    },
    'ui.loyalty.update': {
        'en': 'Update',
        'uz': 'Yangilash',
        'ru': 'Обновить'
    },
    'ui.loyalty.update_program': {
        'en': 'Update Program',
        'uz': 'Dasturni yangilash',
        'ru': 'Обновить программу'
    },
    'ui.loyalty.update_success': {
        'en': 'Program updated successfully',
        'uz': 'Dastur muvaffaqiyatli yangilandi',
        'ru': 'Программа успешно обновлена'
    },
    'ui.loyalty.uzs_per_point': {
        'en': 'UZS per Point',
        'uz': 'Bir ball uchun UZS',
        'ru': 'UZS за балл'
    },
    'ui.loyalty.valid_from': {
        'en': 'Valid From',
        'uz': 'Amal boshlanishi',
        'ru': 'Действует с'
    },
    'ui.loyalty.valid_until': {
        'en': 'Valid Until',
        'uz': 'Amal qilish muddati',
        'ru': 'Действует до'
    },
    'ui.loyalty.voucher_code': {
        'en': 'Voucher Code',
        'uz': 'Vaucher kodi',
        'ru': 'Код ваучера'
    },
    'ui.orders.payment_business_account': {
        'en': 'Business Account',
        'uz': 'Hisob raqami orqali',
        'ru': 'Безналичный счёт'
    },
    'ui.orders.order_create_validation_title': {
        'en': 'Could not create order',
        'uz': 'Buyurtmani yaratib bo\'lmadi',
        'ru': 'Не удалось создать заказ'
    },
    'ui.corporate.account': {
        'en': 'Account Number',
        'uz': 'Hisob raqami',
        'ru': 'Номер счёта'
    },
    'ui.corporate.active_contracts': {
        'en': 'Active Contracts',
        'uz': 'Faol shartnomalar',
        'ru': 'Активные договоры'
    },
    'ui.corporate.add_price_override': {
        'en': 'Add Price Override',
        'uz': 'Maxsus narx qo\'shish',
        'ru': 'Добавить особую цену'
    },
    'ui.corporate.amount': {
        'en': 'Amount',
        'uz': 'Summa',
        'ru': 'Сумма'
    },
    'ui.corporate.allows_debt': {
        'en': 'Allow contract debt',
        'uz': 'Shartnoma qarziga ruxsat berish',
        'ru': 'Разрешить долг по договору'
    },
    'ui.corporate.available': {
        'en': 'Available',
        'uz': 'Mavjud',
        'ru': 'Доступно'
    },
    'ui.corporate.balance': {
        'en': 'Balance',
        'uz': 'Balans',
        'ru': 'Баланс'
    },
    'ui.corporate.bank': {
        'en': 'Bank',
        'uz': 'Bank',
        'ru': 'Банк'
    },
    'ui.corporate.bank_details': {
        'en': 'Bank Details',
        'uz': 'Bank ma\'lumotlari',
        'ru': 'Банковские реквизиты'
    },
    'ui.corporate.consumed': {
        'en': 'Consumed',
        'uz': 'Sarflangan',
        'ru': 'Списано'
    },
    'ui.corporate.contract_create_failed': {
        'en': 'Failed to create contract',
        'uz': 'Shartnomani yaratib bo\'lmadi',
        'ru': 'Не удалось создать договор'
    },
    'ui.corporate.contract_created': {
        'en': 'Corporate contract created',
        'uz': 'Korporativ shartnoma yaratildi',
        'ru': 'Корпоративный договор создан'
    },
    'ui.corporate.contract_details': {
        'en': 'Contract Details',
        'uz': 'Shartnoma tafsilotlari',
        'ru': 'Детали договора'
    },
    'ui.corporate.contract_name': {
        'en': 'Contract Name',
        'uz': 'Shartnoma nomi',
        'ru': 'Название договора'
    },
    'ui.corporate.contract_name_required': {
        'en': 'Contract name is required',
        'uz': 'Shartnoma nomi majburiy',
        'ru': 'Название договора обязательно'
    },
    'ui.corporate.contract_number': {
        'en': 'Contract Number',
        'uz': 'Shartnoma raqami',
        'ru': 'Номер договора'
    },
    'ui.corporate.contract_number_required': {
        'en': 'Contract number is required',
        'uz': 'Shartnoma raqami majburiy',
        'ru': 'Номер договора обязателен'
    },
    'ui.corporate.contract_update_failed': {
        'en': 'Failed to update contract',
        'uz': 'Shartnomani yangilab bo\'lmadi',
        'ru': 'Не удалось обновить договор'
    },
    'ui.corporate.contract_updated': {
        'en': 'Corporate contract updated',
        'uz': 'Korporativ shartnoma yangilandi',
        'ru': 'Корпоративный договор обновлён'
    },
    'ui.corporate.contracts': {
        'en': 'Corporate Contracts',
        'uz': 'Korporativ shartnomalar',
        'ru': 'Корпоративные договоры'
    },
    'ui.corporate.contracts_with_debt': {
        'en': 'Contracts With Debt',
        'uz': 'Qarzdor shartnomalar',
        'ru': 'Договоры с задолженностью'
    },
    'ui.corporate.currency': {
        'en': 'Currency',
        'uz': 'Valyuta',
        'ru': 'Валюта'
    },
    'ui.corporate.customer': {
        'en': 'Customer',
        'uz': 'Mijoz',
        'ru': 'Клиент'
    },
    'ui.corporate.customer_required': {
        'en': 'Customer is required',
        'uz': 'Mijozni tanlash majburiy',
        'ru': 'Клиент обязателен'
    },
    'ui.corporate.debt': {
        'en': 'Debt',
        'uz': 'Qarz',
        'ru': 'Долг'
    },
    'ui.corporate.debt_allowed': {
        'en': 'Debt allowed',
        'uz': 'Qarzga ruxsat berilgan',
        'ru': 'Долг разрешён'
    },
    'ui.corporate.debt_disallowed': {
        'en': 'No debt',
        'uz': 'Qarzga ruxsat yo\'q',
        'ru': 'Без долга'
    },
    'ui.corporate.debt_policy': {
        'en': 'Debt Policy',
        'uz': 'Qarz siyosati',
        'ru': 'Политика долга'
    },
    'ui.corporate.edit_contract': {
        'en': 'Edit Contract',
        'uz': 'Shartnomani tahrirlash',
        'ru': 'Редактировать договор'
    },
    'ui.corporate.end_date': {
        'en': 'End Date',
        'uz': 'Tugash sanasi',
        'ru': 'Дата окончания'
    },
    'ui.corporate.event_date': {
        'en': 'Created',
        'uz': 'Yaratilgan',
        'ru': 'Создано'
    },
    'ui.corporate.event_type': {
        'en': 'Event',
        'uz': 'Hodisa',
        'ru': 'Событие'
    },
    'ui.corporate.inn': {
        'en': 'INN',
        'uz': 'STIR',
        'ru': 'ИНН'
    },
    'ui.corporate.is_active': {
        'en': 'Contract is active',
        'uz': 'Shartnoma faol',
        'ru': 'Договор активен'
    },
    'ui.corporate.last_topup': {
        'en': 'Last Topup',
        'uz': 'Oxirgi to\'ldirish',
        'ru': 'Последнее пополнение'
    },
    'ui.corporate.ledger': {
        'en': 'Ledger',
        'uz': 'Harakatlar jurnali',
        'ru': 'Журнал операций'
    },
    'ui.corporate.manage_prices': {
        'en': 'Manage Prices',
        'uz': 'Narxlarni boshqarish',
        'ru': 'Управление ценами'
    },
    'ui.corporate.mfo': {
        'en': 'MFO',
        'uz': 'MFO',
        'ru': 'МФО'
    },
    'ui.corporate.new_contract': {
        'en': 'New Contract',
        'uz': 'Yangi shartnoma',
        'ru': 'Новый договор'
    },
    'ui.corporate.no_balance_products': {
        'en': 'No prepayment-eligible products configured yet.',
        'uz': 'Oldindan to\'lovga mos mahsulotlar hali sozlanmagan.',
        'ru': 'Товары, доступные для предоплаты, пока не настроены.'
    },
    'ui.corporate.no_ledger_entries': {
        'en': 'No ledger entries yet.',
        'uz': 'Harakatlar hali yo\'q.',
        'ru': 'Записей в журнале пока нет.'
    },
    'ui.corporate.no_overlap_conflicts': {
        'en': 'No overlapping active contract coverage found for the current selection.',
        'uz': 'Joriy tanlov uchun faol shartnomalar kesishuvi topilmadi.',
        'ru': 'Для текущего выбора пересечений активных договоров не найдено.'
    },
    'ui.corporate.no_price_overrides': {
        'en': 'No contract-specific price overrides yet.',
        'uz': 'Shartnomaga xos narxlar hali kiritilmagan.',
        'ru': 'Индивидуальные цены по договору пока не настроены.'
    },
    'ui.corporate.notes': {
        'en': 'Notes',
        'uz': 'Izohlar',
        'ru': 'Примечания'
    },
    'ui.corporate.open_ended': {
        'en': 'Open ended',
        'uz': 'Ochiq muddatli',
        'ru': 'Без даты окончания'
    },
    'ui.corporate.overlap_conflicts_found': {
        'en': 'Overlap conflicts found',
        'uz': 'Kesishuvlar topildi',
        'ru': 'Найдены пересечения'
    },
    'ui.corporate.overlap_preview_failed': {
        'en': 'Failed to preview overlaps',
        'uz': 'Kesishuvlarni oldindan ko\'rib bo\'lmadi',
        'ru': 'Не удалось показать пересечения'
    },
    'ui.corporate.period': {
        'en': 'Period',
        'uz': 'Davr',
        'ru': 'Период'
    },
    'ui.corporate.phone': {
        'en': 'Phone',
        'uz': 'Telefon',
        'ru': 'Телефон'
    },
    'ui.corporate.prepaid': {
        'en': 'Prepaid',
        'uz': 'Oldindan to\'langan',
        'ru': 'Предоплачено'
    },
    'ui.corporate.prepayment_eligible': {
        'en': 'Prepayment eligible',
        'uz': 'Oldindan to\'lovga mos',
        'ru': 'Доступно для предоплаты'
    },
    'ui.corporate.prepayment_scope': {
        'en': 'Prepayment Scope',
        'uz': 'Oldindan to\'lov qamrovi',
        'ru': 'Покрытие предоплаты'
    },
    'ui.corporate.preview_overlaps': {
        'en': 'Preview Overlaps',
        'uz': 'Kesishuvlarni tekshirish',
        'ru': 'Проверить пересечения'
    },
    'ui.corporate.price_override': {
        'en': 'Price Override',
        'uz': 'Maxsus narx',
        'ru': 'Особая цена'
    },
    'ui.corporate.price_overrides': {
        'en': 'Price Overrides',
        'uz': 'Maxsus narxlar',
        'ru': 'Особые цены'
    },
    'ui.corporate.prices_update_failed': {
        'en': 'Failed to update prices',
        'uz': 'Narxlarni yangilab bo\'lmadi',
        'ru': 'Не удалось обновить цены'
    },
    'ui.corporate.prices_updated': {
        'en': 'Contract prices updated',
        'uz': 'Shartnoma narxlari yangilandi',
        'ru': 'Цены по договору обновлены'
    },
    'ui.corporate.product': {
        'en': 'Product',
        'uz': 'Mahsulot',
        'ru': 'Товар'
    },
    'ui.corporate.product_required': {
        'en': 'Product is required',
        'uz': 'Mahsulotni tanlash majburiy',
        'ru': 'Товар обязателен'
    },
    'ui.corporate.products_in_debt': {
        'en': 'Products In Debt',
        'uz': 'Qarzdagi mahsulotlar',
        'ru': 'Товары в долге'
    },
    'ui.corporate.products_reserved': {
        'en': 'Reserved Products',
        'uz': 'Rezervdagi mahsulotlar',
        'ru': 'Зарезервированные товары'
    },
    'ui.corporate.reference': {
        'en': 'Transfer Reference',
        'uz': 'To\'lov havolasi',
        'ru': 'Референс перевода'
    },
    'ui.corporate.reserved': {
        'en': 'Reserved',
        'uz': 'Rezerv qilingan',
        'ru': 'Зарезервировано'
    },
    'ui.corporate.search_contracts': {
        'en': 'Search by contract, customer, phone',
        'uz': 'Shartnoma, mijoz yoki telefon bo\'yicha qidirish',
        'ru': 'Поиск по договору, клиенту или телефону'
    },
    'ui.corporate.select_contract_hint': {
        'en': 'Select a contract to view pricing, balances, and ledger entries.',
        'uz': 'Narxlar, balans va jurnal yozuvlarini ko\'rish uchun shartnomani tanlang.',
        'ru': 'Выберите договор, чтобы посмотреть цены, баланс и записи журнала.'
    },
    'ui.corporate.select_customer': {
        'en': 'Select customer',
        'uz': 'Mijozni tanlang',
        'ru': 'Выберите клиента'
    },
    'ui.corporate.select_product': {
        'en': 'Select product',
        'uz': 'Mahsulotni tanlang',
        'ru': 'Выберите товар'
    },
    'ui.corporate.start_date': {
        'en': 'Start Date',
        'uz': 'Boshlanish sanasi',
        'ru': 'Дата начала'
    },
    'ui.corporate.status': {
        'en': 'Status',
        'uz': 'Holat',
        'ru': 'Статус'
    },
    'ui.corporate.topup': {
        'en': 'Top Up',
        'uz': 'To\'ldirish',
        'ru': 'Пополнить'
    },
    'ui.corporate.topup_failed': {
        'en': 'Failed to apply topup',
        'uz': 'To\'ldirishni qo\'llab bo\'lmadi',
        'ru': 'Не удалось применить пополнение'
    },
    'ui.corporate.topup_success': {
        'en': 'Prepayment topup applied',
        'uz': 'Oldindan to\'lov to\'ldirildi',
        'ru': 'Предоплата пополнена'
    },
    'ui.corporate.total_contracts': {
        'en': 'Total Contracts',
        'uz': 'Jami shartnomalar',
        'ru': 'Всего договоров'
    },
    'ui.corporate.tracked_products': {
        'en': 'Tracked Products',
        'uz': 'Hisobga olinadigan mahsulotlar',
        'ru': 'Отслеживаемые товары'
    },
    'ui.corporate.unit_price': {
        'en': 'Unit Price',
        'uz': 'Birlik narxi',
        'ru': 'Цена за единицу'
    },
    'ui.corporate.unit_price_required': {
        'en': 'Unit price is required',
        'uz': 'Birlik narxi majburiy',
        'ru': 'Цена за единицу обязательна'
    },
    'ui.corporate.units': {
        'en': 'Units',
        'uz': 'Dona',
        'ru': 'Единицы'
    },
    'ui.corporate.units_required': {
        'en': 'Units are required',
        'uz': 'Dona soni majburiy',
        'ru': 'Количество обязательно'
    },
    'ui.corporate.unknown_customer': {
        'en': 'Unknown',
        'uz': 'Noma\'lum',
        'ru': 'Неизвестно'
    },
    'ui.users.user_type': {
        'en': 'User Type',
        'uz': 'Foydalanuvchi turi',
        'ru': 'Тип пользователя'
    },
    'ui.users.select_user_type': {
        'en': 'Select user type',
        'uz': 'Foydalanuvchi turini tanlang',
        'ru': 'Выберите тип пользователя'
    },
    'ui.users.user_type_entity': {
        'en': 'Entity',
        'uz': 'Yuridik shaxs',
        'ru': 'Юридическое лицо'
    },
    'ui.users.user_type_individual': {
        'en': 'Individual',
        'uz': 'Jismoniy shaxs',
        'ru': 'Физическое лицо'
    },
    'ui.users.user_type_staff': {
        'en': 'Staff',
        'uz': 'Xodim',
        'ru': 'Сотрудник'
    },
    'ui.users.company_name': {
        'en': 'Company Name',
        'uz': 'Kompaniya nomi',
        'ru': 'Название компании'
    },
    'ui.users.company_name_required': {
        'en': 'Company name is required for entity users',
        'uz': 'Yuridik shaxs foydalanuvchilari uchun kompaniya nomi majburiy',
        'ru': 'Для юридических лиц название компании обязательно'
    },
    'ui.users.enter_company_name_optional': {
        'en': 'Enter company name (optional)',
        'uz': 'Kompaniya nomini kiriting (ixtiyoriy)',
        'ru': 'Введите название компании (необязательно)'
    },
    'ui.users.tax_id': {
        'en': 'Tax ID',
        'uz': 'STIR',
        'ru': 'ИНН'
    },
    'ui.users.invalid_tax_id': {
        'en': 'Use 5-20 uppercase letters, digits, or dashes',
        'uz': '5-20 ta katta harf, raqam yoki chiziqchadan foydalaning',
        'ru': 'Используйте 5-20 заглавных букв, цифр или дефисов'
    },
    'ui.users.enter_tax_id_optional': {
        'en': 'Enter tax ID (optional)',
        'uz': 'STIRni kiriting (ixtiyoriy)',
        'ru': 'Введите ИНН (необязательно)'
    },
    'ui.users.entity_client': {
        'en': 'Entity client',
        'uz': 'Yuridik mijoz',
        'ru': 'Юридический клиент'
    },
    'ui.users.entity_client_note': {
        'en': 'Users created with user type "Entity" become selectable in the Corporate Contracts screen.',
        'uz': '"Yuridik shaxs" turi bilan yaratilgan foydalanuvchilar Corporate Contracts sahifasida tanlanadi.',
        'ru': 'Пользователи с типом "Юридическое лицо" становятся доступными для выбора на экране Corporate Contracts.'
    },
    'ui.users.edit_user': {
        'en': 'Edit User',
        'uz': 'Foydalanuvchini tahrirlash',
        'ru': 'Редактировать пользователя'
    },
    'ui.users.user_updated_success': {
        'en': 'User updated successfully',
        'uz': 'Foydalanuvchi muvaffaqiyatli yangilandi',
        'ru': 'Пользователь успешно обновлён'
    },
    'ui.users.user_update_failed': {
        'en': 'Failed to update user',
        'uz': 'Foydalanuvchini yangilab bo\'lmadi',
        'ru': 'Не удалось обновить пользователя'
    },
    'ui.users.admin_created_user_note': {
        'en': 'Users created here are for phone orders only. They cannot login to the customer portal.',
        'uz': 'Bu yerda yaratilgan foydalanuvchilar faqat telefon buyurtmalari uchun. Ular mijoz portaliga kira olmaydi.',
        'ru': 'Пользователи, созданные здесь, предназначены только для телефонных заказов. Они не могут войти в клиентский портал.'
    },
    'api.notifications.success.deleted': {
        'en': 'Notification deleted successfully',
        'uz': 'Bildirishnoma muvaffaqiyatli o\'chirildi',
        'ru': 'Уведомление успешно удалено'
    },
    'api.notifications.success.marked_read': {
        'en': 'Notification marked as read',
        'uz': 'Bildirishnoma o\'qilgan deb belgilandi',
        'ru': 'Уведомление отмечено как прочитанное'
    },
    'api.notifications.success.push_registered': {
        'en': 'Push token registered successfully',
        'uz': 'Push token muvaffaqiyatli ro\'yxatdan o\'tkazildi',
        'ru': 'Push-токен успешно зарегистрирован'
    },
    'api.notifications.success.push_unregistered': {
        'en': 'Push token unregistered successfully',
        'uz': 'Push token muvaffaqiyatli o\'chirildi',
        'ru': 'Push-токен успешно удален'
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
    'error.validation.card_number_required': {
        'en': 'Card number is required',
        'uz': 'Karta raqami majburiy',
        'ru': 'Номер карты обязателен'
    },
    'error.validation.cardholder_name_required': {
        'en': 'Cardholder name is required',
        'uz': 'Karta egasi ismi majburiy',
        'ru': 'Имя держателя карты обязательно'
    },
    'error.validation.invalid_card_expiry': {
        'en': 'Invalid card expiry date',
        'uz': 'Karta amal qilish muddati noto\'g\'ri',
        'ru': 'Неверный срок действия карты'
    },
    'error.validation.invalid_boolean': {
        'en': 'Invalid boolean value',
        'uz': 'Boolean qiymati noto\'g\'ri',
        'ru': 'Недопустимое булево значение'
    },
    'error.validation.invalid_expiry_format_mm_yy': {
        'en': 'Invalid expiry format. Use MM/YY.',
        'uz': 'Amal qilish muddati formati noto\'g\'ri. MM/YY dan foydalaning.',
        'ru': 'Неверный формат срока действия. Используйте MM/YY.'
    },
    'error.validation.invalid_expiry_format_mm_yy_or_mmyy': {
        'en': 'Invalid expiry format. Use MM/YY or MMYY.',
        'uz': 'Amal qilish muddati formati noto\'g\'ri. MM/YY yoki MMYY dan foydalaning.',
        'ru': 'Неверный формат срока действия. Используйте MM/YY или MMYY.'
    },
    'error.validation.card_token_required': {
        'en': 'Card token is required',
        'uz': 'Karta tokeni majburiy',
        'ru': 'Требуется токен карты'
    },
    'error.validation.order_id_required': {
        'en': 'Order ID is required',
        'uz': 'Buyurtma ID majburiy',
        'ru': 'Требуется ID заказа'
    },
    'error.validation.verification_code_required': {
        'en': 'Verification code is required',
        'uz': 'Tasdiqlash kodi majburiy',
        'ru': 'Код подтверждения обязателен'
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
    'telegram.payment.success_message': {
        'en': '✅ Payment successful!\n\nOrder No: #{order_number}\nAmount: {amount} {currency}\n\nThank you for your purchase!',
        'uz': '✅ To\'lov muvaffaqiyatli amalga oshirildi!\n\nBuyurtma raqami: #{order_number}\nSumma: {amount} {currency}\n\nXaridingiz uchun rahmat!',
        'ru': '✅ Оплата прошла успешно!\n\nНомер заказа: #{order_number}\nСумма: {amount} {currency}\n\nСпасибо за покупку!'
    },
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
        'en': 'Order #{order_number}\nAmount: {amount} UZS\n\nPlease pay using the button below:',
        'uz': 'Buyurtma №{order_number}\nMiqdor: {amount} UZS\n\nIltimos, quyidagi tugma orqali to\'lovni amalga oshiring:',
        'ru': 'Заказ №{order_number}\nСумма: {amount} UZS\n\nПожалуйста, оплатите с помощью кнопки ниже:'
    },
    'telegram.payment.pay_btn': {
        'en': '💳 Pay',
        'uz': '💳 To\'lash',
        'ru': '💳 Оплатить'
    },
    'telegram.payment.create_link_failed_with_error': {
        'en': 'Failed to create payment link: {error}',
        'uz': 'To\'lov havolasini yaratib bo\'lmadi: {error}',
        'ru': 'Не удалось создать ссылку для оплаты: {error}'
    },
    'telegram.payment.invalid_link_received': {
        'en': 'Invalid payment link received.',
        'uz': 'Noto\'g\'ri to\'lov havolasi olindi.',
        'ru': 'Получена некорректная ссылка для оплаты.'
    },
    'telegram.payment.failed_message': {
        'en': 'Failed to create payment. Please try again.',
        'uz': 'To\'lovni yaratib bo\'lmadi. Iltimos, qayta urinib ko\'ring.',
        'ru': 'Не удалось создать оплату. Пожалуйста, попробуйте снова.'
    },
    'telegram.payment.error_order_not_found': {
        'en': 'Order not found. Please create a new order.',
        'uz': 'Buyurtma topilmadi. Iltimos, yangi buyurtma yarating.',
        'ru': 'Заказ не найден. Пожалуйста, создайте новый заказ.'
    },
    'telegram.payment.error_already_paid': {
        'en': 'This order has already been paid.',
        'uz': 'Bu buyurtma allaqachon to\'langan.',
        'ru': 'Этот заказ уже оплачен.'
    },
    'telegram.payment.cancelled_message': {
        'en': 'You cancelled the payment. Your order is still pending.\n\nWould you like to try again?',
        'uz': 'Siz to\'lovni bekor qildingiz. Buyurtmangiz hali ham kutilmoqda.\n\nYana urinib ko\'rishni xohlaysizmi?',
        'ru': 'Вы отменили оплату. Ваш заказ все еще ожидает.\n\nХотите попробовать снова?'
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
    'telegram.order.number': {
        'en': 'Order #{0}',
        'uz': 'Buyurtma #{0}',
        'ru': 'Заказ #{0}'
    },
    'telegram.order.total': {
        'en': 'Total: {0} UZS',
        'uz': 'Jami: {0} UZS',
        'ru': 'Итого: {0} UZS'
    },

    # ============================================================================
    # Telegram Orders Flow Texts (telegram.orders.*)
    # ============================================================================
    'telegram.orders.no_orders': {
        'en': 'You have no orders yet.',
        'uz': 'Sizda hali buyurtmalar yo\'q.',
        'ru': 'У вас пока нет заказов.'
    },
    'telegram.orders.your_orders': {
        'en': 'Your orders ({count})',
        'uz': 'Buyurtmalaringiz ({count})',
        'ru': 'Ваши заказы ({count})'
    },
    'telegram.orders.items_header': {
        'en': 'Items',
        'uz': 'Mahsulotlar',
        'ru': 'Товары'
    },
    'telegram.orders.delivery_info': {
        'en': 'Delivery info',
        'uz': 'Yetkazib berish ma\'lumotlari',
        'ru': 'Информация о доставке'
    },
    'telegram.orders.cancel_confirm': {
        'en': 'Are you sure you want to cancel this order?',
        'uz': 'Ushbu buyurtmani bekor qilmoqchimisiz?',
        'ru': 'Вы уверены, что хотите отменить этот заказ?'
    },
    'telegram.orders.cancel_success': {
        'en': 'Order cancelled successfully.',
        'uz': 'Buyurtma muvaffaqiyatli bekor qilindi.',
        'ru': 'Заказ успешно отменен.'
    },
    'telegram.orders.tracking_title': {
        'en': 'Order Tracking',
        'uz': 'Buyurtmani kuzatish',
        'ru': 'Отслеживание заказа'
    },
    'telegram.orders.timeline': {
        'en': 'Timeline',
        'uz': 'Jarayon',
        'ru': 'Хронология'
    },
    'telegram.orders.current_status': {
        'en': 'Current',
        'uz': 'Joriy',
        'ru': 'Текущий'
    },
    'telegram.orders.status_created': {
        'en': 'Order Placed',
        'uz': 'Buyurtma qabul qilindi',
        'ru': 'Заказ создан'
    },
    'telegram.orders.status_pending': {
        'en': 'Pending Confirmation',
        'uz': 'Tasdiqlanish kutilmoqda',
        'ru': 'Ожидает подтверждения'
    },
    'telegram.orders.status_confirmed': {
        'en': 'Order Confirmed',
        'uz': 'Buyurtma tasdiqlandi',
        'ru': 'Заказ подтвержден'
    },
    'telegram.orders.status_preparing': {
        'en': 'Being Prepared',
        'uz': 'Tayyorlanmoqda',
        'ru': 'Готовится'
    },
    'telegram.orders.status_out_for_delivery': {
        'en': 'Out for Delivery',
        'uz': 'Yetkazib berish yo\'lida',
        'ru': 'Передан в доставку'
    },
    'telegram.orders.status_delivered': {
        'en': 'Delivered',
        'uz': 'Yetkazib berildi',
        'ru': 'Доставлено'
    },
    'telegram.orders.status_cancelled': {
        'en': 'Cancelled',
        'uz': 'Bekor qilindi',
        'ru': 'Отменен'
    },
    'telegram.orders.status_returned': {
        'en': 'Returned',
        'uz': 'Qaytarildi',
        'ru': 'Возвращен'
    },
    'telegram.orders.estimated_remaining': {
        'en': 'Estimated remaining',
        'uz': 'Taxminiy qolgan vaqt',
        'ru': 'Примерное оставшееся время'
    },
    'telegram.orders.driver': {
        'en': 'Driver',
        'uz': 'Kuryer',
        'ru': 'Курьер'
    },
    'telegram.orders.no_address_prompt': {
        'en': 'Please add a delivery address to continue.',
        'uz': 'Davom etish uchun yetkazib berish manzilini qo\'shing.',
        'ru': 'Пожалуйста, добавьте адрес доставки для продолжения.'
    },
    'telegram.orders.select_address': {
        'en': 'Select delivery address:',
        'uz': 'Yetkazib berish manzilini tanlang:',
        'ru': 'Выберите адрес доставки:'
    },
    'telegram.orders.select_payment': {
        'en': 'Select payment method:',
        'uz': 'To\'lov usulini tanlang:',
        'ru': 'Выберите способ оплаты:'
    },
    'telegram.orders.missing_info': {
        'en': 'Missing order information. Please try again.',
        'uz': 'Buyurtma ma\'lumotlari yetishmayapti. Iltimos, qayta urinib ko\'ring.',
        'ru': 'Недостаточно данных для заказа. Пожалуйста, попробуйте снова.'
    },
    'telegram.orders.cart_empty': {
        'en': 'Your cart is empty.',
        'uz': 'Savatingiz bo\'sh.',
        'ru': 'Ваша корзина пуста.'
    },
    'telegram.orders.placed_success': {
        'en': '✅ Order placed successfully!',
        'uz': '✅ Buyurtma muvaffaqiyatli joylashtirildi!',
        'ru': '✅ Заказ успешно оформлен!'
    },
    'telegram.orders.cash_note': {
        'en': 'Please have the exact amount ready for the driver.',
        'uz': 'Iltimos, kuryer uchun aniq summani tayyorlab qo\'ying.',
        'ru': 'Пожалуйста, подготовьте точную сумму для курьера.'
    },
    'telegram.orders.confirmation_title': {
        'en': 'Order Confirmation',
        'uz': 'Buyurtmani tasdiqlash',
        'ru': 'Подтверждение заказа'
    },
    'telegram.orders.payment_info': {
        'en': 'Payment method',
        'uz': 'To\'lov usuli',
        'ru': 'Способ оплаты'
    },
    'telegram.orders.delivery_fee': {
        'en': 'Delivery fee: {amount} UZS',
        'uz': 'Yetkazib berish narxi: {amount} UZS',
        'ru': 'Стоимость доставки: {amount} UZS'
    },
    'telegram.orders.grand_total': {
        'en': 'Grand total: {amount} UZS',
        'uz': 'Umumiy summa: {amount} UZS',
        'ru': 'Итого к оплате: {amount} UZS'
    },
    'telegram.orders.selected_address': {
        'en': 'Selected address #{address_id}',
        'uz': 'Tanlangan manzil #{address_id}',
        'ru': 'Выбранный адрес #{address_id}'
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
    'telegram.subscription.total_deliveries': {
        'en': 'Total Deliveries',
        'uz': 'Jami yetkazib berishlar',
        'ru': 'Всего доставок'
    },
    'telegram.subscription.total_spent': {
        'en': 'Total Spent',
        'uz': 'Jami sarflangan',
        'ru': 'Всего потрачено'
    },
    'telegram.subscription.total_savings': {
        'en': 'Total Savings',
        'uz': 'Jami tejalgan',
        'ru': 'Всего сэкономлено'
    },

    # ============================================================================
    # Telegram Help/Support/Admin Messages (telegram.help/support/admin.*)
    # ============================================================================
    'telegram.help.command_hint': {
        'en': '🆘 Help: Use /menu to see available options.',
        'uz': '🆘 Yordam: Mavjud bo\'limlarni ko\'rish uchun /menu dan foydalaning.',
        'ru': '🆘 Помощь: Используйте /menu, чтобы увидеть доступные разделы.'
    },
    'telegram.support.menu_coming_soon': {
        'en': '🆘 Support menu coming soon!',
        'uz': '🆘 Yordam menyusi tez orada ishga tushadi!',
        'ru': '🆘 Меню поддержки скоро будет доступно!'
    },
    'telegram.support.faq_coming_soon': {
        'en': '❓ FAQ coming soon!',
        'uz': '❓ Tez-tez so\'raladigan savollar tez orada qo\'shiladi!',
        'ru': '❓ Раздел FAQ скоро будет доступен!'
    },
    'telegram.support.contact_message': {
        'en': '📞 Contact support: @bluestreamwater',
        'uz': '📞 Yordam bilan bog\'lanish: @bluestreamwater',
        'ru': '📞 Связаться с поддержкой: @bluestreamwater'
    },
    'telegram.support.message_received': {
        'en': 'Support message received: {message}\nOur team will get back to you soon!',
        'uz': 'Yordam xabaringiz qabul qilindi: {message}\nJamoamiz tez orada siz bilan bog\'lanadi!',
        'ru': 'Сообщение в поддержку получено: {message}\nНаша команда скоро с вами свяжется!'
    },
    'telegram.admin.panel_coming_soon': {
        'en': '🔧 Admin panel functionality coming soon!',
        'uz': '🔧 Admin panel funksiyalari tez orada qo\'shiladi!',
        'ru': '🔧 Функции админ-панели скоро будут доступны!'
    },
    'telegram.admin.orders_panel_coming_soon': {
        'en': '📊 Admin orders panel coming soon!',
        'uz': '📊 Admin buyurtmalar paneli tez orada qo\'shiladi!',
        'ru': '📊 Панель заказов для админа скоро будет доступна!'
    },
    'telegram.admin.analytics_coming_soon': {
        'en': '📈 Admin analytics coming soon!',
        'uz': '📈 Admin analitika tez orada qo\'shiladi!',
        'ru': '📈 Аналитика для админа скоро будет доступна!'
    },
    'telegram.bot.command.start_desc': {
        'en': 'Start the bot and show main menu',
        'uz': 'Botni ishga tushirish va asosiy menyuni ko\'rsatish',
        'ru': 'Запустить бота и открыть главное меню'
    },
    'telegram.bot.command.menu_desc': {
        'en': 'Show main menu',
        'uz': 'Asosiy menyuni ko\'rsatish',
        'ru': 'Показать главное меню'
    },
    'telegram.bot.command.help_desc': {
        'en': 'Get help and support',
        'uz': 'Yordam va qo\'llab-quvvatlash',
        'ru': 'Получить помощь и поддержку'
    },
    'telegram.bot.command.language_desc': {
        'en': 'Change language settings',
        'uz': 'Til sozlamalarini o\'zgartirish',
        'ru': 'Изменить настройки языка'
    },
    'telegram.registration.start_command_prompt': {
        'en': 'Please start with /start command.',
        'uz': 'Iltimos, /start buyrug\'i bilan boshlang.',
        'ru': 'Пожалуйста, начните с команды /start.'
    },
    'telegram.bot.rate_limit_exceeded': {
        'en': '⏳ Please slow down. Try again in a moment.',
        'uz': '⏳ Iltimos, biroz sekinroq yuboring. Bir ozdan keyin yana urinib ko\'ring.',
        'ru': '⏳ Пожалуйста, отправляйте сообщения немного реже. Попробуйте снова через мгновение.'
    },
    'telegram.bot.otp.invalid_format': {
        'en': '❌ Invalid code format. Please enter the 6-digit verification code:',
        'uz': '❌ Kod formati noto\'g\'ri. Iltimos, 6 xonali tasdiqlash kodini kiriting:',
        'ru': '❌ Неверный формат кода. Пожалуйста, введите 6-значный код подтверждения:'
    },
    'telegram.bot.otp.auth_error': {
        'en': '❌ Authentication error. Please try again later.',
        'uz': '❌ Autentifikatsiya xatoligi. Iltimos, keyinroq qayta urinib ko\'ring.',
        'ru': '❌ Ошибка аутентификации. Пожалуйста, попробуйте позже.'
    },
    'telegram.bot.otp.success_message': {
        'en': '✅ *Phone verified successfully!*\n\nYour phone number has been verified. You can now place orders and receive notifications.',
        'uz': '✅ *Telefon muvaffaqiyatli tasdiqlandi!*\n\nTelefon raqamingiz tasdiqlandi. Endi buyurtma berishingiz va bildirishnomalarni olishingiz mumkin.',
        'ru': '✅ *Телефон успешно подтвержден!*\n\nВаш номер телефона подтвержден. Теперь вы можете оформлять заказы и получать уведомления.'
    },
    'telegram.bot.otp.failed_with_reason': {
        'en': '❌ Verification failed: {error}\n\nPlease enter the correct code or click /cancel to stop:',
        'uz': '❌ Tasdiqlash amalga oshmadi: {error}\n\nIltimos, to\'g\'ri kodni kiriting yoki to\'xtatish uchun /cancel ni bosing:',
        'ru': '❌ Проверка не удалась: {error}\n\nПожалуйста, введите правильный код или нажмите /cancel для отмены:'
    },
    'telegram.bot.otp.failed_generic': {
        'en': '❌ Verification failed. Please try again later.',
        'uz': '❌ Tasdiqlash amalga oshmadi. Iltimos, keyinroq qayta urinib ko\'ring.',
        'ru': '❌ Проверка не удалась. Пожалуйста, попробуйте позже.'
    },
    'telegram.bot.location.received_prompt': {
        'en': '📍 Location received! Please provide a title for this address:',
        'uz': '📍 Joylashuv qabul qilindi! Iltimos, ushbu manzilga nom kiriting:',
        'ru': '📍 Местоположение получено! Пожалуйста, укажите название для этого адреса:'
    },
    'telegram.bot.location.shared_general': {
        'en': '📍 Thanks for sharing your location!\n\nLat: {latitude}, Lng: {longitude}\n\nIf you want to add this as a delivery address, please go to:\nProfile → Addresses → Add Address',
        'uz': '📍 Joylashuvingizni ulashganingiz uchun rahmat!\n\nKenglik: {latitude}, Uzunlik: {longitude}\n\nBuni yetkazib berish manzili sifatida qo\'shmoqchi bo\'lsangiz, quyidagiga o\'ting:\nProfil → Manzillar → Manzil qo\'shish',
        'ru': '📍 Спасибо, что поделились местоположением!\n\nШирота: {latitude}, Долгота: {longitude}\n\nЕсли хотите добавить его как адрес доставки, перейдите:\nПрофиль → Адреса → Добавить адрес'
    },
    'telegram.bot.voice.not_supported': {
        'en': '🎙️ Voice message received! Currently, I can only respond to text messages. Please type your message or use the menu buttons.',
        'uz': '🎙️ Ovozli xabar qabul qilindi! Hozircha men faqat matnli xabarlarga javob bera olaman. Iltimos, xabaringizni yozing yoki menyu tugmalaridan foydalaning.',
        'ru': '🎙️ Голосовое сообщение получено! Сейчас я могу отвечать только на текстовые сообщения. Пожалуйста, напишите сообщение или используйте кнопки меню.'
    },

    # ============================================================================
    # Telegram Product Messages (telegram.products.*)
    # ============================================================================
    'telegram.products.category_empty': {
        'en': 'No products found in this category.',
        'uz': 'Ushbu toifada mahsulotlar topilmadi.',
        'ru': 'В этой категории товары не найдены.'
    },
    'telegram.products.invalid_action': {
        'en': 'Invalid action.',
        'uz': 'Noto\'g\'ri amal.',
        'ru': 'Недопустимое действие.'
    },
    'telegram.products.no_results_for_search': {
        'en': 'No products found for "{search_term}".',
        'uz': '"{search_term}" bo\'yicha mahsulot topilmadi.',
        'ru': 'По запросу "{search_term}" товары не найдены.'
    },
    'telegram.products.no_products_found': {
        'en': 'No products found.',
        'uz': 'Mahsulotlar topilmadi.',
        'ru': 'Товары не найдены.'
    },
    'telegram.products.in_stock': {
        'en': 'In stock',
        'uz': 'Mavjud',
        'ru': 'В наличии'
    },
    'telegram.products.out_of_stock': {
        'en': 'Out of stock',
        'uz': 'Tugagan',
        'ru': 'Нет в наличии'
    },
    'telegram.products.volume_label': {
        'en': 'Volume',
        'uz': 'Hajm',
        'ru': 'Объем'
    },
    'telegram.products.stock_label': {
        'en': 'Stock',
        'uz': 'Qoldiq',
        'ru': 'Остаток'
    },
    'telegram.products.search_results_for': {
        'en': '🔍 Search results for "{search_term}":',
        'uz': '🔍 "{search_term}" bo\'yicha qidiruv natijalari:',
        'ru': '🔍 Результаты поиска по запросу "{search_term}":'
    },
    'telegram.products.category_label': {
        'en': 'Category',
        'uz': 'Toifa',
        'ru': 'Категория'
    },
    'telegram.products.cart_cleared': {
        'en': '🗑️ Cart cleared!',
        'uz': '🗑️ Savat tozalandi!',
        'ru': '🗑️ Корзина очищена!'
    },

    # ============================================================================
    # Telegram Profile/Phone Core Messages (telegram.profile/phone/common.*)
    # ============================================================================
    'telegram.common.not_set': {
        'en': 'Not set',
        'uz': 'Belgilanmagan',
        'ru': 'Не указано'
    },
    'telegram.common.unknown': {
        'en': 'Unknown',
        'uz': 'Noma\'lum',
        'ru': 'Неизвестно'
    },
    'telegram.common.address': {
        'en': 'Address',
        'uz': 'Manzil',
        'ru': 'Адрес'
    },
    'telegram.phone.change_number': {
        'en': '📝 Change Phone Number',
        'uz': '📝 Telefon raqamini o\'zgartirish',
        'ru': '📝 Изменить номер телефона'
    },
    'telegram.phone.share_own_phone': {
        'en': '❌ Please share your own phone number.',
        'uz': '❌ Iltimos, o\'zingizning telefon raqamingizni ulashing.',
        'ru': '❌ Пожалуйста, поделитесь своим номером телефона.'
    },
    'telegram.phone.phone_accepted': {
        'en': '✅ Phone number accepted!',
        'uz': '✅ Telefon raqami qabul qilindi!',
        'ru': '✅ Номер телефона принят!'
    },
    'telegram.enter_name': {
        'en': '👤 Please enter your full name:',
        'uz': '👤 Iltimos, to\'liq ismingizni kiriting:',
        'ru': '👤 Пожалуйста, введите ваше полное имя:'
    },
    'telegram.name.too_short': {
        'en': '❌ Name is too short. Please enter at least 2 characters.',
        'uz': '❌ Ism juda qisqa. Iltimos, kamida 2 ta belgi kiriting.',
        'ru': '❌ Имя слишком короткое. Пожалуйста, введите не менее 2 символов.'
    },
    'telegram.name.invalid': {
        'en': '❌ Invalid name. Please try again.',
        'uz': '❌ Noto\'g\'ri ism. Iltimos, qaytadan urinib ko\'ring.',
        'ru': '❌ Некорректное имя. Пожалуйста, попробуйте снова.'
    },
    'telegram.profile_updated': {
        'en': '✅ Profile updated successfully!',
        'uz': '✅ Profil muvaffaqiyatli yangilandi!',
        'ru': '✅ Профиль успешно обновлен!'
    },
    'telegram.phone.verification_sms_sent': {
        'en': '📱 *Phone Verification*\n\nAn SMS with a verification code has been sent to *{phone}*.\n\nPlease reply with the 6-digit code to verify your phone number.',
        'uz': '📱 *Telefonni tasdiqlash*\n\nTasdiqlash kodi yuborildi: *{phone}*.\n\nIltimos, telefon raqamingizni tasdiqlash uchun 6 xonali kodni kiriting.',
        'ru': '📱 *Подтверждение телефона*\n\nSMS с кодом подтверждения отправлено на номер *{phone}*.\n\nПожалуйста, введите 6-значный код для подтверждения номера.'
    },
    'telegram.phone.verification_code_sent_toast': {
        'en': 'Verification code sent!',
        'uz': 'Tasdiqlash kodi yuborildi!',
        'ru': 'Код подтверждения отправлен!'
    },
    'telegram.phone.verification_code_send_failed_toast': {
        'en': 'Failed to send code!',
        'uz': 'Kodni yuborib bo\'lmadi!',
        'ru': 'Не удалось отправить код!'
    },
    'telegram.phone.verification_sms_send_failed': {
        'en': '❌ Could not send verification SMS: {error}\n\nPlease try again later.',
        'uz': '❌ Tasdiqlash SMS xabarini yuborib bo\'lmadi: {error}\n\nIltimos, keyinroq qayta urinib ko\'ring.',
        'ru': '❌ Не удалось отправить SMS с кодом подтверждения: {error}\n\nПожалуйста, попробуйте позже.'
    },
    'telegram.registration.share_contact_prompt': {
        'en': 'Please share your contact:',
        'uz': 'Iltimos, kontaktingizni ulashing:',
        'ru': 'Пожалуйста, поделитесь своим контактом:'
    },
    'telegram.back': {
        'en': '⬅️ Back',
        'uz': '⬅️ Ortga',
        'ru': '⬅️ Назад'
    },
    'telegram.action_cancelled': {
        'en': '❌ Action cancelled.',
        'uz': '❌ Amal bekor qilindi.',
        'ru': '❌ Действие отменено.'
    },
    'telegram.action_cancelled_short': {
        'en': '❌ Cancelled',
        'uz': '❌ Bekor qilindi',
        'ru': '❌ Отменено'
    },
    'telegram.common.processing': {
        'en': 'Processing...',
        'uz': 'Qayta ishlanmoqda...',
        'ru': 'Обработка...'
    },
    'telegram.common.user_fallback': {
        'en': 'User',
        'uz': 'Foydalanuvchi',
        'ru': 'Пользователь'
    },
    'telegram.auth.failed': {
        'en': '❌ Authentication failed.',
        'uz': '❌ Autentifikatsiya muvaffaqiyatsiz.',
        'ru': '❌ Ошибка аутентификации.'
    },
    'telegram.auth.failed_try_again': {
        'en': '❌ Authentication failed. Please try again.',
        'uz': '❌ Autentifikatsiya muvaffaqiyatsiz. Iltimos, qayta urinib ko\'ring.',
        'ru': '❌ Ошибка аутентификации. Пожалуйста, попробуйте снова.'
    },
    'telegram.auth.login_required': {
        'en': '🔐 Please log in first.',
        'uz': '🔐 Iltimos, avval tizimga kiring.',
        'ru': '🔐 Пожалуйста, сначала войдите в систему.'
    },
    'telegram.auth.linking_success': {
        'en': '✅ Your Telegram account has been linked successfully.',
        'uz': '✅ Telegram akkauntingiz muvaffaqiyatli bog\'landi.',
        'ru': '✅ Ваш Telegram-аккаунт успешно привязан.'
    },
    'telegram.auth.linking_already_linked': {
        'en': 'ℹ️ This account is already linked.',
        'uz': 'ℹ️ Bu akkaunt allaqachon bog\'langan.',
        'ru': 'ℹ️ Этот аккаунт уже привязан.'
    },
    'telegram.auth.linking_expired': {
        'en': '⏳ This linking request has expired. Please try again.',
        'uz': '⏳ Bog\'lash so\'rovi muddati tugagan. Iltimos, qayta urinib ko\'ring.',
        'ru': '⏳ Срок действия запроса на привязку истек. Пожалуйста, попробуйте снова.'
    },
    'telegram.auth.linking_failed': {
        'en': '❌ Failed to link your account. Please try again.',
        'uz': '❌ Akkauntni bog\'lab bo\'lmadi. Iltimos, qayta urinib ko\'ring.',
        'ru': '❌ Не удалось привязать аккаунт. Пожалуйста, попробуйте снова.'
    },
    'telegram.auth.linking_error': {
        'en': '❌ An unexpected error occurred during linking.',
        'uz': '❌ Bog\'lash jarayonida kutilmagan xatolik yuz berdi.',
        'ru': '❌ Во время привязки произошла непредвиденная ошибка.'
    },
    'telegram.auth.registration_failed': {
        'en': '❌ Registration failed. Please try again later.',
        'uz': '❌ Ro\'yxatdan o\'tish muvaffaqiyatsiz. Iltimos, keyinroq qayta urinib ko\'ring.',
        'ru': '❌ Не удалось пройти регистрацию. Пожалуйста, попробуйте позже.'
    },
    'telegram.main_menu_prompt': {
        'en': 'Main menu:',
        'uz': 'Asosiy menyu:',
        'ru': 'Главное меню:'
    },
    'telegram.error.unknown': {
        'en': 'Unknown error',
        'uz': 'Noma\'lum xatolik',
        'ru': 'Неизвестная ошибка'
    },
    'telegram.registration_welcome': {
        'en': 'Welcome! Please choose your language.',
        'uz': 'Xush kelibsiz! Iltimos, tilni tanlang.',
        'ru': 'Добро пожаловать! Пожалуйста, выберите язык.'
    },
    'telegram.registration_complete': {
        'en': '✅ Registration completed successfully!',
        'uz': '✅ Ro\'yxatdan o\'tish muvaffaqiyatli yakunlandi!',
        'ru': '✅ Регистрация успешно завершена!'
    },
    'telegram.registration.enter_phone': {
        'en': 'Please share your phone number to continue.',
        'uz': 'Davom etish uchun telefon raqamingizni ulashing.',
        'ru': 'Поделитесь номером телефона, чтобы продолжить.'
    },
    'telegram.registration.enter_name': {
        'en': 'Please enter your full name.',
        'uz': 'Iltimos, to\'liq ismingizni kiriting.',
        'ru': 'Пожалуйста, введите ваше полное имя.'
    },
    'telegram.registration.invalid_language_selection': {
        'en': '❌ Invalid language selection',
        'uz': '❌ Noto\'g\'ri til tanlandi',
        'ru': '❌ Неверный выбор языка'
    },
    'telegram.registration.failed_toast': {
        'en': '❌ Registration failed',
        'uz': '❌ Ro\'yxatdan o\'tish muvaffaqiyatsiz',
        'ru': '❌ Ошибка регистрации'
    },
    'telegram.registration.failed_contact_support': {
        'en': '❌ Registration failed. Please try again with /start or contact support.',
        'uz': '❌ Ro\'yxatdan o\'tish muvaffaqiyatsiz. Iltimos, /start bilan qayta urinib ko\'ring yoki yordamga murojaat qiling.',
        'ru': '❌ Регистрация не удалась. Попробуйте снова через /start или обратитесь в поддержку.'
    },
    'telegram.registration.failed_try_start': {
        'en': '❌ Registration failed. Please try again with /start.',
        'uz': '❌ Ro\'yxatdan o\'tish muvaffaqiyatsiz. Iltimos, /start bilan qayta urinib ko\'ring.',
        'ru': '❌ Регистрация не удалась. Пожалуйста, попробуйте снова через /start.'
    },
    'telegram.registration.language_updated_toast': {
        'en': '✅ Language updated',
        'uz': '✅ Til yangilandi',
        'ru': '✅ Язык обновлен'
    },
    'telegram.registration.share_own_contact': {
        'en': '❌ Please share your own contact information.',
        'uz': '❌ Iltimos, o\'zingizning kontaktingizni ulashing.',
        'ru': '❌ Пожалуйста, поделитесь своим контактом.'
    },
    'telegram.profile_title': {
        'en': '👤 Profile',
        'uz': '👤 Profil',
        'ru': '👤 Профиль'
    },
    'telegram.profile_name': {
        'en': 'Name',
        'uz': 'Ism',
        'ru': 'Имя'
    },
    'telegram.profile_phone': {
        'en': 'Phone',
        'uz': 'Telefon',
        'ru': 'Телефон'
    },
    'telegram.profile_email': {
        'en': 'Email',
        'uz': 'Email',
        'ru': 'Email'
    },
    'telegram.profile_language': {
        'en': 'Language',
        'uz': 'Til',
        'ru': 'Язык'
    },
    'telegram.profile.edit_prompt': {
        'en': '✏️ Edit Profile\n\nWhat would you like to update?\n\nType the new information or use /cancel to go back.',
        'uz': '✏️ Profilni tahrirlash\n\nNimani yangilamoqchisiz?\n\nYangi ma\'lumotni kiriting yoki ortga qaytish uchun /cancel dan foydalaning.',
        'ru': '✏️ Редактирование профиля\n\nЧто вы хотите обновить?\n\nВведите новые данные или используйте /cancel для возврата.'
    },
    'telegram.profile.logout_confirm': {
        'en': 'Are you sure you want to logout?',
        'uz': 'Haqiqatan ham chiqishni xohlaysizmi?',
        'ru': 'Вы уверены, что хотите выйти?'
    },
    'telegram.profile.logout_confirmation_text': {
        'en': '🚪 **Are you sure you want to logout?**\n\nThis will log you out from both Telegram bot and web app.\n\nYou can always log back in by using /start',
        'uz': '🚪 **Haqiqatan ham chiqishni xohlaysizmi?**\n\nBu sizni Telegram bot va web ilovadan chiqaradi.\n\nIstalgan vaqtda /start orqali qayta kirishingiz mumkin.',
        'ru': '🚪 **Вы уверены, что хотите выйти?**\n\nВы выйдете из Telegram-бота и веб-приложения.\n\nВы всегда можете снова войти через /start.'
    },
    'telegram.profile.logout_yes_button': {
        'en': '✅ Yes, Logout',
        'uz': '✅ Ha, chiqish',
        'ru': '✅ Да, выйти'
    },
    'telegram.profile.logout_success_text': {
        'en': '🚪 **Logged out successfully!**\n\nYou have been logged out from all platforms.\n\nTo log back in, use the /start command.',
        'uz': '🚪 **Muvaffaqiyatli chiqildi!**\n\nSiz barcha platformalardan chiqarildingiz.\n\nQayta kirish uchun /start buyrug\'idan foydalaning.',
        'ru': '🚪 **Вы успешно вышли!**\n\nВы вышли со всех платформ.\n\nЧтобы войти снова, используйте команду /start.'
    },
    'telegram.profile.logout_success_toast': {
        'en': '✅ Logged out successfully!',
        'uz': '✅ Muvaffaqiyatli chiqildi!',
        'ru': '✅ Вы успешно вышли!'
    },
    'telegram.phone.title': {
        'en': '📱 Phone Verification',
        'uz': '📱 Telefonni tasdiqlash',
        'ru': '📱 Подтверждение телефона'
    },
    'telegram.phone.no_phone_added': {
        'en': 'No phone number added yet.',
        'uz': 'Hali telefon raqami qo\'shilmagan.',
        'ru': 'Номер телефона пока не добавлен.'
    },
    'telegram.phone.add_prompt': {
        'en': '➕ Add Phone Number',
        'uz': '➕ Telefon raqami qo\'shish',
        'ru': '➕ Добавить номер телефона'
    },
    'telegram.phone.verification_prompt': {
        'en': '✅ Verify Phone',
        'uz': '✅ Telefonni tasdiqlash',
        'ru': '✅ Подтвердить телефон'
    },
    'telegram.phone.phone_not_verified': {
        'en': '❌ Phone number is not verified.',
        'uz': '❌ Telefon raqami tasdiqlanmagan.',
        'ru': '❌ Номер телефона не подтвержден.'
    },
    'telegram.phone.phone_verified': {
        'en': '✅ Phone number is verified.',
        'uz': '✅ Telefon raqami tasdiqlangan.',
        'ru': '✅ Номер телефона подтвержден.'
    },
    'telegram.phone.send_code_prompt': {
        'en': 'Please share your phone number to receive a verification code.',
        'uz': 'Tasdiqlash kodini olish uchun telefon raqamingizni ulashing.',
        'ru': 'Поделитесь номером телефона, чтобы получить код подтверждения.'
    },
    'telegram.phone.invalid_format': {
        'en': '❌ Invalid phone number format.',
        'uz': '❌ Telefon raqami formati noto\'g\'ri.',
        'ru': '❌ Неверный формат номера телефона.'
    },
    'telegram.phone.otp_invalid': {
        'en': '❌ Invalid verification code.',
        'uz': '❌ Tasdiqlash kodi noto\'g\'ri.',
        'ru': '❌ Неверный код подтверждения.'
    },
    'telegram.phone.otp_success': {
        'en': '✅ Phone verified successfully!',
        'uz': '✅ Telefon muvaffaqiyatli tasdiqlandi!',
        'ru': '✅ Телефон успешно подтвержден!'
    },
    'telegram.phone.already_registered_link_prompt': {
        'en': '📱 This phone number is already registered to an account ({masked_name}).\n\nWould you like to link your Telegram to this existing account?\nThis will merge your accounts.',
        'uz': '📱 Bu telefon raqami allaqachon hisobga biriktirilgan ({masked_name}).\n\nTelegram akkauntingizni shu hisobga bog\'lamoqchimisiz?\nBu akkauntlaringizni birlashtiradi.',
        'ru': '📱 Этот номер телефона уже привязан к аккаунту ({masked_name}).\n\nХотите связать Telegram с этим аккаунтом?\nЭто объединит ваши аккаунты.'
    },
    'telegram.phone.link_yes_button': {
        'en': '✅ Yes, link accounts',
        'uz': '✅ Ha, akkauntlarni bog\'lash',
        'ru': '✅ Да, связать аккаунты'
    },
    'telegram.phone.link_no_button': {
        'en': '❌ No, use different phone',
        'uz': '❌ Yo\'q, boshqa raqam ishlataman',
        'ru': '❌ Нет, использовать другой номер'
    },
    'telegram.phone.already_registered_use_different': {
        'en': '❌ This phone number is already registered.\nPlease use a different phone number or contact support.',
        'uz': '❌ Bu telefon raqami allaqachon ro\'yxatdan o\'tgan.\nIltimos, boshqa telefon raqamidan foydalaning yoki yordamga murojaat qiling.',
        'ru': '❌ Этот номер телефона уже зарегистрирован.\nПожалуйста, используйте другой номер или обратитесь в поддержку.'
    },
    'telegram.phone.already_linked_other_account': {
        'en': '❌ This phone number is already linked to another Telegram account.\nPlease use a different phone number.',
        'uz': '❌ Bu telefon raqami boshqa Telegram akkauntiga bog\'langan.\nIltimos, boshqa telefon raqamidan foydalaning.',
        'ru': '❌ Этот номер телефона уже привязан к другому Telegram-аккаунту.\nПожалуйста, используйте другой номер.'
    },
    'telegram.phone.verify_unavailable': {
        'en': '❌ Unable to verify phone. Please try again.',
        'uz': '❌ Telefonni tekshirib bo\'lmadi. Iltimos, qayta urinib ko\'ring.',
        'ru': '❌ Не удалось проверить номер телефона. Пожалуйста, попробуйте снова.'
    },
    'telegram.phone.verify_unavailable_now': {
        'en': '❌ Unable to verify phone right now. Please try again.',
        'uz': '❌ Hozircha telefonni tekshirib bo\'lmadi. Iltimos, qayta urinib ko\'ring.',
        'ru': '❌ Сейчас не удалось проверить номер телефона. Пожалуйста, попробуйте снова.'
    },
    'telegram.phone.verification_code_sent_to_phone_prompt': {
        'en': '📱 A verification code has been sent to {phone_masked}.\n\nPlease enter the 6-digit code:',
        'uz': '📱 Tasdiqlash kodi {phone_masked} raqamiga yuborildi.\n\nIltimos, 6 xonali kodni kiriting:',
        'ru': '📱 Код подтверждения отправлен на {phone_masked}.\n\nПожалуйста, введите 6-значный код:'
    },
    'telegram.phone.session_expired_share_again': {
        'en': '❌ Session expired. Please share your phone number again.',
        'uz': '❌ Sessiya muddati tugadi. Iltimos, telefon raqamingizni qayta ulashing.',
        'ru': '❌ Сессия истекла. Пожалуйста, снова поделитесь номером телефона.'
    },
    'telegram.phone.session_expired_start_again': {
        'en': '❌ Session expired. Please start again with /start',
        'uz': '❌ Sessiya muddati tugadi. Iltimos, /start bilan qayta boshlang.',
        'ru': '❌ Сессия истекла. Пожалуйста, начните заново с /start.'
    },
    'telegram.phone.too_many_verification_attempts': {
        'en': '⏳ Too many verification attempts. Please wait a few minutes and try again.',
        'uz': '⏳ Tasdiqlash urinishlari juda ko\'p. Iltimos, bir necha daqiqa kutib qayta urinib ko\'ring.',
        'ru': '⏳ Слишком много попыток подтверждения. Подождите несколько минут и попробуйте снова.'
    },
    'telegram.phone.verification_code_send_failed_default': {
        'en': 'Failed to send verification code',
        'uz': 'Tasdiqlash kodini yuborib bo\'lmadi',
        'ru': 'Не удалось отправить код подтверждения'
    },
    'telegram.phone.verification_code_send_failed_retry_or_different': {
        'en': '❌ {error}\n\nPlease try again or use a different phone.',
        'uz': '❌ {error}\n\nIltimos, qayta urinib ko\'ring yoki boshqa raqamdan foydalaning.',
        'ru': '❌ {error}\n\nПожалуйста, попробуйте снова или используйте другой номер.'
    },
    'telegram.phone.verification_code_send_failed_generic': {
        'en': '❌ Failed to send verification code. Please try again.',
        'uz': '❌ Tasdiqlash kodini yuborib bo\'lmadi. Iltimos, qayta urinib ko\'ring.',
        'ru': '❌ Не удалось отправить код подтверждения. Пожалуйста, попробуйте снова.'
    },
    'telegram.phone.share_different_phone_prompt': {
        'en': '📱 Please share a different phone number:',
        'uz': '📱 Iltimos, boshqa telefon raqamini ulashing:',
        'ru': '📱 Пожалуйста, поделитесь другим номером телефона:'
    },
    'telegram.phone.share_phone_using_button': {
        'en': 'Share your phone number using the button below:',
        'uz': 'Quyidagi tugma orqali telefon raqamingizni ulashing:',
        'ru': 'Поделитесь номером телефона с помощью кнопки ниже:'
    },
    'telegram.phone.enter_valid_6_digit_code': {
        'en': '❌ Please enter a valid 6-digit code.',
        'uz': '❌ Iltimos, to\'g\'ri 6 xonali kodni kiriting.',
        'ru': '❌ Пожалуйста, введите корректный 6-значный код.'
    },
    'telegram.phone.invalid_verification_code_default': {
        'en': 'Invalid verification code',
        'uz': 'Tasdiqlash kodi noto\'g\'ri',
        'ru': 'Неверный код подтверждения'
    },
    'telegram.phone.verification_code_expired_start_again': {
        'en': '❌ Verification code expired. Please start again with /start',
        'uz': '❌ Tasdiqlash kodi muddati tugagan. Iltimos, /start bilan qayta boshlang.',
        'ru': '❌ Срок действия кода истек. Пожалуйста, начните заново с /start.'
    },
    'telegram.phone.verification_failed_with_error_retry': {
        'en': '❌ {error}\n\nPlease try again:',
        'uz': '❌ {error}\n\nIltimos, qayta urinib ko\'ring:',
        'ru': '❌ {error}\n\nПожалуйста, попробуйте снова:'
    },
    'telegram.phone.verification_failed_retry': {
        'en': '❌ Verification failed. Please try again:',
        'uz': '❌ Tasdiqlash muvaffaqiyatsiz. Iltimos, qayta urinib ko\'ring:',
        'ru': '❌ Проверка не удалась. Пожалуйста, попробуйте снова:'
    },
    'telegram.phone.verification_failed_with_error_skip': {
        'en': '❌ Verification failed: {error}\n\nPlease enter the correct code or /cancel to skip:',
        'uz': '❌ Tasdiqlash muvaffaqiyatsiz: {error}\n\nTo\'g\'ri kodni kiriting yoki o\'tkazib yuborish uchun /cancel ni bosing:',
        'ru': '❌ Проверка не удалась: {error}\n\nВведите правильный код или используйте /cancel для пропуска:'
    },
    'telegram.phone.verification_failed_skip': {
        'en': '❌ Verification failed. Please try again or /cancel to skip.',
        'uz': '❌ Tasdiqlash muvaffaqiyatsiz. Iltimos, qayta urinib ko\'ring yoki o\'tkazib yuborish uchun /cancel ni bosing.',
        'ru': '❌ Проверка не удалась. Пожалуйста, попробуйте снова или используйте /cancel для пропуска.'
    },
    'telegram.phone.accounts_linked_success': {
        'en': '✅ Accounts linked successfully!\n\nWelcome back, {name}! Your Telegram is now connected to your existing account.',
        'uz': '✅ Akkountlar muvaffaqiyatli bog\'landi!\n\nXush kelibsiz, {name}! Telegram hisobingiz endi mavjud akkauntingizga bog\'landi.',
        'ru': '✅ Аккаунты успешно связаны!\n\nС возвращением, {name}! Ваш Telegram теперь подключен к существующему аккаунту.'
    },
    'telegram.address.no_addresses': {
        'en': 'You have no saved addresses yet.',
        'uz': 'Sizda hali saqlangan manzillar yo\'q.',
        'ru': 'У вас пока нет сохраненных адресов.'
    },
    'telegram.address.list_header': {
        'en': '📍 Your Addresses ({count}):\n\n',
        'uz': '📍 Sizning manzillaringiz ({count}):\n\n',
        'ru': '📍 Ваши адреса ({count}):\n\n'
    },
    'telegram.address.title_fallback': {
        'en': 'Address {index}',
        'uz': 'Manzil {index}',
        'ru': 'Адрес {index}'
    },
    'telegram.address.no_address_placeholder': {
        'en': 'No address',
        'uz': 'Manzil ko\'rsatilmagan',
        'ru': 'Адрес не указан'
    },
    'telegram.address.location_received': {
        'en': '📍 Location received!',
        'uz': '📍 Joylashuv qabul qilindi!',
        'ru': '📍 Местоположение получено!'
    },
    'telegram.address.detected_location_prefix': {
        'en': '📍 *Detected location:*\n{address}\n\n',
        'uz': '📍 *Aniqlangan joylashuv:*\n{address}\n\n',
        'ru': '📍 *Определенное местоположение:*\n{address}\n\n'
    },
    'telegram.address.location_based_fallback': {
        'en': 'Location-based address',
        'uz': 'Joylashuv asosidagi manzil',
        'ru': 'Адрес на основе местоположения'
    },
    'telegram.address.title_received': {
        'en': 'Address received. Please provide a title.',
        'uz': 'Manzil qabul qilindi. Iltimos, unga nom bering.',
        'ru': 'Адрес получен. Пожалуйста, укажите название.'
    },
    'telegram.address.added_successfully': {
        'en': '✅ Address "{title}" added successfully!',
        'uz': '✅ "{title}" manzili muvaffaqiyatli qo\'shildi!',
        'ru': '✅ Адрес "{title}" успешно добавлен!'
    },
    'telegram.address.add_failed': {
        'en': '❌ Failed to add address. Please try again.',
        'uz': '❌ Manzilni qo\'shib bo\'lmadi. Iltimos, qayta urinib ko\'ring.',
        'ru': '❌ Не удалось добавить адрес. Пожалуйста, попробуйте снова.'
    },
    'telegram.address.manual_entry_started': {
        'en': '✏️ Manual address entry',
        'uz': '✏️ Manzilni qo\'lda kiritish',
        'ru': '✏️ Ручной ввод адреса'
    },
    'telegram.address.enter_street_required': {
        'en': '📍 District: *{district_name}*\n\n🛤️ Please enter your street name (required):',
        'uz': '📍 Tuman: *{district_name}*\n\n🛤️ Iltimos, ko\'cha nomini kiriting (majburiy):',
        'ru': '📍 Район: *{district_name}*\n\n🛤️ Пожалуйста, введите название улицы (обязательно):'
    },
    'telegram.address.geocode_found_with_address': {
        'en': '📍 *Location Found*\n\nAddress: {address}\n\nIs this location correct?',
        'uz': '📍 *Joylashuv topildi*\n\nManzil: {address}\n\nBu joylashuv to\'g\'rimi?',
        'ru': '📍 *Местоположение найдено*\n\nАдрес: {address}\n\nЭто местоположение верное?'
    },
    'telegram.address.geocode_note_approximate_center': {
        'en': '\n\n⚠️ _Note: Exact location could not be determined. Using approximate district center._',
        'uz': '\n\n⚠️ _Eslatma: Aniq joylashuvni aniqlab bo\'lmadi. Taxminiy tuman markazi ishlatilmoqda._',
        'ru': '\n\n⚠️ _Примечание: Точное местоположение определить не удалось. Используется примерный центр района._'
    },
    'telegram.address.location_confirmed_toast': {
        'en': '✅ Location confirmed!',
        'uz': '✅ Joylashuv tasdiqlandi!',
        'ru': '✅ Местоположение подтверждено!'
    },
    'telegram.address.retry_location': {
        'en': '📍 Let\'s fix the location\n\nPlease share your exact location for accurate delivery,\nor click \'Re-enter Address\' to try again manually.',
        'uz': '📍 Joylashuvni to\'g\'rilaymiz\n\nAniq yetkazib berish uchun aniq joylashuvingizni ulashing,\nyoki qayta qo\'lda kiritish uchun \'Manzilni qayta kiriting\' tugmasini bosing.',
        'ru': '📍 Давайте уточним местоположение\n\nПоделитесь точным местоположением для точной доставки,\nили нажмите \'Ввести адрес заново\', чтобы попробовать вручную.'
    },
    'telegram.address.retry_location_toast': {
        'en': 'Let\'s fix the location!',
        'uz': 'Joylashuvni to\'g\'rilaymiz!',
        'ru': 'Давайте уточним местоположение!'
    },
    'telegram.address.default_title': {
        'en': 'My Address',
        'uz': 'Mening manzilim',
        'ru': 'Мой адрес'
    },
    'telegram.address.default_city': {
        'en': 'Tashkent',
        'uz': 'Toshkent',
        'ru': 'Ташкент'
    },
    'telegram.address.not_found': {
        'en': 'Address not found',
        'uz': 'Manzil topilmadi',
        'ru': 'Адрес не найден'
    },
    'telegram.address.untitled': {
        'en': 'Untitled',
        'uz': 'Nomsiz',
        'ru': 'Без названия'
    },
    'telegram.address.details_title': {
        'en': '📍 **{title}**\n\n',
        'uz': '📍 **{title}**\n\n',
        'ru': '📍 **{title}**\n\n'
    },
    'telegram.address.details_full_address': {
        'en': '**Full Address:** {address}\n',
        'uz': '**To\'liq manzil:** {address}\n',
        'ru': '**Полный адрес:** {address}\n'
    },
    'telegram.address.details_street': {
        'en': '**Street:** {street}\n',
        'uz': '**Ko\'cha:** {street}\n',
        'ru': '**Улица:** {street}\n'
    },
    'telegram.address.details_city': {
        'en': '**City:** {city}\n',
        'uz': '**Shahar:** {city}\n',
        'ru': '**Город:** {city}\n'
    },
    'telegram.address.details_default_badge': {
        'en': '\n🏠 **Default Address**\n',
        'uz': '\n🏠 **Asosiy manzil**\n',
        'ru': '\n🏠 **Адрес по умолчанию**\n'
    },
    'telegram.address.no_addresses_to_edit': {
        'en': 'No addresses to edit',
        'uz': 'Tahrirlash uchun manzillar yo\'q',
        'ru': 'Нет адресов для редактирования'
    },
    'telegram.address.select_edit_prompt': {
        'en': '✏️ **Select address to edit:**\n\nClick on the address you want to modify:',
        'uz': '✏️ **Tahrirlash uchun manzilni tanlang:**\n\nO\'zgartirmoqchi bo\'lgan manzilni bosing:',
        'ru': '✏️ **Выберите адрес для редактирования:**\n\nНажмите адрес, который хотите изменить:'
    },
    'telegram.address.no_addresses_to_delete': {
        'en': 'No addresses to delete',
        'uz': 'O\'chirish uchun manzillar yo\'q',
        'ru': 'Нет адресов для удаления'
    },
    'telegram.address.select_delete_prompt': {
        'en': '🗑️ **Select address to delete:**\n\n⚠️ **Warning:** This action cannot be undone!',
        'uz': '🗑️ **O\'chirish uchun manzilni tanlang:**\n\n⚠️ **Ogohlantirish:** Bu amalni ortga qaytarib bo\'lmaydi!',
        'ru': '🗑️ **Выберите адрес для удаления:**\n\n⚠️ **Внимание:** Это действие нельзя отменить!'
    },
    'telegram.address.set_default_success_toast': {
        'en': '✅ Address set as default!',
        'uz': '✅ Manzil asosiy qilib belgilandi!',
        'ru': '✅ Адрес установлен по умолчанию!'
    },
    'telegram.address.set_default_failed_toast': {
        'en': '❌ Failed to set as default: {error}',
        'uz': '❌ Asosiy qilib belgilab bo\'lmadi: {error}',
        'ru': '❌ Не удалось установить по умолчанию: {error}'
    },
    'telegram.address.edit_options_text': {
        'en': '✏️ **Edit Address Options:**\n\nChoose what you\'d like to edit about this address:\n\n💡 **Quick tip:** For major changes, you can delete this address and add a new one.',
        'uz': '✏️ **Manzilni tahrirlash variantlari:**\n\nUshbu manzilda nimani o\'zgartirmoqchi ekaningizni tanlang:\n\n💡 **Maslahat:** Katta o\'zgarishlar uchun manzilni o\'chirib, yangisini qo\'shishingiz mumkin.',
        'ru': '✏️ **Параметры редактирования адреса:**\n\nВыберите, что хотите изменить в этом адресе:\n\n💡 **Совет:** Для больших изменений удалите адрес и добавьте новый.'
    },
    'telegram.address.edit_title_button': {
        'en': '📝 Edit Title',
        'uz': '📝 Nomini tahrirlash',
        'ru': '📝 Изменить название'
    },
    'telegram.address.edit_location_button': {
        'en': '📍 Edit Location',
        'uz': '📍 Joylashuvni tahrirlash',
        'ru': '📍 Изменить местоположение'
    },
    'telegram.address.edit_details_button': {
        'en': '📋 Edit Details',
        'uz': '📋 Tafsilotlarni tahrirlash',
        'ru': '📋 Изменить детали'
    },
    'telegram.address.edit_instructions_button': {
        'en': '📞 Edit Instructions',
        'uz': '📞 Ko\'rsatmalarni tahrirlash',
        'ru': '📞 Изменить инструкции'
    },
    'telegram.address.delete_readd_button': {
        'en': '🗑️ Delete & Re-add',
        'uz': '🗑️ O\'chirib qayta qo\'shish',
        'ru': '🗑️ Удалить и добавить заново'
    },
    'telegram.address.delete_confirmation': {
        'en': '⚠️ Are you sure you want to delete **{title}**?\n\n{address}',
        'uz': '⚠️ **{title}** manzilini o\'chirmoqchimisiz?\n\n{address}',
        'ru': '⚠️ Вы уверены, что хотите удалить **{title}**?\n\n{address}'
    },
    'telegram.address.delete_confirm_yes': {
        'en': '✅ Yes, Delete',
        'uz': '✅ Ha, o\'chirish',
        'ru': '✅ Да, удалить'
    },
    'telegram.address.deleted_success_toast': {
        'en': '🗑️ Address deleted successfully!',
        'uz': '🗑️ Manzil muvaffaqiyatli o\'chirildi!',
        'ru': '🗑️ Адрес успешно удален!'
    },
    'telegram.address.delete_failed_toast': {
        'en': '❌ Failed to delete address: {error}',
        'uz': '❌ Manzilni o\'chirib bo\'lmadi: {error}',
        'ru': '❌ Не удалось удалить адрес: {error}'
    },
    'telegram.address.delete_failed_detail': {
        'en': '❌ **Error deleting address:**\n\n{error}\n\nPlease try again.',
        'uz': '❌ **Manzilni o\'chirishda xatolik:**\n\n{error}\n\nIltimos, qayta urinib ko\'ring.',
        'ru': '❌ **Ошибка удаления адреса:**\n\n{error}\n\nПожалуйста, попробуйте снова.'
    },
    'telegram.address.edit_title_prompt': {
        'en': '📝 **Edit Address Title**\n\n**Current title:** {current_title}\n\nPlease type the new title for this address:',
        'uz': '📝 **Manzil nomini tahrirlash**\n\n**Joriy nom:** {current_title}\n\nUshbu manzil uchun yangi nom kiriting:',
        'ru': '📝 **Изменение названия адреса**\n\n**Текущее название:** {current_title}\n\nВведите новое название для этого адреса:'
    },
    'telegram.address.location_edit_not_supported': {
        'en': '📍 Location editing: Please delete and re-add the address with the new location for now.',
        'uz': '📍 Joylashuvni tahrirlash: Hozircha manzilni o\'chirib, yangi joylashuv bilan qayta qo\'shing.',
        'ru': '📍 Редактирование местоположения: Пока удалите адрес и добавьте заново с новым местоположением.'
    },
    'telegram.address.details_edit_coming_soon': {
        'en': '📋 Address details editing will be available in the next update!',
        'uz': '📋 Manzil tafsilotlarini tahrirlash keyingi yangilanishda qo\'shiladi!',
        'ru': '📋 Редактирование деталей адреса появится в следующем обновлении!'
    },
    'telegram.address.none_value': {
        'en': 'None',
        'uz': 'Yo\'q',
        'ru': 'Нет'
    },
    'telegram.address.edit_instructions_prompt': {
        'en': '📞 **Edit Delivery Instructions**\n\n**Current instructions:** {current_instructions}\n\nPlease type the new delivery instructions for this address:',
        'uz': '📞 **Yetkazib berish ko\'rsatmalarini tahrirlash**\n\n**Joriy ko\'rsatmalar:** {current_instructions}\n\nUshbu manzil uchun yangi ko\'rsatmalarni kiriting:',
        'ru': '📞 **Изменение инструкций по доставке**\n\n**Текущие инструкции:** {current_instructions}\n\nВведите новые инструкции для этого адреса:'
    },
    'telegram.address.edit_session_expired': {
        'en': '❌ Address editing session expired. Please try again.',
        'uz': '❌ Manzilni tahrirlash sessiyasi tugadi. Iltimos, qayta urinib ko\'ring.',
        'ru': '❌ Сессия редактирования адреса истекла. Пожалуйста, попробуйте снова.'
    },
    'telegram.address.title_too_short': {
        'en': '❌ Title is too short. Please enter at least 2 characters.',
        'uz': '❌ Nom juda qisqa. Iltimos, kamida 2 ta belgi kiriting.',
        'ru': '❌ Название слишком короткое. Пожалуйста, введите не менее 2 символов.'
    },
    'telegram.address.title_too_long': {
        'en': '❌ Title is too long. Please keep it under 50 characters.',
        'uz': '❌ Nom juda uzun. Iltimos, 50 belgidan oshirmang.',
        'ru': '❌ Название слишком длинное. Пожалуйста, не более 50 символов.'
    },
    'telegram.address.title_updated_success': {
        'en': '✅ **Address title updated successfully!**\n\n**New title:** {title}',
        'uz': '✅ **Manzil nomi muvaffaqiyatli yangilandi!**\n\n**Yangi nom:** {title}',
        'ru': '✅ **Название адреса успешно обновлено!**\n\n**Новое название:** {title}'
    },
    'telegram.address.title_update_failed': {
        'en': '❌ **Failed to update address title:**\n\n{error}\n\nPlease try again.',
        'uz': '❌ **Manzil nomini yangilab bo\'lmadi:**\n\n{error}\n\nIltimos, qayta urinib ko\'ring.',
        'ru': '❌ **Не удалось обновить название адреса:**\n\n{error}\n\nПожалуйста, попробуйте снова.'
    },
    'telegram.address.title_update_error': {
        'en': '❌ An error occurred while updating the address title. Please try again.',
        'uz': '❌ Manzil nomini yangilashda xatolik yuz berdi. Iltimos, qayta urinib ko\'ring.',
        'ru': '❌ Произошла ошибка при обновлении названия адреса. Пожалуйста, попробуйте снова.'
    },
    'telegram.address.instructions_too_long': {
        'en': '❌ Instructions are too long. Please keep them under 200 characters.',
        'uz': '❌ Ko\'rsatmalar juda uzun. Iltimos, 200 belgidan oshirmang.',
        'ru': '❌ Инструкции слишком длинные. Пожалуйста, не более 200 символов.'
    },
    'telegram.address.instructions_updated_intro': {
        'en': '📞 **Delivery instructions updated successfully!**\n\n',
        'uz': '📞 **Yetkazib berish ko\'rsatmalari muvaffaqiyatli yangilandi!**\n\n',
        'ru': '📞 **Инструкции по доставке успешно обновлены!**\n\n'
    },
    'telegram.address.instructions_new_value': {
        'en': '**New instructions:** {value}',
        'uz': '**Yangi ko\'rsatmalar:** {value}',
        'ru': '**Новые инструкции:** {value}'
    },
    'telegram.address.instructions_cleared': {
        'en': '**Instructions:** None (cleared)',
        'uz': '**Ko\'rsatmalar:** Yo\'q (tozalandi)',
        'ru': '**Инструкции:** Нет (очищено)'
    },
    'telegram.address.instructions_update_failed': {
        'en': '❌ **Failed to update delivery instructions:**\n\n{error}\n\nPlease try again.',
        'uz': '❌ **Yetkazib berish ko\'rsatmalarini yangilab bo\'lmadi:**\n\n{error}\n\nIltimos, qayta urinib ko\'ring.',
        'ru': '❌ **Не удалось обновить инструкции по доставке:**\n\n{error}\n\nПожалуйста, попробуйте снова.'
    },
    'telegram.address.instructions_update_error': {
        'en': '❌ An error occurred while updating delivery instructions. Please try again.',
        'uz': '❌ Yetkazib berish ko\'rsatmalarini yangilashda xatolik yuz berdi. Iltimos, qayta urinib ko\'ring.',
        'ru': '❌ Произошла ошибка при обновлении инструкций по доставке. Пожалуйста, попробуйте снова.'
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
    'telegram.menu.products': {
        'en': '🛍️ Products',
        'uz': '🛍️ Mahsulotlar',
        'ru': '🛍️ Товары'
    },
    'telegram.menu.orders': {
        'en': '📦 Orders',
        'uz': '📦 Buyurtmalar',
        'ru': '📦 Заказы'
    },
    'telegram.cart_title': {
        'en': '🛒 Cart',
        'uz': '🛒 Savat',
        'ru': '🛒 Корзина'
    },
    'telegram.menu.subscriptions': {
        'en': '🔄 Subscriptions',
        'uz': '🔄 Obunalar',
        'ru': '🔄 Подписки'
    },
    'telegram.menu.loyalty': {
        'en': '🎁 Loyalty',
        'uz': '🎁 Sodiqlik',
        'ru': '🎁 Лояльность'
    },
    'telegram.menu.profile': {
        'en': '👤 Profile',
        'uz': '👤 Profil',
        'ru': '👤 Профиль'
    },
    'telegram.menu.support': {
        'en': '🆘 Support',
        'uz': '🆘 Yordam',
        'ru': '🆘 Поддержка'
    },
    'telegram.menu.language': {
        'en': '🌐 Language',
        'uz': '🌐 Til',
        'ru': '🌐 Язык'
    },
    'telegram.yes': {
        'en': '✅ Yes',
        'uz': '✅ Ha',
        'ru': '✅ Да'
    },
    'telegram.no': {
        'en': '❌ No',
        'uz': '❌ Yo\'q',
        'ru': '❌ Нет'
    },
    'telegram.product.add_to_cart': {
        'en': '➕ Add to Cart',
        'uz': '➕ Savatga qo\'shish',
        'ru': '➕ Добавить в корзину'
    },
    'telegram.cart.checkout': {
        'en': '✅ Checkout',
        'uz': '✅ Buyurtma berish',
        'ru': '✅ Оформить заказ'
    },
    'telegram.cart.add_more': {
        'en': 'Add more items to reach minimum order amount',
        'uz': 'Minimal buyurtma summasiga yetish uchun yana mahsulot qo\'shing',
        'ru': 'Добавьте товары, чтобы достичь минимальной суммы заказа'
    },
    'telegram.back_to_order': {
        'en': 'Back to Order',
        'uz': 'Buyurtmaga qaytish',
        'ru': 'Назад к заказу'
    },
    'telegram.address.reenter_manually_button': {
        'en': '✏️ Re-enter Address',
        'uz': '✏️ Manzilni qayta kiritish',
        'ru': '✏️ Ввести адрес заново'
    },
    'telegram.subscription.frequency_daily': {
        'en': 'Daily',
        'uz': 'Har kuni',
        'ru': 'Ежедневно'
    },
    'telegram.subscription.frequency_weekly': {
        'en': 'Weekly',
        'uz': 'Haftalik',
        'ru': 'Еженедельно'
    },
    'telegram.subscription.frequency_biweekly': {
        'en': 'Every 2 Weeks',
        'uz': 'Har 2 haftada',
        'ru': 'Каждые 2 недели'
    },
    'telegram.subscription.frequency_monthly': {
        'en': 'Monthly',
        'uz': 'Oylik',
        'ru': 'Ежемесячно'
    },
    'telegram.admin.orders': {
        'en': '📊 Orders',
        'uz': '📊 Buyurtmalar',
        'ru': '📊 Заказы'
    },
    'telegram.admin.analytics': {
        'en': '📈 Analytics',
        'uz': '📈 Analitika',
        'ru': '📈 Аналитика'
    },
    'telegram.admin.users': {
        'en': '👥 Users',
        'uz': '👥 Foydalanuvchilar',
        'ru': '👥 Пользователи'
    },
    'telegram.admin.products': {
        'en': '🧴 Products',
        'uz': '🧴 Mahsulotlar',
        'ru': '🧴 Товары'
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
    # ============================================================================
    # Telegram Runtime Keys Added From Bot Scan
    # ============================================================================
    'telegram.back_to_menu': {
        'en': 'Back to menu',
        'uz': 'Back to menu',
        'ru': 'Back to menu'
    },
    'telegram.cart_empty': {
        'en': 'Cart empty',
        'uz': 'Cart empty',
        'ru': 'Cart empty'
    },
    'telegram.cart_min_order_warning': {
        'en': 'Cart min order warning',
        'uz': 'Cart min order warning',
        'ru': 'Cart min order warning'
    },
    'telegram.cart_ready_checkout': {
        'en': 'Cart ready checkout',
        'uz': 'Cart ready checkout',
        'ru': 'Cart ready checkout'
    },
    'telegram.cart_total': {
        'en': 'Cart total',
        'uz': 'Cart total',
        'ru': 'Cart total'
    },
    'telegram.confirm': {
        'en': 'Confirm',
        'uz': 'Confirm',
        'ru': 'Confirm'
    },
    'telegram.continue': {
        'en': 'Continue',
        'uz': 'Continue',
        'ru': 'Continue'
    },
    'telegram.currency.uzs': {
        'en': 'Uzs',
        'uz': 'Uzs',
        'ru': 'Uzs'
    },
    'telegram.delivery_address': {
        'en': 'Delivery address',
        'uz': 'Delivery address',
        'ru': 'Delivery address'
    },
    'telegram.error.auth_failed': {
        'en': 'Auth failed',
        'uz': 'Auth failed',
        'ru': 'Auth failed'
    },
    'telegram.error.generic': {
        'en': 'Generic',
        'uz': 'Generic',
        'ru': 'Generic'
    },
    'telegram.error.invalid_input': {
        'en': 'Invalid input',
        'uz': 'Invalid input',
        'ru': 'Invalid input'
    },
    'telegram.error.product_error': {
        'en': 'Product error',
        'uz': 'Product error',
        'ru': 'Product error'
    },
    'telegram.error_occurred': {
        'en': 'Error occurred',
        'uz': 'Error occurred',
        'ru': 'Error occurred'
    },
    'telegram.language.already_selected': {
        'en': 'Already selected',
        'uz': 'Already selected',
        'ru': 'Already selected'
    },
    'telegram.language.changed_success': {
        'en': 'Changed success',
        'uz': 'Changed success',
        'ru': 'Changed success'
    },
    'telegram.language.confirmation_message': {
        'en': 'Confirmation message',
        'uz': 'Confirmation message',
        'ru': 'Confirmation message'
    },
    'telegram.language.confirmation_title': {
        'en': 'Confirmation title',
        'uz': 'Confirmation title',
        'ru': 'Confirmation title'
    },
    'telegram.language.current': {
        'en': 'Current',
        'uz': 'Current',
        'ru': 'Current'
    },
    'telegram.language.error_changing': {
        'en': 'Error changing',
        'uz': 'Error changing',
        'ru': 'Error changing'
    },
    'telegram.language.invalid_selection': {
        'en': 'Invalid selection',
        'uz': 'Invalid selection',
        'ru': 'Invalid selection'
    },
    'telegram.language.now_using': {
        'en': 'Now using',
        'uz': 'Now using',
        'ru': 'Now using'
    },
    'telegram.language.select_prompt': {
        'en': 'Select prompt',
        'uz': 'Select prompt',
        'ru': 'Select prompt'
    },
    'telegram.loyalty.and_more': {
        'en': 'And more',
        'uz': 'And more',
        'ru': 'And more'
    },
    'telegram.loyalty.available_rewards': {
        'en': 'Available rewards',
        'uz': 'Available rewards',
        'ru': 'Available rewards'
    },
    'telegram.loyalty.current_balance': {
        'en': 'Current balance',
        'uz': 'Current balance',
        'ru': 'Current balance'
    },
    'telegram.loyalty.lifetime_earned': {
        'en': 'Lifetime earned',
        'uz': 'Lifetime earned',
        'ru': 'Lifetime earned'
    },
    'telegram.loyalty.no_history': {
        'en': 'No history',
        'uz': 'No history',
        'ru': 'No history'
    },
    'telegram.loyalty.no_rewards_available': {
        'en': 'No rewards available',
        'uz': 'No rewards available',
        'ru': 'No rewards available'
    },
    'telegram.loyalty.points_history': {
        'en': 'Points history',
        'uz': 'Points history',
        'ru': 'Points history'
    },
    'telegram.loyalty.points_unit': {
        'en': 'Points unit',
        'uz': 'Points unit',
        'ru': 'Points unit'
    },
    'telegram.loyalty.redeem_success': {
        'en': 'Redeem success',
        'uz': 'Redeem success',
        'ru': 'Redeem success'
    },
    'telegram.loyalty.refer_friends': {
        'en': 'Refer friends',
        'uz': 'Refer friends',
        'ru': 'Refer friends'
    },
    'telegram.loyalty.reward_fallback': {
        'en': 'Reward fallback',
        'uz': 'Reward fallback',
        'ru': 'Reward fallback'
    },
    'telegram.loyalty.transaction_earned': {
        'en': 'Transaction earned',
        'uz': 'Transaction earned',
        'ru': 'Transaction earned'
    },
    'telegram.loyalty.transaction_other': {
        'en': 'Transaction other',
        'uz': 'Transaction other',
        'ru': 'Transaction other'
    },
    'telegram.loyalty.transaction_redeemed': {
        'en': 'Transaction redeemed',
        'uz': 'Transaction redeemed',
        'ru': 'Transaction redeemed'
    },
    'telegram.loyalty.view_rewards': {
        'en': 'View rewards',
        'uz': 'View rewards',
        'ru': 'View rewards'
    },
    'telegram.main_menu': {
        'en': 'Main menu',
        'uz': 'Main menu',
        'ru': 'Main menu'
    },
    'telegram.price': {
        'en': 'Price',
        'uz': 'Price',
        'ru': 'Price'
    },
    'telegram.quantity': {
        'en': 'Quantity',
        'uz': 'Quantity',
        'ru': 'Quantity'
    },
    'telegram.registration.phone_shared': {
        'en': 'Phone shared',
        'uz': 'Phone shared',
        'ru': 'Phone shared'
    },
    'telegram.subscription.active': {
        'en': 'Active',
        'uz': 'Active',
        'ru': 'Active'
    },
    'telegram.subscription.activity_logs': {
        'en': 'Activity logs',
        'uz': 'Activity logs',
        'ru': 'Activity logs'
    },
    'telegram.subscription.add_address': {
        'en': 'Add address',
        'uz': 'Add address',
        'ru': 'Add address'
    },
    'telegram.subscription.add_more_or_continue': {
        'en': 'Add more or continue',
        'uz': 'Add more or continue',
        'ru': 'Add more or continue'
    },
    'telegram.subscription.add_new_address': {
        'en': 'Add new address',
        'uz': 'Add new address',
        'ru': 'Add new address'
    },
    'telegram.subscription.amount': {
        'en': 'Amount',
        'uz': 'Amount',
        'ru': 'Amount'
    },
    'telegram.subscription.average_order': {
        'en': 'Average order',
        'uz': 'Average order',
        'ru': 'Average order'
    },
    'telegram.subscription.back_to_items': {
        'en': 'Back to items',
        'uz': 'Back to items',
        'ru': 'Back to items'
    },
    'telegram.subscription.billing_history': {
        'en': 'Billing history',
        'uz': 'Billing history',
        'ru': 'Billing history'
    },
    'telegram.subscription.billing_retry_initiated': {
        'en': 'Billing retry initiated',
        'uz': 'Billing retry initiated',
        'ru': 'Billing retry initiated'
    },
    'telegram.subscription.cancelled_success': {
        'en': 'Cancelled success',
        'uz': 'Cancelled success',
        'ru': 'Cancelled success'
    },
    'telegram.subscription.confirm_title': {
        'en': 'Confirm title',
        'uz': 'Confirm title',
        'ru': 'Confirm title'
    },
    'telegram.subscription.create_template_or_custom': {
        'en': 'Create template or custom',
        'uz': 'Create template or custom',
        'ru': 'Create template or custom'
    },
    'telegram.subscription.created_success': {
        'en': 'Created success',
        'uz': 'Created success',
        'ru': 'Created success'
    },
    'telegram.subscription.creation_cancelled': {
        'en': 'Creation cancelled',
        'uz': 'Creation cancelled',
        'ru': 'Creation cancelled'
    },
    'telegram.subscription.current_items': {
        'en': 'Current items',
        'uz': 'Current items',
        'ru': 'Current items'
    },
    'telegram.subscription.details_title': {
        'en': 'Details title',
        'uz': 'Details title',
        'ru': 'Details title'
    },
    'telegram.subscription.edit_menu': {
        'en': 'Edit menu',
        'uz': 'Edit menu',
        'ru': 'Edit menu'
    },
    'telegram.subscription.favorite_product': {
        'en': 'Favorite product',
        'uz': 'Favorite product',
        'ru': 'Favorite product'
    },
    'telegram.subscription.frequency': {
        'en': 'Frequency',
        'uz': 'Frequency',
        'ru': 'Frequency'
    },
    'telegram.subscription.frequency_updated_successfully': {
        'en': 'Frequency updated successfully',
        'uz': 'Frequency updated successfully',
        'ru': 'Frequency updated successfully'
    },
    'telegram.subscription.id': {
        'en': 'Id',
        'uz': 'Id',
        'ru': 'Id'
    },
    'telegram.subscription.item_added': {
        'en': 'Item added',
        'uz': 'Item added',
        'ru': 'Item added'
    },
    'telegram.subscription.item_added_successfully': {
        'en': 'Item added successfully',
        'uz': 'Item added successfully',
        'ru': 'Item added successfully'
    },
    'telegram.subscription.item_removed_successfully': {
        'en': 'Item removed successfully',
        'uz': 'Item removed successfully',
        'ru': 'Item removed successfully'
    },
    'telegram.subscription.item_updated_successfully': {
        'en': 'Item updated successfully',
        'uz': 'Item updated successfully',
        'ru': 'Item updated successfully'
    },
    'telegram.subscription.items': {
        'en': 'Items',
        'uz': 'Items',
        'ru': 'Items'
    },
    'telegram.subscription.next_billing': {
        'en': 'Next billing',
        'uz': 'Next billing',
        'ru': 'Next billing'
    },
    'telegram.subscription.next_delivery': {
        'en': 'Next delivery',
        'uz': 'Next delivery',
        'ru': 'Next delivery'
    },
    'telegram.subscription.no_activity_logs': {
        'en': 'No activity logs',
        'uz': 'No activity logs',
        'ru': 'No activity logs'
    },
    'telegram.subscription.no_addresses': {
        'en': 'No addresses',
        'uz': 'No addresses',
        'ru': 'No addresses'
    },
    'telegram.subscription.no_billing_history': {
        'en': 'No billing history',
        'uz': 'No billing history',
        'ru': 'No billing history'
    },
    'telegram.subscription.no_items': {
        'en': 'No items',
        'uz': 'No items',
        'ru': 'No items'
    },
    'telegram.subscription.no_subscriptions': {
        'en': 'No subscriptions',
        'uz': 'No subscriptions',
        'ru': 'No subscriptions'
    },
    'telegram.subscription.paused': {
        'en': 'Paused',
        'uz': 'Paused',
        'ru': 'Paused'
    },
    'telegram.subscription.paused_success': {
        'en': 'Paused success',
        'uz': 'Paused success',
        'ru': 'Paused success'
    },
    'telegram.subscription.payment_method_updated_successfully': {
        'en': 'Payment method updated successfully',
        'uz': 'Payment method updated successfully',
        'ru': 'Payment method updated successfully'
    },
    'telegram.subscription.resumed_success': {
        'en': 'Resumed success',
        'uz': 'Resumed success',
        'ru': 'Resumed success'
    },
    'telegram.subscription.select_address': {
        'en': 'Select address',
        'uz': 'Select address',
        'ru': 'Select address'
    },
    'telegram.subscription.select_at_least_one_item': {
        'en': 'Select at least one item',
        'uz': 'Select at least one item',
        'ru': 'Select at least one item'
    },
    'telegram.subscription.select_frequency': {
        'en': 'Select frequency',
        'uz': 'Select frequency',
        'ru': 'Select frequency'
    },
    'telegram.subscription.select_new_frequency': {
        'en': 'Select new frequency',
        'uz': 'Select new frequency',
        'ru': 'Select new frequency'
    },
    'telegram.subscription.select_new_payment_method': {
        'en': 'Select new payment method',
        'uz': 'Select new payment method',
        'ru': 'Select new payment method'
    },
    'telegram.subscription.select_payment': {
        'en': 'Select payment',
        'uz': 'Select payment',
        'ru': 'Select payment'
    },
    'telegram.subscription.skip_success': {
        'en': 'Skip success',
        'uz': 'Skip success',
        'ru': 'Skip success'
    },
    'telegram.subscription.status': {
        'en': 'Status',
        'uz': 'Status',
        'ru': 'Status'
    },
    'telegram.subscription.title': {
        'en': 'Title',
        'uz': 'Title',
        'ru': 'Title'
    },
    'telegram.subscription.total': {
        'en': 'Total',
        'uz': 'Total',
        'ru': 'Total'
    },
    'telegram.subscription.trial': {
        'en': 'Trial',
        'uz': 'Trial',
        'ru': 'Trial'
    },
    'telegram.subscription.view': {
        'en': 'View',
        'uz': 'View',
        'ru': 'View'
    },
    'telegram.total': {
        'en': 'Total',
        'uz': 'Total',
        'ru': 'Total'
    },
    'telegram.unknown_action': {
        'en': 'Unknown action',
        'uz': 'Unknown action',
        'ru': 'Unknown action'
    },
    'total_items': {
        'en': 'Total items',
        'uz': 'Total items',
        'ru': 'Total items'
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
