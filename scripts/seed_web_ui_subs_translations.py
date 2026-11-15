"""Seed the strings of /subscriptions and /services (website redesign step 3, group "subs").

Follows scripts/seed_web_ui_shell_translations.py:
- keys under `web.subs.*` (the /services strings under `web.subs.services.*`), never bare English;
- en / ru / uz for every key: natural Russian, native Uzbek Latin with ‘ in o‘/g‘ and ’ for the tutuq belgisi;
- what a subscription really does (spec 2026-10-23): the system places each order on schedule, every delivery is a
  regular order billed at the regular price (no tier discount on a subscription), pause/skip/cancel cancel only an
  unpaid upcoming order, and changes happen in the online account or the Telegram bot. No discount, savings, bonus
  or SMS wording (tests/integration/test_subscriptions_page_truthful_copy.py), no delivery-speed claim
  (tests/integration/retired_phrases.py), and no customer is told to phone;
- prices, smallest quantities and the delivery line are NOT here: the pages print them live from the Product rows
  (`SubscriptionService.product_catalog`, `get_public_offer_facts`).
`Translation.bulk_create_or_update` inserts the missing rows, updates only the keys listed here and drops their Redis
cache entries, so a re-run is safe.

Run on the dev database (scripts/ is not mounted in the container, so pipe it in):
    docker compose exec -T business_app python - < scripts/seed_web_ui_subs_translations.py
"""

CATEGORY = "web"

