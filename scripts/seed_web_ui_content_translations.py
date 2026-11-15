"""Seed the content pages' strings (redesign step 3, "content" group): the blog, the water-delivery FAQ, the Aqua Club
guide and the policy pages (delivery, pricing, refund, terms, privacy).

Follows the pattern of scripts/seed_web_ui_shell_translations.py:
- keys under `web.content.*`, en / ru / uz for every key; natural Russian; Uzbek Latin with ‘ in o‘/g‘ and ’ for the
  tutuq belgisi (never ʻ U+02BB, which Manrope lacks, and never a straight ');
- customer copy never tells the customer to phone: the Telegram bot and email are the channels;
- the policy texts are the published English wording, unchanged in meaning, with faithful ru and uz translations; only
  the closing "contact" lines changed (the phone number gave way to the Telegram bot and email);
- `Translation.bulk_create_or_update` inserts the missing rows, updates only the keys listed here and drops their
  Redis cache entries, so a re-run is safe.

The /loyalty-guide rows themselves (category `loyalty_guide`: ‘ and ’ in Uzbek, numbers the program config owns read
from the page, "the website" instead of an app, the streak lines as sentences) are shipped by
scripts/seed_backend_translations.py (LOYALTY_GUIDE_TRANSLATIONS), their one source; this seed no longer rewrites them,
so the two seeds can run in either order.

Run on the dev database (scripts/ is not mounted in the container, so pipe it in):
    docker compose exec -T business_app python - < scripts/seed_web_ui_content_translations.py
"""

from business_app import create_app, db
from business_app.models.translation import Translation

CATEGORY = "web"

