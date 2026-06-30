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
        'en': 'Aqua Element: Your registration code: {otp_code}. Valid for 3 minutes.',
        'uz': 'Aqua Element: Ro\'yxatdan o\'tish kodi: {otp_code}. Kod 3 daqiqa amal qiladi.',
        'ru': 'Aqua Element: Код регистрации: {otp_code}. Код действителен 3 минуты.'
    },
    'sms.welcome': {
        'en': 'Welcome to Aqua Element, {first_name}! Use our app to place orders.',
        'uz': 'Aqua Elementga xush kelibsiz, {first_name}! Buyurtma berish uchun ilovamizdan foydalaning.',
        'ru': 'Добро пожаловать в Aqua Element, {first_name}! Используйте наше приложение для заказов.'
    },
    'sms.verification.otp': {
        'en': 'Aqua Element: Your verification code: {otp_code}. Valid for 5 minutes.',
        'uz': 'Aqua Element: Tasdiqlash kodi: {otp_code}. Kod 5 daqiqa amal qiladi.',
        'ru': 'Aqua Element: Код подтверждения: {otp_code}. Код действителен 5 минут.'
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
    'api.orders.tax_committee_unavailable': {
        'en': 'Tax authority system (Asl belgisi) is temporarily unavailable. Please try again or choose cash payment.',
        'uz': '«Asl belgisi» tizimi hozirda mavjud emas. Iltimos, qayta urinib ko\'ring yoki naqd to\'lovni tanlang.',
        'ru': 'Система «Asl belgisi» временно недоступна. Попробуйте снова или выберите оплату наличными.'
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
    # ---- Admin order edit (ORDER_EDITED notification) ----
    'api.orders.edited.subject': {
        'en': 'Your order was updated',
        'uz': 'Buyurtmangiz yangilandi',
        'ru': 'Ваш заказ обновлён'
    },
    'api.orders.edited.body': {
        'en': 'Order {order_number} was updated by our team. New total: {order_total} UZS.',
        'uz': 'Buyurtma {order_number} bizning jamoamiz tomonidan yangilandi. Yangi jami: {order_total} so\'m.',
        'ru': 'Заказ {order_number} был обновлён нашей командой. Новая сумма: {order_total} сум.'
    },
    'api.orders.edited.body_with_reason': {
        'en': 'Order {order_number} was updated. Reason: {reason}. New total: {order_total} UZS.',
        'uz': 'Buyurtma {order_number} yangilandi. Sabab: {reason}. Yangi jami: {order_total} so\'m.',
        'ru': 'Заказ {order_number} был обновлён. Причина: {reason}. Новая сумма: {order_total} сум.'
    },
    'api.orders.edit_window_expired': {
        'en': 'Order edit window has expired',
        'uz': 'Buyurtmani tahrirlash muddati tugagan',
        'ru': 'Срок редактирования заказа истёк'
    },
    'api.orders.card_paid_decrease_creates_prepayment': {
        'en': 'Card-paid order decrease: card will NOT be refunded; the reduced amount becomes customer prepayment credit (usable on future cash orders only)',
        'uz': 'Karta bilan to\'langan buyurtmani kamaytirish: karta qaytarilmaydi; kamaytirilgan miqdor mijozning oldindan to\'lov kreditiga aylanadi (faqat keyingi naqd buyurtmalar uchun)',
        'ru': 'Уменьшение заказа, оплаченного картой: возврат на карту НЕ производится; сумма становится предоплатой клиента (можно использовать только на будущих заказах с наличной оплатой)'
    },
    'api.orders.card_paid_increase_requires_cash': {
        'en': 'Card-paid order increase: the additional amount must be collected in CASH via Personal Card Payment (card will not be re-charged)',
        'uz': 'Karta bilan to\'langan buyurtmani oshirish: qo\'shimcha miqdor NAQD pulda yig\'ilishi kerak (karta qayta yechilmaydi)',
        'ru': 'Увеличение заказа, оплаченного картой: дополнительная сумма должна быть собрана НАЛИЧНЫМИ (карта повторно не списывается)'
    },
    'api.orders.marking_codes_preserved': {
        'en': 'Marking codes will be preserved; the value of the removed quantity goes to customer prepayment',
        'uz': 'Markirovka kodlari saqlanib qoladi; olib tashlangan miqdorning qiymati mijozning oldindan to\'lov hisobiga o\'tadi',
        'ru': 'Маркировочные коды сохраняются; стоимость убранного количества зачисляется в предоплату клиента'
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
    'api.orders.error.business_account_grocery_disallowed': {
        'en': 'Business Account payment is not available for grocery store accounts.',
        'uz': 'Business Account to\'lov usuli oziq-ovqat do\'kon hisoblari uchun mavjud emas.',
        'ru': 'Способ оплаты Business Account недоступен для счетов продуктовых магазинов.'
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
    'api.addresses.error.in_use_by_subscription': {
        'en': 'This address is currently used by an active subscription and cannot be deleted.',
        'uz': 'Bu manzil hozir faol obuna tomonidan ishlatilmoqda va o\'chirib bo\'lmaydi.',
        'ru': 'Этот адрес используется активной подпиской и не может быть удалён.'
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
    'api.delivery.arrived': {
        'en': 'Order has arrived',
        'uz': 'Buyurtma yetib keldi',
        'ru': 'Заказ прибыл'
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
    'staff.notification.cash_session_reopened': {
        'en': 'Your cash session #{session_id} was reopened by admin because the collected cash for order #{order_id} was corrected. Please re-submit when ready.',
        'uz': 'Kassa sessiyangiz #{session_id} administrator tomonidan qayta ochildi: #{order_id} buyurtma bo\'yicha yig\'ilgan summa tuzatildi. Iltimos, qayta yuboring.',
        'ru': 'Ваша кассовая сессия #{session_id} переоткрыта администратором: собранная сумма по заказу #{order_id} скорректирована. Пожалуйста, отправьте повторно.',
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
        'en': 'AquaCoins earned',
        'uz': 'AquaCoins hisobingizga qo‘shildi',
        'ru': 'AquaCoins начислены'
    },
    'api.loyalty.points_redeemed': {
        'en': 'AquaCoins redeemed successfully',
        'uz': 'AquaCoins muvaffaqiyatli ishlatildi',
        'ru': 'AquaCoins успешно списаны'
    },
    'api.loyalty.insufficient_points': {
        'en': 'Not enough AquaCoins',
        'uz': 'AquaCoins yetarli emas',
        'ru': 'Недостаточно AquaCoins'
    },
    'api.loyalty.reward_redeemed_successfully': {
        'en': 'Reward redeemed successfully',
        'uz': 'Mukofot muvaffaqiyatli olindi',
        'ru': 'Награда успешно получена'
    },
    'api.loyalty.points_awarded_successfully': {
        'en': 'AquaCoins awarded successfully',
        'uz': 'AquaCoins muvaffaqiyatli hisobga qo\'shildi',
        'ru': 'AquaCoins успешно начислены'
    },
    'api.loyalty.points_gifted_successfully': {
        'en': 'AquaCoins gifted successfully',
        'uz': 'AquaCoins muvaffaqiyatli sovg\'a qilindi',
        'ru': 'AquaCoins успешно подарены'
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
        'en': 'Insufficient AquaCoins balance',
        'uz': 'AquaCoins balansi yetarli emas',
        'ru': 'Недостаточно AquaCoins на балансе'
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
        'en': 'AquaCoins amount must be positive',
        'uz': 'AquaCoins miqdori musbat bo\'lishi kerak',
        'ru': 'Количество AquaCoins должно быть положительным'
    },
    'api.loyalty.error.cannot_gift_to_self': {
        'en': 'Cannot gift AquaCoins to yourself',
        'uz': 'O\'zingizga AquaCoins sovg\'a qilib bo\'lmaydi',
        'ru': 'Нельзя подарить AquaCoins самому себе'
    },
    'api.loyalty.error.get_membership_tiers_failed': {
        'en': 'Failed to get membership tiers',
        'uz': 'A\'zolik darajalarini olishda xatolik',
        'ru': 'Не удалось получить уровни членства'
    },
    'api.loyalty.error.get_points_failed': {
        'en': 'Failed to retrieve AquaCoins information',
        'uz': 'AquaCoins ma\'lumotlarini olishda xatolik',
        'ru': 'Не удалось получить информацию об AquaCoins'
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
        'en': 'Failed to get AquaCoins history',
        'uz': 'AquaCoins tarixini olishda xatolik',
        'ru': 'Не удалось получить историю AquaCoins'
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
        'en': 'Failed to award AquaCoins',
        'uz': 'AquaCoins hisobga qo‘shishda xatolik',
        'ru': 'Не удалось начислить AquaCoins'
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
        'en': 'Failed to gift AquaCoins',
        'uz': 'AquaCoins sovg\'a qilishda xatolik',
        'ru': 'Не удалось подарить AquaCoins'
    },
    'api.loyalty.error.not_eligible': {
        'en': 'The loyalty program is not available for your account.',
        'uz': 'Sodiqlik dasturi hisobingiz uchun mavjud emas.',
        'ru': 'Программа лояльности недоступна для вашего аккаунта.'
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
        'en': 'AquaCoins Earned',
        'uz': 'Hisoblangan AquaCoinlar',
        'ru': 'Начисленные AquaCoins'
    },
    'ui.analytics.loyalty_points_redeemed': {
        'en': 'AquaCoins Redeemed',
        'uz': 'Ishlatilgan AquaCoins',
        'ru': 'Использованные AquaCoins'
    },
    'ui.analytics.loyalty_points_trend': {
        'en': 'AquaCoins Trend',
        'uz': 'AquaCoins dinamikasi',
        'ru': 'Динамика AquaCoins'
    },
    'ui.analytics.members': {
        'en': 'Members',
        'uz': 'A\'zolar',
        'ru': 'Участники'
    },
    'ui.analytics.points': {
        'en': 'AquaCoins',
        'uz': 'AquaCoinlar',
        'ru': 'AquaCoins'
    },
    'ui.analytics.points_in_circulation': {
        'en': 'AquaCoins In Circulation',
        'uz': 'Muomaladagi AquaCoinlar',
        'ru': 'AquaCoins в обороте'
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
    'ui.analytics.customer_type': {
        'en': 'Customer Type',
        'uz': 'Mijoz turi',
        'ru': 'Тип клиента'
    },
    'ui.analytics.days_since_last_order': {
        'en': 'Days Since Last Order',
        'uz': 'Oxirgi buyurtmadan beri kunlar',
        'ru': 'Дней с последнего заказа'
    },
    'ui.analytics.days_threshold': {
        'en': 'Days Threshold',
        'uz': 'Kunlar chegarasi',
        'ru': 'Порог в днях'
    },
    'ui.analytics.inactive_customers': {
        'en': 'Inactive Customers',
        'uz': 'Faol bo\'lmagan mijozlar',
        'ru': 'Неактивные клиенты'
    },
    'ui.analytics.inactive_customers_list': {
        'en': 'Inactive Customers List',
        'uz': 'Faol bo\'lmagan mijozlar ro\'yxati',
        'ru': 'Список неактивных клиентов'
    },
    'ui.analytics.inactive_total': {
        'en': 'Total Inactive',
        'uz': 'Jami faol bo\'lmaganlar',
        'ru': 'Всего неактивных'
    },
    'ui.analytics.include_never_ordered': {
        'en': 'Include Never-Ordered',
        'uz': 'Hech qachon buyurtma qilmaganlarni qo\'shish',
        'ru': 'Включая никогда не заказывавших'
    },
    'ui.analytics.last_order': {
        'en': 'Last Order',
        'uz': 'Oxirgi buyurtma',
        'ru': 'Последний заказ'
    },
    'ui.analytics.name': {
        'en': 'Name',
        'uz': 'Ismi',
        'ru': 'Имя'
    },
    'ui.analytics.never': {
        'en': 'Never',
        'uz': 'Hech qachon',
        'ru': 'Никогда'
    },
    'ui.analytics.phone': {
        'en': 'Phone',
        'uz': 'Telefon',
        'ru': 'Телефон'
    },
    'ui.analytics.region': {
        'en': 'Region',
        'uz': 'Hudud',
        'ru': 'Регион'
    },
    'ui.analytics.total_orders': {
        'en': 'Total Orders',
        'uz': 'Jami buyurtmalar',
        'ru': 'Всего заказов'
    },
    'ui.analytics.total_spent': {
        'en': 'Total Spent',
        'uz': 'Jami sarflangan',
        'ru': 'Всего потрачено'
    },
    'ui.analytics.type_all': {
        'en': 'All',
        'uz': 'Hammasi',
        'ru': 'Все'
    },
    'ui.analytics.type_grocery': {
        'en': 'Grocery',
        'uz': 'Do\'kon',
        'ru': 'Магазин'
    },
    'ui.analytics.type_individual': {
        'en': 'Individual',
        'uz': 'Jismoniy shaxs',
        'ru': 'Физическое лицо'
    },
    'ui.analytics.type_workplace': {
        'en': 'Workplace',
        'uz': 'Ish joyi',
        'ru': 'Рабочее место'
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
        'en': 'Average AquaCoins per Member',
        'uz': 'Bir a\'zoga o\'rtacha AquaCoins',
        'ru': 'Среднее число AquaCoins на участника'
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
        'en': 'Current AquaCoins',
        'uz': 'Joriy AquaCoins',
        'ru': 'Текущие AquaCoins'
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
    'ui.loyalty.free_product': {
        'en': 'Free Product',
        'uz': 'Bepul mahsulot',
        'ru': 'Бесплатный товар'
    },
    'ui.loyalty.free_product_id': {
        'en': 'Free Product',
        'uz': 'Bepul mahsulot',
        'ru': 'Бесплатный товар'
    },
    'ui.loyalty.free_product_quantity': {
        'en': 'Quantity',
        'uz': 'Miqdori',
        'ru': 'Количество'
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
        'en': 'Maximum AquaCoins',
        'uz': 'Maksimal AquaCoins',
        'ru': 'Максимум AquaCoins'
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
        'en': 'Minimum AquaCoins',
        'uz': 'Minimal AquaCoins',
        'ru': 'Минимум AquaCoins'
    },
    'ui.loyalty.min_redemption_points': {
        'en': 'Minimum Redemption AquaCoins',
        'uz': 'Minimal yechib olish AquaCoins',
        'ru': 'Минимум AquaCoins для списания'
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
        'en': 'AquaCoins Cost',
        'uz': 'AquaCoins qiymati',
        'ru': 'Стоимость в AquaCoins'
    },
    'ui.loyalty.points_distributed': {
        'en': 'AquaCoins In Circulation',
        'uz': 'Muomaladagi AquaCoinlar',
        'ru': 'AquaCoins в обороте'
    },
    'ui.loyalty.points_expiry_days': {
        'en': 'AquaCoins Expiry Days',
        'uz': 'AquaCoinlar amal qilish muddati',
        'ru': 'Срок действия AquaCoins в днях'
    },
    'ui.loyalty.points_range': {
        'en': 'AquaCoins Range',
        'uz': 'AquaCoins oralig\'i',
        'ru': 'Диапазон AquaCoins'
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
    'ui.loyalty.transactions': {
        'en': 'Transactions',
        'uz': 'Tranzaksiyalar',
        'ru': 'Транзакции'
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
        'en': 'UZS per AquaCoin',
        'uz': 'Bir AquaCoin uchun UZS',
        'ru': 'UZS за AquaCoin'
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
    'ui.users.entity_subtype': {
        'en': 'Entity Subtype',
        'uz': 'Yuridik shaxs kichik turi',
        'ru': 'Подтип юр. лица'
    },
    'ui.users.select_entity_subtype': {
        'en': 'Select subtype',
        'uz': 'Kichik turini tanlang',
        'ru': 'Выберите подтип'
    },
    'ui.users.entity_subtype_workplace': {
        'en': 'Workplace',
        'uz': 'Ish joyi',
        'ru': 'Рабочее место'
    },
    'ui.users.entity_subtype_grocery_store': {
        'en': 'Grocery Store',
        'uz': 'Oziq-ovqat do\'koni',
        'ru': 'Продуктовый магазин'
    },
    'ui.users.entity_subtype_required': {
        'en': 'Entity subtype is required for new entity users',
        'uz': 'Yangi yuridik foydalanuvchilar uchun kichik tur majburiy',
        'ru': 'Для новых юридических пользователей подтип обязателен'
    },
    'ui.users.entity_subtype_unassigned': {
        'en': 'Subtype unassigned',
        'uz': 'Kichik tur tayinlanmagan',
        'ru': 'Подтип не назначен'
    },
    'ui.users.entity_subtype_unassigned_note': {
        'en': 'This customer cannot place orders until you assign a subtype.',
        'uz': 'Bu mijoz kichik turi tayinlanmaguncha buyurtma bera olmaydi.',
        'ru': 'Этот клиент не может оформлять заказы, пока не назначен подтип.'
    },
    'ui.users.entity_subtype_workplace_hint': {
        'en': 'Workplaces prepay via Business Account; debt is tracked per product in bottle units.',
        'uz': 'Ish joylari Business Account orqali oldindan to\'laydi; qarz har bir mahsulot uchun shisha sonida hisoblanadi.',
        'ru': 'Рабочие места предоплачивают через Business Account; долг учитывается по каждому товару в бутылках.'
    },
    'ui.users.entity_subtype_grocery_hint': {
        'en': 'Grocery stores pay cash/card on or after delivery; debt is tracked in money. Business Account is unavailable.',
        'uz': 'Oziq-ovqat do\'konlari yetkazib berishda yoki keyinroq naqd/karta bilan to\'laydi; qarz pul birligida hisoblanadi. Business Account mavjud emas.',
        'ru': 'Продуктовые магазины платят наличными/картой при доставке или позже; долг учитывается в деньгах. Business Account недоступен.'
    },
    'ui.corporate.tracking_mode': {
        'en': 'Tracking Mode',
        'uz': 'Hisoblash rejimi',
        'ru': 'Режим учёта'
    },
    'ui.corporate.tracking_mode_units': {
        'en': 'Units (Workplace)',
        'uz': 'Shisha soni (Ish joyi)',
        'ru': 'В бутылках (Рабочее место)'
    },
    'ui.corporate.tracking_mode_amount': {
        'en': 'Money (Grocery Store)',
        'uz': 'Pul (Oziq-ovqat do\'koni)',
        'ru': 'Деньги (Продуктовый магазин)'
    },
    'ui.corporate.outstanding_amount': {
        'en': 'Outstanding (debt)',
        'uz': 'Qoldiq qarz',
        'ru': 'Текущий долг'
    },
    'ui.corporate.lifetime_charged': {
        'en': 'Lifetime Charged',
        'uz': 'Jami hisoblangan',
        'ru': 'Всего начислено'
    },
    'ui.corporate.lifetime_collected': {
        'en': 'Lifetime Collected',
        'uz': 'Jami yig\'ilgan',
        'ru': 'Всего собрано'
    },
    'ui.corporate.last_charged': {
        'en': 'Last Charged',
        'uz': 'Oxirgi hisoblash',
        'ru': 'Последнее начисление'
    },
    'ui.corporate.last_collected': {
        'en': 'Last Collected',
        'uz': 'Oxirgi yig\'ilish',
        'ru': 'Последнее поступление'
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
    'api.notifications.success.user_notification_settings_updated': {
        'en': 'User notification settings updated successfully',
        'uz': 'Foydalanuvchi bildirishnoma sozlamalari muvaffaqiyatli yangilandi',
        'ru': 'Настройки уведомлений пользователя успешно обновлены'
    },
    'api.notifications.validation.delivery_telegram_toggle_boolean': {
        'en': 'delivery_telegram_status_updates_enabled must be a boolean',
        'uz': 'delivery_telegram_status_updates_enabled mantiqiy qiymat (boolean) bo\'lishi kerak',
        'ru': 'delivery_telegram_status_updates_enabled должен быть булевым значением'
    },
    'api.notifications.validation.reason_required': {
        'en': 'Reason is required',
        'uz': 'Sabab ko\'rsatish majburiy',
        'ru': 'Необходимо указать причину'
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
    'error.configuration.email_not_configured': {
        'en': 'Email service is not configured.',
        'uz': 'Email xizmati sozlanmagan.',
        'ru': 'Email-сервис не настроен.'
    },
    'error.validation.no_telegram_id': {
        'en': 'User has no Telegram account linked.',
        'uz': 'Foydalanuvchining Telegram profili ulanmagan.',
        'ru': 'У пользователя нет привязанного аккаунта Telegram.'
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
    'ui.users.notification_settings': {
        'en': 'Notification Settings',
        'uz': 'Bildirishnoma sozlamalari',
        'ru': 'Настройки уведомлений'
    },
    'ui.users.cod_debt_check_exempt': {
        'en': 'Exempt from COD debt limit',
        'uz': 'COD qarz cheklovidan ozod',
        'ru': 'Освобожден от лимита долгов по наложенному платежу'
    },
    'ui.users.cod_debt_check_exempt_tooltip': {
        'en': 'When enabled, this user can always order with cash on delivery regardless of outstanding COD debts. Use sparingly — bypasses financial safeguards. Every toggle is audited.',
        'uz': 'Yoqilganda, bu foydalanuvchi to\'lanmagan COD qarzlardan qat\'i nazar har doim yetkazib berishda naqd pul bilan buyurtma bera oladi. Ehtiyot bo\'lib foydalaning — moliyaviy himoyani chetlab o\'tadi. Har bir o\'zgartirish auditga yoziladi.',
        'ru': 'Если включено, этот пользователь всегда может оформить заказ с оплатой при доставке независимо от непогашенных долгов. Используйте осторожно — обходит финансовые ограничения. Каждое изменение фиксируется в аудите.'
    },
    'ui.users.cod_debt_check_exempt_extra': {
        'en': 'Reserved for trusted customers (close partners, relatives).',
        'uz': 'Ishonchli mijozlar uchun (yaqin hamkorlar, qarindoshlar).',
        'ru': 'Только для доверенных клиентов (близкие партнёры, родственники).'
    },
    'ui.users.delivery_telegram_updates_setting': {
        'en': 'Telegram delivery updates (in transit, arrived)',
        'uz': 'Telegram orqali yetkazib berish yangilanishlari (yo\'lda, yetib bordi)',
        'ru': 'Обновления доставки в Telegram (в пути, прибыл)'
    },
    'ui.users.delivery_telegram_updates_setting_help': {
        'en': 'Controls Telegram notifications for delivery status changes.',
        'uz': 'Yetkazib berish holati o\'zgarganda Telegram bildirishnomalarini boshqaradi.',
        'ru': 'Управляет Telegram-уведомлениями об изменениях статуса доставки.'
    },
    'ui.users.notification_status_enabled': {
        'en': 'Enabled',
        'uz': 'Yoqilgan',
        'ru': 'Включено'
    },
    'ui.users.notification_status_disabled': {
        'en': 'Disabled',
        'uz': 'O\'chirilgan',
        'ru': 'Отключено'
    },
    'ui.users.notification_source_explicit': {
        'en': 'Explicit',
        'uz': 'Aniq sozlangan',
        'ru': 'Явно задано'
    },
    'ui.users.notification_source_default': {
        'en': 'Default',
        'uz': 'Standart',
        'ru': 'По умолчанию'
    },
    'ui.users.telegram_connected': {
        'en': 'Telegram connected',
        'uz': 'Telegram ulangan',
        'ru': 'Telegram подключен'
    },
    'ui.users.telegram_not_connected': {
        'en': 'Telegram not connected',
        'uz': 'Telegram ulanmagan',
        'ru': 'Telegram не подключен'
    },
    'ui.users.bot_active': {
        'en': 'Bot active',
        'uz': 'Bot faol',
        'ru': 'Бот активен'
    },
    'ui.users.bot_inactive': {
        'en': 'Bot inactive',
        'uz': 'Bot nofaol',
        'ru': 'Бот неактивен'
    },
    'ui.users.notification_change_reason_title': {
        'en': 'Confirm Notification Setting Change',
        'uz': 'Bildirishnoma sozlamasini o\'zgartirishni tasdiqlang',
        'ru': 'Подтвердите изменение настройки уведомлений'
    },
    'ui.users.notification_change_reason_prompt_enable': {
        'en': 'Please provide a reason for enabling Telegram delivery updates.',
        'uz': 'Telegram yetkazib berish yangilanishlarini yoqish sababini kiriting.',
        'ru': 'Укажите причину включения Telegram-уведомлений о доставке.'
    },
    'ui.users.notification_change_reason_prompt_disable': {
        'en': 'Please provide a reason for disabling Telegram delivery updates.',
        'uz': 'Telegram yetkazib berish yangilanishlarini o\'chirish sababini kiriting.',
        'ru': 'Укажите причину отключения Telegram-уведомлений о доставке.'
    },
    'ui.users.notification_change_reason_placeholder': {
        'en': 'Enter reason',
        'uz': 'Sababni kiriting',
        'ru': 'Введите причину'
    },
    'ui.users.notification_change_reason_required': {
        'en': 'Reason is required',
        'uz': 'Sabab ko\'rsatish majburiy',
        'ru': 'Необходимо указать причину'
    },
    'ui.users.notification_settings_updated': {
        'en': 'Notification settings updated',
        'uz': 'Bildirishnoma sozlamalari yangilandi',
        'ru': 'Настройки уведомлений обновлены'
    },
    'ui.users.notification_settings_update_failed': {
        'en': 'Failed to update notification settings',
        'uz': 'Bildirishnoma sozlamalarini yangilab bo\'lmadi',
        'ru': 'Не удалось обновить настройки уведомлений'
    },
    'ui.users.notification_settings_load_failed': {
        'en': 'Failed to load notification settings',
        'uz': 'Bildirishnoma sozlamalarini yuklab bo\'lmadi',
        'ru': 'Не удалось загрузить настройки уведомлений'
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
    'telegram.loyalty.history_page_info': {
        'en': 'Page {page} of {pages}',
        'uz': 'Sahifa {page}/{pages}',
        'ru': 'Страница {page} из {pages}'
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
        'en': '✅ Confirm',
        'uz': '✅ Tasdiqlash',
        'ru': '✅ Подтвердить'
    },
    'telegram.order.edit': {
        'en': '✏️ Edit',
        'uz': '✏️ Tahrirlash',
        'ru': '✏️ Изменить'
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
    'telegram.orders.preparing_payment_message': {
        'en': (
            '⏳ Your order #{order_number} has been received!\n\n'
            'We are registering your items with the tax authority (Asl belgisi) — '
            'this is a mandatory step that takes exactly 1 minute.\n\n'
            'Please wait. Your payment link will arrive automatically. Do not close this chat.'
        ),
        'uz': (
            '⏳ #{order_number}-sonli buyurtmangiz qabul qilindi!\n\n'
            'Mahsulotlaringizni soliq tizimida (Asl belgisi) ro\'yxatdan o\'tkazmoqdamiz — '
            'bu majburiy jarayon, atigi 1 daqiqa davom etadi.\n\n'
            'Iltimos, kuting. To\'lov havolasi avtomatik yuboriladi. Chatni yopmang.'
        ),
        'ru': (
            '⏳ Ваш заказ №{order_number} принят!\n\n'
            'Мы регистрируем ваши товары в системе Asl belgisi — '
            'это обязательный процесс, который занимает ровно 1 минуту.\n\n'
            'Пожалуйста, подождите. Ссылка для оплаты придёт автоматически. Не закрывайте чат.'
        ),
    },
    'telegram.orders.payment_link_ready_notice': {
        'en': '✅ Order #{order_number} is ready — payment link sent below.',
        'uz': '✅ #{order_number}-sonli buyurtma tayyor — to\'lov havolasi quyida yuborildi.',
        'ru': '✅ Заказ №{order_number} готов — ссылка для оплаты отправлена ниже.',
    },
    'telegram.orders.asl_belgisi_error_message': {
        'en': (
            '⚠️ The Asl belgisi (tax authority) system is temporarily unavailable.\n\n'
            'We could not prepare your payment link. What would you like to do?'
        ),
        'uz': (
            '⚠️ Hozirda «Asl belgisi» tizimida texnik nosozlik mavjud.\n\n'
            'To\'lov havolasini yaratib bo\'lmayapti. Nima qilmoqchisiz?'
        ),
        'ru': (
            '⚠️ Система «Asl belgisi» временно недоступна.\n\n'
            'Не удаётся создать ссылку для оплаты. Выберите действие:'
        ),
    },
    'telegram.orders.asl_belgisi_switch_cash': {
        'en': '💵 Pay with cash',
        'uz': '💵 Naqd pul bilan to\'lash',
        'ru': '💵 Оплатить наличными',
    },
    'telegram.orders.asl_belgisi_retry': {
        'en': '🔄 Try again',
        'uz': '🔄 Qayta urinish',
        'ru': '🔄 Попробовать снова',
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
        'en': '📞 Contact support: @aqua_element_support',
        'uz': '📞 Yordam bilan bog\'lanish: @aqua_element_support',
        'ru': '📞 Связаться с поддержкой: @aqua_element_support'
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
        'ru': 'Эл. почта'
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
    'telegram.address.outside_delivery_area': {
        'en': '⚠️ This location is outside our delivery area (Tashkent). Please share a location within the service area.',
        'uz': '⚠️ Bu joylashuv yetkazib berish hududimizdan (Toshkent) tashqarida. Iltimos, xizmat ko\'rsatish hududidagi joylashuvni yuboring.',
        'ru': '⚠️ Это местоположение находится вне нашей зоны доставки (Ташкент). Пожалуйста, отправьте местоположение в пределах зоны обслуживания.'
    },
    # ─── Public delivery-coverage page (/coverage) ───────────────────────
    'landing.nav.coverage': {
        'en': 'Coverage', 'uz': 'Hududlar', 'ru': 'Зона доставки'
    },
    'landing.nav.aqua_club': {
        'en': 'Aqua Club', 'uz': 'Aqua Club', 'ru': 'Aqua Club'
    },
    'landing.nav.aqua_club_badge': {
        'en': 'Rewards', 'uz': 'Bonus', 'ru': 'Бонусы'
    },
    'landing.footer.aqua_club': {
        'en': 'Aqua Club rewards', 'uz': 'Aqua Club dasturi', 'ru': 'Программа Aqua Club'
    },
    'landing.coverage.title': {
        'en': 'Where we deliver', 'uz': 'Biz qayerga yetkazib beramiz', 'ru': 'Куда мы доставляем'
    },
    'landing.coverage.checker_title': {
        'en': 'Check your address', 'uz': 'Manzilingizni tekshiring', 'ru': 'Проверьте свой адрес'
    },
    'landing.coverage.checker_hint': {
        'en': 'Type your address or drop a pin on the map to confirm we deliver to you.',
        'uz': "Yetkazib berishni tasdiqlash uchun manzilingizni kiriting yoki xaritada belgilang.",
        'ru': 'Введите адрес или поставьте метку на карте, чтобы подтвердить доставку.'
    },
    'landing.coverage.districts_label': {
        'en': 'Districts we cover', 'uz': 'Biz qamrab olgan tumanlar', 'ru': 'Районы, которые мы обслуживаем'
    },
    'landing.coverage.address_placeholder': {
        'en': "e.g. Amir Temur ko'chasi 12, Tashkent",
        'uz': "masalan, Amir Temur ko'chasi 12, Toshkent",
        'ru': 'напр. улица Амира Темура 12, Ташкент'
    },
    'landing.coverage.check_btn': {
        'en': 'Check', 'uz': 'Tekshirish', 'ru': 'Проверить'
    },
    'landing.coverage.use_location': {
        'en': 'Use my location', 'uz': 'Joylashuvimdan foydalanish', 'ru': 'Использовать моё местоположение'
    },
    'landing.coverage.result_ok': {
        'en': 'Good news — we deliver to this location.',
        'uz': 'Xush xabar — biz bu joyga yetkazib beramiz.',
        'ru': 'Хорошая новость — мы доставляем по этому адресу.'
    },
    'landing.coverage.result_no': {
        'en': "Sorry — that's outside our delivery area.",
        'uz': "Kechirasiz — bu bizning yetkazib berish hududimizdan tashqarida.",
        'ru': 'К сожалению, это вне нашей зоны доставки.'
    },
    'landing.coverage.home_title': {
        'en': 'Delivering across Tashkent', 'uz': 'Toshkent bo\'ylab yetkazib beramiz', 'ru': 'Доставляем по Ташкенту'
    },
    'landing.coverage.home_text': {
        'en': 'All of Tashkent city plus neighbouring areas of the Tashkent Region.',
        'uz': "Butun Toshkent shahri va Toshkent viloyatining qo'shni hududlari.",
        'ru': 'Весь город Ташкент и прилегающие районы Ташкентской области.'
    },
    'landing.coverage.home_cta': {
        'en': 'Check your address', 'uz': 'Manzilingizni tekshiring', 'ru': 'Проверьте свой адрес'
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
    'telegram.profile.edit_field_name': {'en': '👤 Name', 'uz': '👤 Ism', 'ru': '👤 Имя'},
    'telegram.profile.edit_field_birthday': {'en': '🎂 Birthday', 'uz': "🎂 Tug'ilgan kun", 'ru': '🎂 День рождения'},
    'telegram.profile.edit_field_language': {'en': '🌐 Language', 'uz': '🌐 Til', 'ru': '🌐 Язык'},
    'telegram.profile.edit_field_phone': {'en': '📱 Phone', 'uz': '📱 Telefon', 'ru': '📱 Телефон'},
    'telegram.profile.edit_menu_title': {'en': 'What would you like to edit?', 'uz': "Qaysi ma'lumotni o'zgartirmoqchisiz?", 'ru': 'Что вы хотите изменить?'},
    'telegram.profile.name_prompt': {'en': 'Enter your new first and last name:', 'uz': 'Yangi ism va familiyangizni kiriting:', 'ru': 'Введите новое имя и фамилию:'},
    'telegram.profile.name_updated': {'en': '✅ Name updated', 'uz': '✅ Ism yangilandi', 'ru': '✅ Имя обновлено'},
    'telegram.profile_birthday': {'en': '🎂 Birthday', 'uz': "🎂 Tug'ilgan kun", 'ru': '🎂 День рождения'},
    'telegram.profile.birthday_prompt': {
        'en': "Enter your birthday in DD-MM-YYYY format\n\nExample: 17-05-1990",
        'uz': "Tug'ilgan kuningizni DD-MM-YYYY formatida kiriting\n\nMasalan: 17-05-1990",
        'ru': "Введите дату рождения в формате DD-MM-YYYY\n\nПример: 17-05-1990",
    },
    'telegram.profile.birthday_invalid_format': {
        'en': "Invalid format. Please enter your birthday as DD-MM-YYYY\n\nExample: 17-05-1990",
        'uz': "Noto'g'ri format. Tug'ilgan kuningizni DD-MM-YYYY formatida kiriting\n\nMasalan: 17-05-1990",
        'ru': "Неверный формат. Введите дату рождения в формате DD-MM-YYYY\n\nПример: 17-05-1990",
    },
    'telegram.profile.birthday_updated': {'en': '✅ Birthday updated', 'uz': "✅ Tug'ilgan kun yangilandi", 'ru': '✅ День рождения обновлён'},
    'telegram.profile.birthday_update_failed': {'en': '❌ Could not update birthday', 'uz': "❌ Tug'ilgan kunni yangilab bo'lmadi", 'ru': '❌ Не удалось обновить день рождения'},
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
    'telegram.notifications.title': {
        'en': '🔔 Notification Settings',
        'uz': '🔔 Bildirishnoma sozlamalari',
        'ru': '🔔 Настройки уведомлений'
    },
    'telegram.notifications.delivery_status_updates_label': {
        'en': 'Telegram delivery status updates',
        'uz': 'Telegram orqali yetkazib berish holati yangilanishlari',
        'ru': 'Обновления статуса доставки в Telegram'
    },
    'telegram.notifications.delivery_status_updates_description': {
        'en': 'Receive Telegram notifications for "in transit" and "arrived" statuses.',
        'uz': '"Yo\'lda" va "yetib bordi" holatlari uchun Telegram bildirishnomalarini qabul qilish.',
        'ru': 'Получать Telegram-уведомления для статусов «в пути» и «прибыл».'
    },
    'telegram.notifications.current_status_enabled': {
        'en': 'Status: ✅ Enabled',
        'uz': 'Holat: ✅ Yoqilgan',
        'ru': 'Статус: ✅ Включено'
    },
    'telegram.notifications.current_status_disabled': {
        'en': 'Status: ❌ Disabled',
        'uz': 'Holat: ❌ O\'chirilgan',
        'ru': 'Статус: ❌ Отключено'
    },
    'telegram.notifications.toggle_disable_button': {
        'en': 'Turn Off',
        'uz': 'O\'chirish',
        'ru': 'Выключить'
    },
    'telegram.notifications.toggle_enable_button': {
        'en': 'Turn On',
        'uz': 'Yoqish',
        'ru': 'Включить'
    },
    'telegram.notifications.update_success': {
        'en': 'Notification setting updated',
        'uz': 'Bildirishnoma sozlamasi yangilandi',
        'ru': 'Настройка уведомлений обновлена'
    },
    'telegram.notifications.update_failed': {
        'en': 'Failed to update notification setting',
        'uz': 'Bildirishnoma sozlamasini yangilab bo\'lmadi',
        'ru': 'Не удалось обновить настройку уведомлений'
    },
    'telegram.profile.payment_methods': {
        'en': '💳 Payment Methods',
        'uz': '💳 To\'lov usullari',
        'ru': '💳 Способы оплаты'
    },
    'telegram.profile.my_bottles': {
        'en': '📦 My Bottles',
        'uz': '📦 Mening idishlarim',
        'ru': '📦 Моя тара'
    },
    'telegram.bottles.title': {
        'en': 'My Bottle Balance',
        'uz': 'Mening idish balansim',
        'ru': 'Мой баланс тары'
    },
    'telegram.bottles.no_balance': {
        'en': 'You have no returnable bottles on record.',
        'uz': 'Sizda qaytariladigan idish topilmadi.',
        'ru': 'У вас нет возвратной тары на балансе.'
    },
    'telegram.bottles.total': {
        'en': 'Total bottles',
        'uz': 'Jami idishlar',
        'ru': 'Всего тара'
    },
    'telegram.bottles.load_error': {
        'en': 'Could not load bottle balance. Please try again.',
        'uz': 'Idish balansini yuklab bo\'lmadi. Qayta urinib ko\'ring.',
        'ru': 'Не удалось загрузить баланс тары. Попробуйте снова.'
    },
    'telegram.profile.share_phone': {
        'en': '📱 Share Phone Number',
        'uz': '📱 Telefon raqamini ulashish',
        'ru': '📱 Поделиться номером'
    },
    'telegram.menu.products': {
        'en': '💧 Order Water',
        'uz': '💧 Suv buyurtma berish',
        'ru': '💧 Заказать воду'
    },
    'telegram.menu.orders': {
        'en': '📦 My Orders',
        'uz': '📦 Buyurtmalarim',
        'ru': '📦 Мои заказы'
    },
    'telegram.cart_title': {
        'en': '🛒 My Cart',
        'uz': '🛒 Savatim',
        'ru': '🛒 Моя корзина'
    },
    'telegram.menu.subscriptions': {
        'en': '🔄 Auto-Delivery',
        'uz': '🔄 Avto-yetkazib berish',
        'ru': '🔄 Автодоставка'
    },
    'telegram.menu.loyalty': {
        'en': '🎁 Aqua Club',
        'uz': '🎁 Aqua Club',
        'ru': '🎁 Aqua Club'
    },
    'telegram.menu.profile': {
        'en': '👤 My Profile',
        'uz': '👤 Profilim',
        'ru': '👤 Мой профиль'
    },
    'telegram.menu.support': {
        'en': '🆘 Get Help',
        'uz': '🆘 Yordam',
        'ru': '🆘 Помощь'
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
    # Cart edit-mode controls (Deliverable B — full cart editing)
    'telegram.cart.remove': {
        'en': '🗑 Remove',
        'uz': '🗑 O\'chirish',
        'ru': '🗑 Удалить'
    },
    'telegram.cart.add_product': {
        'en': '➕ Add product',
        'uz': '➕ Mahsulot qo\'shish',
        'ru': '➕ Добавить товар'
    },
    'telegram.cart.back_to_cart': {
        'en': '🛒 Back to cart',
        'uz': '🛒 Savatchaga qaytish',
        'ru': '🛒 Назад в корзину'
    },
    'telegram.cart.done': {
        'en': '✅ Done',
        'uz': '✅ Tayyor',
        'ru': '✅ Готово'
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
    # Telegram Quick Order (telegram.quick_order.*)
    # ============================================================================
    'telegram.quick_order.repeat_last': {
        'en': '🔁 Repeat last: {qty}× {product}',
        'uz': '🔁 Oxirgisini takrorlash: {qty}× {product}',
        'ru': '🔁 Повторить последний: {qty}× {product}'
    },
    'telegram.quick_order.repeat_last_multi': {
        'en': '🔁 Repeat last order ({n} items)',
        'uz': '🔁 Oxirgi buyurtmani takrorlash ({n} ta mahsulot)',
        'ru': '🔁 Повторить последний заказ ({n} товаров)'
    },
    'telegram.quick_order.usual': {
        'en': '⭐ Your usual: {qty}× {product}',
        'uz': '⭐ Odatdagisi: {qty}× {product}',
        'ru': '⭐ Ваше обычное: {qty}× {product}'
    },
    'telegram.quick_order.no_history': {
        'en': 'No previous orders found to repeat.',
        'uz': 'Takrorlash uchun avvalgi buyurtmalar topilmadi.',
        'ru': 'Нет предыдущих заказов для повтора.'
    },
    'telegram.quick_order.unavailable': {
        'en': 'This product is no longer available. Please pick from the products list.',
        'uz': 'Bu mahsulot endi mavjud emas. Iltimos, mahsulotlar ro\'yxatidan tanlang.',
        'ru': 'Этот товар больше недоступен. Пожалуйста, выберите из списка товаров.'
    },

    # ============================================================================
    # Telegram Checkout (telegram.checkout.*)
    # ============================================================================
    'telegram.checkout.delivering_to': {
        'en': '📍 Delivering to:',
        'uz': '📍 Yetkazib berish manzili:',
        'ru': '📍 Доставка по адресу:'
    },
    'telegram.checkout.continue': {
        'en': '✅ Continue',
        'uz': '✅ Davom etish',
        'ru': '✅ Продолжить'
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
    'telegram.payment_business_account': {
        'en': 'Bank Transfer',
        'uz': "Bank o'tkazmasi",
        'ru': 'Банковский перевод'
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
        'uz': 'Menyuga qaytish',
        'ru': 'Назад в меню'
    },
    'telegram.cart_empty': {
        'en': 'Your cart is empty. Add some water to get started!',
        'uz': 'Savatingiz bo\'sh. Boshlash uchun suv qo\'shing!',
        'ru': 'Ваша корзина пуста. Добавьте воду, чтобы начать!'
    },
    'telegram.cart_min_order_warning': {
        'en': 'Minimum order is {min_amount} UZS. Add {remaining} UZS more to check out.',
        'uz': 'Eng kam buyurtma {min_amount} UZS. Buyurtma berish uchun yana {remaining} UZS qo\'shing.',
        'ru': 'Минимальный заказ — {min_amount} UZS. Добавьте ещё {remaining} UZS для оформления.'
    },
    'telegram.cart_min_qty_warning': {
        'en': '{product_name}: minimum order quantity is {min_qty}, add {remaining} more',
        'uz': '{product_name}: minimal buyurtma soni {min_qty}, yana {remaining} ta qo\'shing',
        'ru': '{product_name}: минимальное количество заказа {min_qty}, добавьте ещё {remaining}'
    },
    'telegram.cart_ready_checkout': {
        'en': 'Your cart is ready for checkout!',
        'uz': 'Savatingiz buyurtma berishga tayyor!',
        'ru': 'Корзина готова к оформлению!'
    },
    'telegram.products.min_order_quantity_label': {
        'en': 'Minimum order: {min_qty}',
        'uz': 'Minimal buyurtma: {min_qty}',
        'ru': 'Минимальный заказ: {min_qty}'
    },
    'telegram.cart_total': {
        'en': 'Cart total',
        'uz': 'Savat jami',
        'ru': 'Итого по корзине'
    },
    'telegram.confirm': {
        'en': 'Confirm',
        'uz': 'Tasdiqlash',
        'ru': 'Подтвердить'
    },
    'telegram.continue': {
        'en': 'Continue',
        'uz': 'Davom etish',
        'ru': 'Продолжить'
    },
    'telegram.currency.uzs': {
        'en': 'UZS',
        'uz': 'so\'m',
        'ru': 'сум'
    },
    'telegram.delivery_address': {
        'en': 'Delivery address',
        'uz': 'Yetkazib berish manzili',
        'ru': 'Адрес доставки'
    },
    'telegram.error.auth_failed': {
        'en': '⚠️ Your session has expired. Please log in again to continue.',
        'uz': '⚠️ Sessiyangiz muddati tugadi. Davom etish uchun qaytadan tizimga kiring.',
        'ru': '⚠️ Сессия истекла. Пожалуйста, войдите снова, чтобы продолжить.'
    },
    'telegram.error.generic': {
        'en': '⚠️ Something went wrong. Please try again in a moment.',
        'uz': '⚠️ Nimadir xato ketdi. Iltimos, birozdan so\'ng qayta urinib ko\'ring.',
        'ru': '⚠️ Что-то пошло не так. Пожалуйста, повторите попытку чуть позже.'
    },
    'telegram.error.invalid_input': {
        'en': '⚠️ Sorry, I didn\'t understand that. Please try again or use the menu.',
        'uz': '⚠️ Kechirasiz, buni tushunmadim. Qaytadan urinib ko\'ring yoki menyudan foydalaning.',
        'ru': '⚠️ Извините, не удалось распознать запрос. Попробуйте ещё раз или воспользуйтесь меню.'
    },
    'telegram.error.product_error': {
        'en': '⚠️ We couldn\'t load the products right now. Please try again in a moment.',
        'uz': '⚠️ Mahsulotlarni hozir yuklab bo\'lmadi. Iltimos, birozdan so\'ng qayta urinib ko\'ring.',
        'ru': '⚠️ Не удалось загрузить товары прямо сейчас. Пожалуйста, повторите попытку чуть позже.'
    },
    'telegram.error_occurred': {
        'en': '⚠️ Something went wrong. Please try again in a moment.',
        'uz': '⚠️ Nimadir xato ketdi. Iltimos, birozdan so\'ng qayta urinib ko\'ring.',
        'ru': '⚠️ Что-то пошло не так. Пожалуйста, повторите попытку чуть позже.'
    },
    'telegram.language.already_selected': {
        'en': 'You\'re already using this language.',
        'uz': 'Siz allaqachon shu tildan foydalanyapsiz.',
        'ru': 'Вы уже используете этот язык.'
    },
    'telegram.language.changed_success': {
        'en': 'Language changed',
        'uz': 'Til o\'zgartirildi',
        'ru': 'Язык изменён'
    },
    'telegram.language.confirmation_message': {
        'en': 'All menus and messages will now appear in your new language. Enjoy Aqua Element!',
        'uz': 'Barcha menyular va xabarlar endi yangi tilingizda ko\'rsatiladi. Aqua Element xizmatidan zavqlaning!',
        'ru': 'Теперь все меню и сообщения будут на новом языке. Приятного пользования Aqua Element!'
    },
    'telegram.language.confirmation_title': {
        'en': 'Language updated',
        'uz': 'Til yangilandi',
        'ru': 'Язык обновлён'
    },
    'telegram.language.current': {
        'en': 'Current language',
        'uz': 'Joriy til',
        'ru': 'Текущий язык'
    },
    'telegram.language.error_changing': {
        'en': 'Couldn\'t change the language. Please try again.',
        'uz': 'Tilni o\'zgartirib bo\'lmadi. Iltimos, qaytadan urinib ko\'ring.',
        'ru': 'Не удалось изменить язык. Пожалуйста, попробуйте ещё раз.'
    },
    'telegram.language.invalid_selection': {
        'en': 'This language isn\'t available. Please choose another.',
        'uz': 'Bu til mavjud emas. Iltimos, boshqasini tanlang.',
        'ru': 'Этот язык недоступен. Пожалуйста, выберите другой.'
    },
    'telegram.language.now_using': {
        'en': 'You\'re now using {language}',
        'uz': 'Endi {language} tilidan foydalanyapsiz',
        'ru': 'Теперь вы используете язык {language}'
    },
    'telegram.language.select_prompt': {
        'en': 'Choose your language:',
        'uz': 'Tilingizni tanlang:',
        'ru': 'Выберите язык:'
    },
    'telegram.loyalty.and_more': {
        'en': '...and {count} more',
        'uz': '...va yana {count} ta',
        'ru': '...и ещё {count}'
    },
    'telegram.loyalty.available_rewards': {
        'en': 'Available rewards',
        'uz': 'Mavjud mukofotlar',
        'ru': 'Доступные награды'
    },
    'telegram.loyalty.current_balance': {
        'en': 'Current balance',
        'uz': 'Joriy balans',
        'ru': 'Текущий баланс'
    },
    'telegram.loyalty.lifetime_earned': {
        'en': 'Total earned',
        'uz': 'Jami yig\'ilgan',
        'ru': 'Всего заработано'
    },
    'telegram.loyalty.no_history': {
        'en': 'You have no AquaCoins transactions yet.',
        'uz': 'Sizda hali AquaCoins bo\'yicha amallar yo\'q.',
        'ru': 'У вас пока нет операций с AquaCoins.'
    },
    'telegram.loyalty.no_rewards_available': {
        'en': 'No rewards are available right now.',
        'uz': 'Hozircha mukofotlar mavjud emas.',
        'ru': 'Пока нет доступных наград.'
    },
    'telegram.loyalty.points_history': {
        'en': 'AquaCoins history',
        'uz': 'AquaCoins tarixi',
        'ru': 'История AquaCoins'
    },
    'telegram.loyalty.points_unit': {
        'en': 'AquaCoins',
        'uz': 'AquaCoins',
        'ru': 'AquaCoins'
    },
    'telegram.loyalty.redeem_success': {
        'en': 'Reward redeemed successfully!',
        'uz': 'Mukofot muvaffaqiyatli almashtirildi!',
        'ru': 'Награда успешно обменяна!'
    },
    'telegram.loyalty.reward_selected': {
        'en': 'Reward selected — it will be applied at checkout',
        'uz': "Mukofot tanlandi — buyurtma rasmiylashtirilganda qo'llaniladi",
        'ru': 'Награда выбрана — она будет применена при оформлении заказа'
    },
    'telegram.loyalty.apply_reward': {
        'en': 'Apply reward',
        'uz': 'Mukofotni qo\'llash',
        'ru': 'Применить награду'
    },
    'telegram.loyalty.change_reward': {
        'en': 'Change reward',
        'uz': 'Mukofotni o\'zgartirish',
        'ru': 'Изменить награду'
    },
    'telegram.loyalty.remove_reward': {
        'en': 'Remove reward',
        'uz': 'Mukofotni olib tashlash',
        'ru': 'Убрать награду'
    },
    'telegram.loyalty.choose_reward_title': {
        'en': 'Choose a reward to apply to this order',
        'uz': 'Ushbu buyurtmaga qo\'llash uchun mukofotni tanlang',
        'ru': 'Выберите награду для этого заказа'
    },
    'telegram.loyalty.no_rewards_for_order': {
        'en': 'No rewards are available for this order yet.',
        'uz': 'Ushbu buyurtma uchun hozircha mukofotlar mavjud emas.',
        'ru': 'Для этого заказа пока нет доступных наград.'
    },
    'telegram.loyalty.reward_removed': {
        'en': 'Reward removed',
        'uz': 'Mukofot olib tashlandi',
        'ru': 'Награда убрана'
    },
    'telegram.loyalty.balance_header': {
        'en': 'Your balance: {points} AquaCoins',
        'uz': 'Sizning balansingiz: {points} AquaCoins',
        'ru': 'Ваш баланс: {points} AquaCoins'
    },
    'telegram.loyalty.lock_need_coins': {
        'en': 'need +{points} AquaCoins',
        'uz': "yana +{points} AquaCoins kerak",
        'ru': 'нужно ещё +{points} AquaCoins'
    },
    'telegram.loyalty.lock_add_order': {
        'en': 'add +{amount} UZS to your order',
        'uz': "buyurtmaga +{amount} so'm qo'shing",
        'ru': 'добавьте +{amount} сум к заказу'
    },
    'telegram.loyalty.refer_friends': {
        'en': 'Invite friends',
        'uz': 'Do\'stlarni taklif qilish',
        'ru': 'Пригласить друзей'
    },
    'telegram.loyalty.referral_code': {
        'en': 'Referral code',
        'uz': 'Referal kod',
        'ru': 'Реферальный код'
    },
    'telegram.loyalty.referral_total': {
        'en': 'Total referrals',
        'uz': 'Jami referallar',
        'ru': 'Всего рефералов'
    },
    'telegram.loyalty.referral_pending': {
        'en': 'Pending referrals',
        'uz': 'Kutilayotgan referallar',
        'ru': 'Ожидающие рефералы'
    },
    'telegram.loyalty.referral_points_earned': {
        'en': 'AquaCoins earned',
        'uz': "Yig'ilgan AquaCoins",
        'ru': 'Заработано AquaCoins'
    },
    'telegram.loyalty.redeem': {
        'en': 'Redeem',
        'uz': 'Almashtirish',
        'ru': 'Обменять'
    },
    'telegram.loyalty.reward_fallback': {
        'en': 'Reward',
        'uz': 'Mukofot',
        'ru': 'Награда'
    },
    'telegram.loyalty.reward_applied': {
        'en': 'Reward applied',
        'uz': "Mukofot qo'llanildi",
        'ru': 'Награда применена'
    },
    'telegram.loyalty.free_suffix': {
        'en': 'free',
        'uz': 'bepul',
        'ru': 'бесплатно'
    },
    'telegram.loyalty.not_available': {
        'en': "🎁 The loyalty program isn't available for your account.",
        'uz': "🎁 Sodiqlik dasturi hisobingiz uchun mavjud emas.",
        'ru': "🎁 Программа лояльности недоступна для вашего аккаунта."
    },
    # AquaCoins history labels — localized, category-based (derived from the
    # transaction's action_type/transaction_type in the bot). These replace the
    # old telegram.loyalty.transaction_* keys, which were never translated and
    # showed confusing "Transaction other/earned/redeemed" in every language.
    'telegram.loyalty.txn.order_earn': {
        'en': 'Order earnings',
        'uz': 'Buyurtma uchun hisoblandi',
        'ru': 'Начислено за заказ',
    },
    'telegram.loyalty.txn.referral': {
        'en': 'Referral bonus',
        'uz': 'Referal bonusi',
        'ru': 'Реферальный бонус',
    },
    'telegram.loyalty.txn.welcome': {
        'en': 'Welcome bonus',
        'uz': 'Xush kelibsiz bonusi',
        'ru': 'Приветственный бонус',
    },
    'telegram.loyalty.txn.birthday': {
        'en': 'Birthday bonus',
        'uz': "Tug'ilgan kun bonusi",
        'ru': 'Бонус на день рождения',
    },
    'telegram.loyalty.txn.streak': {
        'en': 'Streak bonus',
        'uz': 'Seriya bonusi',
        'ru': 'Бонус за серию заказов',
    },
    'telegram.loyalty.txn.surprise': {
        'en': 'Surprise reward',
        'uz': 'Kutilmagan mukofot',
        'ru': 'Сюрприз-бонус',
    },
    'telegram.loyalty.txn.redeem': {
        'en': 'Reward redeemed',
        'uz': 'Mukofot almashtirildi',
        'ru': 'Обмен на награду',
    },
    'telegram.loyalty.txn.redeem_named': {
        'en': 'Redeemed: {name}',
        'uz': 'Almashtirildi: {name}',
        'ru': 'Обмен: {name}',
    },
    'telegram.loyalty.txn.refund': {
        'en': 'Reward refund',
        'uz': 'Mukofot qaytarildi',
        'ru': 'Возврат обмена',
    },
    'telegram.loyalty.txn.refund_order': {
        'en': 'Reward refund (order #{order_id})',
        'uz': 'Mukofot qaytarildi (buyurtma #{order_id})',
        'ru': 'Возврат обмена (заказ #{order_id})',
    },
    'telegram.loyalty.txn.adjustment': {
        'en': 'Adjustment',
        'uz': 'Tuzatish',
        'ru': 'Корректировка',
    },
    'telegram.loyalty.txn.bonus': {
        'en': 'Bonus',
        'uz': 'Bonus',
        'ru': 'Бонус',
    },
    'telegram.loyalty.txn.expired': {
        'en': 'Expired',
        'uz': 'Muddati tugadi',
        'ru': 'Срок действия истёк',
    },
    'telegram.loyalty.txn.other': {
        'en': 'Transaction',
        'uz': 'Amal',
        'ru': 'Операция',
    },
    'telegram.loyalty.view_rewards': {
        'en': 'View rewards',
        'uz': 'Mukofotlarni ko\'rish',
        'ru': 'Посмотреть награды'
    },
    'telegram.main_menu': {
        'en': '💧 Welcome to Aqua Element! Choose an option below to get started.',
        'uz': '💧 Aqua Elementga xush kelibsiz! Boshlash uchun quyidagi bo\'limlardan birini tanlang.',
        'ru': '💧 Добро пожаловать в Aqua Element! Выберите нужный раздел ниже, чтобы начать.'
    },
    'telegram.price': {
        'en': 'Price',
        'uz': 'Narxi',
        'ru': 'Цена'
    },
    'telegram.quantity': {
        'en': 'Quantity',
        'uz': 'Soni',
        'ru': 'Количество'
    },
    'telegram.registration.phone_shared': {
        'en': 'Phone number saved. Thank you!',
        'uz': 'Telefon raqamingiz saqlandi. Rahmat!',
        'ru': 'Номер телефона сохранён. Спасибо!'
    },
    'telegram.subscription.active': {
        'en': 'Active',
        'uz': 'Faol',
        'ru': 'Активные'
    },
    'telegram.subscription.activity_logs': {
        'en': 'Activity Log',
        'uz': 'Faoliyat tarixi',
        'ru': 'История действий'
    },
    'telegram.subscription.add_address': {
        'en': 'Add Address',
        'uz': 'Manzil qo\'shish',
        'ru': 'Добавить адрес'
    },
    'telegram.subscription.add_more_or_continue': {
        'en': 'Add more items or continue.',
        'uz': 'Yana mahsulot qo\'shing yoki davom eting.',
        'ru': 'Добавьте ещё товары или продолжите.'
    },
    'telegram.subscription.add_new_address': {
        'en': 'Add New Address',
        'uz': 'Yangi manzil qo\'shish',
        'ru': 'Добавить новый адрес'
    },
    'telegram.subscription.amount': {
        'en': 'Amount',
        'uz': 'Summa',
        'ru': 'Сумма'
    },
    'telegram.subscription.average_order': {
        'en': 'Average order',
        'uz': 'O\'rtacha buyurtma',
        'ru': 'Средний заказ'
    },
    'telegram.subscription.back_to_items': {
        'en': 'Back to Items',
        'uz': 'Mahsulotlarga qaytish',
        'ru': 'Назад к товарам'
    },
    'telegram.subscription.billing_history': {
        'en': 'Billing History',
        'uz': 'To\'lovlar tarixi',
        'ru': 'История списаний'
    },
    'telegram.subscription.billing_retry_initiated': {
        'en': 'Billing retry started.',
        'uz': 'To\'lovni qayta o\'tkazishga urinish boshlandi.',
        'ru': 'Повторное списание запущено.'
    },
    'telegram.subscription.cancelled_success': {
        'en': 'Subscription cancelled.',
        'uz': 'Obuna bekor qilindi.',
        'ru': 'Подписка отменена.'
    },
    'telegram.subscription.confirm_title': {
        'en': 'Confirm Subscription',
        'uz': 'Obunani tasdiqlash',
        'ru': 'Подтверждение подписки'
    },
    'telegram.subscription.create_template_or_custom': {
        'en': 'Start from a template or build your own subscription.',
        'uz': 'Tayyor shablondan boshlang yoki o\'z obunangizni tuzing.',
        'ru': 'Начните с шаблона или соберите свою подписку.'
    },
    'telegram.subscription.created_success': {
        'en': 'Subscription created.',
        'uz': 'Obuna yaratildi.',
        'ru': 'Подписка создана.'
    },
    'telegram.subscription.creation_cancelled': {
        'en': 'Subscription creation cancelled.',
        'uz': 'Obuna yaratish bekor qilindi.',
        'ru': 'Создание подписки отменено.'
    },
    'telegram.subscription.current_items': {
        'en': 'Current items',
        'uz': 'Joriy mahsulotlar',
        'ru': 'Текущие товары'
    },
    'telegram.subscription.details_title': {
        'en': 'Subscription Details',
        'uz': 'Obuna tafsilotlari',
        'ru': 'Детали подписки'
    },
    'telegram.subscription.edit_menu': {
        'en': 'What would you like to change in your subscription?',
        'uz': 'Obunangizda nimani o\'zgartirmoqchisiz?',
        'ru': 'Что вы хотите изменить в подписке?'
    },
    'telegram.subscription.favorite_product': {
        'en': 'Favorite product',
        'uz': 'Sevimli mahsulot',
        'ru': 'Любимый товар'
    },
    'telegram.subscription.frequency': {
        'en': 'Frequency',
        'uz': 'Davriylik',
        'ru': 'Периодичность'
    },
    'telegram.subscription.frequency_updated_successfully': {
        'en': 'Delivery frequency updated.',
        'uz': 'Yetkazib berish davriyligi yangilandi.',
        'ru': 'Периодичность доставки обновлена.'
    },
    'telegram.subscription.id': {
        'en': 'Subscription ID',
        'uz': 'Obuna ID',
        'ru': 'ID подписки'
    },
    'telegram.subscription.item_added': {
        'en': 'Item added to your subscription.',
        'uz': 'Mahsulot obunangizga qo\'shildi.',
        'ru': 'Товар добавлен в подписку.'
    },
    'telegram.subscription.item_added_successfully': {
        'en': 'Item added.',
        'uz': 'Mahsulot qo\'shildi.',
        'ru': 'Товар добавлен.'
    },
    'telegram.subscription.item_removed_successfully': {
        'en': 'Item removed.',
        'uz': 'Mahsulot olib tashlandi.',
        'ru': 'Товар удалён.'
    },
    'telegram.subscription.item_updated_successfully': {
        'en': 'Item updated.',
        'uz': 'Mahsulot yangilandi.',
        'ru': 'Товар обновлён.'
    },
    'telegram.subscription.items': {
        'en': 'Items',
        'uz': 'Mahsulotlar',
        'ru': 'Товары'
    },
    'telegram.subscription.next_billing': {
        'en': 'Next billing',
        'uz': 'Keyingi to\'lov',
        'ru': 'Следующее списание'
    },
    'telegram.subscription.next_delivery': {
        'en': 'Next delivery',
        'uz': 'Keyingi yetkazib berish',
        'ru': 'Следующая доставка'
    },
    'telegram.subscription.no_activity_logs': {
        'en': 'No activity yet.',
        'uz': 'Hozircha faoliyat yo\'q.',
        'ru': 'Пока нет активности.'
    },
    'telegram.subscription.no_addresses': {
        'en': 'You don\'t have any saved addresses yet.',
        'uz': 'Sizda hali saqlangan manzillar yo\'q.',
        'ru': 'У вас пока нет сохранённых адресов.'
    },
    'telegram.subscription.no_billing_history': {
        'en': 'No billing history yet.',
        'uz': 'Hozircha to\'lovlar tarixi yo\'q.',
        'ru': 'Пока нет истории платежей.'
    },
    'telegram.subscription.no_items': {
        'en': 'No items in this subscription yet.',
        'uz': 'Bu obunada hali mahsulotlar yo\'q.',
        'ru': 'В этой подписке пока нет товаров.'
    },
    'telegram.subscription.no_subscriptions': {
        'en': 'You don\'t have any subscriptions yet.',
        'uz': 'Sizda hali obunalar yo\'q.',
        'ru': 'У вас пока нет подписок.'
    },
    'telegram.subscription.paused': {
        'en': 'Paused',
        'uz': 'To\'xtatilgan',
        'ru': 'Приостановлено'
    },
    'telegram.subscription.paused_success': {
        'en': 'Subscription paused.',
        'uz': 'Obuna to\'xtatildi.',
        'ru': 'Подписка приостановлена.'
    },
    'telegram.subscription.payment_method_updated_successfully': {
        'en': 'Payment method updated.',
        'uz': 'To\'lov usuli yangilandi.',
        'ru': 'Способ оплаты обновлён.'
    },
    'telegram.subscription.resumed_success': {
        'en': 'Subscription resumed.',
        'uz': 'Obuna qayta tiklandi.',
        'ru': 'Подписка возобновлена.'
    },
    'telegram.subscription.select_address': {
        'en': 'Choose a delivery address:',
        'uz': 'Yetkazib berish manzilini tanlang:',
        'ru': 'Выберите адрес доставки:'
    },
    'telegram.subscription.select_at_least_one_item': {
        'en': 'Please add at least one item.',
        'uz': 'Kamida bitta mahsulot qo\'shing.',
        'ru': 'Добавьте хотя бы один товар.'
    },
    'telegram.subscription.select_frequency': {
        'en': 'How often should we deliver?',
        'uz': 'Qanchalik tez-tez yetkazib beraylik?',
        'ru': 'Как часто доставлять?'
    },
    'telegram.subscription.select_new_frequency': {
        'en': 'Choose a new delivery frequency:',
        'uz': 'Yangi yetkazib berish davriyligini tanlang:',
        'ru': 'Выберите новую частоту доставки:'
    },
    'telegram.subscription.select_new_payment_method': {
        'en': 'Choose a new payment method:',
        'uz': 'Yangi to\'lov usulini tanlang:',
        'ru': 'Выберите новый способ оплаты:'
    },
    'telegram.subscription.select_new_quantity': {
        'en': 'Select new quantity:',
        'uz': 'Yangi miqdorni tanlang:',
        'ru': 'Выберите новое количество:'
    },
    'telegram.subscription.select_payment': {
        'en': 'Choose a payment method:',
        'uz': 'To\'lov usulini tanlang:',
        'ru': 'Выберите способ оплаты:'
    },
    'telegram.subscription.select_product_to_add': {
        'en': 'Select a product to add:',
        'uz': 'Qo\'shish uchun mahsulotni tanlang:',
        'ru': 'Выберите товар для добавления:'
    },
    'telegram.subscription.select_products': {
        'en': 'Select products for your subscription:',
        'uz': 'Obunangiz uchun mahsulotlarni tanlang:',
        'ru': 'Выберите товары для подписки:'
    },
    'telegram.subscription.select_quantity': {
        'en': 'Select quantity:',
        'uz': 'Miqdorni tanlang:',
        'ru': 'Выберите количество:'
    },
    'telegram.subscription.select_quantity_for_item': {
        'en': 'Select quantity for this item:',
        'uz': 'Ushbu mahsulot uchun miqdorni tanlang:',
        'ru': 'Выберите количество для этого товара:'
    },
    'telegram.subscription.skip_success': {
        'en': 'Next delivery skipped.',
        'uz': 'Keyingi yetkazib berish o\'tkazib yuborildi.',
        'ru': 'Следующая доставка пропущена.'
    },
    'telegram.subscription.status': {
        'en': 'Status',
        'uz': 'Holati',
        'ru': 'Статус'
    },
    'telegram.subscription.title': {
        'en': '🔄 My subscriptions',
        'uz': '🔄 Mening obunalarim',
        'ru': '🔄 Мои подписки'
    },
    'telegram.subscription.total': {
        'en': 'Total',
        'uz': 'Jami',
        'ru': 'Итого'
    },
    'telegram.subscription.total_items': {
        'en': 'Total items',
        'uz': 'Jami mahsulotlar',
        'ru': 'Всего товаров'
    },
    'telegram.subscription.trial': {
        'en': 'Free trial: {} days',
        'uz': 'Bepul sinov: {} kun',
        'ru': 'Бесплатный пробный период: {} дн.'
    },
    'telegram.subscription.view': {
        'en': 'View subscription',
        'uz': 'Obunani ko\'rish',
        'ru': 'Открыть подписку'
    },
    'telegram.total': {
        'en': 'Total',
        'uz': 'Jami',
        'ru': 'Итого'
    },
    'telegram.unknown_action': {
        'en': 'Unknown action. Please try again.',
        'uz': 'Noma\'lum amal. Iltimos, qaytadan urinib ko\'ring.',
        'ru': 'Неизвестное действие. Пожалуйста, попробуйте снова.'
    },
    'telegram.welcome': {
        'en': '👋 Welcome to Aqua Element! Your registration is complete. Use the menu below to start ordering.',
        'uz': '👋 Aqua Elementga xush kelibsiz! Ro\'yxatdan o\'tish yakunlandi. Buyurtma berishni boshlash uchun quyidagi menyudan foydalaning.',
        'ru': '👋 Добро пожаловать в Aqua Element! Регистрация завершена. Используйте меню ниже, чтобы оформить заказ.'
    },

}


# BEGIN AUTO-GENERATED ADMIN UI ORDER/PRODUCT TRANSLATIONS
def _ui_tr(en: str, uz: str | None = None, ru: str | None = None) -> dict[str, str | None]:
    """Build a UI translation row.

    Locale values left as ``None`` will use English only for missing DB rows
    and will not overwrite existing localized translations on re-seed.
    """
    return {'en': en, 'uz': uz, 'ru': ru}


def _resolve_seed_value(
    translations: dict[str, str | None],
    language: str,
) -> tuple[str, bool]:
    """Return the value to seed and whether existing rows must be preserved."""
    english_value = translations['en']
    if language == 'en':
        return english_value, False

    localized_value = translations.get(language)
    if localized_value is None:
        return english_value, True

    return localized_value, False


def _category_for(key: str) -> str:
    """Derive the Translation.category for a key.

    Dotted keys (``ui.loyalty.x``) use their first segment; dotless
    English-literal storefront keys (looked up by the literal text via the
    ``t`` filter) have no category and default to ``general`` — mirroring
    TranslationService._get_cache_timeout. The result is capped to the
    ``varchar(50)`` column width so a long key can never abort the reseed.
    """
    category = key.split('.')[0] if '.' in key else 'general'
    return category[:50]


ADMIN_UI_ORDER_TRANSLATIONS = {
    'ui.common.archive': _ui_tr('Archive', 'Arxivlash', 'Архивировать'),
    'ui.common.disabled': _ui_tr('Disabled', "O'chirilgan", 'Отключено'),
    'ui.common.enabled': _ui_tr('Enabled', 'Yoqilgan', 'Включено'),
    'ui.common.failed': _ui_tr('Failed', 'Muvaffaqiyatsiz', 'Неудачно'),
    'ui.common.no': _ui_tr('No', "Yo'q", 'Нет'),
    'ui.common.refresh': _ui_tr('Refresh', 'Yangilash', 'Обновить'),
    'ui.common.success': _ui_tr('Success', 'Muvaffaqiyatli', 'Успешно'),
    'ui.common.yes': _ui_tr('Yes', 'Ha', 'Да'),
    'ui.orders.action': _ui_tr('Action', 'Amal', 'Действие'),
    'ui.orders.actions': _ui_tr('Actions', 'Amallar', 'Действия'),
    'ui.orders.add_item': _ui_tr('Add Item'),
    'ui.orders.address_required': _ui_tr('Please select a delivery address'),
    'ui.orders.amount': _ui_tr('Amount', 'Summa', 'Сумма'),
    'ui.orders.amount_collected': _ui_tr('Collected', "Yig'ilgan", 'Собрано'),
    'ui.orders.amount_required': _ui_tr('Amount is required'),
    'ui.orders.audit_action': _ui_tr('Action'),
    'ui.orders.audit_status': _ui_tr('Status'),
    'ui.orders.callback_note': _ui_tr('Note'),
    'ui.orders.callback_result': _ui_tr('Result'),
    'ui.orders.callback_stage': _ui_tr('Stage'),
    'ui.orders.cancel_order': _ui_tr('Cancel Order'),
    'ui.orders.cancel_order_confirm': _ui_tr('Cancel order'),
    'ui.orders.cancel_order_title': _ui_tr('Cancel order'),
    'ui.orders.cancelled_by_admin': _ui_tr('Cancelled by admin'),
    'ui.orders.click_callback_history': _ui_tr('Click Callback History'),
    'ui.orders.click_orders': _ui_tr('Click/Card Orders', 'Click/karta buyurtmalari', 'Заказы Click/карта'),
    'ui.orders.close': _ui_tr('Close'),
    'ui.orders.cod_restricted': _ui_tr('Cash on delivery is restricted for this customer'),
    'ui.orders.cod_restricted_description': _ui_tr('This customer has reached the active COD debt limit. Use one of the prepaid methods below.'),
    'ui.orders.consume_marking_codes': _ui_tr('Consume Marking Codes', 'Markirovka kodlarini sarflash', 'Списывать коды маркировки'),
    'ui.orders.consume_marking_codes_help': _ui_tr('Leave disabled unless this business-account order should permanently consume product marking codes.'),
    'ui.orders.create_order': _ui_tr('Create Order', 'Buyurtma yaratish', 'Создать заказ'),
    'ui.orders.customer': _ui_tr('Customer', 'Mijoz', 'Клиент'),
    'ui.orders.customer_required': _ui_tr('Please select a customer'),
    'ui.orders.delivery_notes': _ui_tr('Delivery Notes', 'Yetkazish izohlari', 'Примечания к доставке'),
    'ui.orders.delivery_notes_placeholder': _ui_tr('Any special delivery instructions...'),
    'ui.orders.email': _ui_tr('Email', 'Email', 'Email'),
    'ui.orders.end_date': _ui_tr('End date'),
    'ui.orders.error': _ui_tr('Error'),
    'ui.orders.export_orders': _ui_tr('Export Orders'),
    'ui.orders.filter_by_status': _ui_tr('Filter by status'),
    'ui.orders.fiscalization': _ui_tr('Fiscalization', 'Fiskalizatsiya', 'Фискализация'),
    'ui.orders.fiscalization_audit_trail': _ui_tr('Fiscalization Audit Trail', 'Fiskalizatsiya auditi', 'Журнал фискализации'),
    'ui.orders.fiscalization_completed': _ui_tr('Completed', 'Yakunlandi', 'Завершена'),
    'ui.orders.fiscalization_failed': _ui_tr('Failed', 'Muvaffaqiyatsiz', 'Не удалась'),
    'ui.orders.fiscalization_failure_reason': _ui_tr('Failure Reason'),
    'ui.orders.fiscalization_not_required': _ui_tr('Not Required', 'Talab etilmaydi', 'Не требуется'),
    'ui.orders.fiscalization_pending': _ui_tr('Pending', 'Kutilmoqda', 'Ожидает'),
    'ui.orders.fiscalization_processing': _ui_tr('Processing', 'Bajarilmoqda', 'Обрабатывается'),
    'ui.orders.fiscalization_retry_failed': _ui_tr('Failed to retry fiscalization'),
    'ui.orders.fiscalization_retry_success': _ui_tr('Fiscalization retry queued successfully'),
    'ui.orders.fiscalization_status': _ui_tr('Fiscalization Status', 'Fiskalizatsiya holati', 'Статус фискализации'),
    'ui.orders.items': _ui_tr('Items', 'Mahsulotlar', 'Товары'),
    'ui.orders.items_count': _ui_tr('items'),
    'ui.orders.load_customer_context_failed': _ui_tr('Failed to load customer payment context'),
    'ui.orders.marking_code': _ui_tr('Marking Code', 'Markirovka kodi', 'Код маркировки'),
    'ui.orders.marking_code_activity': _ui_tr('Marking-Code Activity', 'Markirovka kodi faolligi', 'Активность кодов маркировки'),
    'ui.orders.marking_code_event_archived': _ui_tr('Archived', 'Arxivlangan', 'Архивирован'),
    'ui.orders.marking_code_event_created': _ui_tr('Created', 'Yaratilgan', 'Создан'),
    'ui.orders.marking_code_event_imported': _ui_tr('Imported', 'Import qilingan', 'Импортирован'),
    'ui.orders.marking_code_event_released': _ui_tr('Released', "Bo'shatilgan", 'Освобождён'),
    'ui.orders.marking_code_event_reserved': _ui_tr('Reserved', 'Rezervlangan', 'Зарезервирован'),
    'ui.orders.marking_code_event_restored': _ui_tr('Restored', 'Tiklangan', 'Восстановлен'),
    'ui.orders.marking_code_event_used': _ui_tr('Used', 'Ishlatilgan', 'Использован'),
    'ui.orders.marking_code_event_utilised': _ui_tr('Utilised', 'Sarflangan', 'Утилизирован'),
    'ui.orders.marking_code_summary': _ui_tr('Marking-Code Summary', 'Markirovka kodi xulosasi', 'Сводка по кодам маркировки'),
    'ui.orders.marking_codes': _ui_tr('Marking Codes', 'Markirovka kodlari', 'Коды маркировки'),
    'ui.orders.marking_codes_count': _ui_tr('{{count}} codes', '{{count}} ta kod', '{{count}} кодов'),
    'ui.orders.more_items': _ui_tr('more'),
    'ui.orders.new_status': _ui_tr('New Status'),
    'ui.orders.no_address_hint': _ui_tr('This user has no saved addresses. Please add an address from the Users page first.'),
    'ui.orders.no_addresses': _ui_tr('No addresses found for this user'),
    'ui.orders.no_click_callbacks': _ui_tr('No Click callback history recorded yet'),
    'ui.orders.no_fiscalization_audit_trail': _ui_tr('No fiscalization audit trail recorded yet'),
    'ui.orders.no_marking_code_activity': _ui_tr('No marking-code activity recorded for this order'),
    'ui.orders.no_payment_transactions': _ui_tr('No payment transactions recorded yet'),
    'ui.orders.notes': _ui_tr('Notes', 'Izohlar', 'Примечания'),
    'ui.orders.notes_optional': _ui_tr('Notes (Optional)'),
    'ui.orders.notes_placeholder': _ui_tr('Notes'),
    'ui.orders.notes_required': _ui_tr('Notes are required'),
    'ui.orders.open_payment_link': _ui_tr('Open Payment Link', "To'lov havolasini ochish", 'Открыть ссылку оплаты'),
    'ui.orders.open_receipt': _ui_tr('Open receipt', 'Chekni ochish', 'Открыть чек'),
    'ui.orders.order_context_missing': _ui_tr('Order context is missing'),
    'ui.orders.order_create_failed': _ui_tr('Failed to create order'),
    'ui.orders.order_created_success': _ui_tr('Order created successfully'),
    'ui.orders.order_date': _ui_tr('Order Date', 'Buyurtma sanasi', 'Дата заказа'),
    'ui.orders.order_details': _ui_tr('Order Details', 'Buyurtma tafsilotlari', 'Детали заказа'),
    'ui.orders.order_item': _ui_tr('Order Item', 'Buyurtma pozitsiyasi', 'Позиция заказа'),
    'ui.orders.order_items': _ui_tr('Order Items', 'Buyurtma mahsulotlari', 'Товары заказа'),
    'ui.orders.order_number': _ui_tr('Order Number', 'Buyurtma raqami', 'Номер заказа'),
    'ui.orders.order_total': _ui_tr('Order Total', 'Buyurtma jami', 'Итого по заказу'),
    'ui.orders.outstanding_amount': _ui_tr('Outstanding', 'Qoldiq qarz', 'Остаток долга'),
    'ui.orders.pagination_text': _ui_tr('orders'),
    'ui.orders.payment_link': _ui_tr('Payment Link', "To'lov havolasi", 'Ссылка оплаты'),
    'ui.orders.payment_link_ready': _ui_tr('Payment link created'),
    'ui.orders.payment_method': _ui_tr('Payment Method', "To'lov usuli", 'Способ оплаты'),
    'ui.orders.payment_method_required': _ui_tr('Please select a payment method'),
    'ui.orders.payment_method_unavailable': _ui_tr('Selected payment method is not available for this user'),
    'ui.orders.payment_provider': _ui_tr('Payment Provider', "To'lov provayderi", 'Провайдер оплаты'),
    'ui.orders.payment_status': _ui_tr('Payment Status', "To'lov holati", 'Статус оплаты'),
    'ui.orders.payment_summary': _ui_tr('Payment Summary', "To'lov xulosasi", 'Сводка оплаты'),
    'ui.orders.payment_timeline': _ui_tr('Payment Timeline', "To'lov tarixi", 'Лента оплаты'),
    'ui.orders.payment_transactions': _ui_tr('Payment Transactions', "To'lov tranzaksiyalari", 'Платежные транзакции'),
    'ui.orders.pending_orders': _ui_tr('Pending Orders', 'Kutilayotgan buyurtmalar', 'Ожидающие заказы'),
    'ui.orders.personal_card_notes_placeholder': _ui_tr('Example: Customer transferred to owner personal card'),
    'ui.orders.personal_card_payment_failed': _ui_tr('Failed to record personal card payment'),
    'ui.orders.personal_card_payment_recorded': _ui_tr('Personal card payment recorded'),
    'ui.orders.phone': _ui_tr('Phone', 'Telefon', 'Телефон'),
    'ui.orders.product_name': _ui_tr('Product', 'Mahsulot', 'Товар'),
    'ui.orders.product_required': _ui_tr('Select product'),
    'ui.orders.provider_transaction_id': _ui_tr('Provider Transaction ID', 'Provayder tranzaksiya ID', 'ID транзакции провайдера'),
    'ui.orders.quantity': _ui_tr('Qty', 'Soni', 'Кол-во'),
    'ui.orders.quantity_required': _ui_tr('Qty'),
    'ui.orders.receipt_id': _ui_tr('Receipt ID', 'Chek ID', 'ID чека'),
    'ui.orders.receipt_link': _ui_tr('Receipt Link', 'Chek havolasi', 'Ссылка на чек'),
    'ui.orders.record_personal_card_payment': _ui_tr('Record Personal Card Payment', "Shaxsiy karta to'lovini qayd etish", 'Зафиксировать оплату на личную карту'),
    'ui.orders.retry_fiscalization': _ui_tr('Retry Fiscalization', 'Fiskalizatsiyani qayta yuborish', 'Повторить фискализацию'),
    'ui.orders.search_customer': _ui_tr('Search customer by name or phone'),
    'ui.orders.search_placeholder': _ui_tr('Search orders'),
    'ui.orders.select_address': _ui_tr('Select Delivery Address'),
    'ui.orders.select_address_placeholder': _ui_tr('Select an address'),
    'ui.orders.select_customer': _ui_tr('Select Customer'),
    'ui.orders.select_customer_first': _ui_tr('Select a customer first'),
    'ui.orders.select_payment_method': _ui_tr('Select a payment method'),
    'ui.orders.select_product': _ui_tr('Select product'),
    'ui.orders.select_status_required': _ui_tr('Please select a status'),
    'ui.orders.start_date': _ui_tr('Start date', 'Boshlanish sanasi', 'Дата начала'),
    'ui.orders.status': _ui_tr('Status', 'Holat', 'Статус'),
    'ui.orders.status_update_failed': _ui_tr('Failed to update order status'),
    'ui.orders.status_updated_success': _ui_tr('Order status updated successfully'),
    'ui.orders.time': _ui_tr('Time', 'Vaqt', 'Время'),
    'ui.orders.timeline_amount': _ui_tr('Amount'),
    'ui.orders.timeline_notes': _ui_tr('Notes'),
    'ui.orders.timeline_timestamp': _ui_tr('Timestamp'),
    'ui.orders.timeline_type': _ui_tr('Type'),
    'ui.orders.total_amount': _ui_tr('Total Amount', 'Jami summa', 'Общая сумма'),
    'ui.orders.total_orders': _ui_tr('Total Orders', 'Jami buyurtmalar', 'Всего заказов'),
    'ui.orders.total_price': _ui_tr('Total'),
    'ui.orders.total_revenue': _ui_tr('Total Revenue', 'Jami tushum', 'Общая выручка'),
    'ui.orders.transaction_status': _ui_tr('Status'),
    'ui.orders.transaction_type': _ui_tr('Type'),
    'ui.orders.unit_price': _ui_tr('Unit Price', 'Birlik narxi', 'Цена за единицу'),
    'ui.orders.update_order_status': _ui_tr('Update Order Status', 'Buyurtma holatini yangilash', 'Обновить статус заказа'),
    'ui.orders.update_status': _ui_tr('Update Status', 'Holatni yangilash', 'Обновить статус'),
    'ui.orders.view_details': _ui_tr('View Details', "Batafsil ko'rish", 'Посмотреть детали'),
    # ---- Admin order-edit feature (Edit Items modal + Order Changes history) ----
    'ui.orders.edit_items': _ui_tr('Edit Items', 'Mahsulotlarni tahrirlash', 'Редактировать товары'),
    'ui.orders.edit_items_hint': _ui_tr(
        'Set the FINAL desired quantity per line. 0 removes a line. Add new rows to insert new line items.',
        'Har bir qator uchun YAKUNIY istalgan miqdorni belgilang. 0 qatorni olib tashlaydi. Yangi qatorlar qo\'shish uchun "Mahsulot qo\'shish" tugmasini bosing.',
        'Укажите ОКОНЧАТЕЛЬНОЕ желаемое количество для каждой позиции. 0 удаляет позицию. Добавьте новые строки, чтобы вставить новые товары.',
    ),
    'ui.orders.edit_reason': _ui_tr('Reason', 'Sabab', 'Причина'),
    'ui.orders.reason_required': _ui_tr('Reason is required', 'Sabab kerak', 'Причина обязательна'),
    'ui.orders.reason_min_length': _ui_tr('Reason must be at least 3 characters', 'Sabab kamida 3 ta belgi bo\'lishi kerak', 'Причина должна быть не менее 3 символов'),
    'ui.orders.reason_placeholder': _ui_tr(
        'Example: customer asked for 2 extra bottles on arrival',
        'Misol: mijoz yetkazib berishda yana 2 ta idish so\'radi',
        'Например: клиент попросил ещё 2 бутыли при доставке',
    ),
    'ui.orders.preview_impacts': _ui_tr('Preview impacts', 'Ta\'sirini ko\'rib chiqish', 'Предпросмотр последствий'),
    'ui.orders.edit_preview_title': _ui_tr('Confirm Order Edit', 'Buyurtma tahririni tasdiqlash', 'Подтвердите редактирование заказа'),
    'ui.orders.edit_preview_failed': _ui_tr('Failed to preview order edit', 'Tahririni oldindan ko\'rishda xatolik', 'Не удалось сформировать предпросмотр'),
    'ui.orders.edit_preview_missing': _ui_tr('Preview the change before applying.', 'Qo\'llashdan oldin o\'zgarishni ko\'rib chiqing.', 'Перед применением посмотрите предпросмотр.'),
    'ui.orders.edit_failed': _ui_tr('Failed to apply order edit', 'Tahririni qo\'llashda xatolik', 'Не удалось применить редактирование'),
    'ui.orders.edit_applied_success': _ui_tr('Order updated successfully', 'Buyurtma muvaffaqiyatli yangilandi', 'Заказ успешно обновлён'),
    'ui.orders.edit_warnings_title': _ui_tr('Order updated with warnings', 'Buyurtma ogohlantirishlar bilan yangilandi', 'Заказ обновлён с предупреждениями'),
    'ui.orders.edit_warnings': _ui_tr('Warnings', 'Ogohlantirishlar', 'Предупреждения'),
    'ui.orders.edit_blocked': _ui_tr('This edit cannot proceed', 'Ushbu tahrir davom etolmaydi', 'Это редактирование невозможно'),
    'ui.orders.edit_prepayment_created': _ui_tr(
        'Prepayment credit recorded for the customer.',
        'Mijoz uchun oldindan to\'lov krediti qayd etildi.',
        'Кредит предоплаты зачислен клиенту.',
    ),
    'ui.orders.edit_collect_extra_cash': _ui_tr(
        'Collect extra cash via Personal Card Payment.',
        'Shaxsiy karta to\'lovi orqali qo\'shimcha naqd yig\'ing.',
        'Соберите доплату наличными через «Оплата на личную карту».',
    ),
    'ui.orders.back_to_edit': _ui_tr('Back to edit', 'Tahrirga qaytish', 'Назад к редактированию'),
    'ui.orders.confirm_apply': _ui_tr('Confirm and apply', 'Tasdiqlash va qo\'llash', 'Подтвердить и применить'),
    'ui.orders.total_before': _ui_tr('Total before', 'Eski jami', 'Сумма до'),
    'ui.orders.total_after': _ui_tr('Total after', 'Yangi jami', 'Сумма после'),
    'ui.orders.subtotal_before': _ui_tr('Subtotal before', 'Eski oraliq jami', 'Промежуточная сумма до'),
    'ui.orders.subtotal_after': _ui_tr('Subtotal after', 'Yangi oraliq jami', 'Промежуточная сумма после'),
    'ui.orders.cascade_impact': _ui_tr('Cascade Impact', 'Kaskad ta\'siri', 'Каскадный эффект'),
    'ui.orders.payment_impact': _ui_tr('Payment', 'To\'lov', 'Оплата'),
    'ui.orders.loyalty_impact': _ui_tr('Loyalty', 'Sodiqlik', 'Лояльность'),
    'ui.orders.bottle_impact': _ui_tr('Bottle balance', 'Idish qoldig\'i', 'Баланс тары'),
    'ui.orders.corporate_impact': _ui_tr('Corporate contract', 'Korporativ shartnoma', 'Корпоративный договор'),
    'ui.orders.payment_prepayment': _ui_tr(
        'Prepayment credit will be recorded',
        'Oldindan to\'lov krediti qayd etiladi',
        'Будет записан кредит предоплаты',
    ),
    'ui.orders.payment_extra_cash': _ui_tr(
        'Collect extra in CASH via Personal Card Payment (card will not be re-charged)',
        'Shaxsiy karta to\'lovi orqali NAQD yig\'ing (karta qayta yechilmaydi)',
        'Соберите доплату НАЛИЧНЫМИ через «Оплата на личную карту» (карта повторно не списывается)',
    ),
    'ui.orders.payment_totals_only': _ui_tr('Totals updated; no payment change', 'Jami yangilandi; to\'lov o\'zgarmadi', 'Итоги обновлены; оплата не изменилась'),
    'ui.orders.loyalty_change': _ui_tr('Loyalty AquaCoins change', 'Sodiqlik AquaCoins o\'zgarishi', 'Изменение AquaCoins лояльности'),
    'ui.orders.no_bottle_change': _ui_tr('No bottle balance change', 'Idish qoldig\'i o\'zgarmaydi', 'Баланс тары не изменяется'),
    'ui.orders.no_corporate_change': _ui_tr('No corporate ledger change', 'Korporativ buxgalteriya o\'zgarmaydi', 'Корпоративная книга не изменяется'),
    'ui.orders.corporate_manual': _ui_tr(
        'Finance must reconcile contract ledger manually',
        'Moliya korporativ buxgalteriyani qo\'lda solishtirishi kerak',
        'Финансы должны вручную сверить корпоративную книгу',
    ),
    'ui.orders.corporate_adjusted': _ui_tr(
        'Contract reserve ledger will be adjusted',
        'Shartnoma rezerv buxgalteriyasi tuzatiladi',
        'Резервная книга договора будет скорректирована',
    ),
    'ui.orders.session_will_reopen': _ui_tr(
        'Driver bottle session will be reopened',
        'Haydovchining idish sessiyasi qayta ochiladi',
        'Сессия по таре курьера будет переоткрыта',
    ),
    # ---- Collected-cash admin edit ----
    'ui.orders.edit_collected_cash': _ui_tr(
        'Edit collected cash',
        "Yig'ilgan summani tahrirlash",
        'Изменить собранную сумму',
    ),
    'ui.orders.collected_cash_confirm': _ui_tr(
        'Confirm cash correction',
        'Tuzatishni tasdiqlang',
        'Подтвердите корректировку',
    ),
    'ui.orders.collected_cash_hint': _ui_tr(
        'Enter the actual cash the driver collected. Any surplus over the order total becomes the customer\'s prepaid credit.',
        'Haydovchi yig\'gan haqiqiy summani kiriting. Buyurtma summasidan ortig\'i mijozning oldindan to\'lovi bo\'ladi.',
        'Введите фактически собранную курьером сумму. Излишек сверх суммы заказа станет предоплатой клиента.',
    ),
    'ui.orders.new_collected_amount': _ui_tr(
        'Actual collected amount',
        'Haqiqiy yig\'ilgan summa',
        'Фактически собранная сумма',
    ),
    'ui.orders.collected_cash_reason': _ui_tr('Reason', 'Sabab', 'Причина'),
    'ui.orders.collected_cash_amount_required': _ui_tr('Enter the collected amount', 'Summani kiriting', 'Введите сумму'),
    'ui.orders.collected_cash_reason_required': _ui_tr('Reason (min 5 chars) is required', 'Sabab (kamida 5 belgi) kerak', 'Требуется причина (мин. 5 симв.)'),
    'ui.orders.cash_session_will_reopen': _ui_tr('Driver session reopen', 'Haydovchi sessiyasi qayta ochiladi', 'Переоткрытие сессии курьера'),
    'ui.orders.preview_impact': _ui_tr('Preview impact', "Ko'rib chiqish", 'Предпросмотр'),
    'ui.orders.surplus_or_shortfall': _ui_tr(
        'Surplus / shortfall',
        'Ortiqcha / kamomad',
        'Излишек / недостача',
    ),
    'ui.orders.customer_credit': _ui_tr(
        'Customer credit change',
        "Mijoz krediti o'zgarishi",
        'Изменение кредита клиента',
    ),
    'ui.orders.apply_correction': _ui_tr('Apply correction', "Qo'llash", 'Применить'),
    'ui.orders.collected_cash_updated': _ui_tr(
        'Collected cash updated',
        'Yig\'ilgan summa yangilandi',
        'Собранная сумма обновлена',
    ),
    'ui.orders.collected_cash_failed': _ui_tr(
        'Failed to update collected cash',
        'Summani yangilab bo\'lmadi',
        'Не удалось обновить сумму',
    ),
    'ui.orders.collected_cash_warnings': _ui_tr(
        'Please note',
        "E'tibor bering",
        'Обратите внимание',
    ),
    'ui.orders.bottles': _ui_tr('bottles', 'idishlar', 'бутыли'),
    # Order Changes (history) section
    'ui.orders.order_changes': _ui_tr('Order Changes', 'Buyurtma o\'zgarishlari', 'Изменения заказа'),
    'ui.orders.no_edit_history': _ui_tr(
        'No admin edits recorded for this order',
        'Ushbu buyurtma uchun admin tahrirlari yo\'q',
        'Для этого заказа нет правок администратора',
    ),
    'ui.orders.edited_at': _ui_tr('When', 'Vaqti', 'Когда'),
    'ui.orders.edited_by': _ui_tr('By', 'Kim tomonidan', 'Кем'),
    'ui.orders.edit_post_delivery': _ui_tr('Post-delivery', 'Yetkazib berilgandan keyin', 'После доставки'),
    'ui.orders.items_before': _ui_tr('Items before', 'Oldingi mahsulotlar', 'Товары до'),
    'ui.orders.items_after': _ui_tr('Items after', 'Yangi mahsulotlar', 'Товары после'),
    'ui.orders.totals_before': _ui_tr('Totals before', 'Eski jami', 'Итоги до'),
    'ui.orders.totals_after': _ui_tr('Totals after', 'Yangi jami', 'Итоги после'),
}

ADMIN_UI_PRODUCT_TRANSLATIONS = {
    'ui.common.archive': _ui_tr('Archive', 'Arxivlash', 'Архивировать'),
    'ui.common.disabled': _ui_tr('Disabled', "O'chirilgan", 'Отключено'),
    'ui.common.enabled': _ui_tr('Enabled', 'Yoqilgan', 'Включено'),
    'ui.common.failed': _ui_tr('Failed', 'Muvaffaqiyatsiz', 'Неудачно'),
    'ui.common.no': _ui_tr('No', "Yo'q", 'Нет'),
    'ui.common.refresh': _ui_tr('Refresh', 'Yangilash', 'Обновить'),
    'ui.common.success': _ui_tr('Success', 'Muvaffaqiyatli', 'Успешно'),
    'ui.common.yes': _ui_tr('Yes', 'Ha', 'Да'),
    'ui.products.actions': _ui_tr('Actions', 'Amallar', 'Действия'),
    'ui.products.add_marking_codes': _ui_tr('Add Marking Codes', "Markirovka kodlarini qo'shish", 'Добавить коды маркировки'),
    'ui.products.add_new_product': _ui_tr('Add New Product', "Yangi mahsulot qo'shish", 'Добавить новый товар'),
    'ui.products.add_product': _ui_tr('Add Product', "Mahsulot qo'shish", 'Добавить товар'),
    'ui.products.archive': _ui_tr('Archive'),
    'ui.products.archive_marking_code': _ui_tr('Archive marking code'),
    'ui.products.archived': _ui_tr('Archived', 'Arxivlangan', 'Архивировано'),
    'ui.products.available': _ui_tr('Available', 'Mavjud', 'Доступно'),
    'ui.products.available_codes': _ui_tr('Available codes', 'Mavjud kodlar', 'Доступные коды'),
    'ui.products.barcode': _ui_tr('Barcode', 'Shtrix-kod', 'Штрихкод'),
    'ui.products.category': _ui_tr('Category', 'Kategoriya', 'Категория'),
    'ui.products.category_label': _ui_tr('Category'),
    'ui.products.category_placeholder': _ui_tr('Select category'),
    'ui.products.category_required': _ui_tr('Category is required'),
    'ui.products.close': _ui_tr('Close', 'Yopish', 'Закрыть'),
    'ui.products.create_failed': _ui_tr('Failed to create product'),
    'ui.products.create_marking_codes': _ui_tr('Create Codes'),
    'ui.products.create_product': _ui_tr('Create Product', 'Mahsulot yaratish', 'Создать товар'),
    'ui.products.created': _ui_tr('Created', 'Yaratilgan', 'Создано'),
    'ui.products.created_success': _ui_tr('Product created successfully'),
    'ui.products.delete': _ui_tr('Delete'),
    'ui.products.delete_failed': _ui_tr('Failed to delete product'),
    'ui.products.delete_product': _ui_tr('Delete Product'),
    'ui.products.delete_product_confirm': _ui_tr('Delete product'),
    'ui.products.delete_product_title': _ui_tr('Delete product'),
    'ui.products.deleted_success': _ui_tr('Product deleted successfully'),
    'ui.products.description': _ui_tr('Description', 'Tavsif', 'Описание'),
    'ui.products.description_en': _ui_tr('Description (EN)'),
    'ui.products.description_label': _ui_tr('Description'),
    'ui.products.description_placeholder': _ui_tr('Description'),
    'ui.products.description_ru': _ui_tr('Description (RU)'),
    'ui.products.edit': _ui_tr('Edit'),
    'ui.products.edit_marking_code': _ui_tr('Edit Marking Code', 'Markirovka kodini tahrirlash', 'Редактировать код маркировки'),
    'ui.products.edit_product': _ui_tr('Edit Product', 'Mahsulotni tahrirlash', 'Редактировать товар'),
    'ui.products.edit_product_title': _ui_tr('Edit Product'),
    'ui.products.export_csv': _ui_tr('Export CSV', 'CSV eksport', 'Экспорт CSV'),
    'ui.products.export_failed': _ui_tr('Export failed'),
    'ui.products.export_products': _ui_tr('Export Products', 'Mahsulotlarni eksport qilish', 'Экспорт товаров'),
    'ui.products.featured_product_label': _ui_tr('Featured Product'),
    'ui.products.filter_by_category': _ui_tr('Filter by category'),
    'ui.products.filter_by_status': _ui_tr('Filter by status'),
    'ui.products.filter_marking_codes': _ui_tr('Filter by status'),
    'ui.products.fiscal_enabled': _ui_tr('Fiscal enabled'),
    'ui.products.fiscal_profile': _ui_tr('Fiscal Profile', 'Fiscal profil', 'Фискальный профиль'),
    'ui.products.fiscalization_enabled': _ui_tr('Fiscalization Enabled', 'Fiskalizatsiya yoqilgan', 'Фискализация включена'),
    'ui.products.fiscalized_products': _ui_tr('Fiscalized Products', 'Fiskal mahsulotlar', 'Фискализируемые товары'),
    'ui.products.image': _ui_tr('Image', 'Rasm', 'Изображение'),
    'ui.products.image_too_large': _ui_tr('Image is too large'),
    'ui.products.image_upload_failed': _ui_tr('Image upload failed'),
    'ui.products.import_csv': _ui_tr('Import CSV', 'CSV import', 'Импорт CSV'),
    'ui.products.is_tryout_eligible': _ui_tr('Try-out Eligible'),
    'ui.products.low_marking_stock': _ui_tr('Low labels'),
    'ui.products.low_marking_stock_products': _ui_tr('Low Marking-Code Stock', 'Kam markirovka zaxirasi', 'Низкий запас кодов маркировки'),
    'ui.products.low_stock_items': _ui_tr('Low Stock Items', 'Kam qoldiqli mahsulotlar', 'Товары с низким остатком'),
    'ui.products.marked_product': _ui_tr('Marked', 'Markirovkalangan', 'Маркируемый'),
    'ui.products.marking_code': _ui_tr('Marking Code', 'Markirovka kodi', 'Код маркировки'),
    'ui.products.marking_code_import_issues': _ui_tr('CSV import completed with issues'),
    'ui.products.marking_code_required': _ui_tr('Marking code is required'),
    'ui.products.marking_code_status_archived': _ui_tr('Archived', 'Arxivlangan', 'Архивирован'),
    'ui.products.marking_code_status_available': _ui_tr('Available', 'Mavjud', 'Доступен'),
    'ui.products.marking_code_status_available_pre_utilised': _ui_tr('Available (pre-utilised)', 'Mavjud (oldindan faollashtirilgan)', 'Доступен (предв. утилизирован)'),
    'ui.products.marking_code_status_available_unutilised': _ui_tr('Available (not utilised)', 'Mavjud (faollashtirilmagan)', 'Доступен (не утилизирован)'),
    'ui.products.marking_code_status_reserved': _ui_tr('Reserved', 'Band qilingan', 'Зарезервирован'),
    'ui.products.marking_code_status_used': _ui_tr('Used', 'Ishlatilgan', 'Использован'),
    'ui.products.marking_code_threshold': _ui_tr('Low-Stock Threshold'),
    'ui.products.marking_code_update_failed': _ui_tr('Failed to update marking code'),
    'ui.products.marking_code_updated': _ui_tr('Marking code updated successfully'),
    'ui.products.marking_codes': _ui_tr('Marking Codes', 'Markirovka kodlari', 'Коды маркировки'),
    'ui.products.marking_codes_create_failed': _ui_tr('Failed to create marking codes'),
    'ui.products.marking_codes_created': _ui_tr('{{count}} marking codes created'),
    'ui.products.marking_codes_help': _ui_tr('Enter one code per line or comma-separated'),
    'ui.products.marking_codes_import_failed': _ui_tr('Failed to import marking codes'),
    'ui.products.marking_codes_imported': _ui_tr('{{count}} marking codes imported'),
    'ui.products.marking_codes_low_stock_alert': _ui_tr('Marking-code stock is below the operational threshold'),
    'ui.products.marking_codes_required': _ui_tr('Enter at least one code'),
    'ui.products.missing_fiscal_fields': _ui_tr('Fiscal profile is incomplete'),
    'ui.products.no_marking_codes_required': _ui_tr('No labels required'),
    'ui.products.notes': _ui_tr('Notes', 'Izohlar', 'Примечания'),
    'ui.products.only_csv_allowed': _ui_tr('Only CSV files are allowed'),
    'ui.products.only_images_allowed': _ui_tr('Only image files are allowed'),
    'ui.products.overview': _ui_tr('Overview', "Umumiy ko'rinish", 'Обзор'),
    'ui.products.package_code': _ui_tr('Package Code', 'Qadoq kodi', 'Код упаковки'),
    'ui.products.pagination_text': _ui_tr('products'),
    'ui.products.price': _ui_tr('Price', 'Narx', 'Цена'),
    'ui.products.price_label': _ui_tr('Price'),
    'ui.products.price_required': _ui_tr('Price is required'),
    'ui.products.product_basics': _ui_tr('Product Basics', "Mahsulot asosiy ma'lumotlari", 'Основные данные товара'),
    'ui.products.product_details': _ui_tr('Product Details', 'Mahsulot tafsilotlari', 'Детали товара'),
    'ui.products.product_image_label': _ui_tr('Product Image'),
    'ui.products.product_name': _ui_tr('Product', 'Mahsulot', 'Товар'),
    'ui.products.product_name_en': _ui_tr('Product name (EN)'),
    'ui.products.product_name_label': _ui_tr('Product name'),
    'ui.products.product_name_placeholder': _ui_tr('Product name'),
    'ui.products.product_name_required': _ui_tr('Product name is required'),
    'ui.products.product_name_ru': _ui_tr('Product name (RU)'),
    'ui.products.product_operations': _ui_tr('Operational Settings', 'Operatsion sozlamalar', 'Операционные настройки'),
    'ui.products.requires_marking_codes': _ui_tr('Requires Marking Codes', 'Markirovka kodlari talab qilinadi', 'Требуются коды маркировки'),
    'ui.products.reserved': _ui_tr('Reserved', 'Band qilingan', 'Зарезервировано'),
    'ui.products.restore': _ui_tr('Restore', 'Qayta tiklash', 'Восстановить'),
    'ui.products.returnable_bottles_per_unit': _ui_tr('Returnable Bottles Per Unit'),
    'ui.products.search_marking_codes': _ui_tr('Search marking codes'),
    'ui.products.search_placeholder': _ui_tr('Search products'),
    'ui.products.select_csv': _ui_tr('Select CSV'),
    'ui.products.select_csv_file': _ui_tr('Select a CSV file first'),
    'ui.products.sku': _ui_tr('SKU', 'SKU', 'SKU'),
    'ui.products.sku_label': _ui_tr('SKU'),
    'ui.products.sku_placeholder': _ui_tr('SKU'),
    'ui.products.sku_required': _ui_tr('SKU is required'),
    'ui.products.spic': _ui_tr('SPIC', 'SPIC', 'SPIC'),
    'ui.products.status': _ui_tr('Status', 'Holat', 'Статус'),
    'ui.products.status_label': _ui_tr('Status'),
    'ui.products.status_placeholder': _ui_tr('Select status'),
    'ui.products.status_required': _ui_tr('Status is required'),
    'ui.products.stock': _ui_tr('Stock', 'Qoldiq', 'Остаток'),
    'ui.products.stock_quantity_label': _ui_tr('Stock Quantity'),
    'ui.products.stock_quantity_required': _ui_tr('Stock quantity is required'),
    'ui.products.min_order_quantity_label': _ui_tr(
        'Minimum Order Quantity',
        'Minimal buyurtma soni',
        'Минимальное количество заказа',
    ),
    'ui.products.min_order_quantity_tooltip': _ui_tr(
        'Customers cannot check out with fewer units of this product than this minimum.',
        "Mijozlar ushbu mahsulotning ushbu minimumdan kam birligini xarid qila olmaydi.",
        'Покупатели не смогут оформить заказ с меньшим количеством этого товара.',
    ),
    'ui.products.min_order_quantity_min': _ui_tr(
        'Must be at least 1',
        "Kamida 1 bo'lishi kerak",
        'Должно быть не меньше 1',
    ),
    'ui.products.total_inventory_value': _ui_tr('Total Inventory Value', 'Jami ombor qiymati', 'Общая стоимость запасов'),
    'ui.products.total_products': _ui_tr('Total Products', 'Jami mahsulotlar', 'Всего товаров'),
    'ui.products.tracks_returnable_bottles': _ui_tr('Tracks Returnable Bottles'),
    'ui.products.units': _ui_tr('Units', "O'lchov birligi", 'Единицы'),
    'ui.products.update_failed': _ui_tr('Failed to update product'),
    'ui.products.update_product': _ui_tr('Update Product', 'Mahsulotni yangilash', 'Обновить товар'),
    'ui.products.updated_success': _ui_tr('Product updated successfully'),
    'ui.products.upload_image': _ui_tr('Upload image'),
    'ui.products.used': _ui_tr('Used', 'Ishlatilgan', 'Использовано'),
    'ui.products.used_at': _ui_tr('Used', 'Ishlatilgan vaqti', 'Использовано'),
    'ui.products.vat_percent': _ui_tr('VAT %', 'QQS %', 'НДС %'),
    'ui.products.view_details': _ui_tr('View Details', "Batafsil ko'rish", 'Посмотреть детали'),
    'ui.products.volume': _ui_tr('Volume', 'Hajm', 'Объем'),
    'ui.products.volume_label': _ui_tr('Volume'),
    'ui.products.volume_required': _ui_tr('Volume is required'),
}

BACKEND_TRANSLATIONS.update(ADMIN_UI_ORDER_TRANSLATIONS)
BACKEND_TRANSLATIONS.update(ADMIN_UI_PRODUCT_TRANSLATIONS)
# END AUTO-GENERATED ADMIN UI ORDER/PRODUCT TRANSLATIONS


# ===========================================================================
# Admin UI — Marking Code Operations page
# ===========================================================================

ADMIN_UI_MARKING_CODE_TRANSLATIONS = {
    'ui.nav.marking_codes': _ui_tr('Marking Codes', 'Markirovka kodlari', 'Маркировочные коды'),

    'ui.marking_codes.title': _ui_tr(
        'Marking Code Operations',
        'Markirovka kodlari operatsiyalari',
        'Операции с маркировочными кодами',
    ),
    'ui.marking_codes.tabs.schedule': _ui_tr('Schedule & Config', 'Jadval va sozlamalar', 'Расписание и настройки'),
    'ui.marking_codes.tabs.runs': _ui_tr('Task Runs', 'Vazifa ishga tushishlari', 'Запуски задач'),
    'ui.marking_codes.tabs.pool': _ui_tr('Pool Status', 'Hovuz holati', 'Состояние пула'),

    'ui.marking_codes.section.schedule': _ui_tr('Schedule', 'Jadval', 'Расписание'),
    'ui.marking_codes.section.target_sizing': _ui_tr('Target sizing', 'Maqsadli hajm', 'Целевой размер'),
    'ui.marking_codes.section.thresholds': _ui_tr('Thresholds', "Bo'sag'alar", 'Пороги'),
    'ui.marking_codes.section.tc_behavior': _ui_tr(
        'Tax Committee behavior',
        'Soliq qo‘mitasi xulq-atvori',
        'Поведение налогового комитета',
    ),

    'ui.marking_codes.schedule.daily': _ui_tr('Daily', 'Har kuni', 'Ежедневно'),
    'ui.marking_codes.schedule.weekly': _ui_tr('Weekly', 'Haftalik', 'Еженедельно'),
    'ui.marking_codes.schedule.interval': _ui_tr(
        'Every N days', 'Har N kunda', 'Каждые N дней',
    ),

    'ui.marking_codes.fields.schedule_type': _ui_tr('Frequency', 'Davriylik', 'Частота'),
    'ui.marking_codes.fields.day_of_week': _ui_tr('Day of week', 'Hafta kuni', 'День недели'),
    'ui.marking_codes.fields.interval_days': _ui_tr(
        'Run every N days', 'Har N kunda ishga tushirish', 'Запускать каждые N дней',
    ),
    'ui.marking_codes.fields.execution_time': _ui_tr(
        'Execution time (UTC)', "Bajarilish vaqti (UTC)", 'Время выполнения (UTC)',
    ),

    'ui.marking_codes.actions.save': _ui_tr('Save', 'Saqlash', 'Сохранить'),
    'ui.marking_codes.actions.save_success': _ui_tr(
        'Saved. The beat container will reload within ~1 minute.',
        'Saqlandi. Beat konteyneri ~1 daqiqada qayta yuklanadi.',
        'Сохранено. Контейнер beat перезагрузится в течение ~1 минуты.',
    ),
    'ui.marking_codes.actions.run_all': _ui_tr(
        'Run for all products', 'Barcha mahsulotlar uchun ishga tushirish', 'Запустить для всех товаров',
    ),
    'ui.marking_codes.actions.run_product': _ui_tr(
        'Run for this product', 'Bu mahsulot uchun ishga tushirish', 'Запустить для этого товара',
    ),
    'ui.marking_codes.actions.edit_overrides': _ui_tr(
        'Edit overrides', "O'zgartirishlarni tahrirlash", 'Изменить переопределения',
    ),
    'ui.marking_codes.actions.run_enqueued': _ui_tr(
        'Run enqueued', 'Ishga tushirish navbatga qo‘shildi', 'Запуск поставлен в очередь',
    ),
    'ui.marking_codes.actions.overrides_saved': _ui_tr(
        'Overrides saved', "O'zgartirishlar saqlandi", 'Переопределения сохранены',
    ),
}

BACKEND_TRANSLATIONS.update(ADMIN_UI_MARKING_CODE_TRANSLATIONS)


# ===========================================================================
# Admin UI — Orders page additions for terminally-failed Click fiscalizations
# (Alerts column badge + filter switch to show only those orders)
# ===========================================================================

ADMIN_UI_FISCALIZATION_FAILURES_TRANSLATIONS = {
    'ui.orders.alerts': _ui_tr('Alerts', 'Ogohlantirishlar', 'Оповещения'),
    'ui.orders.fiscalization_retries_exhausted': _ui_tr(
        'Fiscalization Failed',
        'Fiskalizatsiya muvaffaqiyatsiz',
        'Фискализация не удалась',
    ),
    'ui.orders.fiscalization_failed_only': _ui_tr(
        'Fiscalization failed only',
        'Faqat muvaffaqiyatsiz fiskalizatsiyalar',
        'Только сбои фискализации',
    ),
}

BACKEND_TRANSLATIONS.update(ADMIN_UI_FISCALIZATION_FAILURES_TRANSLATIONS)


# ===========================================================================
# Admin UI — Backfill block for keys discovered via scripts/audit_translation_keys.py.
# Most are short label phrases for the Analytics, Blog, Delivery, Dashboard,
# Login, Nav, Orders, Users, Common, Corporate, Settings, Role, User-menu and
# Marking-codes pages. Grouped by sub-namespace for readability.
# ===========================================================================

ADMIN_UI_BACKFILL_TRANSLATIONS = {
    # ---- ui.analytics.* ----
    'ui.analytics.active_customers': _ui_tr('Active Customers', 'Faol mijozlar', 'Активные клиенты'),
    'ui.analytics.at_risk_customers': _ui_tr('At-Risk Customers', 'Xavf ostidagi mijozlar', 'Клиенты под угрозой оттока'),
    'ui.analytics.avg_delivery_time': _ui_tr('Avg Delivery Time', 'O\'rtacha yetkazib berish vaqti', 'Среднее время доставки'),
    'ui.analytics.avg_order_value': _ui_tr('Avg Order Value', 'O\'rtacha buyurtma qiymati', 'Средний чек'),
    'ui.analytics.churn_rate': _ui_tr('Churn Rate', 'Mijozlar yo\'qotish darajasi', 'Уровень оттока'),
    'ui.analytics.confidence_level': _ui_tr('Confidence Level', 'Ishonch darajasi', 'Уровень достоверности'),
    'ui.analytics.conversion_rate': _ui_tr('Conversion Rate', 'Konversiya darajasi', 'Конверсия'),
    'ui.analytics.customer': _ui_tr('Customer', 'Mijoz', 'Клиент'),
    'ui.analytics.customer_churn': _ui_tr('Customer Churn', 'Mijozlar yo\'qotish', 'Отток клиентов'),
    'ui.analytics.customer_churn_risk_analysis': _ui_tr('Customer Churn Risk Analysis', 'Mijozlar yo\'qotish xavfi tahlili', 'Анализ риска оттока клиентов'),
    'ui.analytics.customer_segments': _ui_tr('Customer Segments', 'Mijoz segmentlari', 'Сегменты клиентов'),
    'ui.analytics.deliveries': _ui_tr('Deliveries', 'Yetkazib berishlar', 'Доставки'),
    'ui.analytics.delivery_performance': _ui_tr('Delivery Performance', 'Yetkazib berish samaradorligi', 'Эффективность доставки'),
    'ui.analytics.export_report': _ui_tr('Export Report', 'Hisobotni eksport qilish', 'Экспортировать отчёт'),
    'ui.analytics.failed_deliveries': _ui_tr('Failed Deliveries', 'Muvaffaqiyatsiz yetkazib berishlar', 'Неудачные доставки'),
    'ui.analytics.forecast_factors': _ui_tr('Forecast Factors', 'Prognoz omillari', 'Факторы прогноза'),
    'ui.analytics.forecasted_revenue': _ui_tr('Forecasted Revenue', 'Bashorat qilingan daromad', 'Прогнозируемая выручка'),
    'ui.analytics.growth_rate': _ui_tr('Growth Rate', 'O\'sish darajasi', 'Темп роста'),
    'ui.analytics.high_risk_customers': _ui_tr('High Risk Customers', 'Yuqori xavfli mijozlar', 'Клиенты высокого риска'),
    'ui.analytics.historical_revenue': _ui_tr('Historical Revenue', 'Tarixiy daromad', 'Историческая выручка'),
    'ui.analytics.hours': _ui_tr('Hours', 'Soatlar', 'Часы'),
    'ui.analytics.hrs': _ui_tr('hrs', 'soat', 'ч'),
    'ui.analytics.impact': _ui_tr('Impact', 'Ta\'sir', 'Влияние'),
    'ui.analytics.last_30_days': _ui_tr('Last 30 days', 'So\'nggi 30 kun', 'Последние 30 дней'),
    'ui.analytics.last_7_days': _ui_tr('Last 7 days', 'So\'nggi 7 kun', 'Последние 7 дней'),
    'ui.analytics.last_90_days': _ui_tr('Last 90 days', 'So\'nggi 90 kun', 'Последние 90 дней'),
    'ui.analytics.last_year': _ui_tr('Last year', 'O\'tgan yil', 'Прошлый год'),
    'ui.analytics.monthly_orders': _ui_tr('Monthly Orders', 'Oylik buyurtmalar', 'Заказы за месяц'),
    'ui.analytics.monthly_revenue': _ui_tr('Monthly Revenue', 'Oylik daromad', 'Выручка за месяц'),
    'ui.analytics.next_month_forecast': _ui_tr('Next Month Forecast', 'Keyingi oy prognozi', 'Прогноз на следующий месяц'),
    'ui.analytics.next_quarter_forecast': _ui_tr('Next Quarter Forecast', 'Keyingi chorak prognozi', 'Прогноз на следующий квартал'),
    'ui.analytics.on_time_rate': _ui_tr('On-Time Rate', 'O\'z vaqtida yetkazib berish darajasi', 'Доля доставок вовремя'),
    'ui.analytics.orders': _ui_tr('Orders', 'Buyurtmalar', 'Заказы'),
    'ui.analytics.overall_on_time_rate': _ui_tr('Overall On-Time Rate', 'Umumiy o\'z vaqtida darajasi', 'Общая доля доставок вовремя'),
    'ui.analytics.overview': _ui_tr('Overview', 'Umumiy ko\'rinish', 'Обзор'),
    'ui.analytics.performance': _ui_tr('Performance', 'Samaradorlik', 'Эффективность'),
    'ui.analytics.recent_insights': _ui_tr('Recent Insights', 'So\'nggi tahlillar', 'Недавние инсайты'),
    'ui.analytics.regional_delivery_performance': _ui_tr('Regional Delivery Performance', 'Hududiy yetkazib berish samaradorligi', 'Эффективность доставки по регионам'),
    'ui.analytics.revenue': _ui_tr('Revenue', 'Daromad', 'Выручка'),
    'ui.analytics.revenue_forecast': _ui_tr('Revenue Forecast', 'Daromad prognozi', 'Прогноз выручки'),
    'ui.analytics.revenue_forecast_analysis': _ui_tr('Revenue Forecast Analysis', 'Daromad prognozi tahlili', 'Анализ прогноза выручки'),
    'ui.analytics.revenue_trend': _ui_tr('Revenue Trend', 'Daromad tendentsiyasi', 'Динамика выручки'),
    'ui.analytics.risk_level': _ui_tr('Risk Level', 'Xavf darajasi', 'Уровень риска'),
    'ui.analytics.risk_score': _ui_tr('Risk Score', 'Xavf bahosi', 'Оценка риска'),
    'ui.analytics.sales': _ui_tr('Sales', 'Sotuvlar', 'Продажи'),
    'ui.analytics.sales_performance_over_time': _ui_tr('Sales Performance Over Time', 'Vaqt davomida sotuvlar samaradorligi', 'Динамика продаж'),
    'ui.analytics.sales_trends': _ui_tr('Sales Trends', 'Sotuvlar tendentsiyasi', 'Тенденции продаж'),
    'ui.analytics.segment_active': _ui_tr('Active', 'Faol', 'Активные'),
    'ui.analytics.segment_at_risk': _ui_tr('At Risk', 'Xavf ostida', 'Под угрозой'),
    'ui.analytics.segment_inactive': _ui_tr('Inactive', 'Faol emas', 'Неактивные'),
    'ui.analytics.segment_loyal': _ui_tr('Loyal', 'Sodiq', 'Лояльные'),
    'ui.analytics.segment_new': _ui_tr('New', 'Yangi', 'Новые'),
    'ui.analytics.top_products': _ui_tr('Top Products', 'Eng yaxshi mahsulotlar', 'Лучшие товары'),
    'ui.analytics.total_revenue': _ui_tr('Total Revenue', 'Jami daromad', 'Общая выручка'),

    # ---- top-level ui.* singletons ----
    'ui.app_name_full': _ui_tr('Aqua Element Admin Panel', 'Aqua Element Admin paneli', 'Панель администратора Aqua Element'),
    'ui.app_name_short': _ui_tr('Aqua Element', 'Aqua Element', 'Aqua Element'),
    'ui.language': _ui_tr('Language', 'Til', 'Язык'),
    'ui.loyalty.no_reward_details': _ui_tr('No reward details available', 'Mukofot tafsilotlari mavjud emas', 'Подробности награды отсутствуют'),
    'ui.status.offline': _ui_tr('Offline', 'Oflayn', 'Офлайн'),
    'ui.sync_failed': _ui_tr('Sync failed', 'Sinxronlashtirish muvaffaqiyatsiz', 'Синхронизация не удалась'),
    'ui.sync_success': _ui_tr('Synced successfully', 'Muvaffaqiyatli sinxronlashtirildi', 'Синхронизация выполнена'),
    'ui.sync_translations': _ui_tr('Sync translations', 'Tarjimalarni sinxronlash', 'Синхронизировать переводы'),

    # ---- ui.blog.* ----
    'ui.blog.actions': _ui_tr('Actions', 'Amallar', 'Действия'),
    'ui.blog.blog_posts': _ui_tr('Blog Posts', 'Blog yozuvlari', 'Записи блога'),
    'ui.blog.cancel': _ui_tr('Cancel', 'Bekor qilish', 'Отмена'),
    'ui.blog.category': _ui_tr('Category', 'Kategoriya', 'Категория'),
    'ui.blog.category_company_news': _ui_tr('Company News', 'Kompaniya yangiliklari', 'Новости компании'),
    'ui.blog.category_environment': _ui_tr('Environment', 'Atrof-muhit', 'Экология'),
    'ui.blog.category_health_tips': _ui_tr('Health Tips', 'Sog\'liq bo\'yicha maslahatlar', 'Советы по здоровью'),
    'ui.blog.category_lifestyle': _ui_tr('Lifestyle', 'Hayot tarzi', 'Образ жизни'),
    'ui.blog.category_quality_assurance': _ui_tr('Quality Assurance', 'Sifat nazorati', 'Контроль качества'),
    'ui.blog.category_water_benefits': _ui_tr('Water Benefits', 'Suvning foydalari', 'Польза воды'),
    'ui.blog.create': _ui_tr('Create', 'Yaratish', 'Создать'),
    'ui.blog.create_blog_post': _ui_tr('Create Blog Post', 'Blog yozuvini yaratish', 'Создать запись блога'),
    'ui.blog.create_failed': _ui_tr('Failed to create post', 'Yozuvni yaratib bo\'lmadi', 'Не удалось создать запись'),
    'ui.blog.created_success': _ui_tr('Post created successfully', 'Yozuv muvaffaqiyatli yaratildi', 'Запись успешно создана'),
    'ui.blog.delete': _ui_tr('Delete', 'O\'chirish', 'Удалить'),
    'ui.blog.delete_confirm': _ui_tr('Are you sure you want to delete this post?', 'Ushbu yozuvni o\'chirishni xohlaysizmi?', 'Вы уверены, что хотите удалить эту запись?'),
    'ui.blog.delete_failed': _ui_tr('Failed to delete post', 'Yozuvni o\'chirib bo\'lmadi', 'Не удалось удалить запись'),
    'ui.blog.delete_post': _ui_tr('Delete Post', 'Yozuvni o\'chirish', 'Удалить запись'),
    'ui.blog.deleted_success': _ui_tr('Post deleted successfully', 'Yozuv muvaffaqiyatli o\'chirildi', 'Запись успешно удалена'),
    'ui.blog.edit': _ui_tr('Edit', 'Tahrirlash', 'Редактировать'),
    'ui.blog.edit_blog_post': _ui_tr('Edit Blog Post', 'Blog yozuvini tahrirlash', 'Редактировать запись блога'),
    'ui.blog.featured': _ui_tr('Featured', 'Tanlangan', 'Избранное'),
    'ui.blog.form_author_en': _ui_tr('Author (English)', 'Muallif (Inglizcha)', 'Автор (английский)'),
    'ui.blog.form_author_placeholder': _ui_tr('Enter author name', 'Muallif ismini kiriting', 'Введите имя автора'),
    'ui.blog.form_author_ru': _ui_tr('Author (Russian)', 'Muallif (Ruscha)', 'Автор (русский)'),
    'ui.blog.form_author_uz': _ui_tr('Author (Uzbek)', 'Muallif (O\'zbekcha)', 'Автор (узбекский)'),
    'ui.blog.form_category': _ui_tr('Category', 'Kategoriya', 'Категория'),
    'ui.blog.form_category_placeholder': _ui_tr('Select a category', 'Kategoriyani tanlang', 'Выберите категорию'),
    'ui.blog.form_category_required': _ui_tr('Category is required', 'Kategoriya talab qilinadi', 'Категория обязательна'),
    'ui.blog.form_change_image': _ui_tr('Change image', 'Rasmni o\'zgartirish', 'Изменить изображение'),
    'ui.blog.form_content_en': _ui_tr('Content (English)', 'Mazmun (Inglizcha)', 'Содержание (английский)'),
    'ui.blog.form_content_en_required': _ui_tr('English content is required', 'Inglizcha mazmun talab qilinadi', 'Английское содержание обязательно'),
    'ui.blog.form_content_ru': _ui_tr('Content (Russian)', 'Mazmun (Ruscha)', 'Содержание (русский)'),
    'ui.blog.form_content_ru_required': _ui_tr('Russian content is required', 'Ruscha mazmun talab qilinadi', 'Русское содержание обязательно'),
    'ui.blog.form_content_uz': _ui_tr('Content (Uzbek)', 'Mazmun (O\'zbekcha)', 'Содержание (узбекский)'),
    'ui.blog.form_content_uz_required': _ui_tr('Uzbek content is required', 'O\'zbekcha mazmun talab qilinadi', 'Узбекское содержание обязательно'),
    'ui.blog.form_excerpt_en': _ui_tr('Excerpt (English)', 'Qisqacha (Inglizcha)', 'Краткое описание (английский)'),
    'ui.blog.form_excerpt_en_placeholder': _ui_tr('Short summary in English', 'Inglizcha qisqacha bayoni', 'Краткое описание на английском'),
    'ui.blog.form_excerpt_en_required': _ui_tr('English excerpt is required', 'Inglizcha qisqacha talab qilinadi', 'Английское описание обязательно'),
    'ui.blog.form_excerpt_ru': _ui_tr('Excerpt (Russian)', 'Qisqacha (Ruscha)', 'Краткое описание (русский)'),
    'ui.blog.form_excerpt_ru_placeholder': _ui_tr('Short summary in Russian', 'Ruscha qisqacha bayoni', 'Краткое описание на русском'),
    'ui.blog.form_excerpt_ru_required': _ui_tr('Russian excerpt is required', 'Ruscha qisqacha talab qilinadi', 'Русское описание обязательно'),
    'ui.blog.form_excerpt_uz': _ui_tr('Excerpt (Uzbek)', 'Qisqacha (O\'zbekcha)', 'Краткое описание (узбекский)'),
    'ui.blog.form_excerpt_uz_placeholder': _ui_tr('Short summary in Uzbek', 'O\'zbekcha qisqacha bayoni', 'Краткое описание на узбекском'),
    'ui.blog.form_excerpt_uz_required': _ui_tr('Uzbek excerpt is required', 'O\'zbekcha qisqacha talab qilinadi', 'Узбекское описание обязательно'),
    'ui.blog.form_featured_homepage': _ui_tr('Featured on homepage', 'Bosh sahifada tanlangan', 'Избранное на главной'),
    'ui.blog.form_featured_image': _ui_tr('Featured image', 'Asosiy rasm', 'Главное изображение'),
    'ui.blog.form_image_alt': _ui_tr('Image alt text', 'Rasm alt matni', 'Альт-текст изображения'),
    'ui.blog.form_image_alt_placeholder': _ui_tr('Describe the image', 'Rasmni tasvirlab bering', 'Опишите изображение'),
    'ui.blog.form_image_url_placeholder': _ui_tr('Image URL', 'Rasm URL manzili', 'URL изображения'),
    'ui.blog.form_meta_description_en': _ui_tr('Meta description (English)', 'Meta tavsif (Inglizcha)', 'Мета-описание (английский)'),
    'ui.blog.form_meta_description_en_placeholder': _ui_tr('Meta description in English', 'Inglizcha meta tavsif', 'Мета-описание на английском'),
    'ui.blog.form_meta_description_ru': _ui_tr('Meta description (Russian)', 'Meta tavsif (Ruscha)', 'Мета-описание (русский)'),
    'ui.blog.form_meta_description_ru_placeholder': _ui_tr('Meta description in Russian', 'Ruscha meta tavsif', 'Мета-описание на русском'),
    'ui.blog.form_meta_description_uz': _ui_tr('Meta description (Uzbek)', 'Meta tavsif (O\'zbekcha)', 'Мета-описание (узбекский)'),
    'ui.blog.form_meta_description_uz_placeholder': _ui_tr('Meta description in Uzbek', 'O\'zbekcha meta tavsif', 'Мета-описание на узбекском'),
    'ui.blog.form_meta_title_en': _ui_tr('Meta title (English)', 'Meta sarlavha (Inglizcha)', 'Мета-заголовок (английский)'),
    'ui.blog.form_meta_title_en_placeholder': _ui_tr('Meta title in English', 'Inglizcha meta sarlavha', 'Мета-заголовок на английском'),
    'ui.blog.form_meta_title_ru': _ui_tr('Meta title (Russian)', 'Meta sarlavha (Ruscha)', 'Мета-заголовок (русский)'),
    'ui.blog.form_meta_title_ru_placeholder': _ui_tr('Meta title in Russian', 'Ruscha meta sarlavha', 'Мета-заголовок на русском'),
    'ui.blog.form_meta_title_uz': _ui_tr('Meta title (Uzbek)', 'Meta sarlavha (O\'zbekcha)', 'Мета-заголовок (узбекский)'),
    'ui.blog.form_meta_title_uz_placeholder': _ui_tr('Meta title in Uzbek', 'O\'zbekcha meta sarlavha', 'Мета-заголовок на узбекском'),
    'ui.blog.form_or_enter_url': _ui_tr('Or enter URL', 'Yoki URL kiriting', 'Или введите URL'),
    'ui.blog.form_seo_settings': _ui_tr('SEO settings', 'SEO sozlamalari', 'Настройки SEO'),
    'ui.blog.form_settings': _ui_tr('Settings', 'Sozlamalar', 'Настройки'),
    'ui.blog.form_slug': _ui_tr('Slug', 'Slug', 'Slug'),
    'ui.blog.form_slug_placeholder': _ui_tr('url-friendly-slug', 'url-uchun-slug', 'url-дружественный-slug'),
    'ui.blog.form_slug_required': _ui_tr('Slug is required', 'Slug talab qilinadi', 'Slug обязателен'),
    'ui.blog.form_sort_order': _ui_tr('Sort order', 'Tartiblash', 'Порядок сортировки'),
    'ui.blog.form_status': _ui_tr('Status', 'Holati', 'Статус'),
    'ui.blog.form_tags': _ui_tr('Tags', 'Teglar', 'Теги'),
    'ui.blog.form_tags_placeholder': _ui_tr('Comma-separated tags', 'Vergul bilan ajratilgan teglar', 'Теги через запятую'),
    'ui.blog.form_title_en': _ui_tr('Title (English)', 'Sarlavha (Inglizcha)', 'Заголовок (английский)'),
    'ui.blog.form_title_en_placeholder': _ui_tr('Title in English', 'Inglizcha sarlavha', 'Заголовок на английском'),
    'ui.blog.form_title_en_required': _ui_tr('English title is required', 'Inglizcha sarlavha talab qilinadi', 'Английский заголовок обязателен'),
    'ui.blog.form_title_ru': _ui_tr('Title (Russian)', 'Sarlavha (Ruscha)', 'Заголовок (русский)'),
    'ui.blog.form_title_ru_placeholder': _ui_tr('Title in Russian', 'Ruscha sarlavha', 'Заголовок на русском'),
    'ui.blog.form_title_ru_required': _ui_tr('Russian title is required', 'Ruscha sarlavha talab qilinadi', 'Русский заголовок обязателен'),
    'ui.blog.form_title_uz': _ui_tr('Title (Uzbek)', 'Sarlavha (O\'zbekcha)', 'Заголовок (узбекский)'),
    'ui.blog.form_title_uz_placeholder': _ui_tr('Title in Uzbek', 'O\'zbekcha sarlavha', 'Заголовок на узбекском'),
    'ui.blog.form_title_uz_required': _ui_tr('Uzbek title is required', 'O\'zbekcha sarlavha talab qilinadi', 'Узбекский заголовок обязателен'),
    'ui.blog.form_upload_error_type': _ui_tr('Unsupported file type', 'Qo\'llab-quvvatlanmaydigan fayl turi', 'Неподдерживаемый тип файла'),
    'ui.blog.form_upload_failed': _ui_tr('Upload failed', 'Yuklash muvaffaqiyatsiz', 'Не удалось загрузить'),
    'ui.blog.form_upload_image': _ui_tr('Upload image', 'Rasmni yuklash', 'Загрузить изображение'),
    'ui.blog.form_upload_success': _ui_tr('Upload successful', 'Yuklash muvaffaqiyatli', 'Загрузка успешна'),
    'ui.blog.form_uploading': _ui_tr('Uploading...', 'Yuklanmoqda...', 'Загрузка...'),
    'ui.blog.image': _ui_tr('Image', 'Rasm', 'Изображение'),
    'ui.blog.no': _ui_tr('No', 'Yo\'q', 'Нет'),
    'ui.blog.posts': _ui_tr('Posts', 'Yozuvlar', 'Записи'),
    'ui.blog.publish': _ui_tr('Publish', 'E\'lon qilish', 'Опубликовать'),
    'ui.blog.publish_failed': _ui_tr('Failed to publish', 'E\'lon qilib bo\'lmadi', 'Не удалось опубликовать'),
    'ui.blog.published': _ui_tr('Published', 'E\'lon qilingan', 'Опубликовано'),
    'ui.blog.published_success': _ui_tr('Published successfully', 'Muvaffaqiyatli e\'lon qilindi', 'Успешно опубликовано'),
    'ui.blog.search_posts': _ui_tr('Search posts', 'Yozuvlarni qidirish', 'Поиск записей'),
    'ui.blog.status': _ui_tr('Status', 'Holati', 'Статус'),
    'ui.blog.status_archived': _ui_tr('Archived', 'Arxivlangan', 'В архиве'),
    'ui.blog.status_draft': _ui_tr('Draft', 'Qoralama', 'Черновик'),
    'ui.blog.status_published': _ui_tr('Published', 'E\'lon qilingan', 'Опубликовано'),
    'ui.blog.title': _ui_tr('Title', 'Sarlavha', 'Заголовок'),
    'ui.blog.total': _ui_tr('Total', 'Jami', 'Всего'),
    'ui.blog.unpublish': _ui_tr('Unpublish', 'E\'londan olib tashlash', 'Снять с публикации'),
    'ui.blog.unpublish_failed': _ui_tr('Failed to unpublish', 'E\'londan olib tashlab bo\'lmadi', 'Не удалось снять с публикации'),
    'ui.blog.unpublished_success': _ui_tr('Unpublished successfully', 'Muvaffaqiyatli e\'londan olib tashlandi', 'Снято с публикации'),
    'ui.blog.update': _ui_tr('Update', 'Yangilash', 'Обновить'),
    'ui.blog.update_failed': _ui_tr('Update failed', 'Yangilab bo\'lmadi', 'Не удалось обновить'),
    'ui.blog.updated_success': _ui_tr('Updated successfully', 'Muvaffaqiyatli yangilandi', 'Успешно обновлено'),
    'ui.blog.views': _ui_tr('Views', 'Ko\'rishlar', 'Просмотры'),
    'ui.blog.yes': _ui_tr('Yes', 'Ha', 'Да'),
    'ui.blog.yes_delete': _ui_tr('Yes, delete', 'Ha, o\'chirish', 'Да, удалить'),

    # ---- ui.common.* ----
    'ui.common.clear': _ui_tr('Clear', 'Tozalash', 'Очистить'),
    'ui.common.confirm': _ui_tr('Confirm', 'Tasdiqlash', 'Подтвердить'),
    'ui.common.loading': _ui_tr('Loading...', 'Yuklanmoqda...', 'Загрузка...'),
    'ui.common.search': _ui_tr('Search', 'Qidirish', 'Поиск'),
    'ui.common.searching': _ui_tr('Searching...', 'Qidirilmoqda...', 'Поиск...'),

    # ---- ui.corporate.* ----
    'ui.corporate.loyalty_eligible': _ui_tr('Loyalty Eligible', 'Sodiqlikka loyiq', 'Доступна программа лояльности'),
    'ui.corporate.loyalty_ineligible': _ui_tr('Loyalty Ineligible', 'Sodiqlikka loyiq emas', 'Программа лояльности недоступна'),
    'ui.corporate.loyalty_points': _ui_tr('Loyalty AquaCoins', 'Sodiqlik AquaCoins', 'AquaCoins лояльности'),
    'ui.corporate.loyalty_points_eligible': _ui_tr('Loyalty AquaCoins eligible', 'Sodiqlik AquaCoins olishga loyiq', 'Доступно для начисления AquaCoins'),
    'ui.corporate.overlap_conflicts_summary': _ui_tr('Overlap conflicts summary', 'Bir-biriga mos kelmaslik xulosasi', 'Сводка по пересечениям'),

    # ---- ui.dashboard.* ----
    'ui.dashboard.active_deliveries': _ui_tr('Active Deliveries', 'Faol yetkazib berishlar', 'Активные доставки'),
    'ui.dashboard.apr': _ui_tr('Apr', 'Apr', 'Апр'),
    'ui.dashboard.cancelled': _ui_tr('Cancelled', 'Bekor qilingan', 'Отменено'),
    'ui.dashboard.delivered': _ui_tr('Delivered', 'Yetkazib berildi', 'Доставлено'),
    'ui.dashboard.failed_today': _ui_tr('Failed Today', 'Bugun muvaffaqiyatsiz', 'Сбои за сегодня'),
    'ui.dashboard.feb': _ui_tr('Feb', 'Fev', 'Фев'),
    'ui.dashboard.jan': _ui_tr('Jan', 'Yan', 'Янв'),
    'ui.dashboard.jun': _ui_tr('Jun', 'Iyun', 'Июн'),
    'ui.dashboard.mar': _ui_tr('Mar', 'Mar', 'Мар'),
    'ui.dashboard.may': _ui_tr('May', 'May', 'Май'),
    'ui.dashboard.monthly_revenue': _ui_tr('Monthly Revenue', 'Oylik daromad', 'Выручка за месяц'),
    'ui.dashboard.order_status_distribution': _ui_tr('Order Status Distribution', 'Buyurtma holatlari taqsimoti', 'Распределение статусов заказов'),
    'ui.dashboard.orders': _ui_tr('Orders', 'Buyurtmalar', 'Заказы'),
    'ui.dashboard.pending': _ui_tr('Pending', 'Kutilmoqda', 'В ожидании'),
    'ui.dashboard.processing': _ui_tr('Processing', 'Qayta ishlanmoqda', 'Обрабатывается'),
    'ui.dashboard.refresh': _ui_tr('Refresh', 'Yangilash', 'Обновить'),
    'ui.dashboard.refresh_10s': _ui_tr('10s', '10s', '10с'),
    'ui.dashboard.refresh_1m': _ui_tr('1m', '1m', '1мин'),
    'ui.dashboard.refresh_30s': _ui_tr('30s', '30s', '30с'),
    'ui.dashboard.refresh_off': _ui_tr('Off', 'O\'chirilgan', 'Выкл'),
    'ui.dashboard.revenue': _ui_tr('Revenue', 'Daromad', 'Выручка'),
    'ui.dashboard.revenue_trend': _ui_tr('Revenue Trend', 'Daromad tendentsiyasi', 'Динамика выручки'),
    'ui.dashboard.sales_performance': _ui_tr('Sales Performance', 'Sotuvlar samaradorligi', 'Эффективность продаж'),
    'ui.dashboard.this_week': _ui_tr('This Week', 'Bu hafta', 'На этой неделе'),
    'ui.dashboard.title': _ui_tr('Dashboard', 'Boshqaruv paneli', 'Панель управления'),
    'ui.dashboard.today': _ui_tr('Today', 'Bugun', 'Сегодня'),
    'ui.dashboard.top_products': _ui_tr('Top Products', 'Eng yaxshi mahsulotlar', 'Лучшие товары'),
    'ui.dashboard.total_orders': _ui_tr('Total Orders', 'Jami buyurtmalar', 'Всего заказов'),
    'ui.dashboard.total_users': _ui_tr('Total Users', 'Jami foydalanuvchilar', 'Всего пользователей'),
    'ui.dashboard.units_sold': _ui_tr('Units Sold', 'Sotilgan birliklar', 'Продано единиц'),
    'ui.dashboard.week': _ui_tr('Week', 'Hafta', 'Неделя'),

    # ---- ui.delivery.* ----
    'ui.delivery.actions': _ui_tr('Actions', 'Amallar', 'Действия'),
    'ui.delivery.active_deliveries': _ui_tr('Active Deliveries', 'Faol yetkazib berishlar', 'Активные доставки'),
    'ui.delivery.address': _ui_tr('Address', 'Manzil', 'Адрес'),
    'ui.delivery.assign_driver': _ui_tr('Assign Driver', 'Haydovchi tayinlash', 'Назначить водителя'),
    'ui.delivery.assigned_to_driver': _ui_tr('Assigned to driver', 'Haydovchiga tayinlangan', 'Назначено водителю'),
    'ui.delivery.cancel': _ui_tr('Cancel', 'Bekor qilish', 'Отмена'),
    'ui.delivery.cancelled_at': _ui_tr('Cancelled at', 'Bekor qilingan vaqt', 'Отменено в'),
    'ui.delivery.close': _ui_tr('Close', 'Yopish', 'Закрыть'),
    'ui.delivery.completion_rate': _ui_tr('Completion Rate', 'Bajarish darajasi', 'Доля выполненных'),
    'ui.delivery.redispatch_delivery': _ui_tr('Re-dispatch', 'Qayta yuborish', 'Переотправить'),
    'ui.delivery.redispatch_confirm_title': _ui_tr(
        'Re-dispatch this delivery?',
        'Yetkazib berishni qayta yuborilsinmi?',
        'Переотправить эту доставку?',
    ),
    'ui.delivery.redispatch_confirm_message': _ui_tr(
        'This returns the failed delivery to the pool (clears the driver) so it can be re-claimed.',
        'Bu muvaffaqiyatsiz yetkazib berishni hovuzga qaytaradi (haydovchini bo‘shatadi), shunda uni qayta olish mumkin.',
        'Доставка вернётся в пул (водитель будет снят), чтобы её можно было взять заново.',
    ),
    'ui.delivery.redispatch_success': _ui_tr(
        'Delivery re-dispatched to pool',
        'Yetkazib berish hovuzga qayta yuborildi',
        'Доставка переотправлена в пул',
    ),
    'ui.delivery.redispatch_failed': _ui_tr(
        'Failed to re-dispatch delivery',
        'Yetkazib berishni qayta yuborib bo‘lmadi',
        'Не удалось переотправить доставку',
    ),
    'ui.delivery.create_delivery_coming_soon': _ui_tr('Create delivery — coming soon', 'Yetkazib berish yaratish — tez orada', 'Создание доставки — скоро'),
    'ui.delivery.current_status': _ui_tr('Current Status', 'Joriy holat', 'Текущий статус'),
    'ui.delivery.customer': _ui_tr('Customer', 'Mijoz', 'Клиент'),
    'ui.delivery.customer_phone': _ui_tr('Customer Phone', 'Mijoz telefoni', 'Телефон клиента'),
    'ui.delivery.delivered': _ui_tr('Delivered', 'Yetkazib berildi', 'Доставлено'),
    'ui.delivery.delivered_at': _ui_tr('Delivered at', 'Yetkazib berildi vaqti', 'Доставлено в'),
    'ui.delivery.delivery_address': _ui_tr('Delivery Address', 'Yetkazib berish manzili', 'Адрес доставки'),
    'ui.delivery.delivery_cancelled': _ui_tr('Delivery cancelled', 'Yetkazib berish bekor qilindi', 'Доставка отменена'),
    'ui.delivery.delivery_details': _ui_tr('Delivery Details', 'Yetkazib berish tafsilotlari', 'Детали доставки'),
    'ui.delivery.delivery_failed': _ui_tr('Delivery failed', 'Yetkazib berish muvaffaqiyatsiz', 'Доставка не удалась'),
    'ui.delivery.delivery_id': _ui_tr('Delivery ID', 'Yetkazib berish ID', 'ID доставки'),
    'ui.delivery.delivery_notes': _ui_tr('Delivery Notes', 'Yetkazib berish eslatmalari', 'Примечания к доставке'),
    'ui.delivery.delivery_progress': _ui_tr('Delivery Progress', 'Yetkazib berish jarayoni', 'Ход доставки'),
    'ui.delivery.delivery_request_created': _ui_tr('Delivery request created', 'Yetkazib berish so\'rovi yaratildi', 'Запрос на доставку создан'),
    'ui.delivery.driver': _ui_tr('Driver', 'Haydovchi', 'Водитель'),
    'ui.delivery.driver_assigned': _ui_tr('Driver assigned', 'Haydovchi tayinlandi', 'Водитель назначен'),
    'ui.delivery.driver_collected_package': _ui_tr('Driver collected package', 'Haydovchi paketni oldi', 'Водитель забрал заказ'),
    'ui.delivery.driver_phone': _ui_tr('Driver Phone', 'Haydovchi telefoni', 'Телефон водителя'),
    'ui.delivery.end_date': _ui_tr('End Date', 'Tugash sanasi', 'Дата окончания'),
    'ui.delivery.estimated_delivery': _ui_tr('Estimated Delivery', 'Taxminiy yetkazib berish', 'Ожидаемая доставка'),
    'ui.delivery.export_report': _ui_tr('Export Report', 'Hisobotni eksport qilish', 'Экспортировать отчёт'),
    'ui.delivery.failed_at': _ui_tr('Failed at', 'Muvaffaqiyatsiz tugadi', 'Сбой в'),
    'ui.delivery.failure_customer_refused': _ui_tr('Customer refused', 'Mijoz rad etdi', 'Клиент отказался'),
    'ui.delivery.failure_customer_unavailable': _ui_tr('Customer unavailable', 'Mijoz mavjud emas', 'Клиент недоступен'),
    'ui.delivery.failure_other': _ui_tr('Other', 'Boshqa', 'Другое'),
    'ui.delivery.failure_product_damaged': _ui_tr('Product damaged', 'Mahsulot shikastlangan', 'Товар повреждён'),
    'ui.delivery.failure_reason': _ui_tr('Failure Reason', 'Muvaffaqiyatsizlik sababi', 'Причина сбоя'),
    'ui.delivery.failure_wrong_address': _ui_tr('Wrong address', 'Noto\'g\'ri manzil', 'Неверный адрес'),
    'ui.delivery.filter_by_status': _ui_tr('Filter by status', 'Holat bo\'yicha filtrlash', 'Фильтр по статусу'),
    'ui.delivery.in_transit': _ui_tr('In Transit', 'Yo\'lda', 'В пути'),
    'ui.delivery.items': _ui_tr('Items', 'Mahsulotlar', 'Товары'),
    'ui.delivery.na': _ui_tr('N/A', 'Mavjud emas', 'Н/Д'),
    'ui.delivery.no_data_to_export': _ui_tr('No data to export', 'Eksport qilish uchun ma\'lumot yo\'q', 'Нет данных для экспорта'),
    'ui.delivery.no_timestamp_available': _ui_tr('No timestamp available', 'Vaqt belgisi mavjud emas', 'Временная метка отсутствует'),
    'ui.delivery.not_assigned': _ui_tr('Not assigned', 'Tayinlanmagan', 'Не назначено'),
    'ui.delivery.notes': _ui_tr('Notes', 'Eslatmalar', 'Примечания'),
    'ui.delivery.notes_placeholder': _ui_tr('Add notes', 'Eslatmalar qo\'shing', 'Добавьте примечания'),
    'ui.delivery.order_created': _ui_tr('Order created', 'Buyurtma yaratildi', 'Заказ создан'),
    'ui.delivery.order_number': _ui_tr('Order Number', 'Buyurtma raqami', 'Номер заказа'),
    'ui.delivery.package_arrived_destination': _ui_tr('Package arrived at destination', 'Paket yetkazish joyiga yetib keldi', 'Заказ прибыл к месту доставки'),
    'ui.delivery.package_delivered_success': _ui_tr('Package delivered successfully', 'Paket muvaffaqiyatli yetkazib berildi', 'Заказ успешно доставлен'),
    'ui.delivery.package_on_way': _ui_tr('Package on the way', 'Paket yo\'lda', 'Заказ в пути'),
    'ui.delivery.package_picked_up': _ui_tr('Package picked up', 'Paket olindi', 'Заказ забран'),
    'ui.delivery.pagination_text': _ui_tr('{from}-{to} of {total}', '{from}-{to} / {total}', '{from}-{to} из {total}'),
    'ui.delivery.pending': _ui_tr('Pending', 'Kutilmoqda', 'В ожидании'),
    'ui.delivery.priority': _ui_tr('Priority', 'Ustuvorlik', 'Приоритет'),
    'ui.delivery.reassign_driver': _ui_tr('Reassign Driver', 'Haydovchini qayta tayinlash', 'Переназначить водителя'),
    'ui.delivery.schedule_delivery': _ui_tr('Schedule Delivery', 'Yetkazib berishni rejalashtirish', 'Запланировать доставку'),
    'ui.delivery.scheduled': _ui_tr('Scheduled', 'Rejalashtirilgan', 'Запланировано'),
    'ui.delivery.scheduled_date': _ui_tr('Scheduled Date', 'Rejalashtirilgan sana', 'Дата планирования'),
    'ui.delivery.search_placeholder': _ui_tr('Search...', 'Qidirish...', 'Поиск...'),
    'ui.delivery.select_failure_reason': _ui_tr('Select a failure reason', 'Muvaffaqiyatsizlik sababini tanlang', 'Выберите причину сбоя'),
    'ui.delivery.select_status_required': _ui_tr('Status selection is required', 'Holatni tanlash talab qilinadi', 'Требуется выбрать статус'),
    'ui.delivery.start_date': _ui_tr('Start Date', 'Boshlanish sanasi', 'Дата начала'),
    'ui.delivery.status': _ui_tr('Status', 'Holati', 'Статус'),
    'ui.delivery.status_arrived': _ui_tr('Arrived', 'Yetib keldi', 'Прибыл'),
    'ui.delivery.status_assigned': _ui_tr('Assigned', 'Tayinlangan', 'Назначено'),
    'ui.delivery.status_cancelled': _ui_tr('Cancelled', 'Bekor qilingan', 'Отменено'),
    'ui.delivery.status_delivered': _ui_tr('Delivered', 'Yetkazib berildi', 'Доставлено'),
    'ui.delivery.status_failed': _ui_tr('Failed', 'Muvaffaqiyatsiz', 'Сбой'),
    'ui.delivery.status_in_transit': _ui_tr('In Transit', 'Yo\'lda', 'В пути'),
    'ui.delivery.status_pending': _ui_tr('Pending', 'Kutilmoqda', 'В ожидании'),
    'ui.delivery.status_picked_up': _ui_tr('Picked up', 'Olindi', 'Забрано'),
    'ui.delivery.status_returned': _ui_tr('Returned', 'Qaytarildi', 'Возвращено'),
    'ui.delivery.status_scheduled': _ui_tr('Scheduled', 'Rejalashtirilgan', 'Запланировано'),
    'ui.delivery.time_slot': _ui_tr('Time Slot', 'Vaqt oralig\'i', 'Временной слот'),
    'ui.delivery.total_amount': _ui_tr('Total Amount', 'Jami summa', 'Общая сумма'),
    'ui.delivery.total_deliveries': _ui_tr('Total Deliveries', 'Jami yetkazib berishlar', 'Всего доставок'),
    'ui.delivery.track_delivery': _ui_tr('Track Delivery', 'Yetkazib berishni kuzatish', 'Отследить доставку'),
    'ui.delivery.track_delivery_title': _ui_tr('Track Delivery', 'Yetkazib berishni kuzatish', 'Отслеживание доставки'),
    'ui.delivery.tracking_number': _ui_tr('Tracking Number', 'Kuzatuv raqami', 'Номер отслеживания'),
    'ui.delivery.update_delivery': _ui_tr('Update Delivery', 'Yetkazib berishni yangilash', 'Обновить доставку'),
    'ui.delivery.update_delivery_button': _ui_tr('Update Delivery', 'Yetkazib berishni yangilash', 'Обновить доставку'),
    'ui.delivery.update_failed': _ui_tr('Update failed', 'Yangilash muvaffaqiyatsiz', 'Не удалось обновить'),
    'ui.delivery.update_status': _ui_tr('Update Status', 'Holatni yangilash', 'Обновить статус'),
    'ui.delivery.updated_success': _ui_tr('Updated successfully', 'Muvaffaqiyatli yangilandi', 'Успешно обновлено'),
    'ui.delivery.view_details': _ui_tr('View Details', 'Tafsilotlarni ko\'rish', 'Подробнее'),
    'ui.delivery.waiting_for_arrival': _ui_tr('Waiting for arrival', 'Yetib kelishini kutilmoqda', 'Ожидается прибытие'),
    'ui.delivery.waiting_for_assignment': _ui_tr('Waiting for assignment', 'Tayinlashni kutilmoqda', 'Ожидается назначение'),
    'ui.delivery.waiting_for_delivery': _ui_tr('Waiting for delivery', 'Yetkazib berishni kutilmoqda', 'Ожидается доставка'),

    # ---- ui.login.* ----
    'ui.login.app_name': _ui_tr('Aqua Element Admin', 'Aqua Element Admin', 'Aqua Element Admin'),
    'ui.login.copyright': _ui_tr('© Aqua Element', '© Aqua Element', '© Aqua Element'),
    'ui.login.email_or_phone': _ui_tr('Email or Phone', 'Email yoki telefon', 'Email или телефон'),
    'ui.login.email_or_phone_placeholder': _ui_tr('Enter email or phone', 'Email yoki telefonni kiriting', 'Введите email или телефон'),
    'ui.login.email_required': _ui_tr('Email is required', 'Email talab qilinadi', 'Email обязателен'),
    'ui.login.password': _ui_tr('Password', 'Parol', 'Пароль'),
    'ui.login.password_placeholder': _ui_tr('Enter password', 'Parolni kiriting', 'Введите пароль'),
    'ui.login.password_required': _ui_tr('Password is required', 'Parol talab qilinadi', 'Пароль обязателен'),
    'ui.login.restricted_access': _ui_tr('Restricted Access', 'Cheklangan kirish', 'Ограниченный доступ'),
    'ui.login.restricted_description': _ui_tr('This area is restricted to authorized staff only.', 'Bu hudud faqat ruxsat etilgan xodimlar uchun.', 'Эта зона доступна только авторизованному персоналу.'),
    'ui.login.sign_in': _ui_tr('Sign In', 'Kirish', 'Войти'),
    'ui.login.subtitle': _ui_tr('Admin Panel', 'Admin paneli', 'Панель администратора'),

    # ---- ui.marking_codes.* ----
    'ui.marking_codes.fields.api_chunk_size_help': _ui_tr('Number of marking codes per API request.', 'Har bir API so\'rov uchun markirovka kodlari soni.', 'Количество маркировочных кодов на один API-запрос.'),
    'ui.marking_codes.section.target_sizing_help': _ui_tr('Configure target sizing for marking code allocation.', 'Markirovka kodi taqsimoti uchun mo\'ljal hajmini sozlang.', 'Настройте целевые размеры для распределения маркировочных кодов.'),

    # ---- ui.nav.* ----
    'ui.nav.analytics': _ui_tr('Analytics', 'Tahlil', 'Аналитика'),
    'ui.nav.blog': _ui_tr('Blog', 'Blog', 'Блог'),
    'ui.nav.bottle_tracking': _ui_tr('Bottle Tracking', 'Idishni kuzatish', 'Отслеживание тары'),
    'ui.nav.categories': _ui_tr('Categories', 'Kategoriyalar', 'Категории'),
    'ui.nav.dashboard': _ui_tr('Dashboard', 'Boshqaruv paneli', 'Панель управления'),
    'ui.nav.deliveries': _ui_tr('Deliveries', 'Yetkazib berishlar', 'Доставки'),
    'ui.nav.delivery': _ui_tr('Delivery', 'Yetkazib berish', 'Доставка'),
    'ui.nav.delivery_persons': _ui_tr('Delivery Persons', 'Yetkazib beruvchilar', 'Курьеры'),
    'ui.nav.delivery_reports': _ui_tr('Delivery Reports', 'Yetkazib berish hisobotlari', 'Отчёты по доставкам'),
    'ui.nav.notifications': _ui_tr('Notifications', 'Bildirishnomalar', 'Уведомления'),
    'ui.nav.operators': _ui_tr('Operators', 'Operatorlar', 'Операторы'),
    'ui.nav.orders': _ui_tr('Orders', 'Buyurtmalar', 'Заказы'),
    'ui.nav.products': _ui_tr('Products', 'Mahsulotlar', 'Товары'),
    'ui.nav.settings': _ui_tr('Settings', 'Sozlamalar', 'Настройки'),
    'ui.nav.staff': _ui_tr('Staff', 'Xodimlar', 'Персонал'),
    'ui.nav.staff_management': _ui_tr('Staff Management', 'Xodimlarni boshqarish', 'Управление персоналом'),
    'ui.nav.time_slots': _ui_tr('Time Slots', 'Vaqt oraliqlari', 'Временные слоты'),
    'ui.nav.translations': _ui_tr('Translations', 'Tarjimalar', 'Переводы'),
    'ui.nav.tryouts': _ui_tr('Tryouts', 'Sinovlar', 'Пробники'),
    'ui.nav.users': _ui_tr('Users', 'Foydalanuvchilar', 'Пользователи'),
    'ui.nav.support_inbox': _ui_tr('Support Inbox', 'Qoʻllab-quvvatlash', 'Поддержка'),

    # ---- ui.support.* ----
    'ui.support.title': _ui_tr('Support Inbox', 'Qoʻllab-quvvatlash', 'Поддержка'),
    'ui.support.conversations': _ui_tr('Conversations', 'Suhbatlar', 'Диалоги'),
    'ui.support.no_conversations': _ui_tr('No conversations yet', 'Hozircha suhbatlar yoʻq', 'Пока нет диалогов'),
    'ui.support.search_placeholder': _ui_tr('Search by name or phone', 'Ism yoki telefon boʻyicha qidirish', 'Поиск по имени или телефону'),
    'ui.support.message_placeholder': _ui_tr('Type a message…', 'Xabar yozing…', 'Введите сообщение…'),
    'ui.support.send': _ui_tr('Send', 'Yuborish', 'Отправить'),
    'ui.support.new_message': _ui_tr('New message', 'Yangi xabar', 'Новое сообщение'),
    'ui.support.select_user': _ui_tr('Select a Telegram-connected user', 'Telegramga ulangan foydalanuvchini tanlang', 'Выберите пользователя с Telegram'),
    'ui.support.sent': _ui_tr('Message sent', 'Xabar yuborildi', 'Сообщение отправлено'),
    'ui.support.send_failed': _ui_tr('Failed to send message', 'Xabar yuborilmadi', 'Не удалось отправить сообщение'),
    'ui.support.delivery_failed': _ui_tr('Not delivered', 'Yetkazilmadi', 'Не доставлено'),
    'ui.support.unread': _ui_tr('unread', 'oʻqilmagan', 'непрочитано'),
    'ui.support.empty_thread': _ui_tr('No messages in this conversation', 'Bu suhbatda xabarlar yoʻq', 'В этом диалоге нет сообщений'),
    'ui.support.select_conversation': _ui_tr('Select a conversation to view messages', 'Xabarlarni koʻrish uchun suhbatni tanlang', 'Выберите диалог, чтобы увидеть сообщения'),

    # ---- ui.orders.* ----
    'ui.orders.bottles_returned': _ui_tr('Bottles Returned', 'Qaytarilgan idishlar', 'Возвращено тары'),
    'ui.orders.bottles_returned_hint': _ui_tr('Number of returnable bottles the customer handed back', 'Mijoz qaytargan qaytariladigan idishlar soni', 'Количество возвратной тары, переданной клиентом'),
    'ui.orders.no_customers_found': _ui_tr('No customers found', 'Mijozlar topilmadi', 'Клиенты не найдены'),
    'ui.orders.no_orders': _ui_tr('No orders', 'Buyurtmalar yo\'q', 'Заказов нет'),
    'ui.orders.no_valid_transitions': _ui_tr('No valid status transitions', 'Yaroqli holat o\'tishlari yo\'q', 'Нет допустимых переходов статуса'),
    'ui.orders.payment': _ui_tr('Payment', 'To\'lov', 'Оплата'),
    'ui.orders.search_customer_hint': _ui_tr('Search by name, phone or email', 'Ism, telefon yoki email bo\'yicha qidirish', 'Поиск по имени, телефону или email'),

    # ---- ui.role.* ----
    'ui.role.admin': _ui_tr('Admin', 'Admin', 'Администратор'),
    'ui.role.super_admin': _ui_tr('Super Admin', 'Super admin', 'Супер-администратор'),

    # ---- ui.settings.* ----
    'ui.settings.coming_soon': _ui_tr('Coming soon', 'Tez orada', 'Скоро'),
    'ui.settings.description': _ui_tr('Application settings', 'Ilova sozlamalari', 'Настройки приложения'),
    'ui.settings.title': _ui_tr('Settings', 'Sozlamalar', 'Настройки'),

    # ---- ui.user_menu.* ----
    'ui.user_menu.logout': _ui_tr('Logout', 'Chiqish', 'Выйти'),
    'ui.user_menu.profile': _ui_tr('Profile', 'Profil', 'Профиль'),
    'ui.user_menu.settings': _ui_tr('Settings', 'Sozlamalar', 'Настройки'),

    # ---- ui.users.* ----
    'ui.users.account_unlocked': _ui_tr('Account unlocked', 'Hisob qulfi ochildi', 'Аккаунт разблокирован'),
    'ui.users.actions': _ui_tr('Actions', 'Amallar', 'Действия'),
    'ui.users.activate': _ui_tr('Activate', 'Faollashtirish', 'Активировать'),
    'ui.users.active_cod_debt_count': _ui_tr('Active COD Debts', 'Faol COD qarzlari', 'Активные долги COD'),
    'ui.users.activity': _ui_tr('Activity', 'Faollik', 'Активность'),
    'ui.users.activity_information': _ui_tr('Activity Information', 'Faollik haqida ma\'lumot', 'Информация об активности'),
    'ui.users.add': _ui_tr('Add', 'Qo\'shish', 'Добавить'),
    'ui.users.add_address': _ui_tr('Add Address', 'Manzil qo\'shish', 'Добавить адрес'),
    'ui.users.add_user': _ui_tr('Add User', 'Foydalanuvchi qo\'shish', 'Добавить пользователя'),
    'ui.users.address_create_failed': _ui_tr('Failed to create address', 'Manzil yaratib bo\'lmadi', 'Не удалось создать адрес'),
    'ui.users.address_created': _ui_tr('Address created', 'Manzil yaratildi', 'Адрес создан'),
    'ui.users.address_delete_failed': _ui_tr('Failed to delete address', 'Manzil o\'chirib bo\'lmadi', 'Не удалось удалить адрес'),
    'ui.users.address_deleted': _ui_tr('Address deleted', 'Manzil o\'chirildi', 'Адрес удалён'),
    'ui.users.address_required': _ui_tr('Address is required', 'Manzil talab qilinadi', 'Адрес обязателен'),
    'ui.users.address_title': _ui_tr('Address Title', 'Manzil nomi', 'Название адреса'),
    'ui.users.address_title_placeholder': _ui_tr('e.g. Home, Work', 'Masalan, Uy, Ish', 'Например: Дом, Работа'),
    'ui.users.address_update_failed': _ui_tr('Failed to update address', 'Manzilni yangilab bo\'lmadi', 'Не удалось обновить адрес'),
    'ui.users.address_updated': _ui_tr('Address updated', 'Manzil yangilandi', 'Адрес обновлён'),
    'ui.users.addresses': _ui_tr('Addresses', 'Manzillar', 'Адреса'),
    'ui.users.addresses_load_failed': _ui_tr('Failed to load addresses', 'Manzillarni yuklab bo\'lmadi', 'Не удалось загрузить адреса'),
    'ui.users.admin_notes': _ui_tr('Admin Notes', 'Admin eslatmalari', 'Заметки администратора'),
    'ui.users.amount_collected': _ui_tr('Amount Collected', 'Yig\'ilgan summa', 'Собранная сумма'),
    'ui.users.apartment': _ui_tr('Apartment', 'Xonadon', 'Квартира'),
    'ui.users.apartment_placeholder': _ui_tr('Apartment number', 'Xonadon raqami', 'Номер квартиры'),
    'ui.users.ban': _ui_tr('Ban', 'Bloklash', 'Заблокировать'),
    'ui.users.basic_information': _ui_tr('Basic Information', 'Asosiy ma\'lumot', 'Основная информация'),
    'ui.users.bot': _ui_tr('Bot', 'Bot', 'Бот'),
    'ui.users.building_details': _ui_tr('Building Details', 'Bino tafsilotlari', 'Сведения о здании'),
    'ui.users.change_status_confirm': _ui_tr('Confirm status change', 'Holatni o\'zgartirishni tasdiqlang', 'Подтвердите изменение статуса'),
    'ui.users.change_status_title': _ui_tr('Change User Status', 'Foydalanuvchi holatini o\'zgartirish', 'Изменить статус пользователя'),
    'ui.users.cod_restricted': _ui_tr('COD restricted', 'COD cheklangan', 'COD ограничен'),
    'ui.users.cod_statement': _ui_tr('COD Statement', 'COD hisoboti', 'Отчёт COD'),
    'ui.users.contact': _ui_tr('Contact', 'Aloqa', 'Контакт'),
    'ui.users.create': _ui_tr('Create', 'Yaratish', 'Создать'),
    'ui.users.create_new_user': _ui_tr('Create New User', 'Yangi foydalanuvchi yaratish', 'Создать нового пользователя'),
    'ui.users.created': _ui_tr('Created', 'Yaratilgan', 'Создан'),
    'ui.users.default': _ui_tr('Default', 'Standart', 'По умолчанию'),
    'ui.users.date_of_birth': _ui_tr('Date of Birth', "Tug'ilgan kun", 'День рождения'),
    'ui.users.delete': _ui_tr('Delete', 'O\'chirish', 'Удалить'),
    'ui.users.delete_address_confirm': _ui_tr('Are you sure you want to delete this address?', 'Ushbu manzilni o\'chirishni xohlaysizmi?', 'Вы уверены, что хотите удалить этот адрес?'),
    'ui.users.delivery_info': _ui_tr('Delivery Info', 'Yetkazib berish ma\'lumoti', 'Информация о доставке'),
    'ui.users.delivery_instructions': _ui_tr('Delivery Instructions', 'Yetkazib berish ko\'rsatmalari', 'Инструкции по доставке'),
    'ui.users.delivery_instructions_placeholder': _ui_tr('Optional instructions for the driver', 'Haydovchi uchun ixtiyoriy ko\'rsatmalar', 'Необязательные инструкции для водителя'),
    'ui.users.district': _ui_tr('District', 'Tuman', 'Район'),
    'ui.users.edit_address': _ui_tr('Edit Address', 'Manzilni tahrirlash', 'Редактировать адрес'),
    'ui.users.email': _ui_tr('Email', 'Email', 'Email'),
    'ui.users.email_verified': _ui_tr('Email Verified', 'Email tasdiqlangan', 'Email подтверждён'),
    'ui.users.enter_email_optional': _ui_tr('Enter email (optional)', 'Email kiriting (ixtiyoriy)', 'Введите email (необязательно)'),
    'ui.users.enter_first_name': _ui_tr('Enter first name', 'Ismni kiriting', 'Введите имя'),
    'ui.users.enter_last_name': _ui_tr('Enter last name', 'Familiyani kiriting', 'Введите фамилию'),
    'ui.users.export': _ui_tr('Export', 'Eksport qilish', 'Экспорт'),
    'ui.users.filter_by_status': _ui_tr('Filter by status', 'Holat bo\'yicha filtrlash', 'Фильтр по статусу'),
    'ui.users.first_name': _ui_tr('First Name', 'Ismi', 'Имя'),
    'ui.users.first_name_required': _ui_tr('First name is required', 'Ism talab qilinadi', 'Имя обязательно'),
    'ui.users.floor': _ui_tr('Floor', 'Qavat', 'Этаж'),
    'ui.users.floor_placeholder': _ui_tr('Floor number', 'Qavat raqami', 'Номер этажа'),
    'ui.users.full_address': _ui_tr('Full Address', 'To\'liq manzil', 'Полный адрес'),
    'ui.users.full_address_placeholder': _ui_tr('Street, building, etc.', 'Ko\'cha, bino va h.k.', 'Улица, дом и т.д.'),
    'ui.users.invalid_email': _ui_tr('Invalid email', 'Noto\'g\'ri email', 'Неверный email'),
    'ui.users.invalid_phone': _ui_tr('Invalid phone', 'Noto\'g\'ri telefon', 'Неверный телефон'),
    'ui.users.is_business': _ui_tr('Business', 'Tadbirkor', 'Бизнес'),
    'ui.users.landmark': _ui_tr('Landmark', 'Mo\'ljal', 'Ориентир'),
    'ui.users.landmark_placeholder': _ui_tr('Nearby landmark', 'Yaqin mo\'ljal', 'Ближайший ориентир'),
    'ui.users.language': _ui_tr('Language', 'Til', 'Язык'),
    'ui.users.last_bot_interaction': _ui_tr('Last Bot Interaction', 'Bot bilan oxirgi muloqot', 'Последнее взаимодействие с ботом'),
    'ui.users.last_login': _ui_tr('Last Login', 'Oxirgi kirish', 'Последний вход'),
    'ui.users.last_name': _ui_tr('Last Name', 'Familiya', 'Фамилия'),
    'ui.users.location_details': _ui_tr('Location Details', 'Joylashuv tafsilotlari', 'Сведения о местоположении'),
    'ui.users.locked': _ui_tr('Locked', 'Bloklangan', 'Заблокирован'),
    'ui.users.login': _ui_tr('Login', 'Kirish', 'Логин'),
    'ui.users.na': _ui_tr('N/A', 'Mavjud emas', 'Н/Д'),
    'ui.users.name': _ui_tr('Name', 'Ismi', 'Имя'),
    'ui.users.never': _ui_tr('Never', 'Hech qachon', 'Никогда'),
    'ui.users.no': _ui_tr('No', 'Yo\'q', 'Нет'),
    'ui.users.no_addresses_yet': _ui_tr('No addresses yet', 'Hozircha manzillar yo\'q', 'Адресов пока нет'),
    'ui.users.no_cod_statement': _ui_tr('No COD statement', 'COD hisoboti yo\'q', 'Отчёт COD отсутствует'),
    'ui.users.note': _ui_tr('Note', 'Eslatma', 'Примечание'),
    'ui.users.notes_placeholder': _ui_tr('Add notes', 'Eslatmalar qo\'shing', 'Добавьте примечания'),
    'ui.users.order_number': _ui_tr('Order Number', 'Buyurtma raqami', 'Номер заказа'),
    'ui.users.outstanding_amount': _ui_tr('Outstanding Amount', 'Qoldiq summa', 'Сумма задолженности'),
    'ui.users.pagination_text': _ui_tr('{from}-{to} of {total}', '{from}-{to} / {total}', '{from}-{to} из {total}'),
    'ui.users.phone': _ui_tr('Phone', 'Telefon', 'Телефон'),
    'ui.users.phone_required': _ui_tr('Phone is required', 'Telefon talab qilinadi', 'Телефон обязателен'),
    'ui.users.phone_verified': _ui_tr('Phone Verified', 'Telefon tasdiqlangan', 'Телефон подтверждён'),
    'ui.users.quick_select': _ui_tr('Quick Select', 'Tez tanlash', 'Быстрый выбор'),
    'ui.users.registration_source': _ui_tr('Registration Source', 'Ro\'yxatdan o\'tish manbai', 'Источник регистрации'),
    'ui.users.role': _ui_tr('Role', 'Rol', 'Роль'),
    'ui.users.search_placeholder': _ui_tr('Search users...', 'Foydalanuvchilarni qidirish...', 'Поиск пользователей...'),
    'ui.users.select_district': _ui_tr('Select district', 'Tumanni tanlang', 'Выберите район'),
    'ui.users.select_location_on_map': _ui_tr('Select location on map', 'Xaritada joylashuvni tanlang', 'Выберите местоположение на карте'),
    'ui.users.set_as_default': _ui_tr('Set as default', 'Standart qilib o\'rnatish', 'Установить по умолчанию'),
    'ui.users.status': _ui_tr('Status', 'Holati', 'Статус'),
    'ui.users.status_active': _ui_tr('Active', 'Faol', 'Активен'),
    'ui.users.status_banned': _ui_tr('Banned', 'Bloklangan', 'Заблокирован'),
    'ui.users.status_changed_by_admin': _ui_tr('Status changed by admin', 'Holat admin tomonidan o\'zgartirildi', 'Статус изменён администратором'),
    'ui.users.status_inactive': _ui_tr('Inactive', 'Faol emas', 'Неактивен'),
    'ui.users.status_suspended': _ui_tr('Suspended', 'To\'xtatilgan', 'Приостановлен'),
    'ui.users.status_update_failed': _ui_tr('Status update failed', 'Holatni yangilash muvaffaqiyatsiz', 'Не удалось обновить статус'),
    'ui.users.status_updated_success': _ui_tr('Status updated', 'Holat yangilandi', 'Статус обновлён'),
    'ui.users.street': _ui_tr('Street', 'Ko\'cha', 'Улица'),
    'ui.users.street_placeholder': _ui_tr('Street name', 'Ko\'cha nomi', 'Название улицы'),
    'ui.users.suspend': _ui_tr('Suspend', 'To\'xtatish', 'Приостановить'),
    'ui.users.telegram_id': _ui_tr('Telegram ID', 'Telegram ID', 'Telegram ID'),
    'ui.users.telegram_information': _ui_tr('Telegram Information', 'Telegram ma\'lumoti', 'Информация Telegram'),
    'ui.users.title_home': _ui_tr('Home', 'Uy', 'Дом'),
    'ui.users.title_other': _ui_tr('Other', 'Boshqa', 'Другое'),
    'ui.users.title_work': _ui_tr('Work', 'Ish', 'Работа'),
    'ui.users.total': _ui_tr('Total', 'Jami', 'Всего'),
    'ui.users.total_amount': _ui_tr('Total Amount', 'Jami summa', 'Общая сумма'),
    'ui.users.total_outstanding_amount': _ui_tr('Total Outstanding Amount', 'Jami qoldiq summa', 'Общая сумма задолженности'),
    'ui.users.unlock': _ui_tr('Unlock', 'Qulfini ochish', 'Разблокировать'),
    'ui.users.unlock_account': _ui_tr('Unlock Account', 'Hisob qulfini ochish', 'Разблокировать аккаунт'),
    'ui.users.unlock_account_confirm': _ui_tr('Are you sure you want to unlock this account?', 'Ushbu hisob qulfini ochishni xohlaysizmi?', 'Вы уверены, что хотите разблокировать аккаунт?'),
    'ui.users.unlock_account_title': _ui_tr('Unlock Account', 'Hisob qulfini ochish', 'Разблокировка аккаунта'),
    'ui.users.unlock_failed': _ui_tr('Failed to unlock account', 'Hisob qulfini ochib bo\'lmadi', 'Не удалось разблокировать аккаунт'),
    'ui.users.updated': _ui_tr('Updated', 'Yangilangan', 'Обновлён'),
    'ui.users.user': _ui_tr('User', 'Foydalanuvchi', 'Пользователь'),
    'ui.users.user_create_failed': _ui_tr('Failed to create user', 'Foydalanuvchi yaratib bo\'lmadi', 'Не удалось создать пользователя'),
    'ui.users.user_created_success': _ui_tr('User created successfully', 'Foydalanuvchi muvaffaqiyatli yaratildi', 'Пользователь успешно создан'),
    'ui.users.user_details': _ui_tr('User Details', 'Foydalanuvchi tafsilotlari', 'Сведения о пользователе'),
    'ui.users.username': _ui_tr('Username', 'Foydalanuvchi nomi', 'Имя пользователя'),
    'ui.users.verified': _ui_tr('Verified', 'Tasdiqlangan', 'Подтверждён'),
    'ui.users.view_details': _ui_tr('View Details', 'Tafsilotlarni ko\'rish', 'Подробнее'),
    'ui.users.yes': _ui_tr('Yes', 'Ha', 'Да'),
}

BACKEND_TRANSLATIONS.update(ADMIN_UI_BACKFILL_TRANSLATIONS)


# ── Public loyalty handbook (/loyalty-guide) + bot entry button ──────────────
# Prose for the customer-facing loyalty program guide. Numbers are interpolated
# at render time from get_loyalty_handbook_context (live program/tier config),
# so these strings stay free of hard-coded amounts. Placeholders: {uzs} {unit}
# {pts} {you} {friend} {orders} {days} {spend} {base} {tier} {mult} {points}.
LOYALTY_GUIDE_TRANSLATIONS = {
    'loyalty_guide.meta.title': _ui_tr(
        'Loyalty Program Guide — Earn, Redeem & Rise',
        "Sodiqlik dasturi qo'llanmasi — Yig'ing, sarflang, ko'tariling",
        'Гид по программе лояльности — копите, тратьте, растите',
    ),
    'loyalty_guide.meta.description': _ui_tr(
        'Everything about the Aqua Element loyalty program: how you earn AquaCoins, tiers and multipliers, expiry, and how to redeem rewards.',
        "Aqua Element sodiqlik dasturi haqida hammasi: AquaCoins qanday yig'asiz, darajalar va koeffitsiyentlar, amal qilish muddati va mukofotlarni almashtirish.",
        'Всё о программе лояльности Aqua Element: как начисляются AquaCoins, уровни и множители, срок действия и как обменивать награды.',
    ),
    'loyalty_guide.hero.eyebrow': _ui_tr('Aqua Club · Loyalty Program', 'Aqua Club · Sodiqlik dasturi', 'Aqua Club · Программа лояльности'),
    'loyalty_guide.hero.title': _ui_tr('Every drop rewards you.', 'Har bir tomchi sizni mukofotlaydi.', 'Каждая капля вознаграждает вас.'),
    'loyalty_guide.hero.subtitle': _ui_tr(
        'Earn AquaCoins on every order, climb through four tiers, and turn your loyalty into real discounts and free water.',
        "Har bir buyurtmada AquaCoins to'plang, to'rtta darajadan ko'tariling va sodiqligingizni haqiqiy chegirma va bepul suvga aylantiring.",
        'Зарабатывайте AquaCoins за каждый заказ, поднимайтесь по четырём уровням и превращайте лояльность в реальные скидки и бесплатную воду.',
    ),
    'loyalty_guide.hero.cta_primary': _ui_tr('Start earning', "Yig'ishni boshlash", 'Начать копить'),
    'loyalty_guide.hero.cta_secondary': _ui_tr('See the tiers', "Darajalarni ko'rish", 'Смотреть уровни'),

    'loyalty_guide.unit.point': _ui_tr('AquaCoin', 'AquaCoin', 'AquaCoin'),
    'loyalty_guide.unit.points': _ui_tr('AquaCoins', 'AquaCoins', 'AquaCoins'),
    'loyalty_guide.unit.uzs': _ui_tr('UZS', "so'm", 'сум'),

    'loyalty_guide.stat.rate_label': _ui_tr('for every {uzs} {unit} spent', 'har {uzs} {unit} uchun', 'за каждые {uzs} {unit}'),
    'loyalty_guide.stat.welcome_label': _ui_tr('welcome bonus', 'xush kelibsiz bonusi', 'приветственный бонус'),
    'loyalty_guide.stat.tiers_label': _ui_tr('status tiers', 'daraja', 'уровня статуса'),
    'loyalty_guide.stat.multiplier_label': _ui_tr('max AquaCoins rate', 'maksimal AquaCoins', 'макс. множитель'),

    'loyalty_guide.earn.kicker': _ui_tr('Earning', "Yig'ish", 'Начисление'),
    'loyalty_guide.earn.title': _ui_tr('How you earn AquaCoins', "AquaCoins qanday yig'asiz", 'Как вы зарабатываете AquaCoins'),
    'loyalty_guide.earn.subtitle': _ui_tr(
        'Five easy ways to grow your balance — most of them happen automatically.',
        "Balansingizni oshirishning beshta oson yo'li — aksariyati avtomatik.",
        'Пять простых способов пополнить баланс — большинство происходит автоматически.',
    ),
    'loyalty_guide.earn.purchase_title': _ui_tr('Every order', 'Har bir buyurtma', 'Каждый заказ'),
    'loyalty_guide.earn.purchase_desc': _ui_tr(
        'Earn 1 AquaCoin for every {uzs} {unit} you spend — then multiplied by your tier rate.',
        "Sarflagan har {uzs} {unit} uchun 1 AquaCoin oling — so'ng darajangiz koeffitsiyentiga ko'paytiriladi.",
        'Получайте 1 AquaCoin за каждые {uzs} {unit} — затем сумма умножается на множитель вашего уровня.',
    ),
    'loyalty_guide.earn.welcome_title': _ui_tr('Welcome gift', 'Xush kelibsiz sovgʻasi', 'Приветственный подарок'),
    'loyalty_guide.earn.welcome_desc': _ui_tr(
        'Get {pts} AquaCoins the moment you join. Our gift to start you off.',
        "Qo'shilishingiz bilan {pts} AquaCoins oling. Boshlashingiz uchun sovgʻamiz.",
        'Получите {pts} AquaCoins сразу при регистрации. Наш подарок для старта.',
    ),
    'loyalty_guide.earn.referral_title': _ui_tr('Invite friends', "Do'stlarni taklif qiling", 'Приглашайте друзей'),
    'loyalty_guide.earn.referral_desc': _ui_tr(
        'Invite a friend: you get {you} AquaCoins and they get {friend}, after their first delivered and paid order.',
        "Do'stingizni taklif qiling: ularning birinchi yetkazib berilgan va to'langan buyurtmasidan so'ng siz {you} AquaCoins, ular {friend} AquaCoins olasiz.",
        'Пригласите друга: вы получите {you} AquaCoins, а он — {friend}, после его первого доставленного и оплаченного заказа.',
    ),
    'loyalty_guide.earn.birthday_title': _ui_tr('Birthday bonus', "Tug'ilgan kun bonusi", 'Бонус на день рождения'),
    'loyalty_guide.earn.birthday_desc': _ui_tr(
        'Celebrate with {pts} bonus AquaCoins on your birthday — even more at higher tiers.',
        "Tug'ilgan kuningizda {pts} bonus AquaCoins bilan nishonlang — yuqori darajalarda yanada ko'proq.",
        'Отпразднуйте с {pts} бонусными AquaCoins в день рождения — на высоких уровнях ещё больше.',
    ),
    'loyalty_guide.earn.streak_title': _ui_tr('Order streak', 'Buyurtmalar seriyasi', 'Серия заказов'),
    'loyalty_guide.earn.streak_desc': _ui_tr(
        'Place {orders} orders within {days} days and earn a {pts}-AquaCoin streak bonus.',
        '{days} kun ichida {orders} ta buyurtma bering va {pts} AquaCoinlik seriya bonusini oling.',
        'Сделайте {orders} заказа за {days} дней и получите бонус серии в {pts} AquaCoins.',
    ),
    'loyalty_guide.earn.streak_line': _ui_tr(
        '{orders} orders / {days} days → +{pts} {unit}',
        '{orders} buyurtma / {days} kun → +{pts} {unit}',
        '{orders} заказа / {days} дней → +{pts} {unit}',
    ),
    'loyalty_guide.earn.streak_min': _ui_tr(
        'each order ≥ {uzs} {unit}',
        'har bir buyurtma ≥ {uzs} {unit}',
        'каждый заказ ≥ {uzs} {unit}',
    ),
    'loyalty_guide.earn.consec_title': _ui_tr(
        'Consecutive Streaks', 'Ketma-ket seriyalar', 'Серии подряд'),
    'loyalty_guide.earn.consec_and': _ui_tr('and', 'va', 'и'),
    'loyalty_guide.earn.consec_or': _ui_tr('or', 'yoki', 'или'),
    'loyalty_guide.earn.consec_line_all': _ui_tr(
        'Achieve {strikes} {n} times in a row → +{pts} {unit}',
        '{strikes} ni ketma-ket {n} marta bajaring → +{pts} {unit}',
        'Выполните {strikes} {n} раз подряд → +{pts} {unit}'),
    'loyalty_guide.earn.consec_line_any': _ui_tr(
        'Achieve {strikes} {n} times in a row → +{pts} {unit}',
        '{strikes} dan birini ketma-ket {n} marta bajaring → +{pts} {unit}',
        'Выполните {strikes} {n} раз подряд → +{pts} {unit}'),
    'loyalty_guide.faq.q9': _ui_tr(
        'How do consecutive-streak rewards work?',
        'Ketma-ket seriya mukofotlari qanday ishlaydi?',
        'Как работают награды за серии подряд?'),
    'loyalty_guide.faq.a9': _ui_tr(
        'Keep achieving the same order goal in consecutive periods. Reach the required number of consecutive achievements and you earn bonus AquaCoins — then it repeats. Skipping a period resets the streak.',
        'Bir xil buyurtma maqsadini ketma-ket davrlarda bajaring. Talab qilingan ketma-ket bajarishlar soniga yeting va bonus AquaCoins olasiz — keyin u takrorlanadi. Davrni o\'tkazib yuborsangiz seriya nolga tushadi.',
        'Достигайте одной и той же цели по заказам в последовательные периоды. Наберите нужное число последовательных достижений и получите бонусные AquaCoins — затем всё повторяется. Пропуск периода сбрасывает серию.'),
    'loyalty_guide.earn.surprise_title': _ui_tr('Surprise rewards', 'Kutilmagan mukofotlar', 'Сюрприз-награды'),
    'loyalty_guide.earn.surprise_desc': _ui_tr(
        "Every now and then we surprise members who order with a little extra — a bonus of AquaCoins that lands in your balance when you least expect it. No schedule, no formula; that's what keeps it a surprise.",
        "Vaqti-vaqti bilan buyurtma bergan a'zolarni kichik sovg'a bilan xursand qilamiz — siz kutmagan paytda balansingizga AquaCoins bonusi tushadi. Jadval ham, formula ham yo'q; mana shu uni chinakam syurprizga aylantiradi.",
        'Время от времени мы радуем участников, которые делают заказы, приятным бонусом — AquaCoins появляются на вашем балансе тогда, когда вы меньше всего этого ждёте. Без расписания и без формул — именно это и делает его сюрпризом.',
    ),

    'loyalty_guide.example.title': _ui_tr('See it in action', 'Amalda koʻring', 'Пример расчёта'),
    'loyalty_guide.example.body': _ui_tr(
        "Spend {spend} {unit} as a {tier} tier member and you'll earn {base} base AquaCoins × {mult} = {points} AquaCoins.",
        "{tier} darajasidagi aʼzo sifatida {spend} {unit} sarflang va {base} asosiy AquaCoins × {mult} = {points} AquaCoins yigʻasiz.",
        'Потратьте {spend} {unit} как участник уровня {tier} — и получите {base} базовых AquaCoins × {mult} = {points} AquaCoins.',
    ),

    # --- Referral section (dedicated "How referrals work" flow) ---
    'loyalty_guide.referral.kicker': _ui_tr('Refer & earn', "Taklif qiling va yig'ing", 'Приглашайте и зарабатывайте'),
    'loyalty_guide.referral.title': _ui_tr(
        'Invite friends, earn together',
        "Do'stlarni taklif qiling — birga yig'ing",
        'Приглашайте друзей — зарабатывайте вместе',
    ),
    'loyalty_guide.referral.subtitle': _ui_tr(
        'Share your personal link. When a friend joins and completes their first order, you get {you} AquaCoins and they get {friend}.',
        "Shaxsiy havolangizni ulashing. Do'stingiz qo'shilib, birinchi buyurtmasini yakunlaganda siz {you} AquaCoins, u esa {friend} AquaCoins oladi.",
        'Поделитесь своей персональной ссылкой. Когда друг зарегистрируется и завершит первый заказ, вы получите {you} AquaCoins, а он — {friend}.',
    ),
    'loyalty_guide.referral.s1_title': _ui_tr('Share your link', 'Havolangizni ulashing', 'Поделитесь ссылкой'),
    'loyalty_guide.referral.s1_desc': _ui_tr(
        'Open the Loyalty section in our Telegram bot to get your personal referral link, then send it to a friend.',
        "Telegram botimizdagi Sodiqlik bo'limini ochib, shaxsiy referal havolangizni oling va do'stingizga yuboring.",
        'Откройте раздел «Лояльность» в нашем Telegram-боте, получите персональную реферальную ссылку и отправьте другу.',
    ),
    'loyalty_guide.referral.s2_title': _ui_tr('Your friend joins', "Do'stingiz qo'shiladi", 'Друг присоединяется'),
    'loyalty_guide.referral.s2_desc': _ui_tr(
        'They open your link, register and start ordering. The referral now shows as Pending.',
        "U havolangizni ochadi, ro'yxatdan o'tadi va buyurtma bera boshlaydi. Referal endi «Kutilmoqda» holatida ko'rinadi.",
        'Он переходит по ссылке, регистрируется и начинает заказывать. Реферал отображается как «Ожидается».',
    ),
    'loyalty_guide.referral.s3_title': _ui_tr('You both earn', "Ikkalangiz ham yig'asiz", 'Вы оба зарабатываете'),
    'loyalty_guide.referral.s3_desc': _ui_tr(
        "Once your friend's first order is delivered and fully paid, you get {you} AquaCoins and they get {friend} — added automatically.",
        "Do'stingizning birinchi buyurtmasi yetkazib berilib, to'liq to'langach, siz {you} AquaCoins, u esa {friend} AquaCoins olasiz — avtomatik qo'shiladi.",
        'Как только первый заказ друга доставлен и полностью оплачен, вы получаете {you} AquaCoins, а он — {friend} — начисляется автоматически.',
    ),
    'loyalty_guide.referral.note_title': _ui_tr(
        'When does a referral count?',
        'Referal qachon hisobga olinadi?',
        'Когда реферал засчитывается?',
    ),
    'loyalty_guide.referral.note_body': _ui_tr(
        "A referral is successful only when your friend's first order is both delivered and fully paid. Until then it stays Pending — a delivered order that hasn't been paid yet (for example, cash on delivery awaiting collection) does not count.",
        "Referal faqat do'stingizning birinchi buyurtmasi yetkazib berilib, to'liq to'langandagina muvaffaqiyatli hisoblanadi. Shu paytgacha u «Kutilmoqda» bo'lib qoladi — yetkazib berilgan, ammo hali to'lanmagan buyurtma (masalan, yetkazib berishda naqd to'lov hali yig'ilmagan) hisobga olinmaydi.",
        'Реферал считается успешным, только когда первый заказ друга доставлен и полностью оплачен. До этого он остаётся «Ожидается» — доставленный, но ещё не оплаченный заказ (например, наличными при доставке, оплата ещё не получена) не засчитывается.',
    ),
    'loyalty_guide.referral.timing': _ui_tr(
        'Bonuses are granted automatically and may take up to a day to appear after the order is delivered and paid.',
        "Bonuslar avtomatik qo'shiladi va buyurtma yetkazib berilib, to'langach paydo bo'lishi bir kungacha vaqt olishi mumkin.",
        'Бонусы начисляются автоматически и могут появиться в течение суток после доставки и оплаты заказа.',
    ),

    'loyalty_guide.tiers.kicker': _ui_tr('Status', 'Maqom', 'Статус'),
    'loyalty_guide.tiers.title': _ui_tr('Climb the tiers', "Darajalar bo'ylab ko'tariling", 'Поднимайтесь по уровням'),
    'loyalty_guide.tiers.subtitle': _ui_tr(
        'The more you earn over a year, the higher you rise — and the faster every future AquaCoin adds up.',
        "Bir yil davomida qancha ko'p yig'sangiz, shuncha yuqori ko'tarilasiz — va har bir kelajakdagi AquaCoin tezroq to'planadi.",
        'Чем больше вы зарабатываете за год, тем выше поднимаетесь — и тем быстрее растут будущие AquaCoins.',
    ),
    'loyalty_guide.tier.from': _ui_tr('From {pts} AquaCoins', '{pts} AquaCoinsdan', 'От {pts} AquaCoins'),
    'loyalty_guide.tier.label_multiplier': _ui_tr('AquaCoins rate', 'AquaCoins koeffitsiyenti', 'множитель'),
    'loyalty_guide.tier.label_discount': _ui_tr('tier discount', 'daraja chegirmasi', 'скидка уровня'),

    # Tier NAMES are model-translatable (LoyaltyTierConfig.name entity translations,
    # editable in admin) — intentionally NOT seeded here as static keys.
    #
    # Multiplier & discount perks are SHARED, config-driven keys rendered per tier
    # from live LoyaltyTierConfig; the discount bullet is hidden at 0%.
    'loyalty_guide.tier.perk_multiplier': _ui_tr('{mult}× AquaCoins on every order', 'Har buyurtmada {mult}× AquaCoins', '{mult}× AquaCoins за каждый заказ'),
    'loyalty_guide.tier.perk_discount': _ui_tr('{pct}% tier discount', '{pct}% daraja chegirmasi', 'Скидка уровня {pct}%'),

    # Tagline + qualitative benefit bullets are keyed by tier display_order (0=lowest
    # tier), a rename-proof identity — renaming a tier never drops its handbook copy.
    'loyalty_guide.tier.0.tagline': _ui_tr('Where every member starts', "Har bir a'zo shu yerdan boshlaydi", 'С этого начинают все'),
    'loyalty_guide.tier.0.benefit1': _ui_tr('Full access to the rewards catalog', "Mukofotlar katalogiga to'liq kirish", 'Полный доступ к каталогу наград'),
    'loyalty_guide.tier.0.benefit2': _ui_tr('Standard delivery, always free', 'Standart yetkazib berish, doim bepul', 'Стандартная доставка, всегда бесплатно'),

    'loyalty_guide.tier.1.tagline': _ui_tr('For our regulars', 'Doimiy mijozlarimiz uchun', 'Для постоянных клиентов'),
    'loyalty_guide.tier.1.benefit1': _ui_tr('Priority support & faster delivery', 'Ustuvor qo‘llab-quvvatlash va tezroq yetkazish', 'Приоритетная поддержка и быстрая доставка'),

    'loyalty_guide.tier.2.tagline': _ui_tr('For true water lovers', 'Haqiqiy suv ixlosmandlari uchun', 'Для настоящих ценителей воды'),
    'loyalty_guide.tier.2.benefit1': _ui_tr('VIP support & early access to promos', 'VIP qo‘llab-quvvatlash va aksiyalarga erta kirish', 'VIP-поддержка и ранний доступ к акциям'),

    'loyalty_guide.tier.3.tagline': _ui_tr('Our most valued members', "Eng qadrli a'zolarimiz", 'Самые ценные участники'),
    'loyalty_guide.tier.3.benefit1': _ui_tr('Dedicated manager & free express delivery', 'Shaxsiy menejer va bepul ekspress yetkazish', 'Персональный менеджер и бесплатная экспресс-доставка'),

    'loyalty_guide.promo.title': _ui_tr('How tier promotion works', 'Daraja oshirish qanday ishlaydi', 'Как происходит повышение уровня'),
    'loyalty_guide.promo.s1_title': _ui_tr('Cross a threshold', "Chegaradan o'ting", 'Преодолейте порог'),
    'loyalty_guide.promo.s1_desc': _ui_tr(
        "Reach a tier's AquaCoin threshold and you're upgraded instantly — no waiting.",
        'Daraja AquaCoins chegarasiga yeting va darhol koʻtarilasiz — kutishsiz.',
        'Достигните порога уровня — и повышение происходит мгновенно, без ожидания.',
    ),
    'loyalty_guide.promo.s2_title': _ui_tr('Locked in for a year', 'Bir yilga mustahkamlanadi', 'Закреплён на год'),
    'loyalty_guide.promo.s2_desc': _ui_tr(
        "Your new tier is guaranteed for {days} days — you won't be downgraded while you stay active.",
        'Yangi darajangiz {days} kunga kafolatlanadi — faol boʻlib turganingizda pasaytirilmaysiz.',
        'Ваш новый уровень гарантирован на {days} дней — вы не понизитесь, пока остаётесь активны.',
    ),
    'loyalty_guide.promo.s3_title': _ui_tr('Stay active', 'Faol boʻling', 'Оставайтесь активны'),
    'loyalty_guide.promo.s3_desc': _ui_tr(
        'Keep ordering to refresh your status and hold your tier — or climb to the next one.',
        'Maqomingizni yangilash va darajangizni saqlash uchun buyurtma berishda davom eting — yoki keyingisiga koʻtariling.',
        'Продолжайте заказывать, чтобы обновлять статус и сохранять уровень — или подняться выше.',
    ),
    'loyalty_guide.rolling.title': _ui_tr('Your status uses the last 12 months.', 'Maqomingiz soʻnggi 12 oyga asoslanadi.', 'Статус считается за последние 12 месяцев.'),
    'loyalty_guide.rolling.body': _ui_tr(
        'Only AquaCoins earned in the trailing {days} days count toward your tier. Spending AquaCoins on rewards never changes your status.',
        'Faqat soʻnggi {days} kun ichida yigʻilgan AquaCoins darajangizga hisoblanadi. Mukofotlarga AquaCoins sarflash maqomingizni oʻzgartirmaydi.',
        'К уровню учитываются только AquaCoins за последние {days} дней. Трата AquaCoins на награды никогда не меняет статус.',
    ),

    'loyalty_guide.twopoints.kicker': _ui_tr('Good to know', 'Bilib qoʻying', 'Важно знать'),
    'loyalty_guide.twopoints.title': _ui_tr('Two kinds of AquaCoins', 'Ikki xil AquaCoins', 'Два вида AquaCoins'),
    'loyalty_guide.twopoints.balance_title': _ui_tr('Spendable balance', 'Sarflanadigan balans', 'Доступный баланс'),
    'loyalty_guide.twopoints.balance_desc': _ui_tr(
        'The AquaCoins you can redeem right now for discounts and free products.',
        'Hozir chegirma va bepul mahsulotlarga almashtira oladigan AquaCoins.',
        'AquaCoins, которые можно прямо сейчас обменять на скидки и товары.',
    ),
    'loyalty_guide.twopoints.status_title': _ui_tr('Status AquaCoins', 'Maqom AquaCoins', 'Статусные AquaCoins'),
    'loyalty_guide.twopoints.status_desc': _ui_tr(
        'AquaCoins earned in the last {days} days. These decide your tier — and redeeming never reduces them.',
        'Soʻnggi {days} kun ichida yigʻilgan AquaCoins. Ular darajangizni belgilaydi — va almashtirish ularni kamaytirmaydi.',
        'AquaCoins за последние {days} дней. Они определяют уровень — и обмен их не уменьшает.',
    ),
    'loyalty_guide.twopoints.note': _ui_tr(
        'Redeeming rewards never lowers your tier. Spend your balance freely — your status stays.',
        'Mukofotlarni almashtirish darajangizni hech qachon pasaytirmaydi. Balansingizni bemalol sarflang — maqomingiz saqlanadi.',
        'Обмен наград никогда не понижает уровень. Тратьте баланс свободно — статус сохраняется.',
    ),

    'loyalty_guide.rewards.kicker': _ui_tr('Rewards', 'Mukofotlar', 'Награды'),
    'loyalty_guide.rewards.title': _ui_tr("Rewards you'll love", 'Sizga yoqadigan mukofotlar', 'Награды, которые вам понравятся'),
    'loyalty_guide.rewards.subtitle': _ui_tr(
        'Turn your AquaCoins into savings on the water you already order.',
        'AquaCoins allaqachon buyurtma qilayotgan suvingizdagi tejamga aylantiring.',
        'Превратите AquaCoins в экономию на воде, которую вы и так заказываете.',
    ),
    'loyalty_guide.rewards.discount_title': _ui_tr('Discounts', 'Chegirmalar', 'Скидки'),
    'loyalty_guide.rewards.discount_desc': _ui_tr(
        'Knock a fixed amount off your order total at checkout.',
        'Toʻlov paytida buyurtma summangizdan belgilangan miqdorni chegiring.',
        'Снижайте сумму заказа на фиксированную величину при оформлении.',
    ),
    'loyalty_guide.rewards.product_title': _ui_tr('Free products', 'Bepul mahsulotlar', 'Бесплатные товары'),
    'loyalty_guide.rewards.product_desc': _ui_tr(
        'Add a free bottle of water straight to your order.',
        'Buyurtmangizga bepul suv shishasini qoʻshing.',
        'Добавьте бесплатную бутылку воды прямо в заказ.',
    ),
    'loyalty_guide.redeem.title': _ui_tr('How to redeem', 'Qanday almashtirish', 'Как обменять'),
    'loyalty_guide.redeem.s1': _ui_tr(
        'Pick a reward at checkout in the app or Telegram bot.',
        'Ilova yoki Telegram botda toʻlov paytida mukofotni tanlang.',
        'Выберите награду при оформлении в приложении или Telegram-боте.',
    ),
    'loyalty_guide.redeem.s2': _ui_tr('Your AquaCoins are deducted instantly.', 'AquaCoins darhol yechib olinadi.', 'AquaCoins списываются мгновенно.'),
    'loyalty_guide.redeem.s3': _ui_tr(
        'The discount or free item applies to that order.',
        'Chegirma yoki bepul mahsulot oʻsha buyurtmaga qoʻllaniladi.',
        'Скидка или бесплатный товар применяется к этому заказу.',
    ),
    'loyalty_guide.redeem.note': _ui_tr(
        'One reward per order. Some rewards have a minimum order value. Cancel an order and your AquaCoins are refunded.',
        'Har buyurtmaga bitta mukofot. Baʼzi mukofotlarda minimal buyurtma summasi bor. Buyurtmani bekor qilsangiz, AquaCoins qaytariladi.',
        'Одна награда на заказ. У некоторых наград есть минимальная сумма заказа. При отмене заказа AquaCoins возвращаются.',
    ),

    'loyalty_guide.expiry.title': _ui_tr('Make them count.', 'AquaCoins ishlating.', 'Используйте вовремя.'),
    'loyalty_guide.expiry.body': _ui_tr(
        'AquaCoins are valid for {days} days from the day you earn them, and we always use your oldest AquaCoins first — so put them to use within the year.',
        'AquaCoins yigʻilgan kundan {days} kun amal qiladi va biz doim eng eski AquaCoins birinchi ishlatamiz — shuning uchun ularni yil davomida ishlating.',
        'AquaCoins действуют {days} дней с момента начисления, и мы всегда используем самые старые AquaCoins первыми — поэтому используйте их в течение года.',
    ),
    'loyalty_guide.delivery_note': _ui_tr(
        'Delivery is always free — you never spend AquaCoins on it.',
        'Yetkazib berish doim bepul — bunga hech qachon AquaCoins sarflamaysiz.',
        'Доставка всегда бесплатна — AquaCoins на неё не тратятся.',
    ),

    'loyalty_guide.faq.kicker': _ui_tr('FAQ', 'Savol-javob', 'Вопросы и ответы'),
    'loyalty_guide.faq.title': _ui_tr('Questions, answered', 'Savollarga javoblar', 'Ответы на вопросы'),
    'loyalty_guide.faq.q1': _ui_tr('Do my AquaCoins expire?', 'AquaCoins muddati tugaydimi?', 'Сгорают ли мои AquaCoins?'),
    'loyalty_guide.faq.a1': _ui_tr(
        'Yes — AquaCoins stay valid for {days} days from when you earn them. We always spend your oldest AquaCoins first.',
        'Ha — AquaCoins yigʻilgan kundan {days} kun amal qiladi. Biz doim eng eski AquaCoins birinchi sarflaymiz.',
        'Да — AquaCoins действуют {days} дней с момента начисления. Сначала всегда расходуются самые старые.',
    ),
    'loyalty_guide.faq.q2': _ui_tr('Does redeeming rewards lower my tier?', 'Mukofot almashtirish darajamni pasaytiradimi?', 'Понижает ли обмен наград мой уровень?'),
    'loyalty_guide.faq.a2': _ui_tr(
        "No. Your tier is based on the AquaCoins you've earned, not your spendable balance. Redeem freely — your status stays.",
        'Yoʻq. Darajangiz sarflanadigan balansga emas, yigʻgan AquaCoins asoslanadi. Bemalol almashtiring — maqomingiz saqlanadi.',
        'Нет. Уровень зависит от заработанных AquaCoins, а не от доступного баланса. Обменивайте свободно — статус сохраняется.',
    ),
    'loyalty_guide.faq.q3': _ui_tr('How is my tier decided?', 'Darajam qanday aniqlanadi?', 'Как определяется мой уровень?'),
    'loyalty_guide.faq.a3': _ui_tr(
        "By the AquaCoins you earn in a rolling 12-month window. Cross a threshold and you're upgraded instantly, then locked in.",
        'Soʻnggi 12 oy ichida yigʻgan AquaCoins boʻyicha. Chegaradan oʻtsangiz, darhol koʻtarilasiz va daraja mustahkamlanadi.',
        'По AquaCoins за скользящие 12 месяцев. Преодолели порог — повышение мгновенно, затем уровень закрепляется.',
    ),
    'loyalty_guide.faq.q4': _ui_tr('How do I redeem a reward?', 'Mukofotni qanday almashtiraman?', 'Как обменять награду?'),
    'loyalty_guide.faq.a4': _ui_tr(
        'Pick a reward at checkout in the app or our Telegram bot. Your AquaCoins are deducted and the benefit applies to that order.',
        'Ilova yoki Telegram botimizda toʻlov paytida mukofotni tanlang. AquaCoins yechiladi va imtiyoz oʻsha buyurtmaga qoʻllaniladi.',
        'Выберите награду при оформлении в приложении или Telegram-боте. AquaCoins спишутся, а выгода применится к заказу.',
    ),
    'loyalty_guide.faq.q5': _ui_tr('Where can I see my balance and tier?', 'Balans va darajamni qayerdan koʻraman?', 'Где посмотреть баланс и уровень?'),
    'loyalty_guide.faq.a5': _ui_tr(
        'Open the Loyalty section in our Telegram bot, or your account on the website — your balance, tier and history are all there.',
        'Telegram botimizdagi Sodiqlik boʻlimini yoki saytdagi hisobingizni oching — balans, daraja va tarix shu yerda.',
        'Откройте раздел «Лояльность» в Telegram-боте или личный кабинет на сайте — там баланс, уровень и история.',
    ),
    'loyalty_guide.faq.q6': _ui_tr('Is delivery free?', 'Yetkazib berish bepulmi?', 'Доставка бесплатная?'),
    'loyalty_guide.faq.a6': _ui_tr(
        'Yes, delivery is always free — you never need to spend AquaCoins on it.',
        'Ha, yetkazib berish doim bepul — bunga AquaCoins sarflash shart emas.',
        'Да, доставка всегда бесплатна — тратить на неё AquaCoins не нужно.',
    ),
    'loyalty_guide.faq.q7': _ui_tr(
        'When does a referral count as successful?',
        'Referal qachon muvaffaqiyatli hisoblanadi?',
        'Когда реферал считается успешным?',
    ),
    'loyalty_guide.faq.a7': _ui_tr(
        "When your invited friend's first order is both delivered and fully paid. Until then it shows as Pending; once both are done, you and your friend receive your AquaCoins automatically.",
        "Taklif qilgan do'stingizning birinchi buyurtmasi yetkazib berilib, to'liq to'langanda. Shu paytgacha u «Kutilmoqda» bo'lib turadi; ikkala shart bajarilgach, siz va do'stingiz AquaCoins avtomatik olasiz.",
        'Когда первый заказ приглашённого друга доставлен и полностью оплачен. До этого он показывается как «Ожидается»; после выполнения обоих условий вы и ваш друг получаете AquaCoins автоматически.',
    ),
    'loyalty_guide.faq.q8': _ui_tr(
        'What are surprise rewards?',
        'Kutilmagan mukofotlar nima?',
        'Что такое сюрприз-награды?',
    ),
    'loyalty_guide.faq.a8': _ui_tr(
        "Exactly what the name says — a little delight. Once in a while, members who order are picked at random to receive a bonus of AquaCoins. We won't say when it happens or how much it'll be; just keep ordering, and one might quietly land in your balance.",
        "Nomidan ko'rinib turibdi — yoqimli kutilmagan sovg'a. Vaqti-vaqti bilan buyurtma bergan a'zolar tasodifan tanlanib, AquaCoins bonusini oladi. Qachon va qancha bo'lishini aytmaymiz; faqat buyurtma berishda davom eting — bonus balansingizga sokin tushib qolishi mumkin.",
        'Ровно то, что следует из названия, — приятный сюрприз. Время от времени участники, которые делают заказы, случайно выбираются для бонуса AquaCoins. Мы не скажем, когда это случится и сколько это будет; просто продолжайте заказывать — и он может тихо появиться на вашем балансе.',
    ),

    'loyalty_guide.cta.title': _ui_tr('Ready to start earning?', "Yig'ishni boshlashga tayyormisiz?", 'Готовы начать копить?'),
    'loyalty_guide.cta.body': _ui_tr(
        'Every bottle brings you closer to your next reward. Place an order and watch your AquaCoins grow.',
        'Har bir shisha sizni keyingi mukofotingizga yaqinlashtiradi. Buyurtma bering va AquaCoins oʻsishini kuzating.',
        'Каждая бутылка приближает вас к следующей награде. Сделайте заказ и наблюдайте, как растут AquaCoins.',
    ),
    'loyalty_guide.cta.button': _ui_tr('Order now', 'Hozir buyurtma berish', 'Заказать сейчас'),

    # Bot entry button (Telegram Loyalty menu → opens /loyalty-guide).
    'telegram.loyalty.guide_button': _ui_tr('How it works', 'Qanday ishlaydi', 'Как это работает'),

    # Web storefront (loyalty.html / my_account.html) — the migrated English
    # AquaCoins strings are keyed by their English text via the |t filter, so
    # these provide the uz/ru localizations. The coin name stays Latin.
    'AquaCoins': _ui_tr('AquaCoins', 'AquaCoins', 'AquaCoins'),
    'AquaCoins Balance': _ui_tr('AquaCoins Balance', 'AquaCoins balansi', 'Баланс AquaCoins'),
    'AquaCoins to next tier': _ui_tr(
        'AquaCoins to next tier',
        'keyingi darajagacha AquaCoins',
        'AquaCoins до следующего уровня',
    ),
    'more coins to keep status': _ui_tr(
        'more coins to keep status',
        'maqomni saqlash uchun yana AquaCoins',
        'ещё AquaCoins для сохранения статуса',
    ),
    'Blue Stream AquaCoins': _ui_tr('Blue Stream AquaCoins', 'Blue Stream AquaCoins', 'Blue Stream AquaCoins'),
    'Earn AquaCoins with every purchase, build streaks, and unlock exclusive rewards': _ui_tr(
        'Earn AquaCoins with every purchase, build streaks, and unlock exclusive rewards',
        "Har bir xariddan AquaCoins yig'ing, streaklar to'plang va eksklyuziv mukofotlarni oching",
        'Зарабатывайте AquaCoins с каждой покупкой, копите серии и открывайте эксклюзивные награды',
    ),
    'Earn 1 AquaCoin for every 250 UZS spent. Higher tiers earn up to 2x coins!': _ui_tr(
        'Earn 1 AquaCoin for every 250 UZS spent. Higher tiers earn up to 2x coins!',
        "Har 250 so'm uchun 1 AquaCoin oling. Yuqori darajalar 2 baravargacha AquaCoins beradi!",
        'Получайте 1 AquaCoin за каждые 250 UZS. Высокие уровни дают до 2x AquaCoins!',
    ),
    'AquaCoins History': _ui_tr(
        'AquaCoins History',
        'AquaCoins tarixi',
        'История AquaCoins',
    ),
    'AquaCoins Earned': _ui_tr(
        'AquaCoins Earned',
        "Yig'ilgan AquaCoins",
        'Начисленные AquaCoins',
    ),
    'AquaCoins Redeemed': _ui_tr(
        'AquaCoins Redeemed',
        'Ishlatilgan AquaCoins',
        'Списанные AquaCoins',
    ),
    'AquaCoins Expired': _ui_tr(
        'AquaCoins Expired',
        'Muddati tugagan AquaCoins',
        'Истёкшие AquaCoins',
    ),
    'Loading AquaCoins history...': _ui_tr(
        'Loading AquaCoins history...',
        'AquaCoins tarixi yuklanmoqda...',
        'Загрузка истории AquaCoins...',
    ),
    'Ways to Earn AquaCoins': _ui_tr(
        'Ways to Earn AquaCoins',
        "AquaCoins yig'ish yo'llari",
        'Способы заработать AquaCoins',
    ),
    'Invite friends with your referral code. Earn 500 AquaCoins when they place their first order!': _ui_tr(
        'Invite friends with your referral code. Earn 500 AquaCoins when they place their first order!',
        "Referal kodingiz bilan do'stlaringizni taklif qiling. Ular birinchi buyurtmasini berganda 500 AquaCoins oling!",
        'Приглашайте друзей по реферальному коду. Получите 500 AquaCoins, когда они сделают первый заказ!',
    ),
    'Share your experience by reviewing products. Get 50 AquaCoins for each review!': _ui_tr(
        'Share your experience by reviewing products. Get 50 AquaCoins for each review!',
        "Mahsulotlarga sharh qoldirib, tajribangiz bilan o'rtoqlashing. Har bir sharh uchun 50 AquaCoins oling!",
        'Делитесь опытом, оставляя отзывы о товарах. Получайте 50 AquaCoins за каждый отзыв!',
    ),
    'Celebrate with us! Receive 200 bonus AquaCoins automatically on your birthday.': _ui_tr(
        'Celebrate with us! Receive 200 bonus AquaCoins automatically on your birthday.',
        "Biz bilan nishonlang! Tug'ilgan kuningizda avtomatik ravishda 200 bonus AquaCoins oling.",
        'Празднуйте с нами! Получите 200 бонусных AquaCoins автоматически в день рождения.',
    ),
    'No AquaCoins history found': _ui_tr(
        'No AquaCoins history found',
        'AquaCoins tarixi topilmadi',
        'История AquaCoins не найдена',
    ),
    'Failed to load AquaCoins history': _ui_tr(
        'Failed to load AquaCoins history',
        'AquaCoins tarixini yuklab bo\'lmadi',
        'Не удалось загрузить историю AquaCoins',
    ),
    'Need more AquaCoins': _ui_tr(
        'Need more AquaCoins',
        "Ko'proq AquaCoins kerak",
        'Нужно больше AquaCoins',
    ),
    'AquaCoins Required': _ui_tr(
        'AquaCoins Required',
        'Kerakli AquaCoins',
        'Требуется AquaCoins',
    ),
    'Your AquaCoins': _ui_tr(
        'Your AquaCoins',
        'Sizning AquaCoins',
        'Ваши AquaCoins',
    ),
    'Earn bonus AquaCoins on subscription orders. Redeem for discounts, free products, and exclusive perks.': _ui_tr(
        'Earn bonus AquaCoins on subscription orders. Redeem for discounts, free products, and exclusive perks.',
        "Obuna buyurtmalarida bonus AquaCoins yig'ing. Chegirmalar, bepul mahsulotlar va eksklyuziv imtiyozlar uchun ishlating.",
        'Зарабатывайте бонусные AquaCoins на заказах по подписке. Тратьте их на скидки, бесплатные товары и эксклюзивные привилегии.',
    ),
    'Subscription members save up to 20% compared to one-time purchases. You also get free delivery, earn bonus AquaCoins (up to 2x multiplier), and access exclusive subscriber-only promotions. The exact savings depend on your chosen products and frequency.': _ui_tr(
        'Subscription members save up to 20% compared to one-time purchases. You also get free delivery, earn bonus AquaCoins (up to 2x multiplier), and access exclusive subscriber-only promotions. The exact savings depend on your chosen products and frequency.',
        "Obuna a'zolari bir martalik xaridlarga nisbatan 20% gacha tejaydi. Shuningdek, bepul yetkazib berish, bonus AquaCoins (2x gacha koeffitsiyent) va faqat obunachilar uchun maxsus aksiyalardan foydalanasiz. Aniq tejamkorlik tanlangan mahsulotlar va buyurtma chastotasiga bog'liq.",
        'Участники подписки экономят до 20% по сравнению с разовыми покупками. Вы также получаете бесплатную доставку, зарабатываете бонусные AquaCoins (множитель до 2x) и доступ к эксклюзивным акциям только для подписчиков. Точная экономия зависит от выбранных товаров и частоты заказов.',
    ),
    'landing.loyalty.points': _ui_tr('AquaCoins', 'AquaCoins', 'AquaCoins'),
    'landing.loyalty.subtitle': _ui_tr(
        'Earn AquaCoins with every purchase and redeem them for amazing rewards!',
        "Har bir xarid bilan AquaCoins to'plang va ularni ajoyib mukofotlarga almashtiring!",
        'Зарабатывайте AquaCoins с каждой покупкой и обменивайте их на удивительные награды!',
    ),
    'landing.loyalty.public_title': {
        'en': 'Join Aqua Club — earn AquaCoins on every order',
        'uz': "Aqua Club'ga qo'shiling — har bir buyurtmada AquaCoins yig'ing",
        'ru': 'Вступайте в Aqua Club — копите AquaCoins с каждого заказа'
    },
    'landing.loyalty.public_subtitle': {
        'en': 'Our free loyalty programme rewards every delivered order. Earn AquaCoins, climb tiers, and redeem rewards.',
        'uz': "Bepul sodiqlik dasturimiz har bir yetkazilgan buyurtmani mukofotlaydi. AquaCoins yig'ing, darajalar bo'ylab ko'tariling va sovg'alarni oling.",
        'ru': 'Наша бесплатная программа лояльности вознаграждает каждый доставленный заказ. Копите AquaCoins, повышайте уровень и получайте награды.'
    },
    'landing.loyalty.public_earn_title': {
        'en': 'Earn on every order', 'uz': 'Har bir buyurtmada yig\'ing', 'ru': 'Копите с каждого заказа'
    },
    'landing.loyalty.public_earn': {
        'en': 'Get a {bonus} AquaCoins welcome bonus when you join, then earn on every delivered, paid order.',
        'uz': "Qo'shilganingizda {bonus} AquaCoins kutib olish bonusini oling, so'ngra har bir yetkazilgan va to'langan buyurtmada yig'ing.",
        'ru': 'Получите приветственный бонус {bonus} AquaCoins при вступлении, затем копите с каждого доставленного оплаченного заказа.'
    },
    'landing.loyalty.public_tiers_title': {
        'en': 'Tiers that reward loyalty', 'uz': 'Sodiqlikni mukofotlovchi darajalar', 'ru': 'Уровни за лояльность'
    },
    'landing.loyalty.public_redeem_title': {
        'en': 'Redeem for rewards', 'uz': 'Sovg\'alarga almashtiring', 'ru': 'Обменивайте на награды'
    },
    'landing.loyalty.public_redeem': {
        'en': 'Spend your AquaCoins on discounts and free products at checkout.',
        'uz': "AquaCoins'ni to'lov vaqtida chegirmalar va bepul mahsulotlarga sarflang.",
        'ru': 'Тратьте AquaCoins на скидки и бесплатные товары при оформлении заказа.'
    },
    'landing.loyalty.public_cta_primary': {
        'en': 'See how Aqua Club works', 'uz': 'Aqua Club qanday ishlashini ko\'ring', 'ru': 'Узнать, как работает Aqua Club'
    },
}
BACKEND_TRANSLATIONS.update(LOYALTY_GUIDE_TRANSLATIONS)


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

            for language in ('en', 'uz', 'ru'):
                value, preserve_existing = _resolve_seed_value(translations, language)

                # Check if translation already exists
                existing = Translation.query.filter_by(
                    key=key,
                    language=language
                ).first()

                if existing:
                    if preserve_existing:
                        skipped_count += 1
                        continue

                    if existing.value != value:
                        print(f"    [{language}] Updating: '{existing.value}' → '{value}'")
                        existing.value = value
                        existing.is_active = True
                        existing.category = _category_for(key)
                        updated_count += 1
                    else:
                        skipped_count += 1
                else:
                    print(f"    [{language}] Creating: '{value}'")
                    translation = Translation(
                        key=key,
                        language=language,
                        value=value,
                        category=_category_for(key),
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