TRANSLATIONS = {
    "en": {
        # /subscriptions
        "web.subs.title": "Water subscription",
        "web.subs.meta": (
            "Aqua Element water subscription: choose the bottles, the quantity and the schedule, and we place every "
            "order for you. Pause, skip or cancel at any time."
        ),
        "web.subs.intro.title": "Never run out of water",
        "web.subs.intro.lead": (
            "Your subscription places each order for you on schedule, so you never forget to order water and never "
            "run out when you need it most."
        ),
        "web.subs.perk.delivery": "Delivery is free, on subscription deliveries as on every order.",
        "web.subs.perk.coins": (
            "Earn AquaCoins on every subscription delivery at your tier's rate, and redeem them for rewards."
        ),
        "web.subs.perk.flexible": (
            "Pause it, skip a delivery or change anything whenever you like, in your account or in our Telegram bot."
        ),
        "web.subs.cta.builder": "Build a subscription",
        "web.subs.cta.telegram": "Subscribe in Telegram",
        "web.subs.how.title": "How it works",
        "web.subs.how.step1.title": "Choose the water",
        "web.subs.how.step1.text": "Pick the bottles and how many come with each delivery.",
        "web.subs.how.step2.title": "Set the schedule",
        "web.subs.how.step2.text": "Every day, once a week, every two weeks or once a month, at a time that suits you.",
        "web.subs.how.step3.title": "We place every order",
        "web.subs.how.step3.text": (
            "On schedule, each delivery becomes a regular order, and we let you know in Telegram or by email."
        ),
        "web.subs.details.title": "What you choose",
        "web.subs.details.lead": (
            "You set all of this in the subscription builder or in our Telegram bot, and you can change it at any time."
        ),
        "web.subs.water.title": "Water and quantity",
        "web.subs.water.min": "From {qty} per delivery",
        "web.subs.water.note": (
            "Today's prices. Each delivery is billed at the regular price on the day its order is placed."
        ),
        "web.subs.schedule.title": "How often",
        "web.subs.schedule.daily_text": "On every day we deliver.",
        "web.subs.schedule.weekday_text": "On the weekday you choose.",
        "web.subs.schedule.monthly_text": "On the date you choose, or on the last day of a shorter month.",
        "web.subs.schedule.time": "Delivery time: pick a time window, or leave it on “{option}”.",
        "web.subs.payment.title": "Payment",
        "web.subs.payment.text": (
            "Each delivery is placed as a regular order and paid with the method on your subscription."
        ),
        "web.subs.payment.click_text": (
            "With the Click wallet or a linked card, online, once the order is placed and before its pay-by time."
        ),
        "web.subs.payment.cash_text": "To the courier when the water arrives.",
        "web.subs.payment.business_text": "For companies with a contract: charged to the company's prepaid balance.",
        "web.subs.address.title": "Address",
        "web.subs.address.text": "One of your saved addresses in our delivery zone.",
        "web.subs.address.link": "See the delivery zone",
        "web.subs.changes.title": "Changes",
        "web.subs.changes.text": (
            "Skip a delivery, pause and resume, change the water, the schedule, the address or the payment method, "
            "or cancel. All of it in your account or in our Telegram bot, with no contract and no cancellation fee."
        ),
        "web.subs.band.title": "Start your subscription",
        "web.subs.band.text": "Build it on the site in a few steps, or set it up in our Telegram bot.",
        "web.subs.faq.title": "Questions about subscriptions",
        "web.subs.faq.how_q": "How does a subscription work?",
        "web.subs.faq.how_a": (
            "You choose the bottles, how many come with each delivery and how often. On schedule, the subscription "
            "places each delivery as a regular order for you, and the courier brings it to your address. You can "
            "change, pause or cancel it at any time."
        ),
        "web.subs.faq.customize_q": "Can I customize my subscription?",
        "web.subs.faq.customize_a": (
            "Yes. Choose any of our bottles, the quantity for each delivery, how often and on which day. Change items, "
            "quantities, or the schedule anytime in your online account or in our Telegram bot. New items and "
            "quantities apply from the next order we place, and a new frequency, day, or time slot sets your next "
            "delivery by the new schedule. An order we have already placed is not changed."
        ),
        "web.subs.faq.pause_q": "What if I need to pause or skip a delivery?",
        "web.subs.faq.pause_a": (
            "You can pause your subscription whenever you like, for example during a vacation, and resume it when you "
            "are back, in your online account or in our Telegram bot. You can also skip any upcoming delivery in your "
            "online account or in our Telegram bot. Pausing, skipping, or cancelling cancels an upcoming order that is "
            "not yet paid. An order you have already paid for, or one already picked up for delivery, cannot be "
            "skipped or paused: it stays and is delivered, and the change applies from the following delivery."
        ),
        "web.subs.faq.contract_q": "Is there a contract or a cancellation fee?",
        "web.subs.faq.contract_a": (
            "No. A subscription has no contract and no cancellation fee: you can cancel it at any time in your online "
            "account or in our Telegram bot."
        ),
        "web.subs.faq.get_q": "What do I get with a subscription?",
        "web.subs.faq.get_a1": (
            "A subscription places each order for you on schedule, so you never have to remember to order water."
        ),
        "web.subs.faq.get_a2": (
            "You earn AquaCoins on every subscription delivery at your tier's rate. Subscription deliveries are billed "
            "at the regular price."
        ),
        "web.subs.faq.payment_q": "What payment methods do you accept?",
        "web.subs.faq.payment_a": (
            "A subscription can be paid through Click, in cash to the courier or, for companies with a contract, by "
            "bank transfer from the company's prepaid balance. Each scheduled delivery is placed as a regular order and paid with the "
            "payment method on your subscription, which you can change in your online account or in our Telegram bot."
        ),
        "web.subs.faq.address_q": "Can I change my delivery address?",
        "web.subs.faq.address_a": (
            "Your subscription delivers to the address you chose. You can change it in your online account or in our "
            "Telegram bot by picking one of your saved addresses. Your upcoming deliveries move to the new address "
            "too, unless the driver already has them."
        ),
        "web.subs.faq.failed_q": "What happens if a payment fails?",
        "web.subs.faq.failed_a": (
            "Each scheduled delivery is placed as a regular order, so it follows the same payment steps as any order. "
            "If a Click payment does not go through, you can pay the order again in your online account or in our "
            "Telegram bot until its pay-by time. If you need help, message us in our Telegram bot."
        ),
        # /services
        "web.subs.services.title": "Our services",
        "web.subs.services.meta": (
            "Aqua Element services: drinking water delivery to homes and offices in Tashkent, empty bottle exchange "
            "and water subscriptions."
        ),
        "web.subs.services.crumb": "Services",
        "web.subs.services.lead": "Drinking water for homes and offices in Tashkent, by the bottle or on a subscription.",
        "web.subs.services.delivery.title": "Water delivery",
        "web.subs.services.delivery.shop": "Order on the site",
        "web.subs.services.delivery.bot": "Order in the Telegram bot",
        "web.subs.services.bottles.title": "Empty bottle exchange",
        "web.subs.services.subscription.link": "How a subscription works",
        "web.subs.services.zone.title": "Delivery zone",
        "web.subs.services.zone.link": "Check your address on the map",
    },
    "ru": {
        # /subscriptions
        "web.subs.title": "Подписка на воду",
        "web.subs.meta": (
            "Подписка на воду Aqua Element: выберите бутыли, количество и расписание — каждый заказ мы оформим за вас. "
            "Пауза, пропуск и отмена в любой момент."
        ),
        "web.subs.intro.title": "Вода всегда в запасе",
        "web.subs.intro.lead": (
            "Подписка сама оформляет каждый заказ по расписанию, поэтому вы не забудете заказать воду и не останетесь "
            "без неё в самый нужный момент."
        ),
        "web.subs.perk.delivery": "Доставка бесплатная: и для доставок по подписке, и для каждого заказа.",
        "web.subs.perk.coins": (
            "Получайте AquaCoins за каждую доставку по подписке по ставке вашего уровня и обменивайте их на награды."
        ),
        "web.subs.perk.flexible": (
            "Приостановить подписку, пропустить доставку или что-то изменить можно в любой момент — в личном кабинете "
            "или в нашем Telegram-боте."
        ),
        "web.subs.cta.builder": "Собрать подписку",
        "web.subs.cta.telegram": "Оформить в Telegram",
        "web.subs.how.title": "Как это работает",
        "web.subs.how.step1.title": "Выберите воду",
        "web.subs.how.step1.text": "Выберите бутыли и сколько их привозить за одну доставку.",
        "web.subs.how.step2.title": "Задайте расписание",
        "web.subs.how.step2.text": "Каждый день, раз в неделю, раз в две недели или раз в месяц — в удобное для вас время.",
        "web.subs.how.step3.title": "Заказы оформляем мы",
        "web.subs.how.step3.text": (
            "По расписанию каждая доставка становится обычным заказом, и мы сообщаем о нём в Telegram или по "
            "электронной почте."
        ),
        "web.subs.details.title": "Что вы выбираете",
        "web.subs.details.lead": (
            "Всё это задаётся в конструкторе подписки или в нашем Telegram-боте и меняется в любой момент."
        ),
        "web.subs.water.title": "Вода и количество",
        "web.subs.water.min": "От {qty} шт. за доставку",
        "web.subs.water.note": (
            "Цены на сегодня. Каждая доставка оплачивается по обычной цене на день, когда оформлен её заказ."
        ),
        "web.subs.schedule.title": "Как часто",
        "web.subs.schedule.daily_text": "Во все дни, когда мы доставляем.",
        "web.subs.schedule.weekday_text": "В выбранный вами день недели.",
        "web.subs.schedule.monthly_text": "В выбранное вами число, а в более коротком месяце — в его последний день.",
        "web.subs.schedule.time": "Время доставки: выберите интервал или оставьте «{option}».",
        "web.subs.payment.title": "Оплата",
        "web.subs.payment.text": (
            "Каждая доставка оформляется как обычный заказ и оплачивается способом, указанным в подписке."
        ),
        "web.subs.payment.click_text": "Кошельком Click или привязанной картой: онлайн, после оформления заказа и до срока оплаты.",
        "web.subs.payment.cash_text": "Курьеру, когда он привезёт воду.",
        "web.subs.payment.business_text": (
            "Для компаний с договором: оплата списывается с предоплаченного баланса компании."
        ),
        "web.subs.address.title": "Адрес",
        "web.subs.address.text": "Один из ваших сохранённых адресов в зоне доставки.",
        "web.subs.address.link": "Посмотреть зону доставки",
        "web.subs.changes.title": "Изменения",
        "web.subs.changes.text": (
            "Пропустить доставку, приостановить и возобновить подписку, поменять воду, расписание, адрес или способ "
            "оплаты, отменить подписку — всё это в личном кабинете или в нашем Telegram-боте. Без договора и без "
            "платы за отмену."
        ),
        "web.subs.band.title": "Оформите подписку",
        "web.subs.band.text": "Соберите её на сайте за несколько шагов или настройте в нашем Telegram-боте.",
        "web.subs.faq.title": "Вопросы о подписке",
        "web.subs.faq.how_q": "Как работает подписка?",
        "web.subs.faq.how_a": (
            "Вы выбираете бутыли, их количество на одну доставку и частоту. По расписанию подписка оформляет каждую "
            "доставку как обычный заказ, а курьер привозит её по вашему адресу. Подписку можно изменить, "
            "приостановить или отменить в любой момент."
        ),
        "web.subs.faq.customize_q": "Можно ли настроить подписку под себя?",
        "web.subs.faq.customize_a": (
            "Да. Выберите любые наши бутыли, количество на одну доставку, частоту и день. Менять товары, количество "
            "или расписание можно в любое время в личном кабинете или в нашем Telegram-боте. Новые товары и "
            "количества действуют со следующего заказа, который мы оформим, а новая частота, день или временной "
            "интервал определяют следующую доставку по новому расписанию. Уже оформленный заказ не меняется."
        ),
        "web.subs.faq.pause_q": "Что делать, если нужно приостановить подписку или пропустить доставку?",
        "web.subs.faq.pause_a": (
            "Вы можете приостановить подписку в любое время, например на время отпуска, и возобновить её, когда "
            "вернётесь, в личном кабинете или в нашем Telegram-боте. Любую предстоящую доставку тоже можно пропустить "
            "в личном кабинете или в нашем Telegram-боте. Приостановка, пропуск или отмена отменяют ближайший ещё не "
            "оплаченный заказ. Уже оплаченный заказ или заказ, который курьер уже забрал для доставки, нельзя "
            "пропустить или приостановить: он остаётся и будет доставлен, а изменение действует со следующей доставки."
        ),
        "web.subs.faq.contract_q": "Есть ли договор или плата за отмену?",
        "web.subs.faq.contract_a": (
            "Нет. У подписки нет ни договора, ни платы за отмену: отменить её можно в любой момент в личном кабинете "
            "или в нашем Telegram-боте."
        ),
        "web.subs.faq.get_q": "Что я получаю с подпиской?",
        "web.subs.faq.get_a1": (
            "Подписка оформляет каждый заказ за вас по расписанию, так что помнить о заказе воды больше не нужно."
        ),
        "web.subs.faq.get_a2": (
            "За каждую доставку по подписке вы получаете AquaCoins по ставке вашего уровня. Доставки по подписке "
            "оплачиваются по обычной цене."
        ),
        "web.subs.faq.payment_q": "Какие способы оплаты доступны?",
        "web.subs.faq.payment_a": (
            "Подписку можно оплачивать через Click, наличными курьеру, а компаниям с договором — банковским "
            "переводом с предоплаченного баланса компании. Каждая плановая доставка оформляется как обычный заказ и оплачивается способом оплаты, "
            "указанным в подписке; его можно изменить в личном кабинете или в нашем Telegram-боте."
        ),
        "web.subs.faq.address_q": "Можно ли изменить адрес доставки?",
        "web.subs.faq.address_a": (
            "Подписка доставляется на выбранный вами адрес. Изменить его можно в личном кабинете или в нашем "
            "Telegram-боте, выбрав один из сохранённых адресов. Ближайшие доставки тоже переедут на новый адрес, если "
            "курьер их ещё не забрал."
        ),
        "web.subs.faq.failed_q": "Что будет, если оплата не прошла?",
        "web.subs.faq.failed_a": (
            "Каждая плановая доставка оформляется как обычный заказ, поэтому оплата проходит так же, как и для любого "
            "заказа. Если оплата через Click не прошла, заказ можно оплатить ещё раз в личном кабинете или в нашем "
            "Telegram-боте до срока оплаты. Если нужна помощь, напишите нам в Telegram-боте."
        ),
        # /services
        "web.subs.services.title": "Наши услуги",
        "web.subs.services.meta": (
            "Услуги Aqua Element: доставка питьевой воды домой и в офис по Ташкенту, обмен пустых бутылей и подписка "
            "на воду."
        ),
        "web.subs.services.crumb": "Услуги",
        "web.subs.services.lead": "Питьевая вода для дома и офиса в Ташкенте — разовыми заказами или по подписке.",
        "web.subs.services.delivery.title": "Доставка воды",
        "web.subs.services.delivery.shop": "Заказать на сайте",
        "web.subs.services.delivery.bot": "Заказать в Telegram-боте",
        "web.subs.services.bottles.title": "Обмен пустых бутылей",
        "web.subs.services.subscription.link": "Как устроена подписка",
        "web.subs.services.zone.title": "Зона доставки",
        "web.subs.services.zone.link": "Проверить адрес на карте",
    },
    "uz": {
        # /subscriptions
        "web.subs.title": "Suv obunasi",
        "web.subs.meta": (
            "Aqua Element suv obunasi: idishlar, miqdor va jadvalni tanlang — har bir buyurtmani biz o‘zimiz beramiz. "
            "Istalgan vaqtda to‘xtatish, o‘tkazib yuborish yoki bekor qilish mumkin."
        ),
        "web.subs.intro.title": "Suvsiz qolmaysiz",
        "web.subs.intro.lead": (
            "Obunangiz har bir buyurtmani jadval bo‘yicha o‘zi beradi, shuning uchun suv buyurtma qilishni unutmaysiz "
            "va eng kerakli paytda suvsiz qolmaysiz."
        ),
        "web.subs.perk.delivery": "Yetkazib berish bepul: obuna bo‘yicha yetkazishlarda ham, har bir buyurtmada ham.",
        "web.subs.perk.coins": (
            "Obuna bo‘yicha har bir yetkazishda darajangiz stavkasi bo‘yicha AquaCoins yig‘ing va ularni mukofotlarga "
            "almashtiring."
        ),
        "web.subs.perk.flexible": (
            "Obunani to‘xtatib turish, yetkazishni o‘tkazib yuborish yoki biror narsani o‘zgartirish istalgan vaqtda "
            "mumkin — shaxsiy kabinetda yoki Telegram botimizda."
        ),
        "web.subs.cta.builder": "Obuna yaratish",
        "web.subs.cta.telegram": "Telegramda obuna bo‘lish",
        "web.subs.how.title": "Qanday ishlaydi",
        "web.subs.how.step1.title": "Suvni tanlang",
        "web.subs.how.step1.text": "Idishlarni va har bir yetkazishda nechtasi kelishini tanlang.",
        "web.subs.how.step2.title": "Jadvalni belgilang",
        "web.subs.how.step2.text": "Har kuni, haftada, ikki haftada yoki oyda bir marta — o‘zingizga qulay vaqtda.",
        "web.subs.how.step3.title": "Buyurtmalarni biz beramiz",
        "web.subs.how.step3.text": (
            "Jadval bo‘yicha har bir yetkazish oddiy buyurtmaga aylanadi, biz bu haqda Telegramda yoki elektron pochta "
            "orqali xabar beramiz."
        ),
        "web.subs.details.title": "Nimani tanlaysiz",
        "web.subs.details.lead": (
            "Bularning barchasini obuna konstruktorida yoki Telegram botimizda belgilaysiz va istalgan vaqtda "
            "o‘zgartira olasiz."
        ),
        "web.subs.water.title": "Suv va miqdor",
        "web.subs.water.min": "Har yetkazishda kamida {qty} ta",
        "web.subs.water.note": (
            "Bugungi narxlar. Har bir yetkazish uning buyurtmasi berilgan kundagi oddiy narxda hisoblanadi."
        ),
        "web.subs.schedule.title": "Yetkazish chastotasi",
        "web.subs.schedule.daily_text": "Biz yetkazadigan har bir kunda.",
        "web.subs.schedule.weekday_text": "O‘zingiz tanlagan hafta kunida.",
        "web.subs.schedule.monthly_text": "O‘zingiz tanlagan sanada, qisqaroq oyda esa uning oxirgi kunida.",
        "web.subs.schedule.time": "Yetkazish vaqti: vaqt oralig‘ini tanlang yoki «{option}» deb qoldiring.",
        "web.subs.payment.title": "To‘lov",
        "web.subs.payment.text": (
            "Har bir yetkazish oddiy buyurtma sifatida beriladi va obunadagi to‘lov usuli bilan to‘lanadi."
        ),
        "web.subs.payment.click_text": "Click hamyoni yoki ulangan karta bilan: onlayn, buyurtma berilgach, to‘lov muddati tugaguncha.",
        "web.subs.payment.cash_text": "Suvni olib kelganda kuryerga.",
        "web.subs.payment.business_text": (
            "Shartnomasi bor kompaniyalar uchun: to‘lov kompaniyaning oldindan to‘langan balansidan yechiladi."
        ),
        "web.subs.address.title": "Manzil",
        "web.subs.address.text": "Yetkazib berish hududidagi saqlangan manzillaringizdan biri.",
        "web.subs.address.link": "Yetkazib berish hududini ko‘rish",
        "web.subs.changes.title": "O‘zgartirishlar",
        "web.subs.changes.text": (
            "Yetkazishni o‘tkazib yuborish, obunani to‘xtatib turish va davom ettirish, suv, jadval, manzil yoki to‘lov "
            "usulini o‘zgartirish, obunani bekor qilish — barchasi shaxsiy kabinetda yoki Telegram botimizda. "
            "Shartnomasiz va bekor qilish uchun to‘lovsiz."
        ),
        "web.subs.band.title": "Obunani boshlang",
        "web.subs.band.text": "Uni saytda bir necha qadamda yarating yoki Telegram botimizda sozlang.",
        "web.subs.faq.title": "Obuna haqida savollar",
        "web.subs.faq.how_q": "Obuna qanday ishlaydi?",
        "web.subs.faq.how_a": (
            "Siz idishlarni, har bir yetkazishdagi miqdorni va chastotani tanlaysiz. Jadval bo‘yicha obuna har bir "
            "yetkazishni siz uchun oddiy buyurtma sifatida beradi, kuryer esa uni manzilingizga olib keladi. Obunani "
            "istalgan vaqtda o‘zgartirish, to‘xtatib turish yoki bekor qilish mumkin."
        ),
        "web.subs.faq.customize_q": "Obunani o‘zimga moslab sozlasam bo‘ladimi?",
        "web.subs.faq.customize_a": (
            "Ha. Istalgan idishlarimizni, har bir yetkazish uchun miqdorni, chastota va kunni tanlang. Mahsulotlar, "
            "miqdor yoki jadvalni istalgan vaqtda shaxsiy kabinetda yoki Telegram botimizda o‘zgartirishingiz mumkin. "
            "Yangi mahsulotlar va miqdorlar biz beradigan keyingi buyurtmadan boshlab kuchga kiradi, yangi chastota, "
            "kun yoki vaqt oralig‘i esa keyingi yetkazishni yangi jadval bo‘yicha belgilaydi. Biz allaqachon bergan "
            "buyurtma o‘zgarmaydi."
        ),
        "web.subs.faq.pause_q": "Obunani to‘xtatib turish yoki yetkazishni o‘tkazib yuborish kerak bo‘lsa-chi?",
        "web.subs.faq.pause_a": (
            "Obunani istalgan vaqtda to‘xtatib turishingiz mumkin, masalan ta’til paytida, va qaytganingizda davom "
            "ettirasiz: buni shaxsiy kabinetda yoki Telegram botimizda qilish mumkin. Istalgan navbatdagi yetkazishni "
            "ham shaxsiy kabinetda yoki Telegram botimizda o‘tkazib yuborishingiz mumkin. To‘xtatish, o‘tkazib yuborish "
            "yoki bekor qilish hali to‘lanmagan kelgusi buyurtmani bekor qiladi. Siz allaqachon to‘lagan yoki kuryer "
            "yetkazib berish uchun olib ketgan buyurtmani o‘tkazib yuborib ham, to‘xtatib ham bo‘lmaydi: u qoladi va "
            "yetkaziladi, o‘zgarish esa keyingi yetkazishdan boshlab kuchga kiradi."
        ),
        "web.subs.faq.contract_q": "Shartnoma yoki bekor qilish uchun to‘lov bormi?",
        "web.subs.faq.contract_a": (
            "Yo‘q. Obunada shartnoma ham, bekor qilish uchun to‘lov ham yo‘q: uni istalgan vaqtda shaxsiy kabinetda "
            "yoki Telegram botimizda bekor qilishingiz mumkin."
        ),
        "web.subs.faq.get_q": "Obuna nimalarni beradi?",
        "web.subs.faq.get_a1": (
            "Obuna har bir buyurtmani jadval bo‘yicha siz uchun beradi, shuning uchun suv buyurtma qilishni eslab "
            "yurishingiz shart emas."
        ),
        "web.subs.faq.get_a2": (
            "Obuna bo‘yicha har bir yetkazishda darajangiz stavkasi bo‘yicha AquaCoins yig‘asiz. Obuna bo‘yicha "
            "yetkazishlar oddiy narxda hisoblanadi."
        ),
        "web.subs.faq.payment_q": "Qanday to‘lov usullari bor?",
        "web.subs.faq.payment_a": (
            "Obunani Click orqali, kuryerga naqd pul bilan yoki shartnomasi bor kompaniyalar uchun kompaniyaning "
            "oldindan to‘langan balansidan bank o‘tkazmasi orqali to‘lash mumkin. Har bir rejalashtirilgan yetkazish oddiy buyurtma sifatida beriladi va "
            "obunangizdagi to‘lov usuli bilan to‘lanadi; uni shaxsiy kabinetda yoki Telegram botimizda "
            "o‘zgartirishingiz mumkin."
        ),
        "web.subs.faq.address_q": "Yetkazib berish manzilini o‘zgartirsa bo‘ladimi?",
        "web.subs.faq.address_a": (
            "Obunangiz siz tanlagan manzilga yetkaziladi. Uni shaxsiy kabinetda yoki Telegram botimizda saqlangan "
            "manzillaringizdan birini tanlab o‘zgartirishingiz mumkin. Agar kuryer navbatdagi buyurtmalarni hali "
            "olmagan bo‘lsa, ular ham yangi manzilga o‘tadi."
        ),
        "web.subs.faq.failed_q": "To‘lov o‘tmasa nima bo‘ladi?",
        "web.subs.faq.failed_a": (
            "Har bir rejalashtirilgan yetkazish oddiy buyurtma sifatida beriladi, shuning uchun to‘lov har qanday "
            "buyurtmadagi kabi amalga oshiriladi. Click orqali to‘lov o‘tmasa, buyurtmani to‘lov muddati tugaguncha "
            "shaxsiy kabinetda yoki Telegram botimizda qayta to‘lashingiz mumkin. Yordam kerak bo‘lsa, Telegram "
            "botimizga yozing."
        ),
        # /services
        "web.subs.services.title": "Xizmatlarimiz",
        "web.subs.services.meta": (
            "Aqua Element xizmatlari: Toshkent bo‘ylab uy va ofislarga ichimlik suvi yetkazib berish, bo‘sh idishlarni "
            "almashtirish va suv obunasi."
        ),
        "web.subs.services.crumb": "Xizmatlar",
        "web.subs.services.lead": "Toshkentdagi uy va ofislar uchun ichimlik suvi — alohida buyurtma yoki obuna orqali.",
        "web.subs.services.delivery.title": "Suv yetkazib berish",
        "web.subs.services.delivery.shop": "Saytda buyurtma berish",
        "web.subs.services.delivery.bot": "Telegram botda buyurtma berish",
        "web.subs.services.bottles.title": "Bo‘sh idishlarni almashtirish",
        "web.subs.services.subscription.link": "Obuna qanday ishlaydi",
        "web.subs.services.zone.title": "Yetkazib berish hududi",
        "web.subs.services.zone.link": "Manzilni xaritada tekshirish",
    },
}

# Typography: " — " becomes NO-BREAK SPACE + dash + space, so a line never starts with the dash.
TRANSLATIONS = {
    language: {key: value.replace(" — ", "\u00a0— ") for key, value in rows.items()}
    for language, rows in TRANSLATIONS.items()
}

if __name__ == "__main__":
    # Guarded so tests can import TRANSLATIONS without writing to a database.
    from business_app import create_app, db
    from business_app.models.translation import Translation

    app = create_app()
    with app.app_context():
        Translation.bulk_create_or_update(TRANSLATIONS, category=CATEGORY)
        db.session.commit()
        print(f"Seeded {sum(len(v) for v in TRANSLATIONS.values())} rows into category '{CATEGORY}'")
