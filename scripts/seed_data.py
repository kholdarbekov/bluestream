#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Comprehensive database seeding script for Blue Stream Water Business Platform
"""
import os
import sys
from datetime import datetime, UTC

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from business_app import create_app, db
from business_app.models.translation import Translation


def seed_essential_translations():
    """Seed essential translations for the multilingual system"""
    print("Seeding essential translations...")

    # Essential translations
    ESSENTIAL_TRANSLATIONS = {
        "Home": {"en": "Home", "uz": "Bosh sahifa", "ru": "Главная"},
        "Shop": {"en": "Shop", "uz": "Do'kon", "ru": "Магазин"},
        "Services": {"en": "Services", "uz": "Xizmatlar", "ru": "Услуги"},
        "About Us": {"en": "About Us", "uz": "Biz haqimizda", "ru": "О нас"},
        "Contact": {"en": "Contact", "uz": "Aloqa", "ru": "Контакты"},
        "Gallery": {"en": "Gallery", "uz": "Galereya", "ru": "Галерея"},
        "Pages": {"en": "Pages", "uz": "Sahifalar", "ru": "Страницы"},
        "Subscriptions": {"en": "Subscriptions", "uz": "Obunalar", "ru": "Подписки"},
        "Login": {"en": "Login", "uz": "Kirish", "ru": "Войти"},
        "Logout": {"en": "Logout", "uz": "Chiqish", "ru": "Выйти"},
        "Register": {"en": "Register", "uz": "Ro'yxatdan o'tish", "ru": "Регистрация"},
        "My Account": {"en": "My Account", "uz": "Mening hisobim", "ru": "Мой аккаунт"},
        "My Orders": {"en": "My Orders", "uz": "Buyurtmalarim", "ru": "Мои заказы"},
        "Profile Settings": {"en": "Profile Settings", "uz": "Profil sozlamalari", "ru": "Настройки профиля"},
        "Addresses": {"en": "Addresses", "uz": "Manzillar", "ru": "Адреса"},
        "Security": {"en": "Security", "uz": "Xavfsizlik", "ru": "Безопасность"},
        "Search products...": {"en": "Search products...", "uz": "Mahsulot qidirish...", "ru": "Поиск товаров..."},
        "Shopping Cart": {"en": "Shopping Cart", "uz": "Savat", "ru": "Корзина"},
        "Add to Cart": {"en": "Add to Cart", "uz": "Savatga qo'shish", "ru": "Добавить в корзину"},
        "Checkout": {"en": "Checkout", "uz": "To'lov", "ru": "Оформить заказ"},
        "Contact Info": {"en": "Contact Info", "uz": "Aloqa ma'lumotlari", "ru": "Контактная информация"},
        "Call Us": {"en": "Call Us", "uz": "Qo'ng'iroq qiling", "ru": "Позвоните нам"},
        "Address": {"en": "Address", "uz": "Manzil", "ru": "Адрес"},
        "Useful Links": {"en": "Useful Links", "uz": "Foydali havolalar", "ru": "Полезные ссылки"},
        "Subscribe": {"en": "Subscribe", "uz": "Obuna bo'lish", "ru": "Подписаться"},
        "All Rights Reserved": {"en": "All Rights Reserved", "uz": "Barcha huquqlar himoyalangan", "ru": "Все права защищены"},
        "Terms of Service": {"en": "Terms of Service", "uz": "Xizmat ko'rsatish shartlari", "ru": "Условия обслуживания"},
        "Privacy Policy": {"en": "Privacy Policy", "uz": "Maxfiylik siyosati", "ru": "Политика конфиденциальности"},
        "Save": {"en": "Save", "uz": "Saqlash", "ru": "Сохранить"},
        "Cancel": {"en": "Cancel", "uz": "Bekor qilish", "ru": "Отмена"},
        "Edit": {"en": "Edit", "uz": "Tahrirlash", "ru": "Редактировать"},
        "Delete": {"en": "Delete", "uz": "O'chirish", "ru": "Удалить"},
        "Submit": {"en": "Submit", "uz": "Yuborish", "ru": "Отправить"},
        "Loading": {"en": "Loading...", "uz": "Yuklanmoqda...", "ru": "Загрузка..."},
        "Logged out successfully": {"en": "Logged out successfully", "uz": "Muvaffaqiyatli chiqildi", "ru": "Выход выполнен успешно"},
        "Product added to cart": {"en": "Product added to cart", "uz": "Mahsulot savatga qo'shildi", "ru": "Товар добавлен в корзину"},
        "Your session has expired. Please login again.": {"en": "Session expired", "uz": "Sessiya tugadi", "ru": "Сессия истекла"},
        "Preloader Close": {"en": "Close", "uz": "Yopish", "ru": "Закрыть"},

        # Banner Headlines
        "Always Want Safe and Good Water for Healthy Life": {
            "en": "Always Want Safe and Good Water for Healthy Life",
            "uz": "Sog'lom hayot uchun har doim xavfsiz va sifatli suv",
            "ru": "Всегда нужна безопасная и качественная вода для здоровой жизни"
        },
        "Pure Water Delivery To Your Doorstep": {
            "en": "Pure Water Delivery To Your Doorstep",
            "uz": "Toza suvni eshigingizgacha yetkazib beramiz",
            "ru": "Доставка чистой воды к вашему порогу"
        },
        "Trusted Name In Bottled Water Industry": {
            "en": "Trusted Name In Bottled Water Industry",
            "uz": "Butillangan suv sanoatidagi ishonchli nom",
            "ru": "Надежное имя в индустрии бутилированной воды"
        },

        # Banner Descriptions
        "Experience the convenience of premium water delivery service with our reliable, eco-friendly solutions for your home and office.": {
            "en": "Experience the convenience of premium water delivery service with our reliable, eco-friendly solutions for your home and office.",
            "uz": "Uy va ofisingiz uchun ishonchli va ekologik toza yechimlar bilan premium suv yetkazib berish xizmatining qulayligini his eting.",
            "ru": "Ощутите удобство премиальной доставки воды с нашими надежными и экологичными решениями для дома и офиса."
        },
        "We provide our services across Uzbekistan with a network of professional delivery partners ensuring the highest quality standards.": {
            "en": "We provide our services across Uzbekistan with a network of professional delivery partners ensuring the highest quality standards.",
            "uz": "Biz O'zbekiston bo'ylab professional yetkazib berish hamkorlari tarmog'i orqali eng yuqori sifat standartlari bilan xizmat ko'rsatamiz.",
            "ru": "Мы предоставляем услуги по всему Узбекистану через сеть профессиональных партнеров по доставке, обеспечивая самые высокие стандарты качества."
        },

        # Buttons
        "Our Services": {"en": "Our Services", "uz": "Bizning xizmatlarimiz", "ru": "Наши услуги"},
        "Discover": {"en": "Discover", "uz": "Kashf qiling", "ru": "Открыть"},
        "Shop Now": {"en": "Shop Now", "uz": "Xarid qiling", "ru": "Купить сейчас"},
        "View Plans": {"en": "View Plans", "uz": "Rejalarni ko'rish", "ru": "Посмотреть планы"},
        "Read More": {"en": "Read More", "uz": "Batafsil", "ru": "Подробнее"},
        "Learn More": {"en": "Learn More", "uz": "Ko'proq o'rganing", "ru": "Узнать больше"},

        # Shopping
        "Price": {"en": "Price", "uz": "Narx", "ru": "Цена"},
        "Quantity": {"en": "Quantity", "uz": "Soni", "ru": "Количество"},
        "Total": {"en": "Total", "uz": "Jami", "ru": "Итого"},

        # Quality Features
        "Maximum Purity": {"en": "Maximum Purity", "uz": "Maksimal tozalik", "ru": "Максимальная чистота"},
        "Chlorine Free": {"en": "Chlorine Free", "uz": "Xlorsiz", "ru": "Без хлора"},
        "8 Steps Filtration": {"en": "8 Steps Filtration", "uz": "8 bosqichli filtrlash", "ru": "8-ступенчатая фильтрация"},
        "Healthy Water": {"en": "Healthy Water", "uz": "Sog'lom suv", "ru": "Здоровая вода"},

        # Status & Messages
        "Active": {"en": "Active", "uz": "Faol", "ru": "Активно"},
        "Verified": {"en": "Verified", "uz": "Tasdiqlangan", "ru": "Подтверждено"},
        "Pending": {"en": "Pending", "uz": "Kutilmoqda", "ru": "Ожидает"},
        "Free": {"en": "Free", "uz": "Bepul", "ru": "Бесплатно"},
        "Delivery": {"en": "Delivery", "uz": "Yetkazib berish", "ru": "Доставка"},

        # Contact & Support
        "Phone Number": {"en": "Phone Number", "uz": "Telefon raqami", "ru": "Номер телефона"},
        "Email Address": {"en": "Email Address", "uz": "Elektron pochta manzili", "ru": "Адрес электронной почты"},
        "Password": {"en": "Password", "uz": "Parol", "ru": "Пароль"},
        "Address": {"en": "Address", "uz": "Manzil", "ru": "Адрес"},
        "First Name": {"en": "First Name", "uz": "Ism", "ru": "Имя"},
        "Last Name": {"en": "Last Name", "uz": "Familiya", "ru": "Фамилия"},

    }

    TEMPLATE_TRANSLATIONS = {
        "1 point per $1 spent": {
            "en": "1 point per $1 spent",
            "uz": "Har $1 uchun 1 ball",
            "ru": "1 балл за каждый $1"
        },
        "1.25 points per $1 spent": {
            "en": "1.25 points per $1 spent",
            "uz": "Har $1 uchun 1.25 ball",
            "ru": "1.25 балла за каждый $1"
        },
        "1.5 points per $1 spent": {
            "en": "1.5 points per $1 spent",
            "uz": "Har $1 uchun 1.5 ball",
            "ru": "1.5 балла за каждый $1"
        },
        "10% birthday discount": {
            "en": "10% birthday discount",
            "uz": "Tug'ilgan kun uchun 10% chegirma",
            "ru": "10% скидка на день рождения"
        },
        "12: 00 PM - 3: 00 PM": {
            "en": "12:00 PM - 3:00 PM",
            "uz": "12:00 - 15:00",
            "ru": "12:00 - 15:00"
        },
        "15% Annual Discount": {
            "en": "15% Annual Discount",
            "uz": "15% yillik chegirma",
            "ru": "15% годовая скидка"
        },
        "15% birthday discount": {
            "en": "15% birthday discount",
            "uz": "Tug'ilgan kun uchun 15% chegirma",
            "ru": "15% скидка на день рождения"
        },
        "2 points per $1 spent": {
            "en": "2 points per $1 spent",
            "uz": "Har $1 uchun 2 ball",
            "ru": "2 балла за каждый $1"
        },
        "20 bottles of 19L water per month": {
            "en": "20 bottles of 19L water per month",
            "uz": "Oyiga 19L suvdan 20 dona",
            "ru": "20 бутылей по 19 л в месяц"
        },
        "24/7 Customer Support": {
            "en": "24/7 Customer Support",
            "uz": "24/7 mijozlarni qo'llab-quvvatlash",
            "ru": "Круглосуточная поддержка клиентов"
        },
        "24/7 phone & chat support": {
            "en": "24/7 phone & chat support",
            "uz": "24/7 telefon va chat orqali qo'llab-quvvatlash",
            "ru": "Круглосуточная поддержка по телефону и чату"
        },
        "24/7 support available": {
            "en": "24/7 support available",
            "uz": "24/7 qo'llab-quvvatlash mavjud",
            "ru": "Поддержка доступна 24/7"
        },
        "3: 00 PM - 6: 00 PM": {
            "en": "3:00 PM - 6:00 PM",
            "uz": "15:00 - 18:00",
            "ru": "15:00 - 18:00"
        },
        "4 Bottles Per Month": {
            "en": "4 Bottles Per Month",
            "uz": "Oyiga 4 dona",
            "ru": "4 бутыли в месяц"
        },
        "4 bottles of 19L water per month": {
            "en": "4 bottles of 19L water per month",
            "uz": "Oyiga 19L suvdan 4 dona",
            "ru": "4 бутыли по 19 л в месяц"
        },
        "48 Bottles Per Year": {
            "en": "48 Bottles Per Year",
            "uz": "Yiliga 48 dona",
            "ru": "48 бутылей в год"
        },
        "7 Days A Week Service": {
            "en": "7 Days A Week Service",
            "uz": "Haftaning 7 kuni xizmat",
            "ru": "Обслуживание 7 дней в неделю"
        },
        "8 Steps Filtration": {
            "en": "8 Steps Filtration",
            "uz": "8 bosqichli filtrlash",
            "ru": "8-ступенчатая фильтрация"
        },
        "8 bottles of 19L water per month": {
            "en": "8 bottles of 19L water per month",
            "uz": "Oyiga 19L suvdan 8 dona",
            "ru": "8 бутылей по 19 л в месяц"
        },
        "9: 00 AM - 12: 00 PM": {
            "en": "9:00 AM - 12:00 PM",
            "uz": "09:00 - 12:00",
            "ru": "09:00 - 12:00"
        },
        "A Trusted Name In": {
            "en": "A Trusted Name In",
            "uz": "Ishonchli nom",
            "ru": "Надежное имя в"
        },
        "About": {
            "en": "About",
            "uz": "Haqida",
            "ru": "О нас"
        },
        "About Blue Stream": {
            "en": "About Blue Stream",
            "uz": "Blue Stream haqida",
            "ru": "О Blue Stream"
        },
        "About Company": {
            "en": "About Company",
            "uz": "Kompaniya haqida",
            "ru": "О компании"
        },
        "About Us": {
            "en": "About Us",
            "uz": "Biz haqimizda",
            "ru": "О нас"
        },
        "Account Activity": {
            "en": "Account Activity",
            "uz": "Hisob faoliyati",
            "ru": "Активность аккаунта"
        },
        "Account Created": {
            "en": "Account Created",
            "uz": "Hisob yaratildi",
            "ru": "Аккаунт создан"
        },
        "Account Security": {
            "en": "Account Security",
            "uz": "Hisob xavfsizligi",
            "ru": "Безопасность аккаунта"
        },
        "Account Status": {
            "en": "Account Status",
            "uz": "Hisob holati",
            "ru": "Статус аккаунта"
        },
        "Account created successfully! Please check your email to verify your account.": {
            "en": "Account created successfully! Please check your email to verify your account.",
            "uz": "Hisob muvaffaqiyatli yaratildi! Hisobingizni tasdiqlash uchun emailingizni tekshiring.",
            "ru": "Аккаунт успешно создан! Проверьте вашу почту, чтобы подтвердить аккаунт."
        },
        "Account data downloaded successfully!": {
            "en": "Account data downloaded successfully!",
            "uz": "Hisob ma'lumotlari muvaffaqiyatli yuklab olindi!",
            "ru": "Данные аккаунта успешно загружены!"
        },
        "Account was successfully created": {
            "en": "Account was successfully created",
            "uz": "Hisob muvaffaqiyatli yaratildi",
            "ru": "Аккаунт был успешно создан"
        },
        "Actions": {
            "en": "Actions",
            "uz": "Harakatlar",
            "ru": "Действия"
        },
        "Active": {
            "en": "Active",
            "uz": "Faol",
            "ru": "Активный"
        },
        "Active Subscriptions": {
            "en": "Active Subscriptions",
            "uz": "Faol obunalar",
            "ru": "Активные подписки"
        },
        "Add New Address": {
            "en": "Add New Address",
            "uz": "Yangi manzil qo'shish",
            "ru": "Добавить новый адрес"
        },
        "Add Your First Address": {
            "en": "Add Your First Address",
            "uz": "Birinchi manzilingizni qo'shing",
            "ru": "Добавьте свой первый адрес"
        },
        "Add an extra layer of security by requiring SMS verification for login.": {
            "en": "Add an extra layer of security by requiring SMS verification for login.",
            "uz": "Kirish uchun SMS tasdiqlashni talab qilib, qo'shimcha xavfsizlik qatlamini qo'shing.",
            "ru": "Добавьте дополнительный уровень безопасности, требуя SMS-подтверждение для входа."
        },
        "Add some products to your cart to see them here": {
            "en": "Add some products to your cart to see them here",
            "uz": "Mahsulotlarni savatchangizga qo'shing va ularni bu yerda ko'rasiz",
            "ru": "Добавьте товары в корзину, чтобы увидеть их здесь"
        },
        "Add to Cart": {
            "en": "Add to Cart",
            "uz": "Savatchaga qo'shish",
            "ru": "Добавить в корзину"
        },
        "Add your delivery addresses to make ordering easier": {
            "en": "Add your delivery addresses to make ordering easier",
            "uz": "Buyurtmani osonlashtirish uchun yetkazib berish manzillaringizni qo'shing",
            "ru": "Добавьте адреса доставки, чтобы упростить заказ"
        },
        "Address": {
            "en": "Address",
            "uz": "Manzil",
            "ru": "Адрес"
        },
        "Address Title": {
            "en": "Address Title",
            "uz": "Manzil nomi",
            "ru": "Название адреса"
        },
        "Address deleted successfully!": {
            "en": "Address deleted successfully!",
            "uz": "Manzil muvaffaqiyatli o'chirildi!",
            "ru": "Адрес успешно удалён!"
        },
        "Address saved successfully!": {
            "en": "Address saved successfully!",
            "uz": "Manzil muvaffaqiyatli saqlandi!",
            "ru": "Адрес успешно сохранён!"
        },
        "Addresses": {
            "en": "Addresses",
            "uz": "Manzillar",
            "ru": "Адреса"
        },
        "Admin": {
            "en": "Admin",
            "uz": "Admin",
            "ru": "Администратор"
        },
        "Advanced multi-stage filtration system for optimal water quality.": {
            "en": "Advanced multi-stage filtration system for optimal water quality.",
            "uz": "Suv sifatini yaxshilash uchun ilg'or ko'p bosqichli filtrlash tizimi.",
            "ru": "Современная многоступенчатая система фильтрации для оптимального качества воды."
        },
        "Advanced testing equipment and certified technicians ensure the highest water quality standards": {
            "en": "Advanced testing equipment and certified technicians ensure the highest water quality standards",
            "uz": "Ilg'or sinov uskunalari va sertifikatlangan mutaxassislar eng yuqori suv sifati standartlarini ta'minlaydi",
            "ru": "Современное испытательное оборудование и сертифицированные специалисты обеспечивают самые высокие стандарты качества воды"
        },
        "Advanced water quality testing": {
            "en": "Advanced water quality testing",
            "uz": "Ilg'or suv sifatini tekshirish",
            "ru": "Современное тестирование качества воды"
        },




        "After registration:": {
            "en": "After registration:",
            "uz": "Ro'yxatdan o'tgandan so'ng:",
            "ru": "После регистрации:"
        },
        "All": {
            "en": "All",
            "uz": "Hammasi",
            "ru": "Все"
        },
        "All Activity": {
            "en": "All Activity",
            "uz": "Barcha faoliyat",
            "ru": "Вся активность"
        },
        "All Orders": {
            "en": "All Orders",
            "uz": "Barcha buyurtmalar",
            "ru": "Все заказы"
        },
        "All Products": {
            "en": "All Products",
            "uz": "Barcha mahsulotlar",
            "ru": "Все продукты"
        },
        "All Rights Reserved": {
            "en": "All Rights Reserved",
            "uz": "Barcha huquqlar himoyalangan",
            "ru": "Все права защищены"
        },
        "All Time": {
            "en": "All Time",
            "uz": "Har doim",
            "ru": "Все время"
        },
        "Already have an account?": {
            "en": "Already have an account?",
            "uz": "Hisobingiz bormi?",
            "ru": "Уже есть аккаунт?"
        },
        "Always Want Safe and Good Water for Healthy Life": {
            "en": "Always Want Safe and Good Water for Healthy Life",
            "uz": "Sog'lom hayot uchun doim xavfsiz va sifatli suv istaysiz",
            "ru": "Всегда хотите безопасную и качественную воду для здоровой жизни"
        },
        "Amount": {
            "en": "Amount",
            "uz": "Miqdor",
            "ru": "Сумма"
        },
        "An error occurred. Please try again later.": {
            "en": "An error occurred. Please try again later.",
            "uz": "Xatolik yuz berdi. Iltimos, keyinroq qayta urinib ko'ring.",
            "ru": "Произошла ошибка. Пожалуйста, попробуйте позже."
        },
        "Apartment/Floor": {
            "en": "Apartment/Floor",
            "uz": "Kvartira/Qavat",
            "ru": "Квартира/Этаж"
        },
        "Apply Coupon": {
            "en": "Apply Coupon",
            "uz": "Kuponni qo'llash",
            "ru": "Применить купон"
        },
        "Apt/Floor number": {
            "en": "Apt/Floor number",
            "uz": "Kvartira/Qavat raqami",
            "ru": "Номер квартиры/этажа"
        },
        "Are there any setup fees or contracts?": {
            "en": "Are there any setup fees or contracts?",
            "uz": "O'rnatish to'lovlari yoki shartnomalar bormi?",
            "ru": "Есть ли плата за установку или контракты?"
        },
        "Are you sure you want to cancel this order?": {
            "en": "Are you sure you want to cancel this order?",
            "uz": "Ushbu buyurtmani bekor qilmoqchimisiz?",
            "ru": "Вы уверены, что хотите отменить этот заказ?"
        },
        "Are you sure you want to cancel this subscription? This action cannot be undone.": {
            "en": "Are you sure you want to cancel this subscription? This action cannot be undone.",
            "uz": "Ushbu obunani bekor qilmoqchimisiz? Ushbu amalni qaytarib bo'lmaydi.",
            "ru": "Вы уверены, что хотите отменить эту подписку? Это действие необратимо."
        },
        "Are you sure you want to clear your cart?": {
            "en": "Are you sure you want to clear your cart?",
            "uz": "Savatingizni bo'shatmoqchimisiz?",
            "ru": "Вы уверены, что хотите очистить корзину?"
        },
        "Are you sure you want to delete this address?": {
            "en": "Are you sure you want to delete this address?",
            "uz": "Ushbu manzilni o'chirib tashlamoqchimisiz?",
            "ru": "Вы уверены, что хотите удалить этот адрес?"
        },
        "Are you sure you want to pause this subscription?": {
            "en": "Are you sure you want to pause this subscription?",
            "uz": "Ushbu obunani to'xtatib turmoqchimisiz?",
            "ru": "Вы уверены, что хотите приостановить эту подписку?"
        },
        "Automatic billing and payment": {
            "en": "Automatic billing and payment",
            "uz": "Avtomatik hisob-kitob va to'lov",
            "ru": "Автоматическое выставление счетов и оплата"
        },
        "Available Rewards": {
            "en": "Available Rewards",
            "uz": "Mavjud mukofotlar",
            "ru": "Доступные награды"
        },
        "Back to Login": {
            "en": "Back to Login",
            "uz": "Kirishga qaytish",
            "ru": "Назад ко входу"
        },
        "Back to My Account": {
            "en": "Back to My Account",
            "uz": "Mening hisobimga qaytish",
            "ru": "Назад к моему аккаунту"
        },
        "Basic Plan": {
            "en": "Basic Plan",
            "uz": "Asosiy reja",
            "ru": "Базовый план"
        },
        "Basic Plan - $29/month": {
            "en": "Basic Plan - $29/month",
            "uz": "Asosiy reja - oyiga $29",
            "ru": "Базовый план - $29/месяц"
        },
        "Basic customer support": {
            "en": "Basic customer support",
            "uz": "Asosiy mijozlarni qo'llab-quvvatlash",
            "ru": "Базовая поддержка клиентов"
        },
        "Basic water quality testing": {
            "en": "Basic water quality testing",
            "uz": "Asosiy suv sifatini tekshirish",
            "ru": "Базовое тестирование качества воды"
        },
        "Bi-weekly": {
            "en": "Bi-weekly",
            "uz": "Haftada ikki marta",
            "ru": "Дважды в неделю"
        },
        "Bi-weekly delivery schedule": {
            "en": "Bi-weekly delivery schedule",
            "uz": "Haftada ikki marta yetkazib berish jadvali",
            "ru": "График доставки дважды в неделю"
        },
        "Birthday Bonus": {
            "en": "Birthday Bonus",
            "uz": "Tug'ilgan kun bonusi",
            "ru": "Бонус на день рождения"
        },
        "Blue Stream": {
            "en": "Blue Stream",
            "uz": "Blue Stream",
            "ru": "Blue Stream"
        },
        "Blue Stream Expands Delivery Network Across Uzbekistan": {
            "en": "Blue Stream Expands Delivery Network Across Uzbekistan",
            "uz": "Blue Stream O'zbekiston bo'ylab yetkazib berish tarmog'ini kengaytirmoqda",
            "ru": "Blue Stream расширяет сеть доставки по всему Узбекистану"
        },
        "Blue Stream Group has been serving communities across Uzbekistan with premium water delivery services for over a decade.": {
            "en": "Blue Stream Group has been serving communities across Uzbekistan with premium water delivery services for over a decade.",
            "uz": "Blue Stream guruhi O'zbekiston bo'ylab jamoalarga 10 yildan ortiq vaqt davomida yuqori sifatli suv yetkazib berish xizmatini taqdim etib kelmoqda.",
            "ru": "Группа Blue Stream более десяти лет обслуживает сообщества по всему Узбекистану, предоставляя премиальные услуги по доставке воды."
        },
        "Blue Stream Group has been serving communities across Uzbekistan with premium water delivery services for over a decade. Our commitment to quality, reliability, and customer satisfaction has made us the trusted choice for thousands of families and businesses.": {
            "en": "Blue Stream Group has been serving communities across Uzbekistan with premium water delivery services for over a decade. Our commitment to quality, reliability, and customer satisfaction has made us the trusted choice for thousands of families and businesses.",
            "uz": "Blue Stream guruhi O'zbekiston bo'ylab 10 yildan ortiq vaqt davomida yuqori sifatli suv yetkazib berish xizmatini taqdim etmoqda. Sifat, ishonchlilik va mijozlar qoniqishiga sodiqligimiz minglab oilalar va bizneslar uchun ishonchli tanlov bo'ldi.",
            "ru": "Группа Blue Stream более десяти лет обслуживает сообщества по всему Узбекистану, предоставляя премиальные услуги по доставке воды. Наша приверженность качеству, надежности и удовлетворенности клиентов сделала нас надежным выбором для тысяч семей и предприятий."
        },
        "Blue Stream Group is dedicated to providing the highest quality water delivery services to homes and businesses across Uzbekistan. Our commitment to excellence ensures that every drop meets the strictest safety and purity standards.": {
            "en": "Blue Stream Group is dedicated to providing the highest quality water delivery services to homes and businesses across Uzbekistan. Our commitment to excellence ensures that every drop meets the strictest safety and purity standards.",
            "uz": "Blue Stream guruhi O'zbekiston bo'ylab uy va bizneslarga eng yuqori sifatli suv yetkazib berish xizmatini taqdim etishga sodiqdir. Mukammallikka sodiqligimiz har bir tomchi eng qat'iy xavfsizlik va tozalik standartlariga javob berishini ta'minlaydi.",
            "ru": "Группа Blue Stream посвящена предоставлению услуг по доставке воды наивысшего качества в дома и предприятия по всему Узбекистану. Наша приверженность совершенству гарантирует, что каждая капля соответствует самым строгим стандартам безопасности и чистоты."
        },
        "Blue Stream Loyalty Program": {
            "en": "Blue Stream Loyalty Program",
            "uz": "Blue Stream sodiqlik dasturi",
            "ru": "Программа лояльности Blue Stream"
        },
        "Blue Stream has achieved new international quality certifications, ensuring the highest standards in water purification and delivery.": {
            "en": "Blue Stream has achieved new international quality certifications, ensuring the highest standards in water purification and delivery.",
            "uz": "Blue Stream yangi xalqaro sifat sertifikatlarini qo'lga kiritdi va suvni tozalash va yetkazib berishda eng yuqori standartlarga rioya qilmoqda.",
            "ru": "Blue Stream получила новые международные сертификаты качества, обеспечивая самые высокие стандарты очистки и доставки воды."
        },
        "Bottled Water Industry": {
            "en": "Bottled Water Industry",
            "uz": "Idishlangan suv sanoati",
            "ru": "Индустрия бутилированной воды"
        },
        "Bronze": {
            "en": "Bronze",
            "uz": "Bronza",
            "ru": "Бронза"
        },
        "Browse Plans": {
            "en": "Browse Plans",
            "uz": "Rejalarni ko'rish",
            "ru": "Просмотреть планы"
        },
        "Business": {
            "en": "Business",
            "uz": "Biznes",
            "ru": "Бизнес"
        },
        "Business Owner": {
            "en": "Business Owner",
            "uz": "Biznes egasi",
            "ru": "Владелец бизнеса"
        },
        "Business Plan": {
            "en": "Business Plan",
            "uz": "Biznes rejasi",
            "ru": "Бизнес-план"
        },
        "Business Plan - $99/month": {
            "en": "Business Plan - $99/month",
            "uz": "Biznes rejasi - oyiga $99",
            "ru": "Бизнес-план - $99/месяц"
        },
        "Call Us": {
            "en": "Call Us",
            "uz": "Bizga qo'ng'iroq qiling",
            "ru": "Позвоните нам"
        },
        "Call Us:": {
            "en": "Call Us:",
            "uz": "Qo'ng'iroq qiling:",
            "ru": "Позвоните нам:"
        },
        "Can I change my subscription plan?": {
            "en": "Can I change my subscription plan?",
            "uz": "Obuna rejamni o'zgartira olamanmi?",
            "ru": "Могу ли я изменить свой тарифный план?"
        },
        "Cancel": {
            "en": "Cancel",
            "uz": "Bekor qilish",
            "ru": "Отмена"
        },
        "Cancel Order": {
            "en": "Cancel Order",
            "uz": "Buyurtmani bekor qilish",
            "ru": "Отменить заказ"
        },



        "Cancel Subscription": {
            "en": "Cancel Subscription",
            "uz": "Obunani bekor qilish",
            "ru": "Отменить подписку"
        },
        "Cancelled": {
            "en": "Cancelled",
            "uz": "Bekor qilingan",
            "ru": "Отменено"
        },
        "Cart": {
            "en": "Cart",
            "uz": "Savat",
            "ru": "Корзина"
        },
        "Cart Total": {
            "en": "Cart Total",
            "uz": "Savat jami",
            "ru": "Итого корзины"
        },
        "Cart cleared": {
            "en": "Cart cleared",
            "uz": "Savat bo'shatildi",
            "ru": "Корзина очищена"
        },
        "Cart updated": {
            "en": "Cart updated",
            "uz": "Savat yangilandi",
            "ru": "Корзина обновлена"
        },
        "Categories": {
            "en": "Categories",
            "uz": "Kategoriyalar",
            "ru": "Категории"
        },
        "Change Password": {
            "en": "Change Password",
            "uz": "Parolni o'zgartirish",
            "ru": "Изменить пароль"
        },
        "Change Phone Number": {
            "en": "Change Phone Number",
            "uz": "Telefon raqamini o'zgartirish",
            "ru": "Изменить номер телефона"
        },
        "Changes Effective From": {
            "en": "Changes Effective From",
            "uz": "O'zgarishlar kuchga kirish sanasi",
            "ru": "Изменения вступают в силу с"
        },
        "Chlorine Free": {
            "en": "Chlorine Free",
            "uz": "Xlorsiz",
            "ru": "Без хлора"
        },
        "Choose Basic Plan": {
            "en": "Choose Basic Plan",
            "uz": "Asosiy tarifni tanlash",
            "ru": "Выбрать базовый план"
        },
        "Choose Business Plan": {
            "en": "Choose Business Plan",
            "uz": "Biznes tarifni tanlash",
            "ru": "Выбрать бизнес-план"
        },
        "Choose Frequency": {
            "en": "Choose Frequency",
            "uz": "Yetkazib berish chastotasini tanlash",
            "ru": "Выберите частоту"
        },
        "Choose Plan": {
            "en": "Choose Plan",
            "uz": "Tarifni tanlash",
            "ru": "Выберите план"
        },
        "Choose Premium Plan": {
            "en": "Choose Premium Plan",
            "uz": "Premium tarifni tanlash",
            "ru": "Выбрать премиум-план"
        },
        "Choose Your Best Pricing Plan": {
            "en": "Choose Your Best Pricing Plan",
            "uz": "Eng yaxshi tarifingizni tanlang",
            "ru": "Выберите лучший тарифный план"
        },
        "Choose Your Perfect": {
            "en": "Choose Your Perfect",
            "uz": "Mukammalini tanlang",
            "ru": "Выберите идеальный"
        },
        "Choose Your Plan": {
            "en": "Choose Your Plan",
            "uz": "Tarifingizni tanlang",
            "ru": "Выберите свой план"
        },
        "City": {
            "en": "City",
            "uz": "Shahar",
            "ru": "Город"
        },
        "Clean, pure water delivered right to our office. The team is professional and the quality is consistent. Very happy with their reliable service.": {
            "en": "Clean, pure water delivered right to our office. The team is professional and the quality is consistent. Very happy with their reliable service.",
            "uz": "Toza va sof suv bevosita ofisimizga yetkaziladi. Jamoa professionallar va sifat doimiy. Ularning ishonchli xizmatidan juda mamnunmiz.",
            "ru": "Чистая и свежая вода доставляется прямо в наш офис. Команда профессиональная, качество стабильное. Очень довольны их надежным сервисом."
        },
        "Clear Cart": {
            "en": "Clear Cart",
            "uz": "Savatni bo'shatish",
            "ru": "Очистить корзину"
        },
        "Climate-controlled storage": {
            "en": "Climate-controlled storage",
            "uz": "Iqlim nazoratli saqlash",
            "ru": "Хранение с контролем климата"
        },
        "Close": {
            "en": "Close",
            "uz": "Yopish",
            "ru": "Закрыть"
        },
        "Code copied to clipboard": {
            "en": "Code copied to clipboard",
            "uz": "Kod buferga nusxa qilindi",
            "ru": "Код скопирован в буфер"
        },
        "Code expires in": {
            "en": "Code expires in",
            "uz": "Kod muddati tugash vaqti",
            "ru": "Код истекает через"
        },
        "Company News": {
            "en": "Company News",
            "uz": "Kompaniya yangiliklari",
            "ru": "Новости компании"
        },
        "Complete Product Range": {
            "en": "Complete Product Range",
            "uz": "To'liq mahsulot assortimenti",
            "ru": "Полный ассортимент продукции"
        },
        "Complete water solutions for offices including dispensers and regular delivery schedules.": {
            "en": "Complete water solutions for offices including dispensers and regular delivery schedules.",
            "uz": "Ofislar uchun to'liq suv yechimlari, dispenserlar va muntazam yetkazib berish jadvali bilan.",
            "ru": "Полные решения по воде для офисов, включая диспенсеры и регулярные графики доставки."
        },
        "Comprehensive water analysis": {
            "en": "Comprehensive water analysis",
            "uz": "Keng qamrovli suv tahlili",
            "ru": "Комплексный анализ воды"
        },
        "Confirm New Password": {
            "en": "Confirm New Password",
            "uz": "Yangi parolni tasdiqlash",
            "ru": "Подтвердите новый пароль"
        },
        "Confirm Password": {
            "en": "Confirm Password",
            "uz": "Parolni tasdiqlash",
            "ru": "Подтвердите пароль"
        },
        "Confirm new password": {
            "en": "Confirm new password",
            "uz": "Yangi parolni tasdiqlang",
            "ru": "Подтвердите новый пароль"
        },
        "Confirmed": {
            "en": "Confirmed",
            "uz": "Tasdiqlandi",
            "ru": "Подтверждено"
        },
        "Connect": {
            "en": "Connect",
            "uz": "Ulanish",
            "ru": "Подключить"
        },
        "Connected": {
            "en": "Connected",
            "uz": "Ulandi",
            "ru": "Подключено"
        },
        "Connected platforms": {
            "en": "Connected platforms",
            "uz": "Ulangan platformalar",
            "ru": "Подключенные платформы"
        },
        "Contact": {
            "en": "Contact",
            "uz": "Aloqa",
            "ru": "Контакт"
        },
        "Contact Info": {
            "en": "Contact Info",
            "uz": "Aloqa ma'lumotlari",
            "ru": "Контактная информация"
        },
        "Contact Information": {
            "en": "Contact Information",
            "uz": "Aloqa ma'lumotlari",
            "ru": "Контактная информация"
        },
        "Contact Us": {
            "en": "Contact Us",
            "uz": "Biz bilan bog'laning",
            "ru": "Свяжитесь с нами"
        },
        "Contact information updated successfully!": {
            "en": "Contact information updated successfully!",
            "uz": "Aloqa ma'lumotlari muvaffaqiyatli yangilandi!",
            "ru": "Контактная информация успешно обновлена!"
        },
        "Continue": {
            "en": "Continue",
            "uz": "Davom etish",
            "ru": "Продолжить"
        },
        "Continue Shopping": {
            "en": "Continue Shopping",
            "uz": "Xaridni davom ettirish",
            "ru": "Продолжить покупки"
        },
        "Convenient regular deliveries to your door": {
            "en": "Convenient regular deliveries to your door",
            "uz": "Sizning eshigingizgacha qulay muntazam yetkazib berish",
            "ru": "Удобные регулярные доставки к вашей двери"
        },
        "Convenient subscription plans with automatic delivery and special discounts.": {
            "en": "Convenient subscription plans with automatic delivery and special discounts.",
            "uz": "Avtomatik yetkazib berish va maxsus chegirmalar bilan qulay obuna rejalari.",
            "ru": "Удобные планы подписки с автоматической доставкой и специальными скидками."
        },
        "Cooler Installation": {
            "en": "Cooler Installation",
            "uz": "Suv sovutgichni o'rnatish",
            "ru": "Установка кулера"
        },
        "Copy Code": {
            "en": "Copy Code",
            "uz": "Kod nusxalash",
            "ru": "Скопировать код"
        },
        "Cost-effective compared to one-time purchases": {
            "en": "Cost-effective compared to one-time purchases",
            "uz": "Bir martalik xaridlarga nisbatan tejamkor",
            "ru": "Экономичнее по сравнению с разовыми покупками"
        },
        "Coupon applied successfully!": {
            "en": "Coupon applied successfully!",
            "uz": "Kupon muvaffaqiyatli qo'llandi!",
            "ru": "Купон успешно применён!"
        },



        "Create Account": {
            "en": "Create Account",
            "ru": "Создать аккаунт",
            "uz": "Hisob yaratish"
        },
        "Create New Password": {
            "en": "Create New Password",
            "ru": "Создать новый пароль",
            "uz": "Yangi parol yaratish"
        },
        "Current Password": {
            "en": "Current Password",
            "ru": "Текущий пароль",
            "uz": "Joriy parol"
        },
        "Current Points": {
            "en": "Current Points",
            "ru": "Текущие баллы",
            "uz": "Joriy ballar"
        },
        "Current Tier": {
            "en": "Current Tier",
            "ru": "Текущий уровень",
            "uz": "Joriy daraja"
        },
        "Custom delivery schedule": {
            "en": "Custom delivery schedule",
            "ru": "Индивидуальный график доставки",
            "uz": "Moslashtirilgan yetkazib berish jadvali"
        },
        "Customer Service": {
            "en": "Customer Service",
            "ru": "Служба поддержки",
            "uz": "Mijozlarga xizmat"
        },
        "Customer service center": {
            "en": "Customer service center",
            "ru": "Центр обслуживания клиентов",
            "uz": "Mijozlarga xizmat ko'rsatish markazi"
        },
        "Daily Water Intake Guide": {
            "en": "Daily Water Intake Guide",
            "ru": "Руководство по ежедневному потреблению воды",
            "uz": "Kunlik suv iste'moli bo'yicha qo'llanma"
        },
        "Dashboard": {
            "en": "Dashboard",
            "ru": "Панель управления",
            "uz": "Boshqaruv paneli"
        },
        "Date": {
            "en": "Date",
            "ru": "Дата",
            "uz": "Sana"
        },
        "Date of Birth": {
            "en": "Date of Birth",
            "ru": "Дата рождения",
            "uz": "Tug'ilgan sana"
        },
        "Dec 20, 2023": {
            "en": "Dec 20, 2023",
            "ru": "20 декабря 2023",
            "uz": "2023-yil 20-dekabr"
        },
        "Dedicated account manager": {
            "en": "Dedicated account manager",
            "ru": "Персональный менеджер аккаунта",
            "uz": "Shaxsiy hisob menejeri"
        },
        "Dedicated water specialists": {
            "en": "Dedicated water specialists",
            "ru": "Специалисты по воде",
            "uz": "Suv bo'yicha mutaxassislar"
        },
        "Default": {
            "en": "Default",
            "ru": "По умолчанию",
            "uz": "Standart"
        },
        "Default address updated!": {
            "en": "Default address updated!",
            "ru": "Адрес по умолчанию обновлен!",
            "uz": "Standart manzil yangilandi!"
        },
        "Delete": {
            "en": "Delete",
            "ru": "Удалить",
            "uz": "O'chirish"
        },
        "Delivered": {
            "en": "Delivered",
            "ru": "Доставлено",
            "uz": "Yetkazildi"
        },
        "Deliveries This Month": {
            "en": "Deliveries This Month",
            "ru": "Доставки в этом месяце",
            "uz": "Ushbu oydagi yetkazib berishlar"
        },
        "Delivery": {
            "en": "Delivery",
            "ru": "Доставка",
            "uz": "Yetkazib berish"
        },
        "Delivery Address": {
            "en": "Delivery Address",
            "ru": "Адрес доставки",
            "uz": "Yetkazib berish manzili"
        },
        "Delivery Fee": {
            "en": "Delivery Fee",
            "ru": "Плата за доставку",
            "uz": "Yetkazib berish narxi"
        },
        "Delivery Frequency": {
            "en": "Delivery Frequency",
            "ru": "Частота доставки",
            "uz": "Yetkazib berish tezligi"
        },
        "Delivery Information": {
            "en": "Delivery Information",
            "ru": "Информация о доставке",
            "uz": "Yetkazib berish ma'lumotlari"
        },
        "Delivery Instructions": {
            "en": "Delivery Instructions",
            "ru": "Инструкции по доставке",
            "uz": "Yetkazib berish bo'yicha ko'rsatmalar"
        },
        "Delivery Window": {
            "en": "Delivery Window",
            "ru": "Окно доставки",
            "uz": "Yetkazib berish vaqti"
        },
        "Delivery frequency": {
            "en": "Delivery frequency",
            "ru": "Частота доставки",
            "uz": "Yetkazib berish tezligi"
        },
        "Delivery rescheduling feature coming soon!": {
            "en": "Delivery rescheduling feature coming soon!",
            "ru": "Функция переноса доставки скоро будет доступна!",
            "uz": "Yetkazib berishni qayta rejalash xizmati tez orada mavjud bo'ladi!"
        },
        "Delivery tracking and notifications": {
            "en": "Delivery tracking and notifications",
            "ru": "Отслеживание и уведомления о доставке",
            "uz": "Yetkazib berishni kuzatish va bildirishnomalar"
        },
        "Delivery: 24/7": {
            "en": "Delivery: 24/7",
            "ru": "Доставка: 24/7",
            "uz": "Yetkazib berish: 24/7"
        },
        "Discount": {
            "en": "Discount",
            "ru": "Скидка",
            "uz": "Chegirma"
        },
        "Discover": {
            "en": "Discover",
            "ru": "Откройте",
            "uz": "Kashf eting"
        },
        "Discover how proper hydration supports your immune system, skin health, and overall well-being.": {
            "en": "Discover how proper hydration supports your immune system, skin health, and overall well-being.",
            "ru": "Узнайте, как правильное увлажнение поддерживает иммунитет, здоровье кожи и общее самочувствие.",
            "uz": "To'g'ri suv iste'moli immunitet, teri salomatligi va umumiy farovonlikni qanday qo'llab-quvvatlashini bilib oling."
        },
        "Discover the importance of proper hydration and learn about the health benefits of quality water.": {
            "en": "Discover the importance of proper hydration and learn about the health benefits of quality water.",
            "ru": "Узнайте важность правильного увлажнения и пользу качественной воды для здоровья.",
            "uz": "To'g'ri suv ichishning ahamiyatini va sifatli suvning sog'liqqa foydasini o'rganing."
        },
        "District": {
            "en": "District",
            "ru": "Район",
            "uz": "Tuman"
        },
        "District/Area": {
            "en": "District/Area",
            "ru": "Район/Область",
            "uz": "Tuman/Hudud"
        },
        "Early access to new products": {
            "en": "Early access to new products",
            "ru": "Ранний доступ к новым продуктам",
            "uz": "Yangi mahsulotlarga erta kirish"
        },
        "Earn 1-2 points for every dollar spent (based on your tier)": {
            "en": "Earn 1-2 points for every dollar spent (based on your tier)",
            "ru": "Зарабатывайте 1-2 балла за каждый потраченный доллар (в зависимости от уровня)",
            "uz": "Har sarflangan 1 dollarga 1-2 ball oling (darajangizga qarab)"
        },
        "Earn 50 points for each product review you write": {
            "en": "Earn 50 points for each product review you write",
            "ru": "Получайте 50 баллов за каждый отзыв о продукте",
            "uz": "Har bir mahsulot sharhi uchun 50 ball oling"
        },
        "Earn points with every purchase and redeem them for amazing rewards!": {
            "en": "Earn points with every purchase and redeem them for amazing rewards!",
            "ru": "Зарабатывайте баллы с каждой покупкой и обменивайте их на потрясающие награды!",
            "uz": "Har bir xariddan ball yig'ing va ularni ajoyib sovrinlarga almashtiring!"
        },
        "Earn points with every purchase and unlock exclusive rewards": {
            "en": "Earn points with every purchase and unlock exclusive rewards",
            "ru": "Получайте баллы с каждой покупкой и открывайте эксклюзивные награды",
            "uz": "Har bir xaridda ball yig'ing va eksklyuziv mukofotlarni oching"
        },
        "Easy online account management": {
            "en": "Easy online account management",
            "ru": "Простое управление аккаунтом онлайн",
            "uz": "Oson onlayn hisob boshqaruvi"
        },
        "Edit": {
            "en": "Edit",
            "ru": "Редактировать",
            "uz": "Tahrirlash"
        },
        "Edit Address": {
            "en": "Edit Address",
            "ru": "Редактировать адрес",
            "uz": "Manzilni tahrirlash"
        },
        "Edit Profile": {
            "en": "Edit Profile",
            "ru": "Редактировать профиль",
            "uz": "Profilni tahrirlash"
        },
        "Email": {
            "en": "Email",
            "ru": "Электронная почта",
            "uz": "Elektron pochta"
        },
        "Email Address": {
            "en": "Email Address",
            "ru": "Адрес электронной почты",
            "uz": "Elektron pochta manzili"
        },
        "Email Address or Phone Number": {
            "en": "Email Address or Phone Number",
            "ru": "Электронная почта или номер телефона",
            "uz": "Elektron pochta yoki telefon raqami"
        },
        "Email Pending": {
            "en": "Email Pending",
            "ru": "Ожидание подтверждения email",
            "uz": "Email tasdiqlanishi kutilmoqda"
        },




        "Email Verification": {
            "en": "Email Verification",
            "ru": "Подтверждение электронной почты",
            "uz": "Elektron pochtani tasdiqlash"
        },
        "Email Verified": {
            "en": "Email Verified",
            "ru": "Электронная почта подтверждена",
            "uz": "Elektron pochta tasdiqlandi"
        },
        "Email address cannot be changed. Contact support if needed.": {
            "en": "Email address cannot be changed. Contact support if needed.",
            "ru": "Адрес электронной почты нельзя изменить. Свяжитесь с поддержкой, если необходимо.",
            "uz": "Elektron pochta manzilini o'zgartirib bo'lmaydi. Kerak bo'lsa, yordam xizmati bilan bog'laning."
        },
        "Email address verified successfully! Your account is now fully activated.": {
            "en": "Email address verified successfully! Your account is now fully activated.",
            "ru": "Адрес электронной почты успешно подтверждён! Ваш аккаунт теперь полностью активирован.",
            "uz": "Elektron pochta manzili muvaffaqiyatli tasdiqlandi! Hisobingiz to'liq faollashtirildi."
        },
        "Email customer support": {
            "en": "Email customer support",
            "ru": "Написать в службу поддержки",
            "uz": "Mijozlarni qo'llab-quvvatlash xizmatiga yozish"
        },
        "Email or Phone Number": {
            "en": "Email or Phone Number",
            "ru": "Электронная почта или номер телефона",
            "uz": "Elektron pochta yoki telefon raqami"
        },
        "Email sent": {
            "en": "Email sent",
            "ru": "Письмо отправлено",
            "uz": "Xat yuborildi"
        },
        "Email verified successfully!": {
            "en": "Email verified successfully!",
            "ru": "Электронная почта успешно подтверждена!",
            "uz": "Elektron pochta muvaffaqiyatli tasdiqlandi!"
        },
        "Emergency water supply": {
            "en": "Emergency water supply",
            "ru": "Аварийное водоснабжение",
            "uz": "Favqulodda suv ta'minoti"
        },
        "Enable 2FA": {
            "en": "Enable 2FA",
            "ru": "Включить двухфакторную аутентификацию",
            "uz": "Ikki bosqichli autentifikatsiyani yoqish"
        },
        "Enjoy unified access across web and Telegram!": {
            "en": "Enjoy unified access across web and Telegram!",
            "ru": "Наслаждайтесь единым доступом через веб и Telegram!",
            "uz": "Veb va Telegram orqali yagona kirishdan foydalaning!"
        },
        "Enter 6-digit verification code": {
            "en": "Enter 6-digit verification code",
            "ru": "Введите 6-значный код подтверждения",
            "uz": "6 xonali tasdiqlash kodini kiriting"
        },
        "Enter complete address": {
            "en": "Enter complete address",
            "ru": "Введите полный адрес",
            "uz": "To'liq manzilni kiriting"
        },
        "Enter coupon code": {
            "en": "Enter coupon code",
            "ru": "Введите код купона",
            "uz": "Kupon kodini kiriting"
        },
        "Enter current password": {
            "en": "Enter current password",
            "ru": "Введите текущий пароль",
            "uz": "Joriy parolni kiriting"
        },
        "Enter first name": {
            "en": "Enter first name",
            "ru": "Введите имя",
            "uz": "Ismni kiriting"
        },
        "Enter last name": {
            "en": "Enter last name",
            "ru": "Введите фамилию",
            "uz": "Familiyani kiriting"
        },
        "Enter new password": {
            "en": "Enter new password",
            "ru": "Введите новый пароль",
            "uz": "Yangi parolni kiriting"
        },
        "Enter the email address or phone number associated with your account": {
            "en": "Enter the email address or phone number associated with your account",
            "ru": "Введите адрес электронной почты или номер телефона, связанный с вашей учетной записью",
            "uz": "Hisobingizga bog'langan elektron pochta manzili yoki telefon raqamini kiriting"
        },
        "Enter verification code from email": {
            "en": "Enter verification code from email",
            "ru": "Введите код подтверждения из письма",
            "uz": "Elektron pochtadan tasdiqlash kodini kiriting"
        },
        "Enter your email address or phone number and we will send you instructions to reset your password": {
            "en": "Enter your email address or phone number and we will send you instructions to reset your password",
            "ru": "Введите адрес электронной почты или номер телефона, и мы вышлем вам инструкции для сброса пароля",
            "uz": "Elektron pochta yoki telefon raqamingizni kiriting, biz sizga parolni tiklash bo'yicha ko'rsatmalar yuboramiz"
        },
        "Enter your phone number with country code": {
            "en": "Enter your phone number with country code",
            "ru": "Введите номер телефона с кодом страны",
            "uz": "Mamlakat kodi bilan telefon raqamingizni kiriting"
        },
        "Error applying coupon": {
            "en": "Error applying coupon",
            "ru": "Ошибка применения купона",
            "uz": "Kuponni qo'llashda xato"
        },
        "Essential minerals preserved for your health and well-being.": {
            "en": "Essential minerals preserved for your health and well-being.",
            "ru": "Сохранены необходимые минералы для вашего здоровья и благополучия.",
            "uz": "Sizning sog'lig'ingiz va farovonligingiz uchun zarur minerallar saqlangan."
        },
        "Est. delivery": {
            "en": "Est. delivery",
            "ru": "Ориентировочная доставка",
            "uz": "Taxminiy yetkazib berish"
        },
        "Excellent service! The water quality is outstanding and delivery is always on time. Blue Stream has become an essential part of our daily routine for healthy living.": {
            "en": "Excellent service! The water quality is outstanding and delivery is always on time. Blue Stream has become an essential part of our daily routine for healthy living.",
            "ru": "Отличный сервис! Качество воды превосходное, доставка всегда вовремя. Blue Stream стал важной частью нашего ежедневного здорового образа жизни.",
            "uz": "Ajoyib xizmat! Suv sifati zo'r va yetkazib berish har doim o'z vaqtida. Blue Stream sog'lom hayotimizning ajralmas qismiga aylandi."
        },
        "Exclusive monthly offers": {
            "en": "Exclusive monthly offers",
            "ru": "Эксклюзивные ежемесячные предложения",
            "uz": "Eksklyuziv oylik takliflar"
        },
        "Exclusive platinum rewards": {
            "en": "Exclusive platinum rewards",
            "ru": "Эксклюзивные платиновые награды",
            "uz": "Eksklyuziv platina mukofotlari"
        },
        "Experience the convenience of premium water delivery service with our reliable, eco-friendly solutions for your home and office.": {
            "en": "Experience the convenience of premium water delivery service with our reliable, eco-friendly solutions for your home and office.",
            "ru": "Ощутите удобство премиального сервиса доставки воды с нашими надежными и экологичными решениями для дома и офиса.",
            "uz": "Ishonchli, ekologik toza yechimlarimiz bilan uy va ofisingiz uchun premium suv yetkazib berish xizmatining qulayligini his eting."
        },
        "Expires": {
            "en": "Expires",
            "ru": "Истекает",
            "uz": "Muddati tugaydi"
        },
        "Explore Our World": {
            "en": "Explore Our World",
            "ru": "Исследуйте наш мир",
            "uz": "Bizning dunyomizni o'rganing"
        },
        "Export Data": {
            "en": "Export Data",
            "ru": "Экспорт данных",
            "uz": "Ma'lumotlarni eksport qilish"
        },
        "Facilities": {
            "en": "Facilities",
            "ru": "Удобства",
            "uz": "Imkoniyatlar"
        },
        "Failed to cancel order": {
            "en": "Failed to cancel order",
            "ru": "Не удалось отменить заказ",
            "uz": "Buyurtmani bekor qilib bo'lmadi"
        },
        "Failed to cancel subscription": {
            "en": "Failed to cancel subscription",
            "ru": "Не удалось отменить подписку",
            "uz": "Obunani bekor qilib bo'lmadi"
        },
        "Failed to change password": {
            "en": "Failed to change password",
            "ru": "Не удалось изменить пароль",
            "uz": "Parolni o'zgartirib bo'lmadi"
        },
        "Failed to copy referral code": {
            "en": "Failed to copy referral code",
            "ru": "Не удалось скопировать реферальный код",
            "uz": "Referal kodni nusxalab bo'lmadi"
        },
        "Failed to create subscription. Please try again.": {
            "en": "Failed to create subscription. Please try again.",
            "ru": "Не удалось создать подписку. Пожалуйста, попробуйте снова.",
            "uz": "Obunani yaratib bo'lmadi. Iltimos, qaytadan urinib ko'ring."
        },
        "Failed to delete address": {
            "en": "Failed to delete address",
            "ru": "Не удалось удалить адрес",
            "uz": "Manzilni o'chirib bo'lmadi"
        },
        "Failed to download account data. Please try again.": {
            "en": "Failed to download account data. Please try again.",
            "ru": "Не удалось загрузить данные учетной записи. Пожалуйста, попробуйте снова.",
            "uz": "Hisob ma'lumotlarini yuklab bo'lmadi. Iltimos, qaytadan urinib ko'ring."
        },
        "Failed to generate referral code": {
            "en": "Failed to generate referral code",
            "ru": "Не удалось сгенерировать реферальный код",
            "uz": "Referal kodni yaratib bo'lmadi"
        },
        "Failed to load deliveries": {
            "en": "Failed to load deliveries",
            "ru": "Не удалось загрузить доставки",
            "uz": "Yetkazib berishlarni yuklab bo'lmadi"
        },
        "Failed to load inactive subscriptions": {
            "en": "Failed to load inactive subscriptions",
            "ru": "Не удалось загрузить неактивные подписки",
            "uz": "Faol bo'lmagan obunalarni yuklab bo'lmadi"
        },
        "Failed to load order details": {
            "en": "Failed to load order details",
            "ru": "Не удалось загрузить детали заказа",
            "uz": "Buyurtma tafsilotlarini yuklab bo'lmadi"
        },
        "Failed to load orders": {
            "en": "Failed to load orders",
            "ru": "Не удалось загрузить заказы",
            "uz": "Buyurtmalarni yuklab bo'lmadi"
        },
        "Failed to load orders. Please try again.": {
            "en": "Failed to load orders. Please try again.",
            "ru": "Не удалось загрузить заказы. Пожалуйста, попробуйте снова.",
            "uz": "Buyurtmalarni yuklab bo'lmadi. Iltimos, qaytadan urinib ko'ring."
        },
        "Failed to load points history": {
            "en": "Failed to load points history",
            "ru": "Не удалось загрузить историю баллов",
            "uz": "Ballar tarixini yuklab bo'lmadi"
        },
        "Failed to load reward details": {
            "en": "Failed to load reward details",
            "ru": "Не удалось загрузить детали награды",
            "uz": "Mukofot tafsilotlarini yuklab bo'lmadi"
        },
        "Failed to load rewards": {
            "en": "Failed to load rewards",
            "ru": "Не удалось загрузить награды",
            "uz": "Mukofotlarni yuklab bo'lmadi"
        },
        "Failed to load subscription details": {
            "en": "Failed to load subscription details",
            "ru": "Не удалось загрузить детали подписки",
            "uz": "Obuna tafsilotlarini yuklab bo'lmadi"
        },



        "Failed to load subscriptions": {
            "en": "Failed to load subscriptions",
            "ru": "Не удалось загрузить подписки",
            "uz": "Obunalarni yuklab bo'lmadi"
        },
        "Failed to pause subscription": {
            "en": "Failed to pause subscription",
            "ru": "Не удалось приостановить подписку",
            "uz": "Obunani to'xtatib bo'lmadi"
        },
        "Failed to redeem reward": {
            "en": "Failed to redeem reward",
            "ru": "Не удалось использовать награду",
            "uz": "Mukofotni olish amalga oshmadi"
        },
        "Failed to reorder items": {
            "en": "Failed to reorder items",
            "ru": "Не удалось повторно заказать товары",
            "uz": "Buyurtmani qayta berish amalga oshmadi"
        },
        "Failed to resend code": {
            "en": "Failed to resend code",
            "ru": "Не удалось повторно отправить код",
            "uz": "Kod qayta yuborilmadi"
        },
        "Failed to reset password": {
            "en": "Failed to reset password",
            "ru": "Не удалось сбросить пароль",
            "uz": "Parolni tiklab bo'lmadi"
        },
        "Failed to resume subscription": {
            "en": "Failed to resume subscription",
            "ru": "Не удалось возобновить подписку",
            "uz": "Obunani davom ettirib bo'lmadi"
        },
        "Failed to save address": {
            "en": "Failed to save address",
            "ru": "Не удалось сохранить адрес",
            "uz": "Manzilni saqlab bo'lmadi"
        },
        "Failed to send message": {
            "en": "Failed to send message",
            "ru": "Не удалось отправить сообщение",
            "uz": "Xabar yuborilmadi"
        },
        "Failed to send reset instructions": {
            "en": "Failed to send reset instructions",
            "ru": "Не удалось отправить инструкции для сброса",
            "uz": "Tiklash bo'yicha ko'rsatmalar yuborilmadi"
        },
        "Failed to send verification code": {
            "en": "Failed to send verification code",
            "ru": "Не удалось отправить код подтверждения",
            "uz": "Tasdiqlash kodi yuborilmadi"
        },
        "Failed to send verification email": {
            "en": "Failed to send verification email",
            "ru": "Не удалось отправить письмо для подтверждения",
            "uz": "Tasdiqlash xati yuborilmadi"
        },
        "Failed to set default address": {
            "en": "Failed to set default address",
            "ru": "Не удалось установить адрес по умолчанию",
            "uz": "Asosiy manzilni belgilab bo'lmadi"
        },
        "Failed to update contact information": {
            "en": "Failed to update contact information",
            "ru": "Не удалось обновить контактную информацию",
            "uz": "Kontakt ma'lumotlarini yangilab bo'lmadi"
        },
        "Failed to update personal information": {
            "en": "Failed to update personal information",
            "ru": "Не удалось обновить личные данные",
            "uz": "Shaxsiy ma'lumotlarni yangilab bo'lmadi"
        },
        "Failed to update subscription": {
            "en": "Failed to update subscription",
            "ru": "Не удалось обновить подписку",
            "uz": "Obunani yangilab bo'lmadi"
        },
        "Fair": {
            "en": "Fair",
            "ru": "Удовлетворительно",
            "uz": "O'rtacha"
        },
        "Fast and reliable water delivery to your doorstep with flexible scheduling options.": {
            "en": "Fast and reliable water delivery to your doorstep with flexible scheduling options.",
            "ru": "Быстрая и надежная доставка воды к вашей двери с гибкими вариантами расписания.",
            "uz": "Moslashuvchan jadval bilan tez va ishonchli suv yetkazib berish xizmati."
        },
        "Female": {
            "en": "Female",
            "ru": "Женский",
            "uz": "Ayol"
        },
        "Filter": {
            "en": "Filter",
            "ru": "Фильтр",
            "uz": "Filtr"
        },
        "First Name": {
            "en": "First Name",
            "ru": "Имя",
            "uz": "Ism"
        },
        "Flexible Scheduling": {
            "en": "Flexible Scheduling",
            "ru": "Гибкое расписание",
            "uz": "Moslashuvchan jadval"
        },
        "Flexible pause/resume options": {
            "en": "Flexible pause/resume options",
            "ru": "Гибкие варианты паузы/возобновления",
            "uz": "Moslashuvchan to'xtatish/davom ettirish imkoniyatlari"
        },
        "Flexible scheduling and easy management": {
            "en": "Flexible scheduling and easy management",
            "ru": "Гибкое расписание и простое управление",
            "uz": "Moslashuvchan jadval va oson boshqaruv"
        },
        "Follow the bot instructions": {
            "en": "Follow the bot instructions",
            "ru": "Следуйте инструкциям бота",
            "uz": "Bot ko'rsatmalariga amal qiling"
        },
        "Forgot Password": {
            "en": "Forgot Password",
            "ru": "Забыли пароль",
            "uz": "Parolni unutdingizmi"
        },
        "Forgot Password?": {
            "en": "Forgot Password?",
            "ru": "Забыли пароль?",
            "uz": "Parolni unutdingizmi?"
        },
        "Free": {
            "en": "Free",
            "ru": "Бесплатно",
            "uz": "Bepul"
        },
        "Free Delivery": {
            "en": "Free Delivery",
            "ru": "Бесплатная доставка",
            "uz": "Bepul yetkazib berish"
        },
        "Free expedited shipping": {
            "en": "Free expedited shipping",
            "ru": "Бесплатная ускоренная доставка",
            "uz": "Bepul tezkor yetkazib berish"
        },
        "Free installation & maintenance": {
            "en": "Free installation & maintenance",
            "ru": "Бесплатная установка и обслуживание",
            "uz": "Bepul o'rnatish va texnik xizmat"
        },
        "Free maintenance service": {
            "en": "Free maintenance service",
            "ru": "Бесплатное сервисное обслуживание",
            "uz": "Bepul xizmat ko'rsatish"
        },
        "Free premium water cooler": {
            "en": "Free premium water cooler",
            "ru": "Бесплатный премиум кулер для воды",
            "uz": "Bepul premium suv sovutgichi"
        },
        "Free same-day delivery": {
            "en": "Free same-day delivery",
            "ru": "Бесплатная доставка в тот же день",
            "uz": "Bepul kunlik yetkazib berish"
        },
        "Free standard shipping": {
            "en": "Free standard shipping",
            "ru": "Бесплатная стандартная доставка",
            "uz": "Bepul standart yetkazib berish"
        },
        "Free water cooler rental": {
            "en": "Free water cooler rental",
            "ru": "Бесплатная аренда кулера",
            "uz": "Bepul suv sovutgich ijarasi"
        },
        "Friday": {
            "en": "Friday",
            "ru": "Пятница",
            "uz": "Juma"
        },
        "Friends Referred": {
            "en": "Friends Referred",
            "ru": "Привлеченные друзья",
            "uz": "Taklif qilingan do'stlar"
        },
        "From bottles to dispensers": {
            "en": "From bottles to dispensers",
            "ru": "От бутылок до диспенсеров",
            "uz": "Butilkalardan dispenserlargacha"
        },
        "Full Address": {
            "en": "Full Address",
            "ru": "Полный адрес",
            "uz": "To'liq manzil"
        },
        "Gallery": {
            "en": "Gallery",
            "ru": "Галерея",
            "uz": "Galereya"
        },
        "Gallery -": {
            "en": "Gallery -",
            "ru": "Галерея -",
            "uz": "Galereya -"
        },
        "Gender": {
            "en": "Gender",
            "ru": "Пол",
            "uz": "Jins"
        },
        "Get 200 bonus points on your birthday every year": {
            "en": "Get 200 bonus points on your birthday every year",
            "ru": "Получайте 200 бонусных баллов в день рождения каждый год",
            "uz": "Har yili tug'ilgan kuningizda 200 bonus ball oling"
        },
        "Get 500 points for each friend who makes their first order": {
            "en": "Get 500 points for each friend who makes their first order",
            "ru": "Получайте 500 баллов за каждого друга, который сделал первый заказ",
            "uz": "Birinchi buyurtma qilgan har bir do'stingiz uchun 500 ball oling"
        },
        "Get In Touch": {
            "en": "Get In Touch",
            "ru": "Свяжитесь с нами",
            "uz": "Biz bilan bog'laning"
        },
        "Get My Referral Code": {
            "en": "Get My Referral Code",
            "ru": "Получить мой реферальный код",
            "uz": "Referal kodimni oling"
        },
        "Go to Login": {
            "en": "Go to Login",
            "ru": "Перейти к входу",
            "uz": "Kirishga o'tish"
        },
        "Go to My Account": {
            "en": "Go to My Account",
            "ru": "Перейти в мой аккаунт",
            "uz": "Hisobimga o'tish"
        },
        "Gold": {
            "en": "Gold",
            "ru": "Золото",
            "uz": "Oltin"
        },



        "Good": {
            "en": "Good",
            "ru": "Хорошо",
            "uz": "Yaxshi"
        },
        "Got Questions?": {
            "en": "Got Questions?",
            "ru": "Есть вопросы?",
            "uz": "Savollaringiz bormi?"
        },
        "Have a Coupon?": {
            "en": "Have a Coupon?",
            "ru": "Есть купон?",
            "uz": "Kuponingiz bormi?"
        },
        "Health Team": {
            "en": "Health Team",
            "ru": "Команда здоровья",
            "uz": "Sogʻliq jamoasi"
        },
        "Health Tips": {
            "en": "Health Tips",
            "ru": "Советы по здоровью",
            "uz": "Sogʻliq bo'yicha maslahatlar"
        },
        "Healthy Water": {
            "en": "Healthy Water",
            "ru": "Здоровая вода",
            "uz": "Sogʻlom suv"
        },
        "Home": {
            "en": "Home",
            "ru": "Главная",
            "uz": "Bosh sahifa"
        },
        "Home Delivery": {
            "en": "Home Delivery",
            "ru": "Доставка на дом",
            "uz": "Uyga yetkazib berish"
        },
        "Hydration & Wellness": {
            "en": "Hydration & Wellness",
            "ru": "Гидратация и здоровье",
            "uz": "Namlanish va sogʻliq"
        },
        "Hydration Benefits": {
            "en": "Hydration Benefits",
            "ru": "Польза гидратации",
            "uz": "Suv ichish foydalari"
        },
        "I agree to the": {
            "en": "I agree to the",
            "ru": "Я согласен с",
            "uz": "Men roziman"
        },
        "I want to link my Telegram account": {
            "en": "I want to link my Telegram account",
            "ru": "Я хочу привязать свой Telegram-аккаунт",
            "uz": "Telegram akkauntimni ulashni xohlayman"
        },
        "Ideal for offices & businesses": {
            "en": "Ideal for offices & businesses",
            "ru": "Идеально для офисов и бизнеса",
            "uz": "Ofis va biznes uchun ideal"
        },
        "Instructions": {
            "en": "Instructions",
            "ru": "Инструкции",
            "uz": "Ko'rsatmalar"
        },
        "Invalid coupon code": {
            "en": "Invalid coupon code",
            "ru": "Недействительный код купона",
            "uz": "Noto'g'ri kupon kodi"
        },
        "Invalid or expired verification code": {
            "en": "Invalid or expired verification code",
            "ru": "Недействительный или истекший код подтверждения",
            "uz": "Yaroqsiz yoki muddati o'tgan tasdiqlash kodi"
        },
        "Invalid or missing reset token. Please request a new password reset link.": {
            "en": "Invalid or missing reset token. Please request a new password reset link.",
            "ru": "Недействительный или отсутствующий токен сброса. Пожалуйста, запросите новую ссылку для сброса пароля.",
            "uz": "Yaroqsiz yoki mavjud bo'lmagan qayta tiklash tokeni. Iltimos, yangi parolni tiklash havolasini so'rang."
        },
        "Invalid reset token": {
            "en": "Invalid reset token",
            "ru": "Недействительный токен сброса",
            "uz": "Yaroqsiz tiklash tokeni"
        },
        "Invalid verification code": {
            "en": "Invalid verification code",
            "ru": "Недействительный код подтверждения",
            "uz": "Noto'g'ri tasdiqlash kodi"
        },
        "Invoice billing options": {
            "en": "Invoice billing options",
            "ru": "Варианты оплаты по счету",
            "uz": "Hisob-faktura bo'yicha to'lov variantlari"
        },
        "Items added to cart successfully!": {
            "en": "Items added to cart successfully!",
            "ru": "Товары успешно добавлены в корзину!",
            "uz": "Mahsulotlar savatchaga muvaffaqiyatli qo'shildi!"
        },
        "Jan 15, 2024": {
            "en": "Jan 15, 2024",
            "ru": "15 янв. 2024",
            "uz": "15-yan, 2024"
        },
        "Join Blue Stream Water Delivery": {
            "en": "Join Blue Stream Water Delivery",
            "ru": "Присоединяйтесь к Blue Stream Water Delivery",
            "uz": "Blue Stream suv yetkazib berish xizmatiga qo'shiling"
        },
        "Join Loyalty Program": {
            "en": "Join Loyalty Program",
            "ru": "Присоединиться к программе лояльности",
            "uz": "Sodiqlik dasturiga qo'shiling"
        },
        "Join us today and enjoy premium water delivery services": {
            "en": "Join us today and enjoy premium water delivery services",
            "ru": "Присоединяйтесь к нам сегодня и наслаждайтесь премиальной доставкой воды",
            "uz": "Bugun bizga qo'shiling va premium suv yetkazib berish xizmatlaridan bahramand bo'ling"
        },
        "Landmark": {
            "en": "Landmark",
            "ru": "Ориентир",
            "uz": "Mo'ljal"
        },
        "Last 3 Months": {
            "en": "Last 3 Months",
            "ru": "Последние 3 месяца",
            "uz": "Oxirgi 3 oy"
        },
        "Last 30 Days": {
            "en": "Last 30 Days",
            "ru": "Последние 30 дней",
            "uz": "Oxirgi 30 kun"
        },
        "Last 7 Days": {
            "en": "Last 7 Days",
            "ru": "Последние 7 дней",
            "uz": "Oxirgi 7 kun"
        },
        "Last Login": {
            "en": "Last Login",
            "ru": "Последний вход",
            "uz": "Oxirgi kirish"
        },
        "Last Name": {
            "en": "Last Name",
            "ru": "Фамилия",
            "uz": "Familiya"
        },
        "Last Year": {
            "en": "Last Year",
            "ru": "Прошлый год",
            "uz": "O'tgan yil"
        },
        "Last login": {
            "en": "Last login",
            "ru": "Последний вход",
            "uz": "Oxirgi kirish"
        },
        "Last order": {
            "en": "Last order",
            "ru": "Последний заказ",
            "uz": "Oxirgi buyurtma"
        },
        "Leading Water Delivery Service in Uzbekistan": {
            "en": "Leading Water Delivery Service in Uzbekistan",
            "ru": "Ведущая служба доставки воды в Узбекистане",
            "uz": "O'zbekistondagi yetakchi suv yetkazib berish xizmati"
        },
        "Learn More": {
            "en": "Learn More",
            "ru": "Узнать больше",
            "uz": "Batafsil"
        },
        "Learn how much water you should drink daily and the optimal timing for maximum health benefits.": {
            "en": "Learn how much water you should drink daily and the optimal timing for maximum health benefits.",
            "ru": "Узнайте, сколько воды следует пить ежедневно и в какое время для максимальной пользы здоровью.",
            "uz": "Kuniga qancha suv ichishingiz kerakligi va maksimal foyda uchun eng yaxshi vaqtni bilib oling."
        },
        "Link Telegram Account (Optional)": {
            "en": "Link Telegram Account (Optional)",
            "ru": "Привязать Telegram-аккаунт (необязательно)",
            "uz": "Telegram akkauntini ulash (majburiy emas)"
        },
        "Link your Telegram account to use our bot and sync your data across platforms": {
            "en": "Link your Telegram account to use our bot and sync your data across platforms",
            "ru": "Привяжите свой Telegram-аккаунт, чтобы использовать нашего бота и синхронизировать данные между платформами",
            "uz": "Telegram akkauntingizni ulang, botimizdan foydalaning va maʼlumotlarni platformalar boʻylab sinxronlang"
        },
        "Linking Code": {
            "en": "Linking Code",
            "ru": "Код привязки",
            "uz": "Ulash kodi"
        },
        "Loading inactive subscriptions...": {
            "en": "Loading inactive subscriptions...",
            "ru": "Загрузка неактивных подписок...",
            "uz": "Faol bo'lmagan obunalar yuklanmoqda..."
        },
        "Loading points history...": {
            "en": "Loading points history...",
            "ru": "Загрузка истории баллов...",
            "uz": "Ballar tarixi yuklanmoqda..."
        },
        "Loading rewards...": {
            "en": "Loading rewards...",
            "ru": "Загрузка наград...",
            "uz": "Mukofotlar yuklanmoqda..."
        },
        "Loading upcoming deliveries...": {
            "en": "Loading upcoming deliveries...",
            "ru": "Загрузка предстоящих доставок...",
            "uz": "Kelgusi yetkazib berishlar yuklanmoqda..."
        },
        "Loading your orders...": {
            "en": "Loading your orders...",
            "ru": "Загрузка ваших заказов...",
            "uz": "Buyurtmalaringiz yuklanmoqda..."
        },
        "Loading your subscriptions...": {
            "en": "Loading your subscriptions...",
            "ru": "Загрузка ваших подписок...",
            "uz": "Obunalaringiz yuklanmoqda..."
        },
        "Logged out from all sessions": {
            "en": "Logged out from all sessions",
            "ru": "Вы вышли из всех сессий",
            "uz": "Barcha sessiyalardan chiqdingiz"
        },
        "Logged out successfully": {
            "en": "Logged out successfully",
            "ru": "Вы успешно вышли",
            "uz": "Muvaffaqiyatli chiqildi"
        },
        "Login": {
            "en": "Login",
            "ru": "Войти",
            "uz": "Kirish"
        },
        "Login Here": {
            "en": "Login Here",
            "ru": "Войдите здесь",
            "uz": "Bu yerda kiring"
        },
        "Login failed": {
            "en": "Login failed",
            "ru": "Ошибка входа",
            "uz": "Kirish amalga oshmadi"
        },
        "Login successful!": {
            "en": "Login successful!",
            "ru": "Вход выполнен успешно!",
            "uz": "Kirish muvaffaqiyatli!"
        },
        "Logout": {
            "en": "Logout",
            "ru": "Выйти",
            "uz": "Chiqish"
        },
        "Loyalty Points": {
            "en": "Loyalty Points",
            "ru": "Бонусные баллы",
            "uz": "Sodiqlik ballari"
        },
        "Loyalty Program": {
            "en": "Loyalty Program",
            "ru": "Программа лояльности",
            "uz": "Sodiqlik dasturi"
        },
        "Loyalty Rewards": {
            "en": "Loyalty Rewards",
            "ru": "Бонусы лояльности",
            "uz": "Sodiqlik mukofotlari"
        },
        "Loyalty Rewards Program": {
            "en": "Loyalty Rewards Program",
            "ru": "Программа бонусов лояльности",
            "uz": "Sodiqlik mukofotlari dasturi"
        },
        "Make Purchases": {
            "en": "Make Purchases",
            "ru": "Совершать покупки",
            "uz": "Xarid qilish"
        },
        "Male": {
            "en": "Male",
            "ru": "Мужчина",
            "uz": "Erkak"
        },
        "Manage": {
            "en": "Manage",
            "ru": "Управлять",
            "uz": "Boshqarish"
        },
        "Manage Addresses": {
            "en": "Manage Addresses",
            "ru": "Управлять адресами",
            "uz": "Manzillarni boshqarish"
        },
        "Manage All": {
            "en": "Manage All",
            "ru": "Управлять всем",
            "uz": "Hammasini boshqarish"
        },
        "Manage Subscription": {
            "en": "Manage Subscription",
            "ru": "Управлять подпиской",
            "uz": "Obunani boshqarish"
        },
        "Manage your account, orders, and preferences from your unified dashboard.": {
            "en": "Manage your account, orders, and preferences from your unified dashboard.",
            "ru": "Управляйте аккаунтом, заказами и настройками из единой панели.",
            "uz": "Hisobingiz, buyurtmalaringiz va sozlamalaringizni yagona boshqaruv panelidan boshqaring."
        },
        "Maximum Purity": {
            "en": "Maximum Purity",
            "ru": "Максимальная чистота",
            "uz": "Maksimal tozalik"
        },
        "Member Since": {
            "en": "Member Since",
            "ru": "Участник с",
            "uz": "Aʼzo bo'lgan vaqtidan"
        },
        "Membership Tiers": {
            "en": "Membership Tiers",
            "ru": "Уровни членства",
            "uz": "Aʼzolik darajalari"
        },
        "Message sent successfully!": {
            "en": "Message sent successfully!",
            "ru": "Сообщение успешно отправлено!",
            "uz": "Xabar muvaffaqiyatli yuborildi!"
        },
        "Meters deep": {
            "en": "Meters deep",
            "ru": "Метры глубины",
            "uz": "Metr chuqurlik"
        },
        "Mobile app for schedule changes": {
            "en": "Mobile app for schedule changes",
            "ru": "Мобильное приложение для изменения графика",
            "uz": "Jadvalni o'zgartirish uchun mobil ilova"
        },
        "Modern Office": {
            "en": "Modern Office",
            "ru": "Современный офис",
            "uz": "Zamonaviy ofis"
        },
        "Modify Plan": {
            "en": "Modify Plan",
            "ru": "Изменить план",
            "uz": "Rejani o'zgartirish"
        },
        "Modify Subscription": {
            "en": "Modify Subscription",
            "ru": "Изменить подписку",
            "uz": "Obunani o'zgartirish"
        },
        "Mon - Sat: 9AM - 6PM.": {
            "en": "Mon - Sat: 9AM - 6PM.",
            "ru": "Пн - Сб: 9:00 - 18:00",
            "uz": "Dush - Shan: 9:00 - 18:00"
        },
        "Monday": {
            "en": "Monday",
            "ru": "Понедельник",
            "uz": "Dushanba"
        },
        "Monthly": {
            "en": "Monthly",
            "ru": "Ежемесячно",
            "uz": "Oylik"
        },
        "Monthly Savings": {
            "en": "Monthly Savings",
            "ru": "Ежемесячная экономия",
            "uz": "Oylik tejash"
        },
        "Most Popular": {
            "en": "Most Popular",
            "ru": "Самый популярный",
            "uz": "Eng mashhur"
        },
        "Most popular for families": {
            "en": "Most popular for families",
            "ru": "Самый популярный для семей",
            "uz": "Oila uchun eng mashhur"
        },
        "Multiple premium coolers": {
            "en": "Multiple premium coolers",
            "ru": "Несколько премиальных кулеров",
            "uz": "Bir nechta premium sovutkichlar"
        },
        "My Account": {
            "en": "My Account",
            "ru": "Мой аккаунт",
            "uz": "Mening hisobim"
        },
        "My Addresses": {
            "en": "My Addresses",
            "ru": "Мои адреса",
            "uz": "Mening manzillarim"
        },
        "My Orders": {
            "en": "My Orders",
            "ru": "Мои заказы",
            "uz": "Mening buyurtmalarim"
        },
        "My Subscriptions": {
            "en": "My Subscriptions",
            "ru": "Мои подписки",
            "uz": "Mening obunalarim"
        },
        "Name: A to Z": {
            "en": "Name: A to Z",
            "ru": "Имя: A–Я",
            "uz": "Nomi: A dan Z gacha"
        },
        "Nearby landmark": {
            "en": "Nearby landmark",
            "ru": "Ближайший ориентир",
            "uz": "Yaqin mo'ljal"
        },
        "Network error. Please try again.": {
            "en": "Network error. Please try again.",
            "ru": "Ошибка сети. Пожалуйста, попробуйте снова.",
            "uz": "Tarmoq xatosi. Qayta urinib ko'ring."
        },


        "Never run out of pure, clean water": {
            "en": "Never run out of pure, clean water",
            "ru": "Никогда не оставайтесь без чистой и свежей воды",
            "uz": "Hech qachon toza va sof suvdan qolasiz"
        },
        "New Password": {
            "en": "New Password",
            "ru": "Новый пароль",
            "uz": "Yangi parol"
        },
        "New Quality Standards and Certification Achieved": {
            "en": "New Quality Standards and Certification Achieved",
            "ru": "Достигнуты новые стандарты качества и сертификация",
            "uz": "Yangi sifat standartlari va sertifikatlashga erishildi"
        },
        "New passwords do not match": {
            "en": "New passwords do not match",
            "ru": "Новые пароли не совпадают",
            "uz": "Yangi parollar mos kelmayapti"
        },
        "Next Delivery": {
            "en": "Next Delivery",
            "ru": "Следующая доставка",
            "uz": "Keyingi yetkazib berish"
        },
        "Next billing": {
            "en": "Next billing",
            "ru": "Следующее выставление счета",
            "uz": "Keyingi hisob-kitob"
        },
        "Next delivery": {
            "en": "Next delivery",
            "ru": "Следующая доставка",
            "uz": "Keyingi yetkazib berish"
        },
        "No Active Subscriptions": {
            "en": "No Active Subscriptions",
            "ru": "Нет активных подписок",
            "uz": "Faol obunalar mavjud emas"
        },
        "No addresses saved yet": {
            "en": "No addresses saved yet",
            "ru": "Адреса еще не сохранены",
            "uz": "Hali manzillar saqlanmagan"
        },
        "No contracts - cancel or modify anytime": {
            "en": "No contracts - cancel or modify anytime",
            "ru": "Без контрактов — отменяйте или изменяйте в любое время",
            "uz": "Hech qanday shartnoma yo'q — istalgan vaqtda bekor qiling yoki o'zgartiring"
        },
        "No orders found": {
            "en": "No orders found",
            "ru": "Заказы не найдены",
            "uz": "Buyurtmalar topilmadi"
        },
        "No orders yet": {
            "en": "No orders yet",
            "ru": "Пока нет заказов",
            "uz": "Hali buyurtmalar yo'q"
        },
        "No paused or cancelled subscriptions": {
            "en": "No paused or cancelled subscriptions",
            "ru": "Нет приостановленных или отменённых подписок",
            "uz": "To'xtatilgan yoki bekor qilingan obunalar yo'q"
        },
        "No phone number available": {
            "en": "No phone number available",
            "ru": "Номер телефона недоступен",
            "uz": "Telefon raqami mavjud emas"
        },
        "No points history found": {
            "en": "No points history found",
            "ru": "История баллов не найдена",
            "uz": "Ballar tarixi topilmadi"
        },
        "No rewards available": {
            "en": "No rewards available",
            "ru": "Нет доступных наград",
            "uz": "Mukofotlar mavjud emas"
        },
        "No setup fees and no long-term contracts required. You can cancel your subscription at any time with 30 days notice. Water cooler rental is included in all plans at no extra cost.": {
            "en": "No setup fees and no long-term contracts required. You can cancel your subscription at any time with 30 days notice. Water cooler rental is included in all plans at no extra cost.",
            "ru": "Нет платы за установку и долгосрочных контрактов. Вы можете отменить подписку в любое время, уведомив за 30 дней. Аренда кулера включена во все тарифы без дополнительной оплаты.",
            "uz": "O'rnatish uchun to'lov va uzoq muddatli shartnoma talab qilinmaydi. Obunani istalgan vaqtda 30 kun oldin ogohlantirib bekor qilishingiz mumkin. Suv sovutgichini ijaraga olish barcha rejalar uchun bepul."
        },
        "No upcoming deliveries": {
            "en": "No upcoming deliveries",
            "ru": "Нет предстоящих доставок",
            "uz": "Yaqinda bo'ladigan yetkazib berishlar yo'q"
        },
        "Note:": {
            "en": "Note:",
            "ru": "Примечание:",
            "uz": "Eslatma:"
        },
        "Nov 10, 2023": {
            "en": "Nov 10, 2023",
            "ru": "10 ноября 2023",
            "uz": "10-noyabr, 2023"
        },
        "Number of Bottles per Delivery": {
            "en": "Number of Bottles per Delivery",
            "ru": "Количество бутылей за доставку",
            "uz": "Har bir yetkazib berishda butilkalar soni"
        },
        "Office Manager": {
            "en": "Office Manager",
            "ru": "Офис-менеджер",
            "uz": "Ofis menejeri"
        },
        "Office Solutions": {
            "en": "Office Solutions",
            "ru": "Офисные решения",
            "uz": "Ofis yechimlari"
        },
        "On-time delivery to your doorstep": {
            "en": "On-time delivery to your doorstep",
            "ru": "Своевременная доставка прямо к вашей двери",
            "uz": "O'z vaqtida eshigingizgacha yetkazib berish"
        },
        "Open Hours:": {
            "en": "Open Hours:",
            "ru": "Часы работы:",
            "uz": "Ish vaqti:"
        },
        "Open Telegram and find our bot": {
            "en": "Open Telegram and find our bot",
            "ru": "Откройте Telegram и найдите нашего бота",
            "uz": "Telegram'ni oching va botimizni toping"
        },
        "Open our Telegram bot": {
            "en": "Open our Telegram bot",
            "ru": "Откройте нашего Telegram-бота",
            "uz": "Telegram botimizni oching"
        },
        "Order": {
            "en": "Order",
            "ru": "Заказ",
            "uz": "Buyurtma"
        },
        "Order #": {
            "en": "Order #",
            "ru": "Заказ №",
            "uz": "Buyurtma №"
        },
        "Order Details": {
            "en": "Order Details",
            "ru": "Детали заказа",
            "uz": "Buyurtma tafsilotlari"
        },
        "Order Items": {
            "en": "Order Items",
            "ru": "Товары в заказе",
            "uz": "Buyurtmadagi mahsulotlar"
        },
        "Order Status": {
            "en": "Order Status",
            "ru": "Статус заказа",
            "uz": "Buyurtma holati"
        },
        "Order Summary": {
            "en": "Order Summary",
            "ru": "Сводка заказа",
            "uz": "Buyurtma xulosasi"
        },
        "Order Tracking": {
            "en": "Order Tracking",
            "ru": "Отслеживание заказа",
            "uz": "Buyurtma kuzatuvi"
        },
        "Order cancelled successfully": {
            "en": "Order cancelled successfully",
            "ru": "Заказ успешно отменён",
            "uz": "Buyurtma muvaffaqiyatli bekor qilindi"
        },
        "Order number, product name...": {
            "en": "Order number, product name...",
            "ru": "Номер заказа, название товара...",
            "uz": "Buyurtma raqami, mahsulot nomi..."
        },
        "Orders": {
            "en": "Orders",
            "ru": "Заказы",
            "uz": "Buyurtmalar"
        },
        "Our Blog": {
            "en": "Our Blog",
            "ru": "Наш блог",
            "uz": "Bizning blog"
        },
        "Our Products": {
            "en": "Our Products",
            "ru": "Наши продукты",
            "uz": "Bizning mahsulotlar"
        },
        "Our Professional Team": {
            "en": "Our Professional Team",
            "ru": "Наша профессиональная команда",
            "uz": "Bizning professional jamoa"
        },
        "Our Services": {
            "en": "Our Services",
            "ru": "Наши услуги",
            "uz": "Bizning xizmatlar"
        },
        "Our Team": {
            "en": "Our Team",
            "ru": "Наша команда",
            "uz": "Bizning jamoa"
        },
        "Our water goes through rigorous filtration processes ensuring the highest quality.": {
            "en": "Our water goes through rigorous filtration processes ensuring the highest quality.",
            "ru": "Наша вода проходит строгие процессы фильтрации, обеспечивающие наивысшее качество.",
            "uz": "Suvimiz eng yuqori sifatni ta'minlash uchun qat'iy filtratsiya jarayonlaridan o'tadi."
        },
        "Pages": {
            "en": "Pages",
            "ru": "Страницы",
            "uz": "Sahifalar"
        },
        "Password": {
            "en": "Password",
            "ru": "Пароль",
            "uz": "Parol"
        },
        "Password Protection": {
            "en": "Password Protection",
            "ru": "Защита паролем",
            "uz": "Parol bilan himoya"
        },
        "Password changed successfully!": {
            "en": "Password changed successfully!",
            "ru": "Пароль успешно изменён!",
            "uz": "Parol muvaffaqiyatli o'zgartirildi!"
        },
        "Password is too weak. Please choose a stronger password.": {
            "en": "Password is too weak. Please choose a stronger password.",
            "ru": "Пароль слишком слабый. Пожалуйста, выберите более надежный пароль.",
            "uz": "Parol juda zaif. Iltimos, kuchliroq parol tanlang."
        },
        "Password must be at least 8 characters long": {
            "en": "Password must be at least 8 characters long",
            "ru": "Пароль должен содержать не менее 8 символов",
            "uz": "Parol kamida 8 ta belgidan iborat bo'lishi kerak"
        },
        "Password reset instructions have been sent to your email address. Please check your inbox and follow the instructions.": {
            "en": "Password reset instructions have been sent to your email address. Please check your inbox and follow the instructions.",
            "ru": "Инструкции по сбросу пароля были отправлены на вашу электронную почту. Пожалуйста, проверьте почту и следуйте инструкциям.",
            "uz": "Parolni tiklash ko'rsatmalari elektron pochtangizga yuborildi. Iltimos, xabarni tekshirib, ko'rsatmalarga amal qiling."
        },
        "Password reset successfully!": {
            "en": "Password reset successfully!",
            "ru": "Пароль успешно сброшен!",
            "uz": "Parol muvaffaqiyatli tiklandi!"
        },
        "Password strength": {
            "en": "Password strength",
            "ru": "Надежность пароля",
            "uz": "Parol kuchi"
        },
        "Passwords do not match": {
            "en": "Passwords do not match",
            "ru": "Пароли не совпадают",
            "uz": "Parollar mos kelmayapti"
        },
        "Pause": {
            "en": "Pause",
            "ru": "Пауза",
            "uz": "To'xtatish"
        },
        "Pause Subscription": {
            "en": "Pause Subscription",
            "ru": "Приостановить подписку",
            "uz": "Obunani to'xtatish"
        },
        "Pause subscriptions for vacations": {
            "en": "Pause subscriptions for vacations",
            "ru": "Приостановите подписку на время отпуска",
            "uz": "Ta'til vaqtida obunani to'xtatib turing"
        },
        "Paused & Cancelled Subscriptions": {
            "en": "Paused & Cancelled Subscriptions",
            "ru": "Приостановленные и отмененные подписки",
            "uz": "To'xtatilgan va bekor qilingan obunalar"
        },
        "Pending": {
            "en": "Pending",
            "ru": "В ожидании",
            "uz": "Kutilmoqda"
        },
        "Perfect for small families": {
            "en": "Perfect for small families",
            "ru": "Идеально для небольших семей",
            "uz": "Kichik oilalar uchun ideal"
        },
        "Personal Information": {
            "en": "Personal Information",
            "ru": "Личная информация",
            "uz": "Shaxsiy ma'lumotlar"
        },
        "Personal information updated successfully!": {
            "en": "Personal information updated successfully!",
            "ru": "Личная информация успешно обновлена!",
            "uz": "Shaxsiy ma'lumotlar muvaffaqiyatli yangilandi!"
        },
        "Personal information was updated": {
            "en": "Personal information was updated",
            "ru": "Личная информация обновлена",
            "uz": "Shaxsiy ma'lumotlar yangilandi"
        },
        "Phone": {
            "en": "Phone",
            "ru": "Телефон",
            "uz": "Telefon"
        },


        "Phone Number": {
            "en": "Phone Number",
            "ru": "Номер телефона",
            "uz": "Telefon raqami"
        },
        "Phone Number (+998XXXXXXXXX)": {
            "en": "Phone Number (+998XXXXXXXXX)",
            "ru": "Номер телефона (+998XXXXXXXXX)",
            "uz": "Telefon raqami (+998XXXXXXXXX)"
        },
        "Phone Pending": {
            "en": "Phone Pending",
            "ru": "Телефон в ожидании",
            "uz": "Telefon kutilmoqda"
        },
        "Phone Verification": {
            "en": "Phone Verification",
            "ru": "Подтверждение телефона",
            "uz": "Telefonni tasdiqlash"
        },
        "Phone Verified": {
            "en": "Phone Verified",
            "ru": "Телефон подтвержден",
            "uz": "Telefon tasdiqlandi"
        },
        "Phone number verified successfully!": {
            "en": "Phone number verified successfully!",
            "ru": "Номер телефона успешно подтвержден!",
            "uz": "Telefon raqami muvaffaqiyatli tasdiqlandi!"
        },
        "Phone number verified successfully! You can now use all features.": {
            "en": "Phone number verified successfully! You can now use all features.",
            "ru": "Номер телефона успешно подтвержден! Теперь вы можете использовать все функции.",
            "uz": "Telefon raqami muvaffaqiyatli tasdiqlandi! Endi barcha funksiyalardan foydalanishingiz mumkin."
        },
        "Place New Order": {
            "en": "Place New Order",
            "ru": "Сделать новый заказ",
            "uz": "Yangi buyurtma berish"
        },
        "Placed on": {
            "en": "Placed on",
            "ru": "Размещено",
            "uz": "Joylashtirilgan"
        },
        "Plan": {
            "en": "Plan",
            "ru": "План",
            "uz": "Reja"
        },
        "Plan Benefits": {
            "en": "Plan Benefits",
            "ru": "Преимущества плана",
            "uz": "Reja afzalliklari"
        },
        "Platform Usage": {
            "en": "Platform Usage",
            "ru": "Использование платформы",
            "uz": "Platformadan foydalanish"
        },
        "Platinum": {
            "en": "Platinum",
            "ru": "Платиновый",
            "uz": "Platina"
        },
        "Please": {
            "en": "Please",
            "ru": "Пожалуйста",
            "uz": "Iltimos"
        },
        "Please check your email and enter the verification code": {
            "en": "Please check your email and enter the verification code",
            "ru": "Пожалуйста, проверьте свою электронную почту и введите код подтверждения",
            "uz": "Iltimos, emailingizni tekshirib tasdiqlash kodini kiriting"
        },
        "Please enter a coupon code": {
            "en": "Please enter a coupon code",
            "ru": "Пожалуйста, введите код купона",
            "uz": "Iltimos, kupon kodini kiriting"
        },
        "Please enter a valid 6-digit code": {
            "en": "Please enter a valid 6-digit code",
            "ru": "Пожалуйста, введите правильный 6-значный код",
            "uz": "Iltimos, to'g'ri 6 xonali kodni kiriting"
        },
        "Please enter a valid phone number": {
            "en": "Please enter a valid phone number",
            "ru": "Пожалуйста, введите правильный номер телефона",
            "uz": "Iltimos, to'g'ri telefon raqamini kiriting"
        },
        "Please enter the verification code": {
            "en": "Please enter the verification code",
            "ru": "Пожалуйста, введите код подтверждения",
            "uz": "Iltimos, tasdiqlash kodini kiriting"
        },
        "Please enter your email address or phone number": {
            "en": "Please enter your email address or phone number",
            "ru": "Пожалуйста, введите ваш адрес электронной почты или номер телефона",
            "uz": "Iltimos, emailingiz yoki telefon raqamingizni kiriting"
        },
        "Please enter your new password below": {
            "en": "Please enter your new password below",
            "ru": "Пожалуйста, введите новый пароль ниже",
            "uz": "Iltimos, yangi parolni quyida kiriting"
        },
        "Please enter your phone number to receive a verification code": {
            "en": "Please enter your phone number to receive a verification code",
            "ru": "Пожалуйста, введите номер телефона, чтобы получить код подтверждения",
            "uz": "Iltimos, tasdiqlash kodini olish uchun telefon raqamingizni kiriting"
        },
        "Please log in first": {
            "en": "Please log in first",
            "ru": "Пожалуйста, сначала войдите в систему",
            "uz": "Iltimos, avval tizimga kiring"
        },
        "Please login to your account to continue": {
            "en": "Please login to your account to continue",
            "ru": "Пожалуйста, войдите в свой аккаунт, чтобы продолжить",
            "uz": "Iltimos, davom etish uchun hisobingizga kiring"
        },
        "Please provide a reason for cancellation (optional)": {
            "en": "Please provide a reason for cancellation (optional)",
            "ru": "Пожалуйста, укажите причину отмены (необязательно)",
            "uz": "Iltimos, bekor qilish sababini kiriting (ixtiyoriy)"
        },
        "Please verify your email address to access all features": {
            "en": "Please verify your email address to access all features",
            "ru": "Пожалуйста, подтвердите свой адрес электронной почты, чтобы получить доступ ко всем функциям",
            "uz": "Iltimos, barcha funksiyalardan foydalanish uchun emailingizni tasdiqlang"
        },
        "Please verify your phone number for enhanced security": {
            "en": "Please verify your phone number for enhanced security",
            "ru": "Пожалуйста, подтвердите номер телефона для повышения безопасности",
            "uz": "Iltimos, xavfsizlikni oshirish uchun telefon raqamingizni tasdiqlang"
        },
        "Points": {
            "en": "Points",
            "ru": "Баллы",
            "uz": "Ballar"
        },
        "Points Earned": {
            "en": "Points Earned",
            "ru": "Заработанные баллы",
            "uz": "Topilgan ballar"
        },
        "Points Expired": {
            "en": "Points Expired",
            "ru": "Баллы истекли",
            "uz": "Ballar muddati tugadi"
        },
        "Points History": {
            "en": "Points History",
            "ru": "История баллов",
            "uz": "Ballar tarixi"
        },
        "Points Redeemed": {
            "en": "Points Redeemed",
            "ru": "Использованные баллы",
            "uz": "Foydalanilgan ballar"
        },
        "Points Required": {
            "en": "Points Required",
            "ru": "Необходимые баллы",
            "uz": "Kerakli ballar"
        },
        "Postal Code": {
            "en": "Postal Code",
            "ru": "Почтовый индекс",
            "uz": "Pochta indeksi"
        },
        "Postal code": {
            "en": "Postal code",
            "ru": "Почтовый индекс",
            "uz": "Pochta indeksi"
        },
        "Preferences": {
            "en": "Preferences",
            "ru": "Настройки",
            "uz": "Sozlamalar"
        },
        "Preferences saved successfully!": {
            "en": "Preferences saved successfully!",
            "ru": "Настройки успешно сохранены!",
            "uz": "Sozlamalar muvaffaqiyatli saqlandi!"
        },
        "Preferred Delivery Day": {
            "en": "Preferred Delivery Day",
            "ru": "Предпочтительный день доставки",
            "uz": "Afzal ko'rilgan yetkazib berish kuni"
        },
        "Preferred Language": {
            "en": "Preferred Language",
            "ru": "Предпочтительный язык",
            "uz": "Afzal ko'rilgan til"
        },
        "Preferred Time": {
            "en": "Preferred Time",
            "ru": "Предпочтительное время",
            "uz": "Afzal ko'rilgan vaqt"
        },
        "Preloader Close": {
            "en": "Preloader Close",
            "ru": "Закрыть прелоадер",
            "uz": "Preloaderni yopish"
        },
        "Premium Plan": {
            "en": "Premium Plan",
            "ru": "Премиум план",
            "uz": "Premium reja"
        },
        "Premium Plan - $49/month": {
            "en": "Premium Plan - $49/month",
            "ru": "Премиум план - $49/месяц",
            "uz": "Premium reja - $49/oy"
        },
        "Premium Quality Guarantee": {
            "en": "Premium Quality Guarantee",
            "ru": "Гарантия премиум качества",
            "uz": "Premium sifat kafolati"
        },
        "Premium Service": {
            "en": "Premium Service",
            "ru": "Премиум сервис",
            "uz": "Premium xizmat"
        },
        "Premium Water": {
            "en": "Premium Water",
            "ru": "Премиум вода",
            "uz": "Premium suv"
        },
        "Premium Water Bottles": {
            "en": "Premium Water Bottles",
            "ru": "Премиум бутылки воды",
            "uz": "Premium suv butilkalari"
        },
        "Premium Water Delivery Services": {
            "en": "Premium Water Delivery Services",
            "ru": "Услуги доставки премиум воды",
            "uz": "Premium suv yetkazib berish xizmatlari"
        },
        "Premium Water Quality": {
            "en": "Premium Water Quality",
            "ru": "Качество премиум воды",
            "uz": "Premium suv sifati"
        },
        "Premium water delivery services with a commitment to quality and customer satisfaction.": {
            "en": "Premium water delivery services with a commitment to quality and customer satisfaction.",
            "ru": "Услуги доставки премиум воды с приверженностью качеству и удовлетворению клиентов.",
            "uz": "Premium suv yetkazib berish xizmatlari sifat va mijozlar qoniqishini kafolatlaydi."
        },


        "Preparing your account data for download...": {
            "en": "Preparing your account data for download...",
            "ru": "Подготавливаем данные вашего аккаунта для скачивания...",
            "uz": "Hisob ma'lumotlaringizni yuklab olish uchun tayyorlanmoqda..."
        },
        "Price": {
            "en": "Price",
            "ru": "Цена",
            "uz": "Narx"
        },
        "Price Range": {
            "en": "Price Range",
            "ru": "Диапазон цен",
            "uz": "Narx oralig'i"
        },
        "Price: High to Low": {
            "en": "Price: High to Low",
            "ru": "Цена: от высокой к низкой",
            "uz": "Narx: Yuqoridan pastga"
        },
        "Price: Low to High": {
            "en": "Price: Low to High",
            "ru": "Цена: от низкой к высокой",
            "uz": "Narx: Pastdan yuqoriga"
        },
        "Priority Support": {
            "en": "Priority Support",
            "ru": "Приоритетная поддержка",
            "uz": "Ustuvor yordam"
        },
        "Priority customer support": {
            "en": "Priority customer support",
            "ru": "Приоритетная поддержка клиентов",
            "uz": "Ustuvor mijozlarni qo'llab-quvvatlash"
        },
        "Priority delivery scheduling": {
            "en": "Priority delivery scheduling",
            "ru": "Приоритетное расписание доставки",
            "uz": "Ustuvor yetkazib berish jadvali"
        },
        "Privacy Policy": {
            "en": "Privacy Policy",
            "ru": "Политика конфиденциальности",
            "uz": "Maxfiylik siyosati"
        },
        "Proceed to Checkout": {
            "en": "Proceed to Checkout",
            "ru": "Перейти к оформлению заказа",
            "uz": "Buyurtmani rasmiylashtirishga o'tish"
        },
        "Processing": {
            "en": "Processing",
            "ru": "Обработка",
            "uz": "Qayta ishlanmoqda"
        },
        "Product": {
            "en": "Product",
            "ru": "Продукт",
            "uz": "Mahsulot"
        },
        "Product Range": {
            "en": "Product Range",
            "ru": "Ассортимент продукции",
            "uz": "Mahsulot assortimenti"
        },
        "Product added to cart": {
            "en": "Product added to cart",
            "ru": "Товар добавлен в корзину",
            "uz": "Mahsulot savatga qo'shildi"
        },
        "Product removed from cart": {
            "en": "Product removed from cart",
            "ru": "Товар удален из корзины",
            "uz": "Mahsulot savatdan olib tashlandi"
        },
        "Products": {
            "en": "Products",
            "ru": "Продукты",
            "uz": "Mahsulotlar"
        },
        "Professional Delivery": {
            "en": "Professional Delivery",
            "ru": "Профессиональная доставка",
            "uz": "Professional yetkazib berish"
        },
        "Professional laboratory testing": {
            "en": "Professional laboratory testing",
            "ru": "Профессиональное лабораторное тестирование",
            "uz": "Professional laboratoriya sinovlari"
        },
        "Professional service and great value for money. The subscription plan saves us time and money. Highly recommended for businesses and families.": {
            "en": "Professional service and great value for money. The subscription plan saves us time and money. Highly recommended for businesses and families.",
            "ru": "Профессиональное обслуживание и отличное соотношение цены и качества. Подписка экономит нам время и деньги. Настоятельно рекомендуем для бизнеса и семей.",
            "uz": "Professional xizmat va narxga mos qiymat. Obuna rejasi bizga vaqt va pul tejaydi. Biznes va oilalar uchun tavsiya etiladi."
        },
        "Professional setup service": {
            "en": "Professional setup service",
            "ru": "Профессиональная установка",
            "uz": "Professional o'rnatish xizmati"
        },
        "Profile Settings": {
            "en": "Profile Settings",
            "ru": "Настройки профиля",
            "uz": "Profil sozlamalari"
        },
        "Profile Updated": {
            "en": "Profile Updated",
            "ru": "Профиль обновлен",
            "uz": "Profil yangilandi"
        },
        "Profile updated": {
            "en": "Profile updated",
            "ru": "Профиль обновлен",
            "uz": "Profil yangilandi"
        },
        "Pure Water Delivery To Your Doorstep": {
            "en": "Pure Water Delivery To Your Doorstep",
            "ru": "Доставка чистой воды прямо к вашему порогу",
            "uz": "Toza suvni eshigingizgacha yetkazib berish"
        },
        "Pure and clean water without harmful chemicals and additives.": {
            "en": "Pure and clean water without harmful chemicals and additives.",
            "ru": "Чистая и прозрачная вода без вредных химикатов и добавок.",
            "uz": "Zararli kimyoviy moddalar va qo'shimchalarsiz toza va tiniq suv."
        },
        "Qty": {
            "en": "Qty",
            "ru": "Кол-во",
            "uz": "Soni"
        },
        "Quality Assurance": {
            "en": "Quality Assurance",
            "ru": "Гарантия качества",
            "uz": "Sifat kafolati"
        },
        "Quality Laboratory": {
            "en": "Quality Laboratory",
            "ru": "Лаборатория качества",
            "uz": "Sifat laboratoriyasi"
        },
        "Quality Standards": {
            "en": "Quality Standards",
            "ru": "Стандарты качества",
            "uz": "Sifat standartlari"
        },
        "Quality Team": {
            "en": "Quality Team",
            "ru": "Команда качества",
            "uz": "Sifat jamoasi"
        },
        "Quality Water": {
            "en": "Quality Water",
            "ru": "Качественная вода",
            "uz": "Sifatli suv"
        },
        "Quality guarantee on every bottle": {
            "en": "Quality guarantee on every bottle",
            "ru": "Гарантия качества на каждую бутылку",
            "uz": "Har bir shishada sifat kafolati"
        },
        "Quantity": {
            "en": "Quantity",
            "ru": "Количество",
            "uz": "Miqdor"
        },
        "Quantity per delivery": {
            "en": "Quantity per delivery",
            "ru": "Количество за доставку",
            "uz": "Har bir yetkazib berishda miqdor"
        },
        "Quick Actions": {
            "en": "Quick Actions",
            "ru": "Быстрые действия",
            "uz": "Tezkor amallar"
        },
        "Read More": {
            "en": "Read More",
            "ru": "Читать далее",
            "uz": "Batafsil o'qish"
        },
        "Ready To Get Our Premium Water Delivery Service": {
            "en": "Ready To Get Our Premium Water Delivery Service",
            "ru": "Готовы воспользоваться нашей премиальной доставкой воды?",
            "uz": "Bizning premium suv yetkazib berish xizmatimizga tayyormisiz?"
        },
        "Receive SMS notifications about deliveries": {
            "en": "Receive SMS notifications about deliveries",
            "ru": "Получать SMS-уведомления о доставках",
            "uz": "Yetkazib berish haqida SMS xabarnomalarini oling"
        },
        "Receive email notifications about orders": {
            "en": "Receive email notifications about orders",
            "ru": "Получать email-уведомления о заказах",
            "uz": "Buyurtmalar haqida email xabarnomalarini oling"
        },
        "Receive promotional emails and offers": {
            "en": "Receive promotional emails and offers",
            "ru": "Получать рекламные письма и предложения",
            "uz": "Reklama email va takliflarni oling"
        },
        "Recent Account Activity": {
            "en": "Recent Account Activity",
            "ru": "Последняя активность аккаунта",
            "uz": "Hisobdagi so'nggi faoliyat"
        },
        "Recent Activity": {
            "en": "Recent Activity",
            "ru": "Последняя активность",
            "uz": "So'nggi faoliyat"
        },
        "Recent Billing History": {
            "en": "Recent Billing History",
            "ru": "История последних счетов",
            "uz": "So'nggi hisob-kitob tarixi"
        },
        "Recent Orders": {
            "en": "Recent Orders",
            "ru": "Последние заказы",
            "uz": "So'nggi buyurtmalar"
        },
        "Redeem": {
            "en": "Redeem",
            "ru": "Использовать",
            "uz": "Foydalanish"
        },
        "Redeem Reward": {
            "en": "Redeem Reward",
            "ru": "Использовать награду",
            "uz": "Mukofotdan foydalanish"
        },
        "Refer Friends": {
            "en": "Refer Friends",
            "ru": "Пригласить друзей",
            "uz": "Do'stlarni taklif qilish"
        },
        "Referral Code (Optional)": {
            "en": "Referral Code (Optional)",
            "ru": "Реферальный код (необязательно)",
            "uz": "Referal kod (ixtiyoriy)"
        },
        "Referral code copied to clipboard!": {
            "en": "Referral code copied to clipboard!",
            "ru": "Реферальный код скопирован в буфер обмена!",
            "uz": "Referal kod xotiraga nusxalandi!"
        },
        "Register": {
            "en": "Register",
            "ru": "Регистрация",
            "uz": "Ro'yxatdan o'tish"
        },
        "Register Here": {
            "en": "Register Here",
            "ru": "Зарегистрируйтесь здесь",
            "uz": "Bu yerda ro'yxatdan o'ting"
        },
        "Registration Source": {
            "en": "Registration Source",
            "ru": "Источник регистрации",
            "uz": "Ro'yxatdan o'tish manbai"
        },
        "Registration failed": {
            "en": "Registration failed",
            "ru": "Регистрация не удалась",
            "uz": "Ro'yxatdan o'tish amalga oshmadi"
        },
        "Remember Me": {
            "en": "Remember Me",
            "ru": "Запомнить меня",
            "uz": "Meni eslab qol"
        },
        "Remember your password?": {
            "en": "Remember your password?",
            "ru": "Помните свой пароль?",
            "uz": "Parolingizni eslaysizmi?"
        },
        "Reorder": {
            "en": "Reorder",
            "ru": "Повторить заказ",
            "uz": "Qayta buyurtma berish"
        },
        "Reorder Items": {
            "en": "Reorder Items",
            "ru": "Повторить товары",
            "uz": "Buyurtmalarni qayta berish"
        },
        "Request New Reset Link": {
            "en": "Request New Reset Link",
            "ru": "Запросить новую ссылку для сброса",
            "uz": "Yangi tiklash havolasini so'rash"
        },
        "Reschedule": {
            "en": "Reschedule",
            "ru": "Перенести",
            "uz": "Qayta belgilash"
        },
        "Resend Code": {
            "en": "Resend Code",
            "ru": "Отправить код повторно",
            "uz": "Kod qayta yuborish"
        },
        "Resend Verification Email": {
            "en": "Resend Verification Email",
            "ru": "Отправить письмо с подтверждением повторно",
            "uz": "Tasdiqlash emailini qayta yuborish"
        },
        "Reset Password": {
            "en": "Reset Password",
            "ru": "Сброс пароля",
            "uz": "Parolni tiklash"
        },
        "Reset Your Password": {
            "en": "Reset Your Password",
            "ru": "Сбросьте ваш пароль",
            "uz": "Parolingizni tiklang"
        },
        "Reset instructions sent successfully!": {
            "en": "Reset instructions sent successfully!",
            "ru": "Инструкции по сбросу успешно отправлены!",
            "uz": "Tiklash ko'rsatmalari muvaffaqiyatli yuborildi!"
        },
        "Resetting...": {
            "en": "Resetting...",
            "ru": "Сброс...",
            "uz": "Tiklanmoqda..."
        },
        "Resume": {
            "en": "Resume",
            "ru": "Возобновить",
            "uz": "Davom ettirish"
        },
        "Resume Subscription": {
            "en": "Resume Subscription",
            "ru": "Возобновить подписку",
            "uz": "Obunani davom ettirish"
        },
        "Reward redeemed successfully!": {
            "en": "Reward redeemed successfully!",
            "ru": "Награда успешно использована!",
            "uz": "Mukofot muvaffaqiyatli foydalanildi!"
        },
        "SMS Authentication": {
            "en": "SMS Authentication",
            "ru": "SMS-аутентификация",
            "uz": "SMS autentifikatsiya"
        },
        "Same-day emergency delivery": {
            "en": "Same-day emergency delivery",
            "ru": "Экстренная доставка в тот же день",
            "uz": "Shu kuni tezkor yetkazib berish"
        },
        "Satisfied Customer": {
            "en": "Satisfied Customer",
            "ru": "Довольный клиент",
            "uz": "Qoniqqan mijoz"
        },
        "Saturday": {
            "en": "Saturday",
            "ru": "Суббота",
            "uz": "Shanba"
        },
        "Save Address": {
            "en": "Save Address",
            "ru": "Сохранить адрес",
            "uz": "Manzilni saqlash"
        },
        "Save Changes": {
            "en": "Save Changes",
            "ru": "Сохранить изменения",
            "uz": "O'zgarishlarni saqlash"
        },
        "Save Preferences": {
            "en": "Save Preferences",
            "ru": "Сохранить предпочтения",
            "uz": "Afzalliklarni saqlash"
        },
        "Save Security Settings": {
            "en": "Save Security Settings",
            "ru": "Сохранить настройки безопасности",
            "uz": "Xavfsizlik sozlamalarini saqlash"
        },
        "Save money and enjoy convenient water delivery with our flexible subscription plans. Choose between monthly and yearly billing cycles to fit your needs.": {
            "en": "Save money and enjoy convenient water delivery with our flexible subscription plans. Choose between monthly and yearly billing cycles to fit your needs.",
            "ru": "Экономьте деньги и наслаждайтесь удобной доставкой воды с нашими гибкими планами подписки. Выбирайте между ежемесячной и ежегодной оплатой в зависимости от ваших потребностей.",
            "uz": "Pulni tejang va qulay suv yetkazib berishdan zavqlaning bizning moslashuvchan obuna rejalarimiz bilan. Oylik yoki yillik to'lov usullaridan o'zingizga mosini tanlang."
        },
        "Save money and never run out of water with our flexible subscription plans.": {
            "en": "Save money and never run out of water with our flexible subscription plans.",
            "ru": "Экономьте деньги и никогда не оставайтесь без воды с нашими гибкими планами подписки.",
            "uz": "Pulni tejang va moslashuvchan obuna rejalarimiz bilan hech qachon suvsiz qolmang."
        },
        "Save this code to link your Telegram account": {
            "en": "Save this code to link your Telegram account",
            "ru": "Сохраните этот код, чтобы привязать свой аккаунт Telegram",
            "uz": "Telegram hisobingizni ulash uchun ushbu kodni saqlang"
        },
        "Saved Addresses": {
            "en": "Saved Addresses",
            "ru": "Сохраненные адреса",
            "uz": "Saqlangan manzillar"
        },
        "Scheduled": {
            "en": "Scheduled",
            "ru": "Запланировано",
            "uz": "Rejalashtirilgan"
        },
        "Search Orders": {
            "en": "Search Orders",
            "ru": "Поиск заказов",
            "uz": "Buyurtmalarni qidirish"
        },
        "Search Products": {
            "en": "Search Products",
            "ru": "Поиск продуктов",
            "uz": "Mahsulotlarni qidirish"
        },
        "Search products...": {
            "en": "Search products...",
            "ru": "Искать продукты...",
            "uz": "Mahsulotlarni qidirish..."
        },
        "Search...": {
            "en": "Search...",
            "ru": "Поиск...",
            "uz": "Qidirish..."
        },
        "Security": {
            "en": "Security",
            "ru": "Безопасность",
            "uz": "Xavfsizlik"
        },
        "Security Overview": {
            "en": "Security Overview",
            "ru": "Обзор безопасности",
            "uz": "Xavfsizlikga umumiy nazar"
        },
        "Security Settings": {
            "en": "Security Settings",
            "ru": "Настройки безопасности",
            "uz": "Xavfsizlik sozlamalari"
        },



        "Security settings saved successfully!": {
            "en": "Security settings saved successfully!",
            "uz": "Xavfsizlik sozlamalari muvaffaqiyatli saqlandi!",
            "ru": "Настройки безопасности успешно сохранены!"
        },
        "Select Day": {
            "en": "Select Day",
            "uz": "Kunni tanlang",
            "ru": "Выберите день"
        },
        "Select Gender": {
            "en": "Select Gender",
            "uz": "Jinsni tanlang",
            "ru": "Выберите пол"
        },
        "Select New Plan": {
            "en": "Select New Plan",
            "uz": "Yangi rejani tanlang",
            "ru": "Выберите новый план"
        },
        "Select Time": {
            "en": "Select Time",
            "uz": "Vaqtni tanlang",
            "ru": "Выберите время"
        },
        "Select the perfect plan for your water needs. All plans include free delivery, quality guarantee, and flexible scheduling.": {
            "en": "Select the perfect plan for your water needs. All plans include free delivery, quality guarantee, and flexible scheduling.",
            "uz": "Suv ehtiyojlaringiz uchun eng yaxshi rejani tanlang. Barcha rejalar bepul yetkazib berish, sifat kafolati va moslashuvchan jadvalni o'z ichiga oladi.",
            "ru": "Выберите идеальный план для ваших потребностей в воде. Все планы включают бесплатную доставку, гарантию качества и гибкий график."
        },
        "Send Message": {
            "en": "Send Message",
            "uz": "Xabar yuborish",
            "ru": "Отправить сообщение"
        },
        "Send Reset Link": {
            "en": "Send Reset Link",
            "uz": "Qayta tiklash havolasini yuborish",
            "ru": "Отправить ссылку для сброса"
        },
        "Send Verification Code": {
            "en": "Send Verification Code",
            "uz": "Tasdiqlash kodini yuborish",
            "ru": "Отправить код подтверждения"
        },
        "Send email notifications for new logins": {
            "en": "Send email notifications for new logins",
            "uz": "Yangi kirishlar uchun email xabarnoma yuborish",
            "ru": "Отправлять уведомления по email о новых входах"
        },
        "Send email notifications for password changes": {
            "en": "Send email notifications for password changes",
            "uz": "Parol o'zgarishlari uchun email xabarnoma yuborish",
            "ru": "Отправлять уведомления по email об изменении пароля"
        },
        "Send the command": {
            "en": "Send the command",
            "uz": "Buyruqni yuboring",
            "ru": "Отправить команду"
        },
        "Send us a message and we will get back to you as soon as possible": {
            "en": "Send us a message and we will get back to you as soon as possible",
            "uz": "Bizga xabar yuboring va biz imkon qadar tezroq sizga javob beramiz",
            "ru": "Отправьте нам сообщение, и мы свяжемся с вами как можно скорее"
        },
        "Sending...": {
            "en": "Sending...",
            "uz": "Yuborilmoqda...",
            "ru": "Отправка..."
        },
        "Services": {
            "en": "Services",
            "uz": "Xizmatlar",
            "ru": "Услуги"
        },
        "Set Default": {
            "en": "Set Default",
            "uz": "Standart qilib belgilash",
            "ru": "Установить по умолчанию"
        },
        "Set as default address": {
            "en": "Set as default address",
            "uz": "Standart manzil sifatida belgilash",
            "ru": "Установить как адрес по умолчанию"
        },
        "Share": {
            "en": "Share",
            "uz": "Ulashish",
            "ru": "Поделиться"
        },
        "Share this code with friends": {
            "en": "Share this code with friends",
            "uz": "Ushbu kodni do'stlaringiz bilan ulashing",
            "ru": "Поделитесь этим кодом с друзьями"
        },
        "Shipped": {
            "en": "Shipped",
            "uz": "Yuborildi",
            "ru": "Отправлено"
        },
        "Shop": {
            "en": "Shop",
            "uz": "Do'kon",
            "ru": "Магазин"
        },
        "Shop Now": {
            "en": "Shop Now",
            "uz": "Hozir xarid qilish",
            "ru": "Купить сейчас"
        },
        "Shop Products": {
            "en": "Shop Products",
            "uz": "Mahsulotlarni xarid qilish",
            "ru": "Товары магазина"
        },
        "Shopping Cart": {
            "en": "Shopping Cart",
            "uz": "Savat",
            "ru": "Корзина"
        },
        "Show Password": {
            "en": "Show Password",
            "uz": "Parolni ko'rsatish",
            "ru": "Показать пароль"
        },
        "Show passwords": {
            "en": "Show passwords",
            "uz": "Parollarni ko'rsatish",
            "ru": "Показать пароли"
        },
        "Silver": {
            "en": "Silver",
            "uz": "Kumush",
            "ru": "Серебряный"
        },
        "Size": {
            "en": "Size",
            "uz": "O'lcham",
            "ru": "Размер"
        },
        "Sort by Latest": {
            "en": "Sort by Latest",
            "uz": "Eng yangilari bo'yicha tartiblash",
            "ru": "Сортировать по новизне"
        },
        "Special delivery instructions (optional)": {
            "en": "Special delivery instructions (optional)",
            "uz": "Maxsus yetkazib berish ko'rsatmalari (ixtiyoriy)",
            "ru": "Особые инструкции для доставки (необязательно)"
        },
        "Standard Plan": {
            "en": "Standard Plan",
            "uz": "Standart reja",
            "ru": "Стандартный план"
        },
        "Standard shipping": {
            "en": "Standard shipping",
            "uz": "Standart yetkazib berish",
            "ru": "Стандартная доставка"
        },
        "Start Shopping": {
            "en": "Start Shopping",
            "uz": "Xarid qilishni boshlang",
            "ru": "Начать покупки"
        },
        "Start shopping to see your orders here": {
            "en": "Start shopping to see your orders here",
            "uz": "Buyurtmalaringizni bu yerda ko'rish uchun xarid qilishni boshlang",
            "ru": "Начните покупки, чтобы увидеть свои заказы здесь"
        },
        "Started": {
            "en": "Started",
            "uz": "Boshlanadi",
            "ru": "Начато"
        },
        "State-of-the-art purification": {
            "en": "State-of-the-art purification",
            "uz": "Zamonaviy tozalash",
            "ru": "Современная очистка"
        },
        "Status": {
            "en": "Status",
            "uz": "Holat",
            "ru": "Статус"
        },
        "Stay Updated with": {
            "en": "Stay Updated with",
            "uz": "Yangiliklardan xabardor bo'ling",
            "ru": "Будьте в курсе"
        },
        "Stay updated with our latest news, offers, and water delivery tips.": {
            "en": "Stay updated with our latest news, offers, and water delivery tips.",
            "uz": "So'nggi yangiliklar, takliflar va suv yetkazib berish bo'yicha maslahatlarimizdan xabardor bo'ling.",
            "ru": "Будьте в курсе наших последних новостей, предложений и советов по доставке воды."
        },
        "Storage Facility": {
            "en": "Storage Facility",
            "uz": "Omborxona",
            "ru": "Склад"
        },
        "Street Address": {
            "en": "Street Address",
            "uz": "Ko'cha manzili",
            "ru": "Улица и номер дома"
        },
        "Street address": {
            "en": "Street address",
            "uz": "Ko'cha manzili",
            "ru": "Адрес улицы"
        },
        "Strong": {
            "en": "Strong",
            "uz": "Kuchli",
            "ru": "Сильный"
        },
        "Subject": {
            "en": "Subject",
            "uz": "Mavzu",
            "ru": "Тема"
        },
        "Subscribe": {
            "en": "Subscribe",
            "uz": "Obuna bo'lish",
            "ru": "Подписаться"
        },
        "Subscribe Now": {
            "en": "Subscribe Now",
            "uz": "Hozir obuna bo'ling",
            "ru": "Подписаться сейчас"
        },
        "Subscribe to": {
            "en": "Subscribe to",
            "uz": "Obuna bo'lish",
            "ru": "Подписаться на"
        },
        "Subscribe to Plan": {
            "en": "Subscribe to Plan",
            "uz": "Rejaga obuna bo'lish",
            "ru": "Подписаться на план"
        },
        "Subscribe to our newsletter for updates and offers": {
            "en": "Subscribe to our newsletter for updates and offers",
            "uz": "Yangiliklar va takliflar uchun bizning xabarnomamizga obuna bo'ling",
            "ru": "Подпишитесь на нашу рассылку, чтобы получать обновления и предложения"
        },
        "Subscription FAQ": {
            "en": "Subscription FAQ",
            "uz": "Obuna bo'yicha tez-tez so'raladigan savollar",
            "ru": "Часто задаваемые вопросы по подписке"
        },
        "Subscription ID": {
            "en": "Subscription ID",
            "uz": "Obuna ID raqami",
            "ru": "ID подписки"
        },
        "Subscription Management": {
            "en": "Subscription Management",
            "uz": "Obunani boshqarish",
            "ru": "Управление подпиской"
        },
        "Subscription Plan": {
            "en": "Subscription Plan",
            "uz": "Obuna rejasi",
            "ru": "План подписки"
        },
        "Subscription Plans": {
            "en": "Subscription Plans",
            "uz": "Obuna rejalar",
            "ru": "Планы подписки"
        },
        "Subscription Plans -": {
            "en": "Subscription Plans -",
            "uz": "Obuna rejalar -",
            "ru": "Планы подписки -"
        },
        "Subscription cancelled successfully": {
            "en": "Subscription cancelled successfully",
            "uz": "Obuna muvaffaqiyatli bekor qilindi",
            "ru": "Подписка успешно отменена"
        },
        "Subscription created successfully! We will contact you to confirm delivery details.": {
            "en": "Subscription created successfully! We will contact you to confirm delivery details.",
            "uz": "Obuna muvaffaqiyatli yaratildi! Yetkazib berish tafsilotlarini tasdiqlash uchun siz bilan bog'lanamiz.",
            "ru": "Подписка успешно создана! Мы свяжемся с вами для подтверждения деталей доставки."
        },
        "Subscription paused successfully": {
            "en": "Subscription paused successfully",
            "uz": "Obuna muvaffaqiyatli to'xtatildi",
            "ru": "Подписка успешно приостановлена"
        },
        "Subscription resumed successfully": {
            "en": "Subscription resumed successfully",
            "uz": "Obuna muvaffaqiyatli davom ettirildi",
            "ru": "Подписка успешно возобновлена"
        },
        "Subscription updated successfully!": {
            "en": "Subscription updated successfully!",
            "uz": "Obuna muvaffaqiyatli yangilandi!",
            "ru": "Подписка успешно обновлена!"
        },
        "Subscriptions": {
            "en": "Subscriptions",
            "uz": "Obunalar",
            "ru": "Подписки"
        },
        "Subtotal": {
            "en": "Subtotal",
            "uz": "Oraliq summa",
            "ru": "Промежуточный итог"
        },



        "Successful login from web browser": {
            "en": "Successful login from web browser",
            "ru": "Успешный вход через веб-браузер",
            "uz": "Veb-brauzer orqali muvaffaqiyatli kirildi"
        },
        "Sync Error": {
            "en": "Sync Error",
            "ru": "Ошибка синхронизации",
            "uz": "Sinxronizatsiya xatosi"
        },
        "Synchronized": {
            "en": "Synchronized",
            "ru": "Синхронизировано",
            "uz": "Sinxronlashtirildi"
        },
        "Syncing...": {
            "en": "Syncing...",
            "ru": "Синхронизация...",
            "uz": "Sinxronizatsiya qilinmoqda..."
        },
        "Tashkent, Uzbekistan": {
            "en": "Tashkent, Uzbekistan",
            "ru": "Ташкент, Узбекистан",
            "uz": "Toshkent, O'zbekiston"
        },
        "Tax": {
            "en": "Tax",
            "ru": "Налог",
            "uz": "Soliq"
        },
        "Telegram Account Linking": {
            "en": "Telegram Account Linking",
            "ru": "Привязка аккаунта Telegram",
            "uz": "Telegram hisobini bog'lash"
        },
        "Telegram Bot": {
            "en": "Telegram Bot",
            "ru": "Телеграм-бот",
            "uz": "Telegram bot"
        },
        "Telegram Linking Code": {
            "en": "Telegram Linking Code",
            "ru": "Код привязки Telegram",
            "uz": "Telegram bog'lash kodi"
        },
        "Telegram connection feature coming soon!": {
            "en": "Telegram connection feature coming soon!",
            "ru": "Функция подключения Telegram скоро появится!",
            "uz": "Telegram ulash funksiyasi tez orada qo'shiladi!"
        },
        "Terms & Conditions": {
            "en": "Terms & Conditions",
            "ru": "Условия и положения",
            "uz": "Shartlar va qoidalar"
        },
        "Terms of Service": {
            "en": "Terms of Service",
            "ru": "Условия обслуживания",
            "uz": "Xizmat ko'rsatish shartlari"
        },
        "The Importance of Staying Hydrated During Winter": {
            "en": "The Importance of Staying Hydrated During Winter",
            "ru": "Важность поддержания водного баланса зимой",
            "uz": "Qishda suv ichishni unutmaslikning ahamiyati"
        },
        "The verification code is in your email": {
            "en": "The verification code is in your email",
            "ru": "Код подтверждения в вашей электронной почте",
            "uz": "Tasdiqlash kodi emailingizda"
        },
        "This is a business address": {
            "en": "This is a business address",
            "ru": "Это рабочий адрес",
            "uz": "Bu ish manzili"
        },
        "Thursday": {
            "en": "Thursday",
            "ru": "Четверг",
            "uz": "Payshanba"
        },
        "Tier": {
            "en": "Tier",
            "ru": "Уровень",
            "uz": "Daraja"
        },
        "Time Period": {
            "en": "Time Period",
            "ru": "Период времени",
            "uz": "Vaqt oralig'i"
        },
        "To link your Telegram account": {
            "en": "To link your Telegram account",
            "ru": "Чтобы привязать ваш Telegram-аккаунт",
            "uz": "Telegram hisobingizni bog'lash uchun"
        },
        "Total": {
            "en": "Total",
            "ru": "Итого",
            "uz": "Jami"
        },
        "Total Orders": {
            "en": "Total Orders",
            "ru": "Всего заказов",
            "uz": "Jami buyurtmalar"
        },
        "Track Package": {
            "en": "Track Package",
            "ru": "Отслеживать посылку",
            "uz": "Posilkani kuzatish"
        },
        "Tracking": {
            "en": "Tracking",
            "ru": "Отслеживание",
            "uz": "Kuzatuv"
        },
        "Tracking Number": {
            "en": "Tracking Number",
            "ru": "Номер отслеживания",
            "uz": "Kuzatuv raqami"
        },
        "Treatment Facility": {
            "en": "Treatment Facility",
            "ru": "Очисное сооружение",
            "uz": "Tozalash inshooti"
        },
        "Trusted Name In Bottled Water Industry": {
            "en": "Trusted Name In Bottled Water Industry",
            "ru": "Надёжное имя в индустрии бутилированной воды",
            "uz": "Butillangan suv sanoatida ishonchli nom"
        },
        "Tuesday": {
            "en": "Tuesday",
            "ru": "Вторник",
            "uz": "Seshanba"
        },
        "Two-Factor Authentication": {
            "en": "Two-Factor Authentication",
            "ru": "Двухфакторная аутентификация",
            "uz": "Ikki bosqichli autentifikatsiya"
        },
        "Two-factor authentication setup coming soon!": {
            "en": "Two-factor authentication setup coming soon!",
            "ru": "Настройка двухфакторной аутентификации скоро появится!",
            "uz": "Ikki bosqichli autentifikatsiya sozlamasi tez orada qo'shiladi!"
        },
        "Understanding water purity standards and what makes Blue Stream water exceptional for your health.": {
            "en": "Understanding water purity standards and what makes Blue Stream water exceptional for your health.",
            "ru": "Понимание стандартов чистоты воды и то, что делает воду Blue Stream исключительной для вашего здоровья.",
            "uz": "Suv tozaligi standartlarini tushunish va Blue Stream suvini sog'liq uchun noyob qiladigan jihatlar."
        },
        "Upcoming Deliveries": {
            "en": "Upcoming Deliveries",
            "ru": "Предстоящие доставки",
            "uz": "Kelgusi yetkazib berishlar"
        },
        "Update Cart": {
            "en": "Update Cart",
            "ru": "Обновить корзину",
            "uz": "Savatni yangilash"
        },



        "Update Contact Info": {
            "en": "Update Contact Info",
            "ru": "Обновить контактную информацию",
            "uz": "Aloqa ma'lumotlarini yangilash"
        },
        "Update Subscription": {
            "en": "Update Subscription",
            "ru": "Обновить подписку",
            "uz": "Obunani yangilash"
        },
        "Upgrade or downgrade plans anytime": {
            "en": "Upgrade or downgrade plans anytime",
            "ru": "Обновляйте или понижайте тариф в любое время",
            "uz": "Tarif rejalarini istalgan vaqtda oshiring yoki tushiring"
        },
        "Urgent": {
            "en": "Urgent",
            "ru": "Срочно",
            "uz": "Shoshilinch"
        },
        "Use my referral code": {
            "en": "Use my referral code",
            "ru": "Используйте мой реферальный код",
            "uz": "Mening referal kodimdan foydalaning"
        },
        "Use the linking code that will be provided": {
            "en": "Use the linking code that will be provided",
            "ru": "Используйте предоставленный код привязки",
            "uz": "Beriladigan ulash kodidan foydalaning"
        },
        "Useful Links": {
            "en": "Useful Links",
            "ru": "Полезные ссылки",
            "uz": "Foydali havolalar"
        },
        "VIP customer support": {
            "en": "VIP customer support",
            "ru": "VIP поддержка клиентов",
            "uz": "VIP mijozlarni qo'llab-quvvatlash"
        },
        "VIP events access": {
            "en": "VIP events access",
            "ru": "Доступ к VIP-мероприятиям",
            "uz": "VIP tadbirlarga kirish"
        },
        "Various sizes available": {
            "en": "Various sizes available",
            "ru": "Доступны различные размеры",
            "uz": "Turli o'lchamlar mavjud"
        },
        "Verification code expired. Please request a new one.": {
            "en": "Verification code expired. Please request a new one.",
            "ru": "Срок действия кода подтверждения истек. Пожалуйста, запросите новый.",
            "uz": "Tasdiqlash kodi muddati tugagan. Iltimos, yangisini so'rang."
        },
        "Verification code resent successfully!": {
            "en": "Verification code resent successfully!",
            "ru": "Код подтверждения успешно отправлен повторно!",
            "uz": "Tasdiqlash kodi muvaffaqiyatli qayta yuborildi!"
        },
        "Verification code sent successfully!": {
            "en": "Verification code sent successfully!",
            "ru": "Код подтверждения успешно отправлен!",
            "uz": "Tasdiqlash kodi muvaffaqiyatli yuborildi!"
        },
        "Verification email sent successfully! Please check your inbox.": {
            "en": "Verification email sent successfully! Please check your inbox.",
            "ru": "Письмо с подтверждением успешно отправлено! Пожалуйста, проверьте вашу почту.",
            "uz": "Tasdiqlash xati muvaffaqiyatli yuborildi! Iltimos, pochtangizni tekshiring."
        },
        "Verification email sent! Please check your inbox.": {
            "en": "Verification email sent! Please check your inbox.",
            "ru": "Письмо с подтверждением отправлено! Пожалуйста, проверьте вашу почту.",
            "uz": "Tasdiqlash xati yuborildi! Iltimos, pochtangizni tekshiring."
        },
        "Verified": {
            "en": "Verified",
            "ru": "Подтверждено",
            "uz": "Tasdiqlangan"
        },
        "Verify": {
            "en": "Verify",
            "ru": "Подтвердить",
            "uz": "Tasdiqlash"
        },
        "Verify Code": {
            "en": "Verify Code",
            "ru": "Подтвердить код",
            "uz": "Kod tasdiqlash"
        },



        "Verify Email": {
            "en": "Verify Email",
            "ru": "Подтвердить Email",
            "uz": "Emailni tasdiqlash"
        },
        "Verify Email Address": {
            "en": "Verify Email Address",
            "ru": "Подтвердить адрес электронной почты",
            "uz": "Email manzilini tasdiqlash"
        },
        "Verify Phone": {
            "en": "Verify Phone",
            "ru": "Подтвердить телефон",
            "uz": "Telefonni tasdiqlash"
        },
        "Verify Phone First": {
            "en": "Verify Phone First",
            "ru": "Сначала подтвердите телефон",
            "uz": "Avval telefonni tasdiqlang"
        },
        "Verify Phone Number": {
            "en": "Verify Phone Number",
            "ru": "Подтвердить номер телефона",
            "uz": "Telefon raqamini tasdiqlash"
        },
        "Verify Your Email Address": {
            "en": "Verify Your Email Address",
            "ru": "Подтвердите ваш адрес электронной почты",
            "uz": "Email manzilingizni tasdiqlang"
        },
        "Verify Your Phone Number": {
            "en": "Verify Your Phone Number",
            "ru": "Подтвердите ваш номер телефона",
            "uz": "Telefon raqamingizni tasdiqlang"
        },
        "Very Strong": {
            "en": "Very Strong",
            "ru": "Очень сильный",
            "uz": "Juda kuchli"
        },
        "View": {
            "en": "View",
            "ru": "Просмотр",
            "uz": "Ko'rish"
        },
        "View All": {
            "en": "View All",
            "ru": "Посмотреть все",
            "uz": "Barchasini ko'rish"
        },
        "View All Products": {
            "en": "View All Products",
            "ru": "Посмотреть все продукты",
            "uz": "Barcha mahsulotlarni ko'rish"
        },
        "View Details": {
            "en": "View Details",
            "ru": "Посмотреть детали",
            "uz": "Batafsil ko'rish"
        },
        "View Plans": {
            "en": "View Plans",
            "ru": "Посмотреть планы",
            "uz": "Rejalarni ko'rish"
        },
        "View Rewards": {
            "en": "View Rewards",
            "ru": "Посмотреть награды",
            "uz": "Mukofotlarni ko'rish"
        },
        "Volume discounts available": {
            "en": "Volume discounts available",
            "ru": "Доступны оптовые скидки",
            "uz": "Ulgurji chegirmalar mavjud"
        },
        "Warehouse Operations": {
            "en": "Warehouse Operations",
            "ru": "Складские операции",
            "uz": "Omborxona ishlari"
        },
        "Water Benefits": {
            "en": "Water Benefits",
            "ru": "Польза воды",
            "uz": "Suvning foydalari"
        },
        "Water Benefits & Health Tips": {
            "en": "Water Benefits & Health Tips",
            "ru": "Польза воды и советы по здоровью",
            "uz": "Suv foydalari va sog'liq bo'yicha maslahatlar"
        },
        "Water Cooler Installation": {
            "en": "Water Cooler Installation",
            "ru": "Установка кулера для воды",
            "uz": "Suv sovutkichini o'rnatish"
        },
        "Water Delivery": {
            "en": "Water Delivery",
            "ru": "Доставка воды",
            "uz": "Suv yetkazib berish"
        },
        "Water Delivery News": {
            "en": "Water Delivery News",
            "ru": "Новости доставки воды",
            "uz": "Suv yetkazib berish yangiliklari"
        },
        "Water Delivery Service": {
            "en": "Water Delivery Service",
            "ru": "Служба доставки воды",
            "uz": "Suv yetkazib berish xizmati"
        },
        "Water Delivery Subscription Plans": {
            "en": "Water Delivery Subscription Plans",
            "ru": "Тарифные планы на доставку воды",
            "uz": "Suv yetkazib berish obuna rejalar"
        },
        "Water Products": {
            "en": "Water Products",
            "ru": "Водная продукция",
            "uz": "Suv mahsulotlari"
        },
        "Water Quality": {
            "en": "Water Quality",
            "ru": "Качество воды",
            "uz": "Suv sifati"
        },
        "Water Quality & Services Gallery": {
            "en": "Water Quality & Services Gallery",
            "ru": "Галерея качества воды и услуг",
            "uz": "Suv sifati va xizmatlar galereyasi"
        },
        "Water Quality Standards": {
            "en": "Water Quality Standards",
            "ru": "Стандарты качества воды",
            "uz": "Suv sifati standartlari"
        },
        "Water Quality Testing": {
            "en": "Water Quality Testing",
            "ru": "Тестирование качества воды",
            "uz": "Suv sifati sinovi"
        },
        "Water Treatment Facility": {
            "en": "Water Treatment Facility",
            "ru": "Очистное сооружение",
            "uz": "Suv tozalash inshooti"
        },
        "Ways to Earn Points": {
            "en": "Ways to Earn Points",
            "ru": "Способы заработать баллы",
            "uz": "Ballarni yig'ish yo'llari"
        },
        "We Always Want Safe and Healthy Water for Healthy Life": {
            "en": "We Always Want Safe and Healthy Water for Healthy Life",
            "ru": "Мы всегда хотим безопасную и здоровую воду для здоровой жизни",
            "uz": "Sog'lom hayot uchun biz doimo xavfsiz va toza suvni xohlaymiz"
        },
        "We Deliver Best Quality": {
            "en": "We Deliver Best Quality",
            "ru": "Мы доставляем лучшее качество",
            "uz": "Biz eng sifatli mahsulotni yetkazib beramiz"
        },
        "We accept all major credit cards, bank transfers, and automatic monthly billing. Business accounts can also use invoice billing with net 30 terms after credit approval.": {
            "en": "We accept all major credit cards, bank transfers, and automatic monthly billing. Business accounts can also use invoice billing with net 30 terms after credit approval.",
            "ru": "Мы принимаем все основные кредитные карты, банковские переводы и автоматическое ежемесячное выставление счетов. Бизнес-аккаунты также могут использовать оплату по счету с отсрочкой 30 дней после одобрения кредита.",
            "uz": "Biz barcha yirik kredit kartalarini, bank o'tkazmalarini va avtomatik oylik to'lovlarni qabul qilamiz. Biznes hisoblari kredit tasdiqlangandan so'ng 30 kunlik muddat bilan hisob-fakturani ishlatishi mumkin."
        },
        "We are excited to announce the expansion of our delivery network to serve more communities across Uzbekistan with premium quality water.": {
            "en": "We are excited to announce the expansion of our delivery network to serve more communities across Uzbekistan with premium quality water.",
            "ru": "Мы рады объявить о расширении нашей сети доставки, чтобы обслуживать больше сообществ по всему Узбекистану с премиальной водой.",
            "uz": "Biz O'zbekiston bo'ylab ko'proq hududlarni qamrab olish uchun yetkazib berish tarmog'imiz kengayganini e'lon qilishdan mamnunmiz."
        },
        "We provide comprehensive water solutions for homes and businesses": {
            "en": "We provide comprehensive water solutions for homes and businesses",
            "ru": "Мы предоставляем комплексные решения по водоснабжению для домов и бизнеса",
            "uz": "Biz uylar va biznes uchun kompleks suv yechimlarini taqdim etamiz"
        },
        "We provide our services across Uzbekistan with a network of professional delivery partners ensuring the highest quality standards.": {
            "en": "We provide our services across Uzbekistan with a network of professional delivery partners ensuring the highest quality standards.",
            "ru": "Мы предоставляем наши услуги по всему Узбекистану через сеть профессиональных партнеров по доставке, обеспечивая наивысшие стандарты качества.",
            "uz": "Biz butun O'zbekiston bo'ylab professional yetkazib berish hamkorlari tarmog'i orqali xizmatlarimizni ko'rsatamiz va yuqori sifat standartlarini ta'minlaymiz."
        },
        "We provide our services across Uzbekistan with a network of professional delivery partners. Experience fast, reliable delivery within 2 hours anywhere in the city.": {
            "en": "We provide our services across Uzbekistan with a network of professional delivery partners. Experience fast, reliable delivery within 2 hours anywhere in the city.",
            "ru": "Мы предоставляем услуги по всему Узбекистану с сетью профессиональных партнеров. Ощутите быструю и надежную доставку в течение 2 часов в любой точке города.",
            "uz": "Biz butun O'zbekiston bo'ylab professional yetkazib beruvchilar tarmog'i bilan xizmat ko'rsatamiz. Shaharning istalgan joyiga 2 soat ichida tez va ishonchli yetkazib berishni sinab ko'ring."
        },



        "We sent a verification code to": {
            "en": "We sent a verification code to",
            "ru": "Мы отправили код подтверждения на",
            "uz": "Tasdiqlash kodi yuborildi"
        },
        "We sent a verification link to your email address. You can either click the link in your email or enter the verification code below.": {
            "en": "We sent a verification link to your email address. You can either click the link in your email or enter the verification code below.",
            "ru": "Мы отправили ссылку для подтверждения на ваш адрес электронной почты. Вы можете либо перейти по ссылке в письме, либо ввести код подтверждения ниже.",
            "uz": "Tasdiqlash havolasi emailingizga yuborildi. Siz emaildagi havolaga bosishingiz yoki quyida tasdiqlash kodini kiritishingiz mumkin."
        },
        "We use state-of-the-art filtration technology and maintain the highest standards of hygiene and safety in our production and delivery processes.": {
            "en": "We use state-of-the-art filtration technology and maintain the highest standards of hygiene and safety in our production and delivery processes.",
            "ru": "Мы используем передовые технологии фильтрации и поддерживаем самые высокие стандарты гигиены и безопасности в процессе производства и доставки.",
            "uz": "Biz eng zamonaviy filtrlash texnologiyasidan foydalanamiz va ishlab chiqarish hamda yetkazib berish jarayonlarida gigiyena va xavfsizlikning eng yuqori standartlariga rioya qilamiz."
        },
        "We will attempt to reschedule your delivery within 24-48 hours at no extra charge. Our drivers will leave a note if you are not available. You can also reschedule deliveries through your online account.": {
            "en": "We will attempt to reschedule your delivery within 24-48 hours at no extra charge. Our drivers will leave a note if you are not available. You can also reschedule deliveries through your online account.",
            "ru": "Мы попытаемся перенести вашу доставку в течение 24-48 часов без дополнительной платы. Наши водители оставят уведомление, если вы будете недоступны. Вы также можете перенести доставку через свой онлайн-аккаунт.",
            "uz": "Biz sizning yetkazib berishingizni 24-48 soat ichida bepul qayta rejalashtirishga harakat qilamiz. Agar siz mavjud bo'lmasangiz, haydovchilarimiz eslatma qoldirishadi. Yetkazib berishni onlayn akkauntingiz orqali ham qayta rejalashtirishingiz mumkin."
        },
        "Weak": {
            "en": "Weak",
            "ru": "Слабый",
            "uz": "Kuchsiz"
        },
        "Web App": {
            "en": "Web App",
            "ru": "Веб-приложение",
            "uz": "Veb ilova"
        },
        "Wednesday": {
            "en": "Wednesday",
            "ru": "Среда",
            "uz": "Chorshanba"
        },
        "Weekly": {
            "en": "Weekly",
            "ru": "Еженедельно",
            "uz": "Haftalik"
        },
        "Weekly delivery schedule": {
            "en": "Weekly delivery schedule",
            "ru": "Еженедельное расписание доставки",
            "uz": "Haftalik yetkazib berish jadvali"
        },
        "Welcome Back": {
            "en": "Welcome Back",
            "ru": "С возвращением",
            "uz": "Xush kelibsiz"
        },
        "Welcome back": {
            "en": "Welcome back",
            "ru": "С возвращением",
            "uz": "Xush kelibsiz"
        },
        "Wellness Guide": {
            "en": "Wellness Guide",
            "ru": "Руководство по здоровью",
            "uz": "Salomatlik qoʻllanmasi"
        },
        "What Our Customers are Saying": {
            "en": "What Our Customers are Saying",
            "ru": "Что говорят наши клиенты",
            "uz": "Mijozlarimiz nima deyishmoqda"
        },


        "What happens if I miss a delivery?": {
            "en": "What happens if I miss a delivery?",
            "ru": "Что произойдет, если я пропущу доставку?",
            "uz": "Agar men yetkazib berishni o'tkazib yuborsam nima bo'ladi?"
        },
        "What if I need to pause my subscription?": {
            "en": "What if I need to pause my subscription?",
            "ru": "Что делать, если мне нужно приостановить подписку?",
            "uz": "Agar men obunamni vaqtincha to'xtatishim kerak bo'lsa, nima qilaman?"
        },
        "What payment methods do you accept?": {
            "en": "What payment methods do you accept?",
            "ru": "Какие способы оплаты вы принимаете?",
            "uz": "Qaysi to'lov usullarini qabul qilasiz?"
        },
        "Why Choose Our Subscription Service?": {
            "en": "Why Choose Our Subscription Service?",
            "ru": "Почему стоит выбрать нашу подписку?",
            "uz": "Nega bizning obuna xizmatimizni tanlash kerak?"
        },
        "Winter weather can lead to dehydration. Learn why maintaining proper hydration is crucial during colder months for optimal health.": {
            "en": "Winter weather can lead to dehydration. Learn why maintaining proper hydration is crucial during colder months for optimal health.",
            "ru": "Зимняя погода может привести к обезвоживанию. Узнайте, почему поддержание правильного водного баланса важно для здоровья в холодное время года.",
            "uz": "Qishki ob-havo suvsizlanishga olib kelishi mumkin. Sovuq oylarida to'g'ri namlikni saqlash nega muhimligini bilib oling."
        },
        "With state-of-the-art filtration technology and a customer-first approach, we have become the trusted choice for thousands of families and businesses seeking reliable water solutions.": {
            "en": "With state-of-the-art filtration technology and a customer-first approach, we have become the trusted choice for thousands of families and businesses seeking reliable water solutions.",
            "ru": "Благодаря современным технологиям фильтрации и клиентоориентированному подходу мы стали надежным выбором для тысяч семей и компаний, ищущих надежные решения для воды.",
            "uz": "Zamonaviy filtrlash texnologiyalari va mijozlarga yo'naltirilgan yondashuvimiz tufayli biz minglab oilalar va bizneslar uchun ishonchli suv yechimlarida ishonchli tanlovga aylandik."
        },
        "Would you like to verify your new phone number now?": {
            "en": "Would you like to verify your new phone number now?",
            "ru": "Хотите подтвердить свой новый номер телефона сейчас?",
            "uz": "Yangi telefon raqamingizni hozir tasdiqlamoqchimisiz?"
        },
        "Write Reviews": {
            "en": "Write Reviews",
            "ru": "Написать отзывы",
            "uz": "Sharh yozish"
        },
        "Yearly": {
            "en": "Yearly",
            "ru": "Ежегодно",
            "uz": "Yiliga"
        },
        "Years Experience": {
            "en": "Years Experience",
            "ru": "Годы опыта",
            "uz": "Yillik tajriba"
        },
        "Yes, you can upgrade or downgrade your subscription plan at any time. Changes will take effect with your next billing cycle. Contact our customer service team or manage your subscription through your online account.": {
            "en": "Yes, you can upgrade or downgrade your subscription plan at any time. Changes will take effect with your next billing cycle. Contact our customer service team or manage your subscription through your online account.",
            "ru": "Да, вы можете изменить свой тарифный план в любое время. Изменения вступят в силу в следующем расчетном периоде. Свяжитесь с нашей службой поддержки или управляйте подпиской через свой онлайн-кабинет.",
            "uz": "Ha, siz obuna rejangizni istalgan vaqtda oshirishingiz yoki pasaytirishingiz mumkin. O'zgarishlar keyingi hisob-kitob davrida kuchga kiradi. Mijozlarga xizmat ko'rsatish jamoamiz bilan bog'laning yoki obunangizni onlayn hisobingiz orqali boshqaring."
        },
        "You can pause your subscription for up to 3 months for vacations or other reasons. Simply log into your account or call our customer service team at least 48 hours before your next scheduled delivery.": {
            "en": "You can pause your subscription for up to 3 months for vacations or other reasons. Simply log into your account or call our customer service team at least 48 hours before your next scheduled delivery.",
            "ru": "Вы можете приостановить подписку на срок до 3 месяцев по причине отпуска или других обстоятельств. Просто войдите в свой аккаунт или позвоните в нашу службу поддержки не позднее чем за 48 часов до следующей запланированной доставки.",
            "uz": "Siz ta'til yoki boshqa sabablarga ko'ra obunangizni 3 oygacha to'xtatishingiz mumkin. Keyingi rejalashtirilgan yetkazib berishdan kamida 48 soat oldin hisobingizga kiring yoki mijozlarga xizmat ko'rsatish jamoamizga qo'ng'iroq qiling."
        },
        "You dont have any active subscriptions": {
            "en": "You don't have any active subscriptions",
            "ru": "У вас нет активных подписок",
            "uz": "Sizda faol obunalar mavjud emas"
        },
        "You havent placed any orders yet or no orders match your filters": {
            "en": "You haven't placed any orders yet or no orders match your filters",
            "ru": "Вы еще не сделали заказ или ни один заказ не соответствует вашим фильтрам",
            "uz": "Siz hali buyurtma qilmagansiz yoki hech qanday buyurtma filtrlaringizga mos kelmaydi"
        },
        "You need to verify your phone number before enabling 2FA. Go to phone verification?": {
            "en": "You need to verify your phone number before enabling 2FA. Go to phone verification?",
            "ru": "Вам нужно подтвердить свой номер телефона, прежде чем включить двухфакторную аутентификацию. Перейти к подтверждению номера?",
            "uz": "Ikki faktorli autentifikatsiyani yoqishdan oldin telefon raqamingizni tasdiqlashingiz kerak. Telefonni tasdiqlashga o'tasizmi?"
        },
        "Your Email": {
            "en": "Your Email",
            "ru": "Ваш email",
            "uz": "Sizning emailingiz"
        },
        "Your Loyalty Status": {
            "en": "Your Loyalty Status",
            "ru": "Ваш статус лояльности",
            "uz": "Sodiqlik holatingiz"
        },
        "Your Message": {
            "en": "Your Message",
            "ru": "Ваше сообщение",
            "uz": "Sizning xabaringiz"
        },
        "Your Name": {
            "en": "Your Name",
            "ru": "Ваше имя",
            "uz": "Ismingiz"
        },
        "Your Points": {
            "en": "Your Points",
            "ru": "Ваши баллы",
            "uz": "Sizning ballaringiz"
        },
        "Your Referral Code": {
            "en": "Your Referral Code",
            "ru": "Ваш реферальный код",
            "uz": "Sizning referal kodingiz"
        },
        "Your account has been created successfully!": {
            "en": "Your account has been created successfully!",
            "ru": "Ваш аккаунт успешно создан!",
            "uz": "Hisobingiz muvaffaqiyatli yaratildi!"
        },
        "Your cart is empty": {
            "en": "Your cart is empty",
            "ru": "Ваша корзина пуста",
            "uz": "Savatingiz bo'sh"
        },
        "Your password has been reset successfully! You can now log in with your new password.": {
            "en": "Your password has been reset successfully! You can now log in with your new password.",
            "ru": "Ваш пароль был успешно сброшен! Теперь вы можете войти с новым паролем.",
            "uz": "Parolingiz muvaffaqiyatli tiklandi! Endi yangi parol bilan tizimga kira olasiz."
        },
        "Your session has expired. Please login again.": {
            "en": "Your session has expired. Please login again.",
            "ru": "Ваша сессия истекла. Пожалуйста, войдите снова.",
            "uz": "Sessiyangiz tugadi. Iltimos, qayta kiring."
        },
        "and": {
            "en": "and",
            "ru": "и",
            "uz": "va"
        },
        "bottles": {
            "en": "bottles",
            "ru": "бутылки",
            "uz": "butilkalar"
        },
        "e.g., Home, Office, etc.": {
            "en": "e.g., Home, Office, etc.",
            "ru": "например, Дом, Офис и т.д.",
            "uz": "masalan, Uy, Ofis va boshqalar"
        },
        "month": {
            "en": "month",
            "ru": "месяц",
            "uz": "oy"
        },
        "more items": {
            "en": "more items",
            "ru": "больше товаров",
            "uz": "ko'proq mahsulot"
        },
        "points": {
            "en": "points",
            "ru": "баллы",
            "uz": "ballar"
        },
        "points to next tier": {
            "en": "points to next tier",
            "ru": "баллов до следующего уровня",
            "uz": "keyingi darajaga qolgan ballar"
        },
        "pts": {
            "en": "pts",
            "ru": "очков",
            "uz": "ball"
        },
        "this month": {
            "en": "this month",
            "ru": "этот месяц",
            "uz": "shu oy"
        },
        "to Take an Extraordinary Service": {
            "en": "to Take an Extraordinary Service",
            "ru": "получить исключительный сервис",
            "uz": "ajoyib xizmat olish uchun"
        }
    }


    added_count = 0
    updated_count = 0

    for key, languages in ESSENTIAL_TRANSLATIONS.items():
        for lang, value in languages.items():
            try:
                existing = Translation.query.filter_by(key=key, language=lang).first()

                if existing:
                    if existing.value != value:
                        existing.value = value
                        existing.updated_at = datetime.now(UTC)
                        updated_count += 1
                        print(f"  Updated: {key} [{lang}]")
                else:
                    new_trans = Translation(
                        key=key,
                        language=lang,
                        value=value,
                        category='essential',
                        is_active=True,
                        created_at=datetime.now(UTC),
                        updated_at=datetime.now(UTC)
                    )
                    db.session.add(new_trans)
                    added_count += 1
                    print(f"  Added: {key} [{lang}]")

            except Exception as e:
                print(f"  Error with {key}[{lang}]: {e}")


    for key, languages in TEMPLATE_TRANSLATIONS.items():
        if key in ESSENTIAL_TRANSLATIONS:
            continue
        for lang, value in languages.items():
            try:
                existing = Translation.query.filter_by(key=key, language=lang).first()

                if existing:
                    if existing.value != value:
                        existing.value = value
                        existing.updated_at = datetime.now(UTC)
                        updated_count += 1
                        print(f"  Updated: {key} [{lang}]")
                else:
                    new_trans = Translation(
                        key=key,
                        language=lang,
                        value=value,
                        category='template',
                        is_active=True,
                        created_at=datetime.now(UTC),
                        updated_at=datetime.now(UTC)
                    )
                    db.session.add(new_trans)
                    added_count += 1
                    print(f"  Added: {key} [{lang}]")

            except Exception as e:
                print(f"  Error with {key}[{lang}]: {e}")

    print(f"Translation seeding complete: {added_count} added, {updated_count} updated")
    return added_count + updated_count > 0


def verify_seeded_data():
    """Verify that seeded data was created correctly"""
    print("\\nVerifying seeded data...")

    total_translations = Translation.query.count()
    print(f"Total translations: {total_translations}")

    for language in ['en', 'uz', 'ru']:
        count = Translation.query.filter_by(language=language, is_active=True).count()
        print(f"  {language}: {count} translations")

    # Test critical translations
    critical_tests = ['Home', 'Shop', 'Login', 'My Account']
    print("\\nTesting critical translations:")

    all_good = True
    for key in critical_tests:
        print(f"  {key}:")
        for lang in ['en', 'uz', 'ru']:
            translation = Translation.query.filter_by(key=key, language=lang, is_active=True).first()
            if translation:
                print(f"    {lang}: OK - {translation.value}")
            else:
                print(f"    {lang}: MISSING")
                all_good = False

    return all_good


def main():
    """Main seeding function"""
    print("DATABASE SEEDING STARTED")
    print("========================")
    print(f"Timestamp: {datetime.now(UTC).isoformat()}")
    print()

    app = create_app()

    with app.app_context():
        try:
            # Seed translations
            translations_changed = seed_essential_translations()

            # Commit changes
            if translations_changed:
                db.session.commit()
                print("All changes committed to database")
            else:
                print("No changes needed - data already up to date")

            # Verify
            verification_passed = verify_seeded_data()

            print("\\n========================")
            if verification_passed:
                print("DATABASE SEEDING COMPLETED SUCCESSFULLY!")
                print("\\nWhat was seeded:")
                print("  - Essential UI translations")
                print("  - All 3 languages (en, uz, ru)")
                print("  - Navigation and interface text")
            else:
                print("SEEDING COMPLETED WITH WARNINGS")
                print("Some translations may be missing.")

        except Exception as e:
            print(f"\\nSEEDING FAILED: {e}")
            import traceback
            traceback.print_exc()
            db.session.rollback()
            sys.exit(1)


if __name__ == '__main__':
    main()
