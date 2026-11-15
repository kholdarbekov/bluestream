"""Seed the home page's strings (redesign step 3, "home" group: the hero, how ordering works, the products, the
subscription builder's new labels, water quality, the Aqua Club teaser and the guides row).

Follows scripts/seed_web_ui_shell_translations.py:
- keys under `web.home.*`, en / ru / uz for every key;
- Uzbek Latin with ‘ in o‘/g‘ and ’ for the tutuq belgisi; natural Russian;
- customer copy in perk tone, never telling the customer to phone (the Telegram bot is the channel);
- no price, size or minimum typed here: those are printed from the public offer facts. The two numbers that do appear
  (the well's ~120 m and the 11 stages) are the published facts of /about/sources and /process/11-step-filtration;
- `Translation.bulk_create_or_update` inserts the missing rows, updates only the keys listed here and drops their
  Redis cache entries, so a re-run is safe.

Run on the dev database (scripts/ is not mounted in the container, so pipe it in):
    docker compose exec -T business_app python - < scripts/seed_web_ui_home_translations.py
"""

from business_app import create_app, db
from business_app.models.translation import Translation

CATEGORY = "web"

TRANSLATIONS = {
    "en": {
        # Hero
        "web.home.hero.title": "Artesian drinking water delivered across Tashkent",
        "web.home.hero.lead": (
            "Water from an artesian well about 120 metres deep, purified and mineralised in 11 stages."
        ),
        "web.home.hero.prices": "Prices",
        "web.home.hero.order_bot": "Order in the Telegram bot",
        "web.home.hero.order_site": "Order on the website",
        "web.home.hero.image_alt": "Aqua Element {size} bottle",
        # How ordering works
        "web.home.how.title": "How to order",
        # The section's lead: only what every customer can do (no fleet or region claim; the delivery zone is the
        # SSOT coverage printed in the next section).
        "web.home.how.lead": "Order in a few taps and follow your delivery in our Telegram bot or on the website.",
        "web.home.how.order_title": "Order in the bot or on the website",
        "web.home.how.order_text": "Choose the bottle size and how many you need: the smallest order is shown with each price.",
        "web.home.how.deliver_title": "We bring the water",
        "web.home.how.deliver_text": "A courier brings the bottles to the address in your order.",
        "web.home.how.return_title": "The courier takes the empty bottle",
        # Products
        "web.home.products.title": "Our water",
        "web.home.products.all": "All products",
        # Subscription builder
        "web.home.builder.title": "Build your subscription",
        "web.home.builder.lead": (
            "Choose the water, how often and what time: each delivery is then ordered for you on schedule."
        ),
        "web.home.builder.sign_in_note": "Sign in to save your subscription. Your choices stay in place.",
        "web.home.builder.sign_in": "Sign in",
        "web.home.builder.steps": "Subscription steps",
        "web.home.builder.step_of": "Step {current} of {total}",
        "web.home.builder.step_address": "Address",
        "web.home.builder.min_quantity": "from {count} pcs",
        "web.home.builder.quantity": "Quantity",
        "web.home.builder.fewer": "Fewer",
        "web.home.builder.more": "More",
        "web.home.builder.catalog_failed": "We couldn’t load the water list. Check your connection and try again.",
        "web.home.builder.retry": "Try again",
        "web.home.builder.method_note.cash": "Pay the courier on delivery",
        "web.home.builder.method_note.click": "Click wallet or a linked card",
        "web.home.builder.method_note.business_account": "From your company’s prepaid balance",
        "web.home.builder.step_products": "Water",
        "web.home.builder.step_schedule": "Schedule",
        "web.home.builder.step_payment": "Payment",
        "web.home.builder.step_review": "Confirm",
        "web.home.builder.products_title": "Choose your water",
        "web.home.builder.schedule_title": "How often and at what time",
        "web.home.builder.address_title": "Where to deliver",
        "web.home.builder.payment_title": "How you pay",
        "web.home.builder.review_title": "Check your subscription",
        "web.home.builder.frequency_label": "How often",
        "web.home.builder.day_label": "Delivery day",
        "web.home.builder.month_day_label": "Day of the month",
        "web.home.builder.time_label": "Delivery time",
        "web.home.builder.back": "Back",
        "web.home.builder.next": "Next",
        "web.home.builder.create": "Create subscription",
        "web.home.builder.manage": "My subscriptions",
        # Water quality
        "web.home.quality.title": "Where our water comes from",
        "web.home.quality.lead": "Every fact here is documented on our water source and filtration pages.",
        "web.home.quality.source_title": "An artesian well about 120 m deep",
        "web.home.quality.source_text": "A confined aquifer in the Quyi Chirchiq district of Tashkent Region.",
        "web.home.quality.stages_title": "11 stages of purification",
        "web.home.quality.stages_text": (
            "Reverse osmosis, then calcium and magnesium are added back for a balanced taste."
        ),
        "web.home.quality.disinfection_title": "UV and ozone, no chlorine",
        "web.home.quality.disinfection_text": (
            "Disinfected with ultraviolet light and ozone; no chlorine is added to the finished water."
        ),
        "web.home.quality.oversight_title": "Under Sanepid supervision",
        "web.home.quality.oversight_text": (
            "Production and quality are overseen by Uzbekistan’s sanitary and epidemiological regulator."
        ),
        "web.home.quality.process_link": "See all 11 stages",
        "web.home.quality.sources_link": "Sources and mineral profile",
        # Aqua Club: AquaCoins come with delivered, paid orders (LoyaltyService's award rule), never "every order".
        "web.home.club.title": "Join Aqua Club and earn AquaCoins",
        "web.home.club.lead": (
            "Our free loyalty programme rewards delivered, paid orders. Earn AquaCoins, move up the tiers and redeem "
            "rewards."
        ),
        "web.home.club.earn_title": "How you earn AquaCoins",
        "web.home.club.tiers_title": "Member tiers",
        "web.home.club.tiers": "Aqua Club tiers",
        "web.home.club.your_tier": "Your tier",
        "web.home.club.balance": "Your balance",
        "web.home.club.my_coins": "My AquaCoins",
        "web.home.club.join": "Join for free",
        # Guides (blog)
        "web.home.guides.title": "Guides and tips",
        "web.home.guides.all": "All articles",
    },
    "ru": {
        # No-break spaces keep the short prepositions with their word ("с доставкой", "по Ташкенту").
        "web.home.hero.title": "Артезианская питьевая вода с\u00a0доставкой по\u00a0Ташкенту",
        "web.home.hero.lead": (
            "Вода из артезианской скважины глубиной около 120 метров проходит 11 этапов очистки и минерализации."
        ),
        "web.home.hero.prices": "Цены",
        "web.home.hero.order_bot": "Заказать в Telegram-боте",
        "web.home.hero.order_site": "Заказать на сайте",
        "web.home.hero.image_alt": "Бутыль Aqua Element {size}",
        "web.home.how.title": "Как заказать",
        "web.home.how.lead": "Закажите в несколько касаний и следите за доставкой в нашем Telegram-боте или на сайте.",
        "web.home.how.order_title": "Закажите в боте или на сайте",
        "web.home.how.order_text": "Выберите объём бутыли и количество — минимальный заказ указан рядом с ценой.",
        "web.home.how.deliver_title": "Привозим воду",
        "web.home.how.deliver_text": "Курьер доставит бутыли по адресу из заказа.",
        "web.home.how.return_title": "Пустую бутыль заберёт курьер",
        "web.home.products.title": "Наша вода",
        "web.home.products.all": "Все товары",
        "web.home.builder.title": "Соберите свою подписку",
        "web.home.builder.lead": (
            "Выберите воду, частоту и время — дальше каждый заказ будет оформляться за вас по графику."
        ),
        "web.home.builder.sign_in_note": "Чтобы сохранить подписку, войдите в аккаунт. Выбранное останется на месте.",
        "web.home.builder.sign_in": "Войти",
        "web.home.builder.steps": "Шаги оформления подписки",
        "web.home.builder.step_of": "Шаг {current} из {total}",
        "web.home.builder.step_address": "Адрес",
        "web.home.builder.min_quantity": "от {count} шт.",
        "web.home.builder.quantity": "Количество",
        "web.home.builder.fewer": "Меньше",
        "web.home.builder.more": "Больше",
        "web.home.builder.catalog_failed": "Не удалось загрузить список воды. Проверьте подключение и попробуйте ещё раз.",
        "web.home.builder.retry": "Попробовать ещё раз",
        "web.home.builder.method_note.cash": "Курьеру при доставке",
        "web.home.builder.method_note.click": "Кошелёк Click или привязанная карта",
        "web.home.builder.method_note.business_account": "С предоплаченного баланса компании",
        "web.home.builder.step_products": "Вода",
        "web.home.builder.step_schedule": "График",
        "web.home.builder.step_payment": "Оплата",
        "web.home.builder.step_review": "Проверка",
        "web.home.builder.products_title": "Выберите воду",
        "web.home.builder.schedule_title": "Как часто и в какое время",
        "web.home.builder.address_title": "Куда привозить",
        "web.home.builder.payment_title": "Способ оплаты",
        "web.home.builder.review_title": "Проверьте подписку",
        "web.home.builder.frequency_label": "Как часто",
        "web.home.builder.day_label": "День доставки",
        "web.home.builder.month_day_label": "Число месяца",
        "web.home.builder.time_label": "Время доставки",
        "web.home.builder.back": "Назад",
        "web.home.builder.next": "Далее",
        "web.home.builder.create": "Оформить подписку",
        "web.home.builder.manage": "Мои подписки",
        "web.home.quality.title": "Откуда наша вода",
        "web.home.quality.lead": "Каждый факт здесь подтверждён на страницах об источнике и очистке воды.",
        "web.home.quality.source_title": "Артезианская скважина глубиной около 120 м",
        "web.home.quality.source_text": "Замкнутый водоносный горизонт в Куйи Чирчикском районе Ташкентской области.",
        "web.home.quality.stages_title": "11 этапов очистки",
        "web.home.quality.stages_text": (
            "Обратный осмос, а затем вода снова получает кальций и магний — для сбалансированного вкуса."
        ),
        "web.home.quality.disinfection_title": "УФ и озон, без хлора",
        "web.home.quality.disinfection_text": (
            "Обеззараживание ультрафиолетом и озоном; хлор в готовую воду не добавляется."
        ),
        "web.home.quality.oversight_title": "Под надзором Санэпида",
        "web.home.quality.oversight_text": (
            "Производство и качество контролирует санитарно-эпидемиологический регулятор Узбекистана."
        ),
        "web.home.quality.process_link": "Все 11 этапов очистки",
        "web.home.quality.sources_link": "Источники и минеральный состав",
        "web.home.club.title": "Вступайте в Aqua Club и копите AquaCoins",
        "web.home.club.lead": (
            "Наша бесплатная программа лояльности вознаграждает доставленные и оплаченные заказы. Копите AquaCoins, "
            "повышайте уровень и получайте награды."
        ),
        "web.home.club.earn_title": "Как начисляются AquaCoins",
        "web.home.club.tiers_title": "Уровни участника",
        "web.home.club.tiers": "Уровни Aqua Club",
        "web.home.club.your_tier": "Ваш уровень",
        "web.home.club.balance": "На вашем счёте",
        "web.home.club.my_coins": "Мои AquaCoins",
        "web.home.club.join": "Вступить бесплатно",
        "web.home.guides.title": "Советы и гиды",
        "web.home.guides.all": "Все статьи",
    },
    "uz": {
        "web.home.hero.title": "Toshkent bo‘ylab artezian ichimlik suvi yetkazib berish",
        "web.home.hero.lead": (
            "Taxminan 120 metr chuqurlikdagi artezian qudug‘idan olingan suv 11 bosqichda tozalanadi va "
            "minerallashtiriladi."
        ),
        "web.home.hero.prices": "Narxlar",
        "web.home.hero.order_bot": "Telegram botda buyurtma berish",
        "web.home.hero.order_site": "Saytda buyurtma berish",
        "web.home.hero.image_alt": "Aqua Element {size} idishi",
        "web.home.how.title": "Qanday buyurtma beriladi",
        "web.home.how.lead": (
            "Bir necha bosishda buyurtma bering va yetkazib berishni Telegram botimizda yoki saytda kuzatib boring."
        ),
        "web.home.how.order_title": "Botda yoki saytda buyurtma bering",
        "web.home.how.order_text": "Idish hajmi va sonini tanlang — eng kichik buyurtma narx yonida ko‘rsatilgan.",
        "web.home.how.deliver_title": "Suvni olib kelamiz",
        "web.home.how.deliver_text": "Kuryer idishlarni buyurtmadagi manzilga yetkazadi.",
        "web.home.how.return_title": "Bo‘sh idishni kuryer olib ketadi",
        "web.home.products.title": "Suvimiz",
        "web.home.products.all": "Barcha mahsulotlar",
        "web.home.builder.title": "Obunangizni yig‘ing",
        "web.home.builder.lead": (
            "Suv, chastota va vaqtni tanlang — keyin har bir buyurtma jadval bo‘yicha siz uchun rasmiylashtiriladi."
        ),
        "web.home.builder.sign_in_note": "Obunani saqlash uchun hisobingizga kiring. Tanlovingiz joyida qoladi.",
        "web.home.builder.sign_in": "Kirish",
        "web.home.builder.steps": "Obuna bosqichlari",
        "web.home.builder.step_of": "{total} qadamdan {current}-qadam",
        "web.home.builder.step_address": "Manzil",
        "web.home.builder.min_quantity": "{count} donadan",
        "web.home.builder.quantity": "Soni",
        "web.home.builder.fewer": "Kamroq",
        "web.home.builder.more": "Ko‘proq",
        "web.home.builder.catalog_failed": (
            "Suvlar ro‘yxatini yuklab bo‘lmadi. Internet aloqasini tekshirib, qaytadan urinib ko‘ring."
        ),
        "web.home.builder.retry": "Qaytadan urinish",
        "web.home.builder.method_note.cash": "Yetkazib berishda kuryerga",
        "web.home.builder.method_note.click": "Click hamyoni yoki ulangan karta",
        "web.home.builder.method_note.business_account": "Kompaniyaning oldindan to‘langan balansidan",
        "web.home.builder.step_products": "Suv",
        "web.home.builder.step_schedule": "Jadval",
        "web.home.builder.step_payment": "To‘lov",
        "web.home.builder.step_review": "Tasdiqlash",
        "web.home.builder.products_title": "Suvni tanlang",
        "web.home.builder.schedule_title": "Qanchalik tez-tez va qaysi vaqtda",
        "web.home.builder.address_title": "Qayerga yetkazamiz",
        "web.home.builder.payment_title": "To‘lov usuli",
        "web.home.builder.review_title": "Obunani tekshiring",
        "web.home.builder.frequency_label": "Qanchalik tez-tez",
        "web.home.builder.day_label": "Yetkazish kuni",
        "web.home.builder.month_day_label": "Oyning kuni",
        "web.home.builder.time_label": "Yetkazish vaqti",
        "web.home.builder.back": "Orqaga",
        "web.home.builder.next": "Keyingi",
        "web.home.builder.create": "Obunani rasmiylashtirish",
        "web.home.builder.manage": "Obunalarim",
        "web.home.quality.title": "Suvimiz qayerdan olinadi",
        "web.home.quality.lead": "Bu yerdagi har bir dalil suv manbai va tozalash sahifalarimizda tasdiqlangan.",
        "web.home.quality.source_title": "Taxminan 120 m chuqurlikdagi artezian qudug‘i",
        "web.home.quality.source_text": "Toshkent viloyati Quyi Chirchiq tumanidagi yopiq suvli qatlam.",
        "web.home.quality.stages_title": "11 bosqichli tozalash",
        "web.home.quality.stages_text": (
            "Teskari osmos, so‘ng muvozanatli ta’m uchun suvga kalsiy va magniy qaytariladi."
        ),
        "web.home.quality.disinfection_title": "Ultrabinafsha nur va ozon, xlorsiz",
        "web.home.quality.disinfection_text": (
            "Ultrabinafsha nur va ozon bilan zararsizlantiriladi; tayyor suvga xlor qo‘shilmaydi."
        ),
        "web.home.quality.oversight_title": "Sanepid nazorati ostida",
        "web.home.quality.oversight_text": (
            "Ishlab chiqarish va sifatni O‘zbekistonning sanitariya-epidemiologiya regulyatori nazorat qiladi."
        ),
        "web.home.quality.process_link": "11 bosqichning barchasi",
        "web.home.quality.sources_link": "Manbalar va mineral tarkib",
        "web.home.club.title": "Aqua Club’ga qo‘shiling va AquaCoins yig‘ing",
        "web.home.club.lead": (
            "Bepul sodiqlik dasturimiz yetkazilgan va to‘langan buyurtmalarni mukofotlaydi. AquaCoins yig‘ing, "
            "darajangizni oshiring va sovg‘alar oling."
        ),
        "web.home.club.earn_title": "AquaCoins qanday yig‘iladi",
        "web.home.club.tiers_title": "A’zolik darajalari",
        "web.home.club.tiers": "Aqua Club darajalari",
        "web.home.club.your_tier": "Darajangiz",
        "web.home.club.balance": "Hisobingizda",
        "web.home.club.my_coins": "Mening AquaCoins’larim",
        "web.home.club.join": "Bepul qo‘shilish",
        "web.home.guides.title": "Maslahatlar va qo‘llanmalar",
        "web.home.guides.all": "Barcha maqolalar",
    },
}

if __name__ == "__main__":
    # Guarded so tests can import TRANSLATIONS without writing to a database.
    app = create_app()
    with app.app_context():
        Translation.bulk_create_or_update(TRANSLATIONS, category=CATEGORY)
        db.session.commit()
        print(f"Seeded {sum(len(v) for v in TRANSLATIONS.values())} rows into category '{CATEGORY}'")
