"""Seed the website shell's strings (redesign step 2: header, drawer, footer, floating links, toasts, 404).

The pattern every redesign group's seed follows:
- one self-contained script per group, `scripts/seed_web_ui_<group>_translations.py`;
- keys under the group's own namespace (`web.shell.*`, `web.toast.*`, `web.error.*` here), never bare English;
- en / ru / uz for every key: natural Russian, native Uzbek Latin with ‘ in o‘/g‘ and ’ for the tutuq belgisi;
- customer copy in perk tone, never telling the customer to phone (the Telegram bot is the channel);
- `Translation.bulk_create_or_update` inserts the missing rows, updates only the keys listed here, and drops their
  Redis cache entries, so a re-run is safe and a changed value shows at once.

Run on the dev database (scripts/ is not mounted in the container, so pipe it in):
    docker compose exec -T business_app python - < scripts/seed_web_ui_shell_translations.py
"""

from business_app import create_app, db
from business_app.models.translation import Translation

CATEGORY = "web"

TRANSLATIONS = {
    "en": {
        "web.shell.skip_to_content": "Skip to content",
        "web.shell.nav.label": "Main menu",
        "web.shell.nav.categories": "Shop categories",
        "web.shell.nav.more": "More",
        "web.shell.nav.blog": "Blog",
        "web.shell.nav.faq": "FAQ",
        "web.shell.search": "Search",
        "web.shell.language": "Language",
        "web.shell.cart": "Cart",
        "web.shell.menu": "Menu",
        "web.shell.menu_open": "Open menu",
        "web.shell.menu_close": "Close menu",
        "web.shell.account": "Account",
        "web.shell.close": "Close",
        "web.shell.back_to_top": "Back to top",
        "web.shell.social.instagram": "Instagram",
        "web.shell.social.channel": "Telegram channel",
        "web.shell.social.bot": "Telegram bot",
        "web.shell.footer.company": "Company",
        "web.shell.footer.help": "Help",
        "web.shell.footer.follow": "Follow us",
        "web.shell.footer.follow_text": "News and offers reach our Telegram channel first.",
        "web.shell.footer.filtration": "11-step filtration",
        "web.shell.footer.delivery_policy": "Delivery policy",
        "web.shell.footer.pricing_policy": "Pricing policy",
        "web.shell.footer.refund_policy": "Refund policy",
        "web.shell.footer.quality": "Quality standards",
        "web.shell.footer.faq": "Water delivery FAQ",
        # {hours}: COMPANY_OPENING_HOURS as the visitor reads it, "Mon–Sat 09:00–18:00" (utils/opening_hours.py); the
        # footer and /contact print it above the delivery line, which keeps its own wording.
        # orders_hours (owner, 2026-11-14): orders are taken around the clock in the bot and on the site (no ordering-hours
        # gate exists); delivery itself stays "every day" (landing.footer.delivery_hours), never "24/7".
        "web.shell.footer.office_hours": "Office hours: {hours}",
        "web.shell.footer.orders_hours": "Orders: around the clock in the bot and on the site",
        "web.toast.success": "Done",
        "web.toast.error": "Error",
        "web.toast.warning": "Please note",
        "web.toast.info": "Good to know",
        "web.error.404.title": "Page not found",
        "web.error.404.text": (
            "This link may be out of date. Everything you need is one tap away: the shop, the home page "
            "or our Telegram bot."
        ),
        "web.error.404.shop": "Go to the shop",
        "web.error.404.home": "Home page",
        "web.error.404.bot": "Order in the Telegram bot",
        "web.error.500.title": "Something went wrong",
        "web.error.500.text": (
            "This page did not open because of a fault on our side. Try again in a minute, or order in our "
            "Telegram bot meanwhile."
        ),
        "web.error.500.retry": "Try again",
        "web.error.429.title": "Too many requests",
        "web.error.429.text": (
            "We received many requests from you in a short time. Wait a minute and try again, or order in our "
            "Telegram bot."
        ),
        "web.shell.meta.default": (
            "Aqua Element: drinking water delivery in Tashkent. Order on the website or in our Telegram bot, "
            "or set up a subscription."
        ),
    },
    "ru": {
        "web.shell.skip_to_content": "Перейти к содержимому",
        "web.shell.nav.label": "Главное меню",
        "web.shell.nav.categories": "Категории магазина",
        "web.shell.nav.more": "Ещё",
        "web.shell.nav.blog": "Блог",
        "web.shell.nav.faq": "Вопросы и ответы",
        "web.shell.search": "Поиск",
        "web.shell.language": "Язык",
        "web.shell.cart": "Корзина",
        "web.shell.menu": "Меню",
        "web.shell.menu_open": "Открыть меню",
        "web.shell.menu_close": "Закрыть меню",
        "web.shell.account": "Аккаунт",
        "web.shell.close": "Закрыть",
        "web.shell.back_to_top": "Наверх",
        "web.shell.social.instagram": "Instagram",
        "web.shell.social.channel": "Telegram-канал",
        "web.shell.social.bot": "Telegram-бот",
        "web.shell.footer.company": "Компания",
        "web.shell.footer.help": "Помощь",
        "web.shell.footer.follow": "Мы в соцсетях",
        "web.shell.footer.follow_text": "Новости и акции — первыми в нашем Telegram-канале.",
        "web.shell.footer.filtration": "11 этапов фильтрации",
        "web.shell.footer.delivery_policy": "Условия доставки",
        "web.shell.footer.pricing_policy": "Ценовая политика",
        "web.shell.footer.refund_policy": "Условия возврата",
        "web.shell.footer.quality": "Стандарты качества",
        "web.shell.footer.faq": "Вопросы о доставке",
        "web.shell.footer.office_hours": "Часы работы офиса: {hours}",
        "web.shell.footer.orders_hours": "Заказы: круглосуточно в боте и на сайте",
        "web.toast.success": "Готово",
        "web.toast.error": "Ошибка",
        "web.toast.warning": "Обратите внимание",
        "web.toast.info": "К сведению",
        "web.error.404.title": "Страница не найдена",
        "web.error.404.text": (
            "Похоже, ссылка устарела. Всё нужное — в одно касание: магазин, главная страница или наш Telegram-бот."
        ),
        "web.error.404.shop": "В магазин",
        "web.error.404.home": "На главную",
        "web.error.404.bot": "Заказать в Telegram-боте",
        "web.error.500.title": "Что-то пошло не так",
        "web.error.500.text": (
            "Страница не открылась из-за сбоя на нашей стороне. Попробуйте ещё раз через минуту или пока закажите "
            "в нашем Telegram-боте."
        ),
        "web.error.500.retry": "Попробовать ещё раз",
        "web.error.429.title": "Слишком много запросов",
        "web.error.429.text": (
            "Мы получили от вас много запросов за короткое время. Подождите минуту и попробуйте снова или закажите "
            "в нашем Telegram-боте."
        ),
        "web.shell.meta.default": (
            "Aqua Element — доставка питьевой воды по Ташкенту. Заказывайте на сайте или в нашем Telegram-боте, "
            "оформляйте подписку."
        ),
    },
    "uz": {
        "web.shell.skip_to_content": "Asosiy qismga o‘tish",
        "web.shell.nav.label": "Asosiy menyu",
        "web.shell.nav.categories": "Do‘kon bo‘limlari",
        "web.shell.nav.more": "Yana",
        "web.shell.nav.blog": "Blog",
        "web.shell.nav.faq": "Savol-javoblar",
        "web.shell.search": "Qidirish",
        "web.shell.language": "Til",
        "web.shell.cart": "Savat",
        "web.shell.menu": "Menyu",
        "web.shell.menu_open": "Menyuni ochish",
        "web.shell.menu_close": "Menyuni yopish",
        "web.shell.account": "Hisob",
        "web.shell.close": "Yopish",
        "web.shell.back_to_top": "Yuqoriga",
        "web.shell.social.instagram": "Instagram",
        "web.shell.social.channel": "Telegram kanal",
        "web.shell.social.bot": "Telegram bot",
        "web.shell.footer.company": "Kompaniya",
        "web.shell.footer.help": "Yordam",
        "web.shell.footer.follow": "Bizni kuzating",
        "web.shell.footer.follow_text": "Yangiliklar va aksiyalar birinchi bo‘lib Telegram kanalimizda.",
        "web.shell.footer.filtration": "11 bosqichli filtratsiya",
        "web.shell.footer.delivery_policy": "Yetkazib berish shartlari",
        "web.shell.footer.pricing_policy": "Narx siyosati",
        "web.shell.footer.refund_policy": "Qaytarish shartlari",
        "web.shell.footer.quality": "Sifat standartlari",
        "web.shell.footer.faq": "Yetkazib berish bo‘yicha savollar",
        "web.shell.footer.office_hours": "Ofis ish vaqti: {hours}",
        "web.shell.footer.orders_hours": "Buyurtmalar: botda va saytda kecha-kunduz",
        "web.toast.success": "Tayyor",
        "web.toast.error": "Xatolik",
        "web.toast.warning": "E’tibor bering",
        "web.toast.info": "Ma’lumot uchun",
        "web.error.404.title": "Sahifa topilmadi",
        "web.error.404.text": (
            "Havola eskirgan bo‘lishi mumkin. Kerakli hammasi bir bosishda: do‘kon, bosh sahifa yoki Telegram botimiz."
        ),
        "web.error.404.shop": "Do‘konga o‘tish",
        "web.error.404.home": "Bosh sahifa",
        "web.error.404.bot": "Telegram botda buyurtma berish",
        "web.error.500.title": "Nimadir noto‘g‘ri ketdi",
        "web.error.500.text": (
            "Sahifa biz tomondagi nosozlik tufayli ochilmadi. Bir daqiqadan so‘ng qayta urinib ko‘ring yoki "
            "hozircha Telegram botimizda buyurtma bering."
        ),
        "web.error.500.retry": "Qayta urinish",
        "web.error.429.title": "So‘rovlar juda ko‘p",
        "web.error.429.text": (
            "Qisqa vaqt ichida sizdan ko‘p so‘rov keldi. Bir daqiqa kutib, qayta urinib ko‘ring yoki Telegram "
            "botimizda buyurtma bering."
        ),
        "web.shell.meta.default": (
            "Aqua Element — Toshkent bo‘ylab ichimlik suvini yetkazib berish. Saytda yoki Telegram botimizda "
            "buyurtma bering yoki obuna rasmiylashtiring."
        ),
    },
}