TRANSLATIONS = {
    "en": {
        # ---- shared by the content pages
        "web.content.policy_nav.label": "Policies and help",
        "web.content.toc.title": "Contents",
        "web.content.contact.bot": "Write in the Telegram bot",
        "web.content.cta.title": "Order water in a few taps",
        "web.content.cta.text": "In our Telegram bot or on the website, whichever suits you.",
        "web.content.cta.shop": "Order on the website",
        # ---- blog
        "web.content.blog.title": "Water benefits and health tips",
        "web.content.blog.lead": "Practical articles about drinking water, health and everyday life from the Aqua Element team.",
        "web.content.blog.meta": "Water benefits, health tips and company news from {company}.",
        "web.content.blog.meta_category": "{category}: articles about water and health from {company}.",
        "web.content.blog.page_n": "Page {n}",
        "web.content.blog.filter_label": "Blog categories",
        "web.content.blog.all": "All articles",
        "web.content.blog.tag_filter": "Filtered by topic",
        "web.content.blog.clear_tag": "Show all articles",
        "web.content.blog.empty_title": "No articles here yet",
        "web.content.blog.empty_text": "Pick another category or read all our articles.",
        "web.content.blog.read": "Read the article",
        "web.content.blog.pagination": "Pages",
        "web.content.blog.prev": "Previous page",
        "web.content.blog.next": "Next page",
        "web.content.blog.share": "Share",
        "web.content.blog.share_telegram": "Telegram",
        "web.content.blog.share_facebook": "Facebook",
        "web.content.blog.products": "Aqua Element water",
        "web.content.blog.related": "Read next",
        "web.content.blog.article": "Article",
        "web.content.blog.in_article": "In this article",
        "web.content.blog.table_label": "Table from the article",
        # ---- water-delivery FAQ
        "web.content.faq.title": "Water delivery FAQ",
        "web.content.faq.lead": "Answers to the questions customers ask most about delivery, prices and subscriptions before they order.",
        "web.content.faq.meta": "Water delivery FAQ from Aqua Element: delivery timing, smallest order, prices, subscriptions and help.",
        "web.content.faq.recurring_q": "Can I set recurring deliveries?",
        "web.content.faq.recurring_a": "Yes. Use the subscriptions page to configure recurring deliveries and adjust them in your account.",
        "web.content.faq.delayed_q": "What if my order is delayed?",
        "web.content.faq.aside_title": "Useful pages",
        # ---- delivery policy
        "web.content.delivery.coverage_title": "Check your address on the map",
        "web.content.delivery.coverage_text": "The delivery zone map shows where we deliver.",
        "web.content.delivery.coverage_cta": "Open the map",
        # ---- pricing policy (the en headings stay as published: tests and links read them)
        "web.content.pricing.meta": "Pricing policy for Aqua Element: displayed prices, discounts, promotional terms, and update rules.",
        "web.content.pricing.displayed_h": "Displayed Prices",
        "web.content.pricing.displayed_p": (
            "Product prices shown on Aqua Element channels are listed in UZS and reflect the current effective selling "
            "price at the time of order placement."
        ),
        "web.content.pricing.discounts_h": "Discounts and Promotions",
        "web.content.pricing.discounts_p": (
            "Where applicable, promotional pricing or discount pricing is displayed alongside the standard product price. "
            "Promotions may be time-limited or quantity-limited."
        ),
        "web.content.pricing.updates_h": "Price Updates",
        "web.content.pricing.updates_p": (
            "Aqua Element reserves the right to adjust prices in response to product, supply, or operational changes. "
            "Changes apply to new orders and do not retroactively alter completed purchases."
        ),
        "web.content.pricing.club_title": "Aqua Club member perks",
        "web.content.pricing.club_text": "How AquaCoins, tiers and member discounts work.",
        "web.content.pricing.club_cta": "Read the guide",
        "web.content.pricing.help": "A question about prices? Write to us in the Telegram bot.",
        # ---- refund policy
        "web.content.refund.meta": (
            "Refund and return policy for Aqua Element: eligibility, issue reporting timelines, and refund processing "
            "details."
        ),
        "web.content.refund.s1_h": "Eligibility",
        "web.content.refund.s1_p": (
            "Refund requests may be reviewed for confirmed fulfillment issues such as damaged items, incorrect items, or "
            "undelivered orders with valid order records."
        ),
        "web.content.refund.s2_h": "How to Report an Issue",
        "web.content.refund.s2_p": (
            "Customers should report issues as soon as possible after delivery attempt, including order ID, issue details, "
            "and supporting photos when relevant."
        ),
        "web.content.refund.s3_h": "Review and Resolution",
        "web.content.refund.s3_p": (
            "Our support and operations teams review each case. Depending on the case, resolution may include "
            "replacement, partial refund, or full refund."
        ),
        "web.content.refund.s4_h": "Refund Timing",
        "web.content.refund.s4_p": (
            "Approved refunds are processed through the original payment channel where available. Processing times "
            "depend on payment providers and bank settlement timelines."
        ),
        "web.content.refund.s5_h": "Support Contact",
        "web.content.refund.s5_p": "For refund and order support, write to us in our Telegram bot or by email.",
        # ---- terms
        "web.content.terms.title": "Terms and conditions",
        "web.content.terms.meta": (
            "Terms and conditions for using Aqua Element website, ordering products, subscriptions, and related delivery "
            "services."
        ),
        "web.content.terms.s1_h": "Acceptance of Terms",
        "web.content.terms.s1_p": (
            "By using the Aqua Element website and placing orders, you agree to these terms. If you do not agree, please "
            "discontinue use of our services."
        ),
        "web.content.terms.s2_h": "Eligibility and Account Responsibilities",
        "web.content.terms.s2_p": (
            "You are responsible for providing accurate contact, delivery, and payment information. You are also "
            "responsible for activity performed through your account credentials."
        ),
        "web.content.terms.s3_h": "Products, Availability, and Orders",
        "web.content.terms.s3_p": (
            "Product listings, stock availability, and delivery windows can change. Order confirmation is subject to "
            "successful processing, operational availability, and verification checks where required."
        ),
        "web.content.terms.s4_h": "Pricing and Payments",
        "web.content.terms.s4_p": (
            "Prices are shown in UZS unless otherwise stated. Final payable totals are presented during checkout. "
            "Applicable promotions or discounts are calculated based on active platform rules."
        ),
        "web.content.terms.s5_h": "Delivery and Fulfillment",
        "web.content.terms.s5_p": (
            "Delivery terms follow the current delivery policy and our delivery coverage area. Delays caused by force "
            "majeure, traffic, weather, or incorrect delivery details may affect estimated timelines."
        ),
        "web.content.terms.s6_h": "Refunds and Issue Resolution",
        "web.content.terms.s6_p": (
            "Refund and replacement handling follows the published refund policy. To resolve issues quickly, provide "
            "order details and relevant evidence (such as photos) when contacting support."
        ),
        "web.content.terms.s7_h": "Acceptable Use",
        "web.content.terms.s7_p": (
            "You agree not to misuse the platform, interfere with service operation, attempt unauthorized access, or "
            "submit fraudulent transactions."
        ),
        "web.content.terms.s8_h": "Intellectual Property",
        "web.content.terms.s8_p": (
            "Site content, branding, and materials are owned by Blue Stream Group or licensed for use. Unauthorized "
            "copying, redistribution, or commercial exploitation is prohibited."
        ),
        "web.content.terms.s9_h": "Limitation of Liability",
        "web.content.terms.s9_p": (
            "To the maximum extent permitted by law, Blue Stream Group is not liable for indirect, incidental, or "
            "consequential damages arising from platform use, except where such limitation is legally prohibited."
        ),
        "web.content.terms.s10_h": "Updates to Terms",
        "web.content.terms.s10_p": (
            "These terms may be updated as services evolve. Updated terms become effective when posted on this page."
        ),
        "web.content.terms.s11_h": "Contact",
        "web.content.terms.s11_p": "For terms-related questions, write to us in our Telegram bot or by email.",
        # ---- privacy
        "web.content.privacy.title": "Privacy policy",
        "web.content.privacy.meta": (
            "Privacy policy for Aqua Element covering data collection, processing, retention, security, and customer "
            "rights."
        ),
        "web.content.privacy.s1_h": "Information We Collect",
        "web.content.privacy.s1_p": (
            "We may collect account details (name, phone, email), delivery addresses, order history, support "
            "communications, and technical usage data required to operate and improve services."
        ),
        "web.content.privacy.s2_h": "How We Use Information",
        "web.content.privacy.s2_p": (
            "Data is used to process orders, coordinate delivery, provide support, detect fraud, send operational "
            "notifications, and maintain platform security and reliability."
        ),
        "web.content.privacy.s3_h": "Payment and Transaction Data",
        "web.content.privacy.s3_p": (
            "Payment processing is handled through integrated payment providers. We store only the data necessary for "
            "order reconciliation, support, and compliance workflows."
        ),
        "web.content.privacy.s4_h": "Sharing of Information",
        "web.content.privacy.s4_p": (
            "Information may be shared with service providers and partners involved in payment, delivery, "
            "infrastructure, or legal compliance, strictly for service operation purposes. The website also uses two "
            "web analytics services, Google Analytics and Yandex Metrica, which receive data about your visits (see "
            "Cookies and Similar Technologies)."
        ),
        "web.content.privacy.s5_h": "Data Retention",
        "web.content.privacy.s5_p": (
            "We retain data for as long as needed to deliver services, resolve disputes, satisfy legal requirements, "
            "and enforce contractual obligations."
        ),
        "web.content.privacy.s6_h": "Security Measures",
        "web.content.privacy.s6_p": (
            "Aqua Element uses technical and organizational safeguards to protect personal data from unauthorized "
            "access, alteration, disclosure, or destruction."
        ),
        "web.content.privacy.s7_h": "Cookies and Similar Technologies",
        "web.content.privacy.s7_p": (
            "We use cookies and related technologies to maintain sessions, improve performance, and support core user "
            "experience functions. We also use Google Analytics and Yandex Metrica to understand how the website is "
            "used. They set their own cookies and receive data about your visit: the pages you open, the links you "
            "click, your device and browser, and an approximate location. Yandex Metrica's click map and Webvisor "
            "also record how pages are scrolled and clicked. You can block or delete these cookies in your browser "
            "settings; Google also offers a browser add-on that turns Google Analytics off."
        ),
        "web.content.privacy.s8_h": "Your Rights and Choices",
        "web.content.privacy.s8_p": (
            "You may request access to, correction of, or deletion of your personal information subject to applicable "
            "legal and operational requirements."
        ),
        "web.content.privacy.s9_h": "Policy Updates",
        "web.content.privacy.s9_p": (
            "This privacy policy may be updated periodically. Material updates take effect when posted on this page."
        ),
        "web.content.privacy.s10_h": "Contact",
        "web.content.privacy.s10_p": "For privacy-related questions, write to us in our Telegram bot or by email.",
        # ---- Aqua Club guide
        "web.content.lg.on_this_page": "On this page",
        "web.content.lg.nav.earn": "Earning",
        "web.content.lg.nav.referral": "Invite friends",
        "web.content.lg.nav.tiers": "Tiers",
        "web.content.lg.nav.points": "Two kinds of AquaCoins",
        "web.content.lg.nav.rewards": "Rewards",
        "web.content.lg.nav.faq": "Questions",
        "web.content.lg.delivery_title": "Delivery is free.",
        "web.content.lg.delivery_text": "You never spend AquaCoins on it.",
    },
    "ru": {
        "web.content.policy_nav.label": "Правила и помощь",
        "web.content.toc.title": "Содержание",
        "web.content.contact.bot": "Написать в Telegram-бот",
        "web.content.cta.title": "Закажите воду в пару касаний",
        "web.content.cta.text": "В нашем Telegram-боте или на сайте — как вам удобнее.",
        "web.content.cta.shop": "Заказать на сайте",
        "web.content.blog.title": "Польза воды и советы для здоровья",
        "web.content.blog.lead": "Практичные статьи о питьевой воде, здоровье и быте от команды Aqua Element.",
        "web.content.blog.meta": "Польза воды, советы для здоровья и новости {company}.",
        "web.content.blog.meta_category": "{category}: статьи о воде и здоровье от {company}.",
        "web.content.blog.page_n": "Страница {n}",
        "web.content.blog.filter_label": "Рубрики блога",
        "web.content.blog.all": "Все статьи",
        "web.content.blog.tag_filter": "Подборка по теме",
        "web.content.blog.clear_tag": "Показать все статьи",
        "web.content.blog.empty_title": "Здесь пока нет статей",
        "web.content.blog.empty_text": "Выберите другую рубрику или откройте все статьи.",
        "web.content.blog.read": "Читать статью",
        "web.content.blog.pagination": "Страницы",
        "web.content.blog.prev": "Предыдущая страница",
        "web.content.blog.next": "Следующая страница",
        "web.content.blog.share": "Поделиться",
        "web.content.blog.share_telegram": "Telegram",
        "web.content.blog.share_facebook": "Facebook",
        "web.content.blog.products": "Вода Aqua Element",
        "web.content.blog.related": "Читайте также",
        "web.content.blog.article": "Статья",
        "web.content.blog.in_article": "В этой статье",
        "web.content.blog.table_label": "Таблица из статьи",
        "web.content.faq.title": "Вопросы о доставке воды",
        "web.content.faq.lead": "Ответы на вопросы, которые чаще всего задают о доставке, ценах и подписках перед заказом.",
        "web.content.faq.meta": "Вопросы о доставке воды Aqua Element: сроки, минимальный заказ, цены, подписки и помощь.",
        "web.content.faq.recurring_q": "Можно ли оформить регулярную доставку?",
        "web.content.faq.recurring_a": (
            "Да, на странице подписок можно настроить регулярные доставки и менять их в личном кабинете."
        ),
        "web.content.faq.delayed_q": "Что делать, если заказ задерживается?",
        "web.content.faq.aside_title": "Полезные страницы",
        "web.content.delivery.coverage_title": "Проверьте адрес на карте",
        "web.content.delivery.coverage_text": "На карте зоны доставки видно, куда мы доставляем.",
        "web.content.delivery.coverage_cta": "Открыть карту",
        "web.content.pricing.meta": (
            "Ценовая политика Aqua Element: отображение цен, скидки, промоусловия и правила обновления стоимости."
        ),
        "web.content.pricing.displayed_h": "Отображаемые цены",
        "web.content.pricing.displayed_p": (
            "Цены на товары в каналах Aqua Element указаны в сумах и соответствуют действующей цене продажи на момент "
            "оформления заказа."
        ),
        "web.content.pricing.discounts_h": "Скидки и акции",
        "web.content.pricing.discounts_p": (
            "Если применимо, акционная или сниженная цена указывается рядом со стандартной ценой товара. Акции могут "
            "быть ограничены по времени или по количеству."
        ),
        "web.content.pricing.updates_h": "Изменение цен",
        "web.content.pricing.updates_p": (
            "Aqua Element оставляет за собой право изменять цены в связи с изменениями товаров, поставок или условий "
            "работы. Изменения применяются к новым заказам и не затрагивают уже совершённые покупки."
        ),
        "web.content.pricing.club_title": "Привилегии Aqua Club",
        "web.content.pricing.club_text": "Как работают AquaCoins, уровни и скидки участников.",
        "web.content.pricing.club_cta": "Открыть гид",
        "web.content.pricing.help": "Есть вопрос о ценах? Напишите нам в Telegram-бот.",
        "web.content.refund.meta": (
            "Условия возврата Aqua Element: основания, сроки обращения по проблемам и порядок обработки возврата."
        ),
        "web.content.refund.s1_h": "Основания для возврата",
        "web.content.refund.s1_p": (
            "Запросы на возврат могут рассматриваться при подтверждённых проблемах с выполнением заказа, например при "
            "повреждённом товаре, не том товаре или недоставленном заказе, если заказ подтверждён записями."
        ),
        "web.content.refund.s2_h": "Как сообщить о проблеме",
        "web.content.refund.s2_p": (
            "Сообщите о проблеме как можно скорее после попытки доставки: укажите номер заказа, опишите проблему и, "
            "если это уместно, приложите фото."
        ),
        "web.content.refund.s3_h": "Рассмотрение и решение",
        "web.content.refund.s3_p": (
            "Наша служба поддержки и операционная команда рассматривают каждый случай. В зависимости от ситуации "
            "решением может быть замена, частичный или полный возврат средств."
        ),
        "web.content.refund.s4_h": "Сроки возврата",
        "web.content.refund.s4_p": (
            "Одобренные возвраты проводятся тем же способом, которым был оплачен заказ, если это возможно. Сроки "
            "обработки зависят от платёжных провайдеров и сроков расчётов банков."
        ),
        "web.content.refund.s5_h": "Связь с поддержкой",
        "web.content.refund.s5_p": "По вопросам возврата и заказов напишите нам в Telegram-бот или на электронную почту.",
        "web.content.terms.title": "Условия использования",
        "web.content.terms.meta": (
            "Условия использования Aqua Element: сайт, заказы товаров, подписки и связанные услуги доставки."
        ),
        "web.content.terms.s1_h": "Принятие условий",
        "web.content.terms.s1_p": (
            "Пользуясь сайтом Aqua Element и оформляя заказы, вы соглашаетесь с этими условиями. Если вы с ними не "
            "согласны, пожалуйста, прекратите пользоваться нашими услугами."
        ),
        "web.content.terms.s2_h": "Требования и ответственность за аккаунт",
        "web.content.terms.s2_p": (
            "Вы отвечаете за точность контактных данных, данных для доставки и платёжной информации. Вы также несёте "
            "ответственность за действия, совершённые с использованием данных для входа в ваш аккаунт."
        ),
        "web.content.terms.s3_h": "Товары, наличие и заказы",
        "web.content.terms.s3_p": (
            "Ассортимент, наличие товаров и интервалы доставки могут меняться. Заказ подтверждается при условии его "
            "успешной обработки, операционной возможности и, где это требуется, проверки."
        ),
        "web.content.terms.s4_h": "Цены и оплата",
        "web.content.terms.s4_p": (
            "Цены указаны в сумах, если не указано иное. Итоговая сумма к оплате показывается при оформлении заказа. "
            "Действующие акции или скидки рассчитываются по правилам, действующим на платформе."
        ),
        "web.content.terms.s5_h": "Доставка и выполнение заказов",
        "web.content.terms.s5_p": (
            "Доставка выполняется по действующим условиям доставки и в пределах нашей зоны доставки. Форс-мажор, "
            "дорожная обстановка, погода или неверные данные для доставки могут повлиять на расчётные сроки."
        ),
        "web.content.terms.s6_h": "Возвраты и решение проблем",
        "web.content.terms.s6_p": (
            "Возвраты и замены выполняются по опубликованным условиям возврата. Чтобы мы быстрее решили вопрос, при "
            "обращении в поддержку укажите данные заказа и приложите подтверждения (например, фото)."
        ),
        "web.content.terms.s7_h": "Допустимое использование",
        "web.content.terms.s7_p": (
            "Вы соглашаетесь не злоупотреблять платформой, не мешать её работе, не пытаться получить "
            "несанкционированный доступ и не совершать мошеннических операций."
        ),
        "web.content.terms.s8_h": "Интеллектуальная собственность",
        "web.content.terms.s8_p": (
            "Материалы сайта, элементы бренда и прочий контент принадлежат Blue Stream Group или используются по "
            "лицензии. Несанкционированное копирование, распространение или коммерческое использование запрещено."
        ),
        "web.content.terms.s9_h": "Ограничение ответственности",
        "web.content.terms.s9_p": (
            "В максимальной степени, допустимой законом, Blue Stream Group не несёт ответственности за косвенный, "
            "случайный или последующий ущерб, возникший в связи с использованием платформы, кроме случаев, когда "
            "такое ограничение запрещено законом."
        ),
        "web.content.terms.s10_h": "Изменение условий",
        "web.content.terms.s10_p": (
            "Эти условия могут обновляться по мере развития сервиса. Обновлённые условия вступают в силу с момента "
            "публикации на этой странице."
        ),
        "web.content.terms.s11_h": "Контакты",
        "web.content.terms.s11_p": (
            "По вопросам, связанным с этими условиями, напишите нам в Telegram-бот или на электронную почту."
        ),
        "web.content.privacy.title": "Политика конфиденциальности",
        "web.content.privacy.meta": (
            "Политика конфиденциальности Aqua Element: сбор, обработка, хранение данных, безопасность и права клиентов."
        ),
        "web.content.privacy.s1_h": "Какие данные мы собираем",
        "web.content.privacy.s1_p": (
            "Мы можем собирать данные аккаунта (имя, телефон, электронную почту), адреса доставки, историю заказов, "
            "переписку со службой поддержки и технические данные об использовании, необходимые для работы и улучшения "
            "сервиса."
        ),
        "web.content.privacy.s2_h": "Как мы используем данные",
        "web.content.privacy.s2_p": (
            "Данные используются для обработки заказов, организации доставки, поддержки клиентов, выявления "
            "мошенничества, отправки служебных уведомлений, а также для безопасности и надёжности платформы."
        ),
        "web.content.privacy.s3_h": "Платёжные данные и данные о транзакциях",
        "web.content.privacy.s3_p": (
            "Платежи обрабатываются через подключённых платёжных провайдеров. Мы храним только те данные, которые "
            "необходимы для сверки заказов, поддержки клиентов и процедур соблюдения требований."
        ),
        "web.content.privacy.s4_h": "Передача данных",
        "web.content.privacy.s4_p": (
            "Данные могут передаваться поставщикам услуг и партнёрам, участвующим в платежах, доставке, обеспечении "
            "инфраструктуры или соблюдении требований закона, исключительно для работы сервиса. Сайт также "
            "использует две службы веб-аналитики, Google Analytics и Яндекс Метрику: они получают данные о ваших "
            "посещениях (см. раздел «Файлы cookie и похожие технологии»)."
        ),
        "web.content.privacy.s5_h": "Хранение данных",
        "web.content.privacy.s5_p": (
            "Мы храним данные столько, сколько необходимо для оказания услуг, разрешения споров, выполнения требований "
            "закона и исполнения договорных обязательств."
        ),
        "web.content.privacy.s6_h": "Меры безопасности",
        "web.content.privacy.s6_p": (
            "Aqua Element применяет технические и организационные меры, чтобы защитить персональные данные от "
            "несанкционированного доступа, изменения, раскрытия или уничтожения."
        ),
        "web.content.privacy.s7_h": "Файлы cookie и похожие технологии",
        "web.content.privacy.s7_p": (
            "Мы используем файлы cookie и похожие технологии, чтобы поддерживать сеансы, повышать производительность и "
            "обеспечивать работу основных функций сайта. Ещё мы используем Google Analytics и Яндекс Метрику, чтобы "
            "понимать, как пользуются сайтом. Они устанавливают собственные файлы cookie и получают данные о вашем "
            "посещении: какие страницы вы открываете, по каким ссылкам переходите, ваше устройство и браузер, "
            "примерное местоположение. Карта кликов и Вебвизор Яндекс Метрики также записывают, как страницы "
            "прокручивают и где по ним нажимают. Эти файлы cookie можно заблокировать или удалить в настройках "
            "браузера; у Google есть и расширение для браузера, которое отключает Google Analytics."
        ),
        "web.content.privacy.s8_h": "Ваши права и выбор",
        "web.content.privacy.s8_p": (
            "Вы можете запросить доступ к своим персональным данным, их исправление или удаление с учётом применимых "
            "правовых и операционных требований."
        ),
        "web.content.privacy.s9_h": "Обновление политики",
        "web.content.privacy.s9_p": (
            "Эта политика конфиденциальности может периодически обновляться. Существенные изменения вступают в силу с "
            "момента публикации на этой странице."
        ),
        "web.content.privacy.s10_h": "Контакты",
        "web.content.privacy.s10_p": (
            "По вопросам конфиденциальности напишите нам в Telegram-бот или на электронную почту."
        ),
        "web.content.lg.on_this_page": "На этой странице",
        "web.content.lg.nav.earn": "Начисление",
        "web.content.lg.nav.referral": "Приглашения",
        "web.content.lg.nav.tiers": "Уровни",
        "web.content.lg.nav.points": "Два вида AquaCoins",
        "web.content.lg.nav.rewards": "Награды",
        "web.content.lg.nav.faq": "Вопросы",
        "web.content.lg.delivery_title": "Доставка бесплатна.",
        "web.content.lg.delivery_text": "AquaCoins на неё не тратятся.",
    },
    "uz": {
        "web.content.policy_nav.label": "Qoidalar va yordam",
        "web.content.toc.title": "Mundarija",
        "web.content.contact.bot": "Telegram botga yozish",
        "web.content.cta.title": "Suvni bir necha bosishda buyurtma qiling",
        "web.content.cta.text": "Telegram botimizda yoki saytda — o‘zingizga qulay usulda.",
        "web.content.cta.shop": "Saytda buyurtma berish",
        "web.content.blog.title": "Suv foydasi va salomatlik bo‘yicha maslahatlar",
        "web.content.blog.lead": "Ichimlik suvi, salomatlik va kundalik hayot haqida Aqua Element jamoasidan amaliy maqolalar.",
        "web.content.blog.meta": "Suv foydasi, salomatlik bo‘yicha maslahatlar va {company} yangiliklari.",
        "web.content.blog.meta_category": "{category}: {company} blogidagi suv va salomatlik haqidagi maqolalar.",
        "web.content.blog.page_n": "{n}-sahifa",
        "web.content.blog.filter_label": "Blog bo‘limlari",
        "web.content.blog.all": "Barcha maqolalar",
        "web.content.blog.tag_filter": "Mavzu bo‘yicha saralangan",
        "web.content.blog.clear_tag": "Barcha maqolalarni ko‘rsatish",
        "web.content.blog.empty_title": "Bu yerda hozircha maqola yo‘q",
        "web.content.blog.empty_text": "Boshqa bo‘limni tanlang yoki barcha maqolalarni o‘qing.",
        "web.content.blog.read": "Maqolani o‘qish",
        "web.content.blog.pagination": "Sahifalar",
        "web.content.blog.prev": "Oldingi sahifa",
        "web.content.blog.next": "Keyingi sahifa",
        "web.content.blog.share": "Ulashish",
        "web.content.blog.share_telegram": "Telegram",
        "web.content.blog.share_facebook": "Facebook",
        "web.content.blog.products": "Aqua Element suvi",
        "web.content.blog.related": "Yana o‘qing",
        "web.content.blog.article": "Maqola",
        "web.content.blog.in_article": "Ushbu maqolada",
        "web.content.blog.table_label": "Maqoladagi jadval",
        "web.content.faq.title": "Suv yetkazib berish bo‘yicha savollar",
        "web.content.faq.lead": (
            "Buyurtma berishdan oldin eng ko‘p so‘raladigan yetkazib berish, narx va obuna savollariga javoblar."
        ),
        "web.content.faq.meta": (
            "Aqua Element suv yetkazib berish bo‘yicha savollar: muddatlar, minimal buyurtma, narxlar, obunalar va yordam."
        ),
        "web.content.faq.recurring_q": "Muntazam yetkazib berish buyurtmasini sozlash mumkinmi?",
        "web.content.faq.recurring_a": (
            "Ha, obuna sahifasi orqali muntazam yetkazib berishni sozlab, kabinetda boshqarish mumkin."
        ),
        "web.content.faq.delayed_q": "Buyurtma kechiksa nima qilish kerak?",
        "web.content.faq.aside_title": "Foydali sahifalar",
        "web.content.delivery.coverage_title": "Manzilingizni xaritada tekshiring",
        "web.content.delivery.coverage_text": "Yetkazib berish hududi xaritasida qayerlarga yetkazib berishimiz ko‘rsatilgan.",
        "web.content.delivery.coverage_cta": "Xaritani ochish",
        "web.content.pricing.meta": (
            "Aqua Element narx siyosati: ko‘rsatiladigan narxlar, chegirmalar, aksiya shartlari va yangilash qoidalari."
        ),
        "web.content.pricing.displayed_h": "Ko‘rsatiladigan narxlar",
        "web.content.pricing.displayed_p": (
            "Aqua Element kanallarida ko‘rsatilgan mahsulot narxlari so‘mda berilgan va buyurtma berilgan paytdagi "
            "amaldagi sotuv narxiga mos keladi."
        ),
        "web.content.pricing.discounts_h": "Chegirmalar va aksiyalar",
        "web.content.pricing.discounts_p": (
            "Tegishli hollarda aksiya yoki chegirma narxi mahsulotning standart narxi yonida ko‘rsatiladi. Aksiyalar "
            "muddat yoki miqdor bo‘yicha cheklangan bo‘lishi mumkin."
        ),
        "web.content.pricing.updates_h": "Narxlarning o‘zgarishi",
        "web.content.pricing.updates_p": (
            "Aqua Element mahsulot, ta’minot yoki ish sharoitidagi o‘zgarishlar sababli narxlarni o‘zgartirish huquqini "
            "saqlab qoladi. O‘zgarishlar yangi buyurtmalarga qo‘llaniladi va yakunlangan xaridlarga ta’sir qilmaydi."
        ),
        "web.content.pricing.club_title": "Aqua Club imtiyozlari",
        "web.content.pricing.club_text": "AquaCoins, darajalar va a’zolar chegirmalari qanday ishlaydi.",
        "web.content.pricing.club_cta": "Qo‘llanmani ochish",
        "web.content.pricing.help": "Narxlar bo‘yicha savolingiz bormi? Telegram botimizga yozing.",
        "web.content.refund.meta": (
            "Aqua Element qaytarish shartlari: asoslar, murojaat muddatlari va qaytarishni ko‘rib chiqish tartibi."
        ),
        "web.content.refund.s1_h": "Qaytarish asoslari",
        "web.content.refund.s1_p": (
            "Qaytarish so‘rovlari buyurtmani bajarishdagi tasdiqlangan muammolar, masalan, shikastlangan mahsulot, "
            "noto‘g‘ri mahsulot yoki yetkazib berilmagan buyurtma bo‘yicha, buyurtma qaydlari mavjud bo‘lsa, ko‘rib "
            "chiqilishi mumkin."
        ),
        "web.content.refund.s2_h": "Muammo haqida qanday xabar berish",
        "web.content.refund.s2_p": (
            "Yetkazib berishga urinishdan so‘ng muammo haqida imkon qadar tezroq xabar bering: buyurtma raqami, muammo "
            "tafsilotlari va kerak bo‘lsa, tasdiqlovchi suratlarni yuboring."
        ),
        "web.content.refund.s3_h": "Ko‘rib chiqish va hal qilish",
        "web.content.refund.s3_p": (
            "Qo‘llab-quvvatlash va operatsion jamoalarimiz har bir holatni ko‘rib chiqadi. Vaziyatga qarab, almashtirish, "
            "qisman yoki to‘liq pul qaytarish taklif etilishi mumkin."
        ),
        "web.content.refund.s4_h": "Qaytarish muddatlari",
        "web.content.refund.s4_p": (
            "Tasdiqlangan qaytarishlar imkon bo‘lsa, buyurtma to‘langan usul orqali amalga oshiriladi. Qayta ishlash "
            "muddati to‘lov provayderlari va banklarning hisob-kitob muddatlariga bog‘liq."
        ),
        "web.content.refund.s5_h": "Qo‘llab-quvvatlash bilan aloqa",
        "web.content.refund.s5_p": (
            "Qaytarish va buyurtmalar bo‘yicha savollar bo‘lsa, Telegram botimizga yoki elektron pochtamizga yozing."
        ),
        "web.content.terms.title": "Foydalanish shartlari",
        "web.content.terms.meta": (
            "Aqua Element foydalanish shartlari: sayt, buyurtmalar, obunalar va tegishli yetkazib berish xizmatlari."
        ),
        "web.content.terms.s1_h": "Shartlarni qabul qilish",
        "web.content.terms.s1_p": (
            "Aqua Element saytidan foydalanish va buyurtma berish orqali siz ushbu shartlarga rozilik bildirasiz. Agar "
            "rozi bo‘lmasangiz, xizmatlarimizdan foydalanishni to‘xtating."
        ),
        "web.content.terms.s2_h": "Talablar va hisob uchun javobgarlik",
        "web.content.terms.s2_p": (
            "Aloqa, yetkazib berish va to‘lov ma’lumotlarini to‘g‘ri ko‘rsatish uchun siz javobgarsiz. Hisobingizga "
            "kirish ma’lumotlari orqali amalga oshirilgan harakatlar uchun ham siz javob berasiz."
        ),
        "web.content.terms.s3_h": "Mahsulotlar, mavjudlik va buyurtmalar",
        "web.content.terms.s3_p": (
            "Mahsulotlar ro‘yxati, ularning mavjudligi va yetkazib berish vaqt oraliqlari o‘zgarishi mumkin. Buyurtma "
            "muvaffaqiyatli qayta ishlangan, operatsion imkoniyat bo‘lgan va kerak bo‘lsa, tekshiruvdan o‘tgan taqdirda "
            "tasdiqlanadi."
        ),
        "web.content.terms.s4_h": "Narxlar va to‘lovlar",
        "web.content.terms.s4_p": (
            "Agar boshqacha ko‘rsatilmagan bo‘lsa, narxlar so‘mda ko‘rsatiladi. To‘lanadigan yakuniy summa buyurtmani "
            "rasmiylashtirish paytida ko‘rsatiladi. Amaldagi aksiyalar yoki chegirmalar platformaning amaldagi "
            "qoidalari asosida hisoblanadi."
        ),
        "web.content.terms.s5_h": "Yetkazib berish va buyurtmani bajarish",
        "web.content.terms.s5_p": (
            "Yetkazib berish amaldagi yetkazib berish shartlari asosida va yetkazib berish hududimiz doirasida amalga "
            "oshiriladi. Fors-major holatlar, yo‘llardagi vaziyat, ob-havo yoki noto‘g‘ri ko‘rsatilgan yetkazib berish "
            "ma’lumotlari taxminiy muddatlarga ta’sir qilishi mumkin."
        ),
        "web.content.terms.s6_h": "Qaytarish va muammolarni hal qilish",
        "web.content.terms.s6_p": (
            "Qaytarish va almashtirish e’lon qilingan qaytarish shartlariga muvofiq amalga oshiriladi. Muammoni tezroq "
            "hal qilish uchun qo‘llab-quvvatlash xizmatiga murojaat qilganda buyurtma ma’lumotlari va tegishli "
            "dalillarni (masalan, suratlarni) taqdim eting."
        ),
        "web.content.terms.s7_h": "Ruxsat etilgan foydalanish",
        "web.content.terms.s7_p": (
            "Siz platformani suiiste’mol qilmaslik, uning ishiga xalal bermaslik, ruxsatsiz kirishga urinmaslik va "
            "firibgarlik operatsiyalarini amalga oshirmaslikka rozilik bildirasiz."
        ),
        "web.content.terms.s8_h": "Intellektual mulk",
        "web.content.terms.s8_p": (
            "Sayt materiallari, brend elementlari va boshqa kontent Blue Stream Group’ga tegishli yoki litsenziya "
            "asosida foydalaniladi. Ruxsatsiz nusxa ko‘chirish, tarqatish yoki tijoriy maqsadlarda foydalanish "
            "taqiqlanadi."
        ),
        "web.content.terms.s9_h": "Javobgarlikni cheklash",
        "web.content.terms.s9_p": (
            "Qonun ruxsat bergan eng yuqori darajada Blue Stream Group platformadan foydalanish natijasida yuzaga "
            "kelgan bilvosita, tasodifiy yoki oqibatli zarar uchun javobgar emas, bunday cheklash qonun bilan "
            "taqiqlangan holatlar bundan mustasno."
        ),
        "web.content.terms.s10_h": "Shartlarning yangilanishi",
        "web.content.terms.s10_p": (
            "Xizmatlar rivojlanib borishi bilan ushbu shartlar yangilanishi mumkin. Yangilangan shartlar shu sahifada "
            "e’lon qilingan paytdan kuchga kiradi."
        ),
        "web.content.terms.s11_h": "Aloqa",
        "web.content.terms.s11_p": (
            "Ushbu shartlar bo‘yicha savollar bo‘lsa, Telegram botimizga yoki elektron pochtamizga yozing."
        ),
        "web.content.privacy.title": "Maxfiylik siyosati",
        "web.content.privacy.meta": (
            "Aqua Element maxfiylik siyosati: ma’lumot yig‘ish, qayta ishlash, saqlash, xavfsizlik va mijoz huquqlari."
        ),
        "web.content.privacy.s1_h": "Qanday ma’lumotlarni yig‘amiz",
        "web.content.privacy.s1_p": (
            "Biz hisob ma’lumotlari (ism, telefon, elektron pochta), yetkazib berish manzillari, buyurtmalar tarixi, "
            "qo‘llab-quvvatlash xizmati bilan yozishmalar hamda xizmat ishlashi va uni yaxshilash uchun zarur bo‘lgan "
            "texnik foydalanish ma’lumotlarini yig‘ishimiz mumkin."
        ),
        "web.content.privacy.s2_h": "Ma’lumotlardan qanday foydalanamiz",
        "web.content.privacy.s2_p": (
            "Ma’lumotlar buyurtmalarni qayta ishlash, yetkazib berishni tashkil etish, qo‘llab-quvvatlash ko‘rsatish, "
            "firibgarlikni aniqlash, xizmat bildirishnomalarini yuborish hamda platformaning xavfsizligi va "
            "ishonchliligini ta’minlash uchun ishlatiladi."
        ),
        "web.content.privacy.s3_h": "To‘lov va tranzaksiya ma’lumotlari",
        "web.content.privacy.s3_p": (
            "To‘lovlar ulangan to‘lov provayderlari orqali amalga oshiriladi. Biz faqat buyurtmalarni solishtirish, "
            "qo‘llab-quvvatlash va talablarga muvofiqlik jarayonlari uchun zarur bo‘lgan ma’lumotlarni saqlaymiz."
        ),
        "web.content.privacy.s4_h": "Ma’lumotlarni uzatish",
        "web.content.privacy.s4_p": (
            "Ma’lumotlar to‘lov, yetkazib berish, infratuzilma yoki qonun talablariga rioya qilishda ishtirok etuvchi "
            "xizmat ko‘rsatuvchilar va hamkorlarga faqat xizmat ishlashi uchun uzatilishi mumkin. Sayt, shuningdek, "
            "ikkita veb-analitika xizmatidan — Google Analytics va Yandex Metrikadan foydalanadi: ular "
            "tashriflaringiz haqidagi ma’lumotlarni oladi («Cookie fayllari va shunga o‘xshash texnologiyalar» "
            "bo‘limiga qarang)."
        ),
        "web.content.privacy.s5_h": "Ma’lumotlarni saqlash",
        "web.content.privacy.s5_p": (
            "Biz ma’lumotlarni xizmat ko‘rsatish, nizolarni hal qilish, qonun talablarini bajarish va shartnoma "
            "majburiyatlarini ta’minlash uchun zarur bo‘lgan muddat davomida saqlaymiz."
        ),
        "web.content.privacy.s6_h": "Xavfsizlik choralari",
        "web.content.privacy.s6_p": (
            "Aqua Element shaxsiy ma’lumotlarni ruxsatsiz kirish, o‘zgartirish, oshkor qilish yoki yo‘q qilishdan "
            "himoya qilish uchun texnik va tashkiliy choralarni qo‘llaydi."
        ),
        "web.content.privacy.s7_h": "Cookie fayllari va shunga o‘xshash texnologiyalar",
        "web.content.privacy.s7_p": (
            "Biz seanslarni saqlash, ishlash samaradorligini oshirish va saytning asosiy funksiyalarini ta’minlash uchun "
            "cookie fayllari va shunga o‘xshash texnologiyalardan foydalanamiz. Saytdan qanday foydalanilishini "
            "tushunish uchun Google Analytics va Yandex Metrikadan ham foydalanamiz. Ular o‘z cookie fayllarini "
            "o‘rnatadi va tashrifingiz haqidagi ma’lumotlarni oladi: qaysi sahifalarni ochganingiz, qaysi havolalarni "
            "bosganingiz, qurilmangiz va brauzeringiz, taxminiy joylashuvingiz. Yandex Metrikaning bosishlar xaritasi "
            "va Vebvizori sahifalar qanday aylantirilishi va qayerda bosilishini ham yozib oladi. Bu cookie "
            "fayllarini brauzer sozlamalarida bloklash yoki o‘chirish mumkin; Google Analytics’ni o‘chiradigan "
            "brauzer kengaytmasini Google ham taklif qiladi."
        ),
        "web.content.privacy.s8_h": "Huquqlaringiz va tanlovingiz",
        "web.content.privacy.s8_p": (
            "Amaldagi qonun va operatsion talablarga muvofiq shaxsiy ma’lumotlaringizga kirish, ularni tuzatish yoki "
            "o‘chirishni so‘rashingiz mumkin."
        ),
        "web.content.privacy.s9_h": "Siyosatning yangilanishi",
        "web.content.privacy.s9_p": (
            "Ushbu maxfiylik siyosati vaqti-vaqti bilan yangilanishi mumkin. Muhim o‘zgarishlar shu sahifada e’lon "
            "qilingan paytdan kuchga kiradi."
        ),
        "web.content.privacy.s10_h": "Aloqa",
        "web.content.privacy.s10_p": (
            "Maxfiylik bo‘yicha savollar bo‘lsa, Telegram botimizga yoki elektron pochtamizga yozing."
        ),
        "web.content.lg.on_this_page": "Ushbu sahifada",
        "web.content.lg.nav.earn": "Yig‘ish",
        "web.content.lg.nav.referral": "Do‘stlarni taklif qilish",
        "web.content.lg.nav.tiers": "Darajalar",
        "web.content.lg.nav.points": "Ikki xil AquaCoins",
        "web.content.lg.nav.rewards": "Mukofotlar",
        "web.content.lg.nav.faq": "Savollar",
        "web.content.lg.delivery_title": "Yetkazib berish bepul.",
        "web.content.lg.delivery_text": "Unga hech qachon AquaCoins sarflamaysiz.",
    },
}

if __name__ == "__main__":
    # Guarded so tests can import TRANSLATIONS without writing to a database.
    app = create_app()
    with app.app_context():
        Translation.bulk_create_or_update(TRANSLATIONS, category=CATEGORY)
        db.session.commit()
        print(f"Seeded {sum(len(v) for v in TRANSLATIONS.values())} rows into category '{CATEGORY}'")