# Older `landing.*` rows the shell and the shared product card print on every page. Their own category stays
# (bulk_create_or_update writes the category it is given).
# - Uzbek rows written with a straight ' where the new rows use ‘ (o‘, g‘) and ’ (tutuq belgisi): one convention on
#   one screen. Only the Uzbek value changes.
# - The footer's brand line made a claim nothing backs ("premium … commitment to quality and customer satisfaction"):
#   now one plain fact in every language (owner decision 2: no unverifiable claims).
# - The product card's button lived only in the database, with a straight ' in Uzbek and Title Case in English: it
#   ships here in all three languages.
LANDING_SHELL_ROWS_CATEGORY = "landing"
LANDING_SHELL_ROWS = {
    "en": {
        "landing.footer.tagline": "Aqua Element drinking water delivery in Tashkent",
        "landing.button.add_to_cart": "Add to cart",
    },
    "ru": {
        "landing.footer.tagline": "Доставка питьевой воды Aqua Element по Ташкенту",
        "landing.button.add_to_cart": "Добавить в корзину",
    },
    "uz": {
        "landing.nav.shop": "Do‘kon",
        "landing.nav.register": "Ro‘yxatdan o‘tish",
        "landing.footer.contact_info": "Aloqa ma’lumotlari",
        "landing.footer.tagline": "Aqua Element ichimlik suvini Toshkent bo‘ylab yetkazib berish",
        "landing.button.add_to_cart": "Savatga qo‘shish",
        "landing.contact.location": "Toshkent, O‘zbekiston",
        "landing.notification.product_added_to_cart": "Mahsulot savatchaga qo‘shildi",
        "landing.shop.view_all_products": "Barcha mahsulotlarni ko‘rish",
    },
}

if __name__ == "__main__":
    # Guarded so tests can import TRANSLATIONS without writing to a database.
    app = create_app()
    with app.app_context():
        Translation.bulk_create_or_update(TRANSLATIONS, category=CATEGORY)
        Translation.bulk_create_or_update(LANDING_SHELL_ROWS, category=LANDING_SHELL_ROWS_CATEGORY)
        db.session.commit()
        print(f"Seeded {sum(len(v) for v in TRANSLATIONS.values())} rows into category '{CATEGORY}'")
        print(
            f"Updated {sum(len(v) for v in LANDING_SHELL_ROWS.values())} shell rows in category "
            f"'{LANDING_SHELL_ROWS_CATEGORY}'"
        )
