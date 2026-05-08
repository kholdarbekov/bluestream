# -*- coding: utf-8 -*-
"""Water subscription in Tashkent — recurring delivery, returnable bottles, no running out.

Milestone 2, post 8/8. Conversion-oriented content for the subscription
model; cadence-planning table; routes via the Telegram bot.
"""

ARTICLE = {
    "slug": "podpiska-na-vodu-tashkent",
    "category": "company_news",
    "tags": "subscription,savings,recurring,plan,convenience,delivery,tashkent",
    "featured_image": "/static/images/news/news-8.jpg",
    "image_alt_text": "customer scheduling water-delivery subscription on a phone",
    "is_featured": False,
    "sort_order": 70,
    "translations": {
        "ru": {
            "title": "Подписка на воду в Ташкенте: автодоставка, возвратные бутыли, без перебоев",
            "excerpt": "Подписка на воду в Ташкенте от Aqua Element — регулярная доставка 19 л и 10 л бутылей с удобной периодичностью. Пустая бутыль возвращается со следующей доставкой, расходы предсказуемы. Управляйте графиком на сайте, в Telegram-боте или по телефону.",
            "meta_title": "Подписка на воду в Ташкенте — автодоставка 19 л и 10 л | Aqua Element",
            "meta_description": "Подписка на воду в Ташкенте: автодоставка 19 л и 10 л, возврат бутылей, гибкий график, оформление в Telegram-боте и на сайте Aqua Element.",
            "content": """
<p class="lead">«Закончилась вода в субботу вечером» — знакомая картина для многих ташкентских семей и офисов. В жарком климате расход питьевой воды растёт незаметно, а одноразовые заявки приходится оформлять «по факту». Подписка на воду в Ташкенте от Aqua Element снимает эту нагрузку: вы один раз согласуете график — и 19 л или 10 л бутыли приезжают регулярно, пустые забирают, бюджет на воду становится предсказуемым. В этой статье разберём, как устроена подписка, какую периодичность выбрать для разных профилей домохозяйства и офиса, и как оформить план на сайте или через Telegram-бот.</p>

<h2>Почему именно подписка: «вода закончилась» как системная проблема</h2>
<p>Питьевая вода — это базовое потребление, которое легко недооценить. Один человек выпивает в среднем 1,5–2,5 л воды в день, в жару — заметно больше. Прибавьте воду для приготовления пищи, чая, кофе и для гостей — и одна 19 л бутыль уходит у пары за 4–7 дней, у семьи из четырёх — за 3–5 дней. В офисе расход выше: 30 человек в жаркий месяц легко выпивают 8–12 бутылей в неделю.</p>
<p>Проблема не в воде как таковой, а в когнитивной нагрузке: нужно помнить, проверять остаток в кулере, оформлять заявку, сверяться с графиком водителя. На фоне работы, семейных дел и поездок этот контур регулярно ломается — и заканчивается тем, что в субботу вечером вода кончилась, а доставка начнётся только в понедельник. Подписка на воду переводит эту задачу из «надо помнить» в «уже работает в фоне».</p>

<h2>Что такое подписка на воду от Aqua Element</h2>
<p>Подписка — это запланированная регулярная доставка артезианской питьевой воды Aqua Element с выбранной вами периодичностью. Вы определяете три параметра:</p>
<ul>
<li><strong>Формат бутылей</strong>: 19 л (возвратная, для кулера или помпы), 10 л (одноразовая, удобна для квартир без кулера) — или комбинация обоих.</li>
<li><strong>Количество за одну доставку</strong>: 1, 2, 3, 4 и более бутылей.</li>
<li><strong>Периодичность</strong>: еженедельно, раз в две недели, ежемесячно — или ваш собственный график.</li>
</ul>
<p>Дальше вода приезжает сама. Курьер привозит свежие бутыли по согласованному графику, забирает пустые 19 л, при необходимости меняет помпу, оставляет квитанцию или отметку в системе. Все ваши доставки видны в личном кабинете и в Telegram-боте — там же можно перенести, пропустить или поставить на паузу.</p>

<h2>Шесть преимуществ подписки</h2>
<h3>1. Предсказуемый бюджет на воду в месяц</h3>
<p>Вы знаете заранее, сколько бутылей в месяц закажете и какая сумма уйдёт на питьевую воду. Это особенно важно для офисов, ведущих месячный бюджет, и для семей, планирующих коммунальные расходы.</p>

<h3>2. Никаких «вода закончилась в субботу»</h3>
<p>Подписка строится не на остатке, а на скорости расхода. Если вы пьёте 4 бутыли 19 л в месяц, доставка раз в неделю по одной бутыли удержит вас в комфортной зоне без скачков и пустых дней.</p>

<h3>3. Автоматический возврат тары</h3>
<p>На каждой доставке курьер забирает пустые 19 л бутыли и привозит полные. Цикл закрытый: вы не храните пустую тару неделями, а возвратные бутыли проходят промышленную мойку и санитарную обработку перед повторным розливом.</p>

<h3>4. Лучшая удельная цена за литр</h3>
<p>На определённые подписочные планы действуют скидки относительно разовых заказов — пересмотрите актуальные тарифы на странице <a href="/subscriptions">/subscriptions</a>. Подписка выгоднее, если вы и так стабильно заказываете воду каждую неделю или две.</p>

<h3>5. Гибкость: пауза, пропуск, отмена</h3>
<p>Уехали в отпуск на месяц? Поставьте подписку на паузу. Нужно пропустить одну доставку? Это делается в один клик. Хотите отменить — отмена доступна без штрафов в стандартных условиях. Подробности и текущие правила — на странице <a href="/subscriptions">/subscriptions</a>.</p>

<h3>6. Меньше пластика на единицу воды</h3>
<p>Возвратная 19 л бутыль обслуживает десятки циклов. Это меньше одноразового пластика на литр выпитой воды, чем у мелкой расфасовки. Если для вас важна экологическая нагрузка — подписка с акцентом на 19 л формат уменьшает её ощутимо.</p>

<h2>Как выбрать периодичность: таблица для разных профилей</h2>
<p>Эта таблица — практический ориентир, основанный на средних показателях расхода питьевой воды для типичных ташкентских домохозяйств и офисов. Цифры даны на жаркий сезон (апрель–октябрь); зимой расход обычно на 20–30% ниже.</p>

<table>
<thead>
<tr>
<th>Профиль</th>
<th>Расход в месяц, л</th>
<th>19 л бутылей в месяц</th>
<th>10 л бутылей в месяц</th>
<th>Периодичность</th>
</tr>
</thead>
<tbody>
<tr>
<td>Один человек</td>
<td>40–60</td>
<td>2–3</td>
<td>4–6</td>
<td>Раз в 2 недели</td>
</tr>
<tr>
<td>Пара (2 человека)</td>
<td>80–120</td>
<td>4–6</td>
<td>—</td>
<td>Еженедельно по 1 бутыли</td>
</tr>
<tr>
<td>Семья из 3</td>
<td>120–160</td>
<td>6–8</td>
<td>—</td>
<td>Еженедельно по 2 бутыли</td>
</tr>
<tr>
<td>Семья из 4–5</td>
<td>160–230</td>
<td>8–12</td>
<td>—</td>
<td>Еженедельно по 2–3 бутыли</td>
</tr>
<tr>
<td>Офис 10–15 человек</td>
<td>250–400</td>
<td>13–20</td>
<td>—</td>
<td>2 раза в неделю по 2–3 бутыли</td>
</tr>
<tr>
<td>Офис 30–50 человек</td>
<td>700–1200</td>
<td>37–63</td>
<td>—</td>
<td>2–3 раза в неделю по 5–8 бутылей</td>
</tr>
</tbody>
</table>

<p>Если вы не уверены, начните с консервативной оценки и через 2–3 доставки скорректируйте план — изменить количество бутылей и периодичность можно в любой момент в личном кабинете или Telegram-боте.</p>

<h2>Как оформить подписку: три канала</h2>
<p>Aqua Element поддерживает три равноправных канала оформления подписки. Вы можете начать в одном и продолжить в другом — данные синхронизированы.</p>
<ul>
<li><strong>Сайт</strong>: страница <a href="/subscriptions">/subscriptions</a> — выбор формата, количества, периодичности, адреса и оплаты в одном экране.</li>
<li><strong>Telegram-бот</strong>: <a href="https://t.me/aqua_element_bot">@aqua_element_bot</a>. Самый удобный канал для повторных клиентов — управление подпиской, история доставок, перенос, пауза и связь с менеджером в одном чате.</li>
<li><strong>Телефон</strong>: классический способ — оператор согласует график и оформит подписку за вас. Подходит, если вы оформляете для офиса или предпочитаете говорить голосом.</li>
</ul>

<h2>Логистика возвратной тары на подписке</h2>
<p>19 л бутыли Aqua Element — возвратные. На каждой доставке курьер привозит полные бутыли и забирает пустые в обмен. Залоговая стоимость и условия возврата прозрачны и фиксируются при оформлении подписки. Если вы только начинаете и пустых бутылей у вас нет, при первой доставке тара выдаётся под залог; при отмене подписки залог возвращается при сдаче бутылей.</p>
<p>Этот цикл — один из самых экологичных способов потреблять питьевую воду: одна бутыль обслуживает десятки циклов «розлив → доставка → потребление → возврат → мойка → розлив». Никаких сотен мелких пластиковых бутылок в мусоре.</p>

<h2>Как Aqua Element обеспечивает качество воды на подписке</h2>
<p>Подписка имеет смысл только тогда, когда за каждой доставкой стоит стабильное качество воды. Вода Aqua Element поступает из артезианской скважины глубиной около 120 метров в Куйичирчикском районе Ташкентской области и проходит 11 этапов очистки и обработки:</p>
<ol>
<li>Кварцевая фильтрация — удаление взвесей и крупных частиц.</li>
<li>Активированный уголь — снятие хлора, органики, привкусов и запахов.</li>
<li>Ионообменный умягчитель — снижение солей жёсткости.</li>
<li>Регенерация фильтра — восстановление ионообменной смолы.</li>
<li>Полипропиленовая мембрана 5 мкм — тонкая механическая очистка.</li>
<li>Полипропиленовая мембрана 1 мкм — финишная механическая очистка.</li>
<li>Обратный осмос (RO) — снятие до 99% растворённых примесей.</li>
<li>CIP-промывка мембраны — поддержание чистоты RO.</li>
<li>УФ-обеззараживание — инактивация микроорганизмов.</li>
<li>Минерализация — возврат сбалансированного профиля Ca, Mg, Na, K, HCO₃.</li>
<li>Озонирование — финальная санитарная обработка перед розливом.</li>
</ol>
<p>На выходе — стабильный профиль: TDS 30–50, pH 7,5; Ca 10–60, Mg 7–20, Na 5–15, K 1–4, HCO₃ 50–120 мг/л. Подробное описание каждого этапа — на странице <a href="/process/11-step-filtration">/process/11-step-filtration</a>. Деятельность находится под надзором Sanepid.</p>

<h2>Подписка как часть инфраструктуры дома и офиса</h2>
<p>Когда вода приезжает по графику, она перестаёт быть задачей и становится инфраструктурой — как электричество или интернет. Это особенно ощутимо для офисов: HR и АХО получают предсказуемый расход и не разбираются с заявками каждую неделю. Для семей подписка — это один из тех «маленьких автоматизмов», которые суммарно высвобождают часы в месяц и убирают мелкие, но раздражающие провалы.</p>
<p>Многие наши клиенты, начав с одной разовой заявки, переходят на подписку после второго или третьего заказа — обычно после первого «вода закончилась в воскресенье». Это естественная траектория: пока расход кажется случайным, разовая модель работает; как только вы видите, что заказываете воду каждые семь–десять дней, разовая модель начинает создавать ненужное трение. Подписка — это просто формализация уже существующего ритма потребления.</p>

<h2>Сезонность и корректировка плана</h2>
<p>В Ташкенте расход питьевой воды заметно меняется по сезонам. С июня по август расход легко вырастает на 30–50% по сравнению с зимой: жара, спорт, больше чая со льдом и холодной воды для гостей. Подписка позволяет учесть это без хлопот: летом вы добавляете одну дополнительную бутыль в неделю или временно переходите на более частую периодичность, осенью возвращаетесь к привычному графику. Все изменения вносятся в личном кабинете или в Telegram-боте за минуту и применяются со следующей доставки. Не нужно держать таблицу расхода — достаточно реагировать на наблюдаемое: если за месяц вы один раз остались без воды или заказывали внеплановую доставку, увеличьте план на одну позицию.</p>

<h2>Подписка для офисов: операционная экономия</h2>
<p>Для бизнеса подписка — это не только удобство, но и операционная экономия. Офис-менеджер перестаёт каждую неделю запрашивать заявку, согласовывать оплату, контролировать остаток в кулере. Бюджет на питьевую воду превращается в один периодический платёж — это упрощает учёт и снимает споры о «лишних бутылях в этом месяце». Для крупных офисов и сетей доступен ежемесячный счёт с НДС, договор и регулярная отчётность — детали и форматы оформления уточните при подключении на <a href="/subscriptions">/subscriptions</a> или у менеджера.</p>

<h2>Часто задаваемые вопросы</h2>

<h3>Как поставить подписку на паузу?</h3>
<p>В Telegram-боте <a href="https://t.me/aqua_element_bot">@aqua_element_bot</a> или в личном кабинете на сайте — раздел «Моя подписка», кнопка «Пауза». Также можно позвонить менеджеру. Возобновление — в один клик.</p>

<h3>Что делать, если я уезжаю на месяц?</h3>
<p>Поставьте подписку на паузу на нужный период или пропустите ближайшие доставки. Никаких штрафов и пересчётов вручную не требуется.</p>

<h3>Можно ли в середине подписки перейти с 10 л на 19 л (или наоборот)?</h3>
<p>Да. Формат бутылей меняется в настройках подписки — изменения вступают в силу со следующей доставки. Если у вас нет кулера и вы хотите попробовать 19 л — добавьте помпу в заказ.</p>

<h3>Какая минимальная продолжительность подписки?</h3>
<p>Минимального обязательного срока нет. Вы можете начать подписку и отменить её в любой момент по стандартным условиям, описанным на <a href="/subscriptions">/subscriptions</a>.</p>

<h3>Есть ли плата за подключение или активацию?</h3>
<p>Нет, активация подписки бесплатна. Залог за возвратные 19 л бутыли начисляется при первой доставке (если у вас ещё нет бутылей) и возвращается при сдаче тары.</p>

<h3>Можно ли в одной подписке заказывать одновременно 19 л и 10 л?</h3>
<p>Да. Подписка поддерживает смешанные позиции: например, 2 бутыли 19 л на кухню и 4 бутыли 10 л в детскую — в одной доставке.</p>

<h3>Как идёт оплата — раз в месяц или за каждую доставку?</h3>
<p>Доступны оба варианта: по факту каждой доставки или ежемесячным счётом для офисов. Способ оплаты выбирается при оформлении и меняется в настройках подписки.</p>

<h3>Где увидеть ближайшие доставки?</h3>
<p>В Telegram-боте — команда «Мои доставки», там виден график на ближайшие недели. На сайте — раздел «Моя подписка» в личном кабинете.</p>

<h3>Что, если в день доставки никого не будет дома?</h3>
<p>Перенесите доставку через бот или личный кабинет. Курьер также свяжется с вами заранее, если в графике появятся вопросы.</p>

<h3>Подписка действует в Ташкентской области?</h3>
<p>Да, доставка покрывает Ташкент и Ташкентскую область. Точную зону по вашему адресу проверьте при оформлении на <a href="/subscriptions">/subscriptions</a>.</p>

<h2>Готовы оформить подписку?</h2>
<p>Если вы заказываете воду стабильно, подписка — это просто более удобная и предсказуемая форма того же потребления. Откройте страницу <a href="/subscriptions">/subscriptions</a>, выберите формат, количество и график — и забудьте про «надо заказать воду» как класс задач. Если вы предпочитаете чат — оформите и управляйте подпиской в Telegram-боте <a href="https://t.me/aqua_element_bot">@aqua_element_bot</a>. Качество воды за каждой доставкой обеспечивает 11-этапная очистка артезианской скважины — детали на <a href="/process/11-step-filtration">/process/11-step-filtration</a>.</p>

<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "FAQPage",
  "mainEntity": [
    {"@type": "Question", "name": "Как поставить подписку на паузу?", "acceptedAnswer": {"@type": "Answer", "text": "В Telegram-боте @aqua_element_bot или в личном кабинете на сайте, раздел Моя подписка, кнопка Пауза. Возобновление в один клик."}},
    {"@type": "Question", "name": "Что делать, если я уезжаю на месяц?", "acceptedAnswer": {"@type": "Answer", "text": "Поставьте подписку на паузу на нужный период или пропустите ближайшие доставки. Никаких штрафов и пересчётов вручную не требуется."}},
    {"@type": "Question", "name": "Можно ли перейти с 10 л на 19 л в середине подписки?", "acceptedAnswer": {"@type": "Answer", "text": "Да. Формат бутылей меняется в настройках подписки, изменения вступают в силу со следующей доставки."}},
    {"@type": "Question", "name": "Какая минимальная продолжительность подписки?", "acceptedAnswer": {"@type": "Answer", "text": "Минимального обязательного срока нет. Вы можете начать и отменить подписку в любой момент по стандартным условиям."}},
    {"@type": "Question", "name": "Есть ли плата за подключение?", "acceptedAnswer": {"@type": "Answer", "text": "Нет, активация подписки бесплатна. Залог за возвратные 19 л бутыли начисляется при первой доставке и возвращается при сдаче тары."}},
    {"@type": "Question", "name": "Можно ли в одной подписке заказывать 19 л и 10 л?", "acceptedAnswer": {"@type": "Answer", "text": "Да. Подписка поддерживает смешанные позиции в одной доставке."}},
    {"@type": "Question", "name": "Как идёт оплата — раз в месяц или за каждую доставку?", "acceptedAnswer": {"@type": "Answer", "text": "Доступны оба варианта: по факту каждой доставки или ежемесячным счётом для офисов."}},
    {"@type": "Question", "name": "Где увидеть ближайшие доставки?", "acceptedAnswer": {"@type": "Answer", "text": "В Telegram-боте команда Мои доставки, на сайте раздел Моя подписка в личном кабинете."}}
  ]
}
</script>
"""
        },
        "uz": {
            "title": "Toshkentda suv obunasi: avtoyetkazib berish, qaytariladigan idishlar, uzilishsiz",
            "excerpt": "Aqua Element’dan Toshkentda suv obunasi: 19 l va 10 l idishlarning siz tanlagan davriylikda yetkazib berilishi. Bo‘sh idish keyingi yetkazib berishda olib ketiladi, byudjet bashoratli. Saytda, Telegram-botda yoki telefon orqali boshqaring.",
            "meta_title": "Toshkentda suv obunasi — 19 l va 10 l avtoyetkazib berish | Aqua Element",
            "meta_description": "Toshkentda suv obunasi: 19 l va 10 l avtoyetkazib berish, idish qaytarish, moslashuvchan jadval, Telegram-bot va saytda rasmiylashtirish.",
            "content": """
<p class="lead">«Shanba kuni kechqurun suv tugab qoldi» — ko‘pchilik toshkentlik oilalar va ofislar uchun tanish manzara. Issiq iqlimda ichimlik suv sarfi sezdirmay oshadi, bir martalik buyurtmalarni esa har safar qayta rasmiylashtirishga to‘g‘ri keladi. Aqua Element’dan Toshkentda suv obunasi bu yukni olib tashlaydi: bir marta jadvalni kelishasiz va 19 l yoki 10 l idishlar muntazam keladi, bo‘shlari olib ketiladi, suv uchun byudjet oldindan ma’lum bo‘ladi. Ushbu maqolada obuna qanday ishlashi, turli uy va ofis profillari uchun qaysi davriylikni tanlash, hamda saytda yoki Telegram-bot orqali rejani qanday rasmiylashtirishni ko‘rib chiqamiz.</p>

<h2>Nima uchun aynan obuna: «suv tugadi» — tizimli muammo</h2>
<p>Ichimlik suv — bu asosiy iste’mol bo‘lib, uni baholashda ko‘pincha xato qilamiz. Bir kishi kuniga o‘rtacha 1,5–2,5 l suv ichadi, jazirama kunlari esa sezilarli darajada ko‘proq. Bunga ovqat tayyorlash, choy, kofe va mehmonlar uchun suvni qo‘shing — va bitta 19 l idish juftlikda 4–7 kunda, to‘rt kishilik oilada 3–5 kunda tugaydi. Ofisda sarf yanada yuqori: 30 kishi issiq oyda haftasiga 8–12 idish ichib qo‘yadi.</p>
<p>Muammo suvning o‘zida emas, balki kognitiv yukda: kulerdagi qoldiqni eslab turish, buyurtma berish, haydovchi jadvali bilan kelishish kerak. Ish, oilaviy ishlar va safarlar fonida bu zanjir muntazam buziladi va shanba oqshomi suv tugaydi, yetkazib berish esa faqat dushanbada boshlanadi. Suv obunasi bu vazifani «eslash kerak» darajasidan «fonda allaqachon ishlamoqda» darajasiga olib chiqadi.</p>

<h2>Aqua Element’dan suv obunasi nima</h2>
<p>Obuna — bu Aqua Element artezian ichimlik suvini siz tanlagan davriylikda muntazam yetkazib berish. Siz uchta parametrni belgilaysiz:</p>
<ul>
<li><strong>Idish formati</strong>: 19 l (qaytariladigan, kuler yoki pompa uchun), 10 l (bir martalik, kulersiz xonadonlarga qulay) yoki ikkalasining kombinatsiyasi.</li>
<li><strong>Bir yetkazib berishdagi soni</strong>: 1, 2, 3, 4 va undan ortiq idish.</li>
<li><strong>Davriylik</strong>: haftalik, ikki haftada bir, oylik yoki o‘zingizning shaxsiy jadvalingiz.</li>
</ul>
<p>Keyin suv o‘zi keladi. Kuryer kelishilgan jadval bo‘yicha yangi idishlarni olib keladi, bo‘sh 19 l idishlarni olib ketadi, kerak bo‘lsa pompani almashtiradi, kvitansiya qoldiradi yoki tizimda belgilab qo‘yadi. Barcha yetkazib berishlaringiz shaxsiy kabinetda va Telegram-botda ko‘rinadi — o‘sha yerdan ko‘chirish, o‘tkazib yuborish yoki pauzaga qo‘yish mumkin.</p>

<h2>Obunaning oltita afzalligi</h2>
<h3>1. Suv uchun oydagi byudjet oldindan ma’lum</h3>
<p>Bir oyda qancha idish buyurtma qilishingiz va qancha pul ketishi oldindan ma’lum bo‘ladi. Bu oylik byudjetni yuritadigan ofislar va kommunal xarajatlarni rejalashtiruvchi oilalar uchun ayniqsa muhim.</p>

<h3>2. «Shanba kuni suv tugadi» bo‘lmaydi</h3>
<p>Obuna qoldiqqa emas, sarf tezligiga asoslanadi. Agar siz oyda 4 ta 19 l idishni iste’mol qilsangiz, haftada bir idishlik yetkazib berish sizni qulay zonada ushlab turadi.</p>

<h3>3. Idishni avtomatik qaytarish</h3>
<p>Har yetkazib berishda kuryer bo‘sh 19 l idishlarni olib ketadi va to‘lalarini olib keladi. Sikl yopiq: bo‘sh idishlarni haftalab saqlamaysiz, qaytariladigan idishlar esa qayta to‘ldirishdan oldin sanoat yuvinishi va sanitariya ishlovidan o‘tadi.</p>

<h3>4. Litri uchun yaxshiroq narx</h3>
<p>Ayrim obuna rejalariga bir martalik buyurtmalarga nisbatan chegirmalar amal qiladi — joriy tariflarni <a href="/subscriptions">/subscriptions</a> sahifasida ko‘ring. Agar siz har hafta yoki ikki haftada barqaror suv buyurtma qilsangiz, obuna foydaliroq.</p>

<h3>5. Moslashuvchanlik: pauza, o‘tkazib yuborish, bekor qilish</h3>
<p>Bir oyga ta’tilga ketdingizmi? Obunani pauzaga qo‘ying. Bitta yetkazib berishni o‘tkazib yuborish kerakmi? Bu bir bosishda bajariladi. Bekor qilish ham standart shartlarda jarimasiz mavjud. Tafsilotlar va joriy qoidalar — <a href="/subscriptions">/subscriptions</a> sahifasida.</p>

<h3>6. Litriga kamroq plastik</h3>
<p>Qaytariladigan 19 l idish o‘nlab sikllarga xizmat qiladi. Bu, mayda paketlash bilan solishtirganda, ichilgan suvning litriga kamroq bir martalik plastik degani.</p>

<h2>Davriylikni qanday tanlash: turli profillar uchun jadval</h2>
<p>Bu jadval — odatiy toshkentlik xonadon va ofislar uchun ichimlik suv sarfining o‘rtacha ko‘rsatkichlariga asoslangan amaliy yo‘riqnoma. Raqamlar issiq mavsumga (aprel–oktyabr) berilgan; qishda sarf odatda 20–30% pastroq bo‘ladi.</p>

<table>
<thead>
<tr>
<th>Profil</th>
<th>Oyda sarf, l</th>
<th>19 l idish/oy</th>
<th>10 l idish/oy</th>
<th>Davriylik</th>
</tr>
</thead>
<tbody>
<tr>
<td>Bir kishi</td>
<td>40–60</td>
<td>2–3</td>
<td>4–6</td>
<td>2 haftada bir</td>
</tr>
<tr>
<td>Juftlik (2 kishi)</td>
<td>80–120</td>
<td>4–6</td>
<td>—</td>
<td>Haftada 1 idish</td>
</tr>
<tr>
<td>3 kishilik oila</td>
<td>120–160</td>
<td>6–8</td>
<td>—</td>
<td>Haftada 2 idish</td>
</tr>
<tr>
<td>4–5 kishilik oila</td>
<td>160–230</td>
<td>8–12</td>
<td>—</td>
<td>Haftada 2–3 idish</td>
</tr>
<tr>
<td>10–15 kishilik ofis</td>
<td>250–400</td>
<td>13–20</td>
<td>—</td>
<td>Haftada 2 marta 2–3 idish</td>
</tr>
<tr>
<td>30–50 kishilik ofis</td>
<td>700–1200</td>
<td>37–63</td>
<td>—</td>
<td>Haftada 2–3 marta 5–8 idish</td>
</tr>
</tbody>
</table>

<p>Agar shubhada bo‘lsangiz, ehtiyotkor baho bilan boshlang va 2–3 yetkazib berishdan so‘ng rejani moslang — idishlar soni va davriylikni istalgan paytda shaxsiy kabinet yoki Telegram-botda o‘zgartirish mumkin.</p>

<h2>Obunani qanday rasmiylashtirish: uchta kanal</h2>
<p>Aqua Element obunani rasmiylashtirishning uchta teng kanalini qo‘llab-quvvatlaydi. Birida boshlab, boshqasida davom ettirishingiz mumkin — ma’lumotlar sinxronlashtiriladi.</p>
<ul>
<li><strong>Sayt</strong>: <a href="/subscriptions">/subscriptions</a> sahifasi — format, son, davriylik, manzil va to‘lovni bitta ekranda tanlash.</li>
<li><strong>Telegram-bot</strong>: <a href="https://t.me/aqua_element_bot">@aqua_element_bot</a>. Takroriy mijozlar uchun eng qulay kanal — obunani boshqarish, yetkazib berish tarixi, ko‘chirish, pauza va menejer bilan bog‘lanish bitta chatda.</li>
<li><strong>Telefon</strong>: klassik usul — operator jadvalni kelishadi va obunani siz uchun rasmiylashtiradi. Ofis uchun rasmiylashtirsangiz yoki ovozli muloqotni afzal ko‘rsangiz, mos.</li>
</ul>

<h2>Obunada qaytariladigan idish logistikasi</h2>
<p>Aqua Element 19 l idishlari qaytariladigan. Har yetkazib berishda kuryer to‘la idishlarni olib keladi va bo‘shlarini almashinuvga oladi. Garov qiymati va qaytarish shartlari shaffof, obunani rasmiylashtirishda qayd etiladi. Agar siz endi boshlayotgan bo‘lsangiz va bo‘sh idishlaringiz bo‘lmasa, birinchi yetkazib berishda idish garov asosida beriladi; obunani bekor qilganingizda idishlarni topshirish bilan garov qaytariladi.</p>
<p>Bu sikl ichimlik suvni iste’mol qilishning eng ekologik usullaridan biri: bitta idish «to‘ldirish → yetkazib berish → iste’mol → qaytarish → yuvish → to‘ldirish» tsikllarining o‘nlab sikllariga xizmat qiladi.</p>

<h2>Aqua Element obunada suv sifatini qanday ta’minlaydi</h2>
<p>Obunaning ma’nosi har bir yetkazib berish ortida barqaror suv sifati turgan taqdirdagina bor. Aqua Element suvi Toshkent viloyati Quyichirchiq tumanida joylashgan taxminan 120 metr chuqurlikdagi artezian quduqdan olinadi va 11 bosqichli tozalash va ishlovdan o‘tadi:</p>
<ol>
<li>Kvars filtratsiyasi — qattiq zarralar va yirik qoldiqlarni olib tashlash.</li>
<li>Faollashgan ko‘mir — xlor, organika, ta’m va hidlarni olib tashlash.</li>
<li>Ion almashinuvchi yumshatkich — qattiqlik tuzlarini kamaytirish.</li>
<li>Filtr regeneratsiyasi — ion almashinuv smolasini tiklash.</li>
<li>Polipropilen membrana 5 mkm — nozik mexanik tozalash.</li>
<li>Polipropilen membrana 1 mkm — yakuniy mexanik tozalash.</li>
<li>Teskari osmoz (RO) — erigan aralashmalarning 99%gachasini olib tashlash.</li>
<li>Membranani CIP yuvish — RO tozaligini saqlash.</li>
<li>UB-zararsizlantirish — mikroorganizmlarni inaktiv qilish.</li>
<li>Mineralizatsiya — Ca, Mg, Na, K, HCO₃ ning muvozanatli profilini qaytarish.</li>
<li>Ozonlash — to‘ldirishdan oldingi yakuniy sanitariya ishlovi.</li>
</ol>
<p>Chiqishda barqaror profil: TDS 30–50, pH 7,5; Ca 10–60, Mg 7–20, Na 5–15, K 1–4, HCO₃ 50–120 mg/l. Har bosqichning batafsil tavsifi — <a href="/process/11-step-filtration">/process/11-step-filtration</a> sahifasida. Faoliyat Sanepid nazorati ostida.</p>

<h2>Obuna — uy va ofis infratuzilmasining qismi sifatida</h2>
<p>Suv jadval bo‘yicha kelganda, u vazifa bo‘lishdan to‘xtaydi va elektr yoki internetdek infratuzilmaga aylanadi. Bu ofislar uchun ayniqsa sezilarli: HR va xo‘jalik xizmati bashoratli sarfni oladi va har hafta buyurtmalar bilan bosh qotirmaydi. Oilalar uchun obuna — oyiga soatlarni bo‘shatib, mayda lekin asabiy uzilishlarni olib tashlaydigan «kichik avtomatizmlar» qatoridan.</p>
<p>Ko‘plab mijozlarimiz birinchi yoki ikkinchi buyurtmadan keyin obunaga o‘tadilar — odatda «yakshanba kuni suv tugab qoldi» tajribasidan keyin. Bu tabiiy yo‘l: agar siz yetti–o‘n kunda bir bor suv buyurtma qilayotgan bo‘lsangiz, bir martalik model keraksiz ishqalanish hosil qiladi. Obuna — bu mavjud iste’mol ritmini rasmiylashtirishning eng oddiy usuli.</p>

<h2>Mavsumiylik va rejani moslash</h2>
<p>Toshkentda ichimlik suv sarfi mavsumlar bo‘yicha sezilarli o‘zgaradi. Iyundan avgustgacha sarf qishga nisbatan 30–50% gacha oshishi mumkin: jazirama, sport, ko‘proq muzli choy va mehmonlar uchun sovuq suv. Obuna buni qiyinchiliksiz hisobga olish imkonini beradi: yozda haftasiga bitta qo‘shimcha idish qo‘shasiz yoki vaqtincha tez-tez davriylikka o‘tasiz, kuzda esa odatdagi jadvalga qaytasiz. Barcha o‘zgarishlar shaxsiy kabinet yoki Telegram-botda bir daqiqada kiritiladi va keyingi yetkazib berishdan kuchga kiradi. Sarf jadvalini yuritish shart emas — kuzatilgan natijaga reaksiya qilish kifoya: agar bir oyda bir marta suvsiz qolgan bo‘lsangiz yoki rejadan tashqari yetkazib berish buyurtma qilgan bo‘lsangiz, rejani bitta pozitsiyaga oshiring.</p>

<h2>Ofislar uchun obuna: operatsion tejamkorlik</h2>
<p>Biznes uchun obuna — bu nafaqat qulaylik, balki operatsion tejamkorlik. Ofis menejer har hafta buyurtma berishni so‘rashni, to‘lovni kelishishni, kulerdagi qoldiqni nazorat qilishni to‘xtatadi. Ichimlik suv byudjeti bitta davriy to‘lovga aylanadi — bu hisob-kitobni soddalashtiradi va «bu oydagi ortiqcha idishlar» haqidagi bahslarni olib tashlaydi. Yirik ofislar va tarmoqlar uchun QQS bilan oylik hisob, shartnoma va muntazam hisobotlar mavjud — tafsilotlar va rasmiylashtirish formatlarini ulash paytida <a href="/subscriptions">/subscriptions</a> da yoki menejerdan aniqlang.</p>

<h2>Tez-tez beriladigan savollar</h2>

<h3>Obunani qanday pauzaga qo‘yish mumkin?</h3>
<p><a href="https://t.me/aqua_element_bot">@aqua_element_bot</a> Telegram-botida yoki saytdagi shaxsiy kabinetda — «Mening obunam» bo‘limi, «Pauza» tugmasi. Menejerga qo‘ng‘iroq qilish ham mumkin. Davom ettirish — bir bosishda.</p>

<h3>Bir oyga safarga ketsam-chi?</h3>
<p>Obunani kerakli muddatga pauzaga qo‘ying yoki yaqin yetkazib berishlarni o‘tkazib yuboring. Hech qanday jarima yoki qo‘lda qayta hisoblash kerak emas.</p>

<h3>Obuna o‘rtasida 10 l dan 19 l ga (yoki aksincha) o‘tish mumkinmi?</h3>
<p>Ha. Idish formati obuna sozlamalarida o‘zgartiriladi — o‘zgarishlar keyingi yetkazib berishdan kuchga kiradi. Agar kuleringiz bo‘lmasa va 19 l ni sinab ko‘rmoqchi bo‘lsangiz, buyurtmaga pompani qo‘shing.</p>

<h3>Obunaning minimal muddati qancha?</h3>
<p>Majburiy minimal muddat yo‘q. Obunani istalgan paytda <a href="/subscriptions">/subscriptions</a> da ko‘rsatilgan standart shartlar bo‘yicha boshlash va bekor qilish mumkin.</p>

<h3>Ulash yoki faollashtirish uchun to‘lov bormi?</h3>
<p>Yo‘q, obunani faollashtirish bepul. Qaytariladigan 19 l idish uchun garov birinchi yetkazib berishda hisoblanadi (agar idishingiz bo‘lmasa) va idishni topshirishda qaytariladi.</p>

<h3>Bitta obunada bir vaqtda 19 l va 10 l buyurtma qilish mumkinmi?</h3>
<p>Ha. Obuna aralash pozitsiyalarni qo‘llab-quvvatlaydi: masalan, oshxona uchun 2 ta 19 l va bolalar xonasi uchun 4 ta 10 l — bitta yetkazib berishda.</p>

<h3>To‘lov qanday — oyiga bir marta yoki har yetkazib berishga?</h3>
<p>Ikkala variant ham mavjud: har yetkazib berish faktiga ko‘ra yoki ofislar uchun oylik hisob-kitob. To‘lov usuli rasmiylashtirishda tanlanadi va sozlamalarda o‘zgartiriladi.</p>

<h3>Yaqin yetkazib berishlarni qayerda ko‘rish mumkin?</h3>
<p>Telegram-botda — «Mening yetkazib berishlarim» buyrug‘i, u yerda yaqin haftalar jadvali ko‘rinadi. Saytda — shaxsiy kabinetdagi «Mening obunam» bo‘limi.</p>

<h3>Yetkazib berish kunida uyda hech kim bo‘lmasa-chi?</h3>
<p>Yetkazib berishni bot yoki shaxsiy kabinet orqali ko‘chiring. Kuryer ham jadvalda savollar yuzaga kelsa, sizga oldindan bog‘lanadi.</p>

<h3>Obuna Toshkent viloyatida amal qiladimi?</h3>
<p>Ha, yetkazib berish Toshkent shahri va Toshkent viloyatini qamrab oladi. Manzilingiz bo‘yicha aniq zonani <a href="/subscriptions">/subscriptions</a> da rasmiylashtirishda tekshiring.</p>

<h2>Obunani rasmiylashtirishga tayyormisiz?</h2>
<p>Agar suvni barqaror buyurtma qilsangiz, obuna shu iste’molning yanada qulay va bashoratli shakli. <a href="/subscriptions">/subscriptions</a> sahifasini oching, format, son va jadvalni tanlang — va «suv buyurtma qilish kerak» degan vazifalar sinfini unuting. Agar chatni afzal ko‘rsangiz — Telegram-bot <a href="https://t.me/aqua_element_bot">@aqua_element_bot</a> da rasmiylashtiring va boshqaring. Har bir yetkazib berish ortidagi suv sifatini artezian quduqning 11 bosqichli tozalashi ta’minlaydi — tafsilotlar <a href="/process/11-step-filtration">/process/11-step-filtration</a> sahifasida.</p>

<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "FAQPage",
  "mainEntity": [
    {"@type": "Question", "name": "Obunani qanday pauzaga qo‘yish mumkin?", "acceptedAnswer": {"@type": "Answer", "text": "Telegram-bot @aqua_element_bot da yoki saytdagi shaxsiy kabinetda Mening obunam bo‘limidagi Pauza tugmasi orqali. Davom ettirish bir bosishda."}},
    {"@type": "Question", "name": "Bir oyga safarga ketsam-chi?", "acceptedAnswer": {"@type": "Answer", "text": "Obunani kerakli muddatga pauzaga qo‘ying yoki yaqin yetkazib berishlarni o‘tkazib yuboring. Jarima yoki qo‘lda qayta hisoblash kerak emas."}},
    {"@type": "Question", "name": "Obuna o‘rtasida 10 l dan 19 l ga o‘tish mumkinmi?", "acceptedAnswer": {"@type": "Answer", "text": "Ha. Format obuna sozlamalarida o‘zgartiriladi va keyingi yetkazib berishdan kuchga kiradi."}},
    {"@type": "Question", "name": "Obunaning minimal muddati qancha?", "acceptedAnswer": {"@type": "Answer", "text": "Majburiy minimal muddat yo‘q. Standart shartlar bo‘yicha istalgan paytda boshlash va bekor qilish mumkin."}},
    {"@type": "Question", "name": "Faollashtirish uchun to‘lov bormi?", "acceptedAnswer": {"@type": "Answer", "text": "Yo‘q, obunani faollashtirish bepul. 19 l idish uchun garov birinchi yetkazib berishda hisoblanadi va idishni topshirishda qaytariladi."}},
    {"@type": "Question", "name": "Bitta obunada 19 l va 10 l buyurtma qilish mumkinmi?", "acceptedAnswer": {"@type": "Answer", "text": "Ha. Obuna bitta yetkazib berishda aralash pozitsiyalarni qo‘llab-quvvatlaydi."}},
    {"@type": "Question", "name": "To‘lov oyiga bir marta yoki har yetkazib berishga?", "acceptedAnswer": {"@type": "Answer", "text": "Ikkala variant ham mavjud: har yetkazib berish faktiga ko‘ra yoki ofislar uchun oylik hisob-kitob."}},
    {"@type": "Question", "name": "Yaqin yetkazib berishlarni qayerda ko‘rish mumkin?", "acceptedAnswer": {"@type": "Answer", "text": "Telegram-botda Mening yetkazib berishlarim buyrug‘i va saytdagi Mening obunam bo‘limi."}}
  ]
}
</script>
"""
        },
        "en": {
            "title": "Water subscription in Tashkent: automatic delivery, returnable bottles, no run-outs",
            "excerpt": "An Aqua Element water subscription in Tashkent is a scheduled, recurring delivery of 19 L and 10 L bottles on the cadence you choose. Empties are collected on the next visit, your monthly water budget becomes predictable. Manage on web, Telegram bot, or phone.",
            "meta_title": "Water subscription in Tashkent — 19 L and 10 L delivery plans | Aqua Element",
            "meta_description": "Water subscription in Tashkent: scheduled 19 L and 10 L delivery, returnable cycle, flexible cadence. Sign up via Aqua Element Telegram bot or web.",
            "content": """
<p class="lead">"We ran out of water on Saturday night" is a familiar scene for many Tashkent households and offices. The hot climate quietly pushes drinking-water consumption higher than people expect, and one-off orders have to be placed reactively. An Aqua Element water subscription in Tashkent removes that load: you agree on a schedule once, and 19 L or 10 L bottles arrive on cadence, empties go back automatically, and the monthly water budget becomes predictable. This article explains how the subscription works, what cadence to pick for different household and office profiles, and how to set up a plan on the website or via the Telegram bot.</p>

<h2>Why a subscription: "we ran out" is a systemic problem</h2>
<p>Drinking water is basic consumption that is easy to underestimate. One person drinks 1.5–2.5 L per day on average, and noticeably more in heat. Add cooking, tea, coffee, and guests — and a single 19 L bottle lasts a couple 4–7 days, a family of four 3–5 days. In offices the rate is higher: 30 people in a hot month easily drink 8–12 bottles per week.</p>
<p>The issue is not the water itself but the cognitive load: someone has to remember the cooler level, place the order, sync with the courier window. Layered on top of work, family, and travel, that loop fails on a regular basis — and you end up out of water on a Saturday with nothing arriving until Monday. A water subscription moves the task from "I need to remember" to "it's already running in the background".</p>

<h2>What an Aqua Element water subscription is</h2>
<p>A subscription is a scheduled, recurring delivery of Aqua Element artesian drinking water on a cadence you choose. You set three parameters:</p>
<ul>
<li><strong>Bottle format</strong>: 19 L (returnable, for cooler or pump), 10 L (single-use, convenient for apartments without a cooler), or a combination.</li>
<li><strong>Quantity per delivery</strong>: 1, 2, 3, 4 or more bottles.</li>
<li><strong>Cadence</strong>: weekly, biweekly, monthly, or your own custom rhythm.</li>
</ul>
<p>After that, the water comes to you. The courier delivers fresh bottles on the agreed schedule, picks up the empty 19 L bottles, swaps the pump if you ask, and leaves a receipt or system mark. Every delivery shows up in your account and in the Telegram bot — and that's where you reschedule, skip, or pause.</p>

<h2>Six benefits of a subscription</h2>
<h3>1. Predictable monthly water budget</h3>
<p>You know in advance how many bottles per month you will order and what the spend will be. That matters for offices on a monthly budget and for households tracking utility-style costs.</p>

<h3>2. No "out of water on Saturday"</h3>
<p>A subscription is built on consumption rate, not on running stock. If you go through 4 bottles of 19 L per month, a weekly cadence of one bottle keeps you in a comfortable zone with no spikes and no empty days.</p>

<h3>3. Automatic bottle return</h3>
<p>On every delivery the courier picks up the empty 19 L bottles and drops off full ones. The cycle is closed: you don't store empty bottles for weeks, and the returnable bottles go through industrial washing and sanitation before being filled again.</p>

<h3>4. Better per-litre value</h3>
<p>Certain subscription plans carry a discount versus one-off orders — check current pricing on <a href="/subscriptions">/subscriptions</a>. The subscription wins on value if you're already ordering steadily every week or two.</p>

<h3>5. Flexibility: pause, skip, cancel</h3>
<p>Travelling for a month? Pause the subscription. Need to skip one delivery? One click. Cancellation is available without penalties under standard terms. Specifics and current rules live on <a href="/subscriptions">/subscriptions</a>.</p>

<h3>6. Less plastic per litre consumed</h3>
<p>A returnable 19 L bottle serves dozens of cycles. That is less single-use plastic per litre of water consumed than small-pack alternatives. If environmental footprint matters to you, a 19 L-heavy subscription reduces it noticeably.</p>

<h2>How to choose your cadence: a profile-based table</h2>
<p>This table is a practical guide based on average drinking-water consumption for typical Tashkent households and offices. Numbers reflect the hot season (April–October); winter is usually 20–30% lower.</p>

<table>
<thead>
<tr>
<th>Profile</th>
<th>Monthly consumption, L</th>
<th>19 L bottles/month</th>
<th>10 L bottles/month</th>
<th>Cadence</th>
</tr>
</thead>
<tbody>
<tr>
<td>One person</td>
<td>40–60</td>
<td>2–3</td>
<td>4–6</td>
<td>Biweekly</td>
</tr>
<tr>
<td>Couple (2 people)</td>
<td>80–120</td>
<td>4–6</td>
<td>—</td>
<td>Weekly, 1 bottle</td>
</tr>
<tr>
<td>Family of 3</td>
<td>120–160</td>
<td>6–8</td>
<td>—</td>
<td>Weekly, 2 bottles</td>
</tr>
<tr>
<td>Family of 4–5</td>
<td>160–230</td>
<td>8–12</td>
<td>—</td>
<td>Weekly, 2–3 bottles</td>
</tr>
<tr>
<td>Office of 10–15</td>
<td>250–400</td>
<td>13–20</td>
<td>—</td>
<td>Twice a week, 2–3 bottles</td>
</tr>
<tr>
<td>Office of 30–50</td>
<td>700–1200</td>
<td>37–63</td>
<td>—</td>
<td>2–3 times a week, 5–8 bottles</td>
</tr>
</tbody>
</table>

<p>If you are unsure, start on the conservative side and adjust after 2–3 deliveries — bottle count and cadence can be changed at any time in your account or the Telegram bot.</p>

<h2>How to subscribe: three channels</h2>
<p>Aqua Element supports three equally valid subscription channels. You can start in one and continue in another — the data is synced.</p>
<ul>
<li><strong>Website</strong>: the page <a href="/subscriptions">/subscriptions</a> — choose format, quantity, cadence, address, and payment in one screen.</li>
<li><strong>Telegram bot</strong>: <a href="https://t.me/aqua_element_bot">@aqua_element_bot</a>. The most convenient channel for repeat customers — manage your subscription, view delivery history, reschedule, pause, and message a manager in one chat.</li>
<li><strong>Phone</strong>: the classic option — an operator agrees on a schedule and sets up the subscription for you. Best if you are setting up an office plan or prefer voice.</li>
</ul>

<h2>Returnable-bottle logistics on a subscription</h2>
<p>Aqua Element 19 L bottles are returnable. On every delivery the courier brings full bottles and picks up the empties in exchange. Deposit value and return rules are transparent and confirmed at sign-up. If you are starting fresh and have no empty bottles, the bottles are issued under deposit on the first delivery; on cancellation the deposit is refunded when the bottles are returned.</p>
<p>This cycle is one of the most environmentally efficient ways to consume drinking water: a single bottle serves dozens of "fill → deliver → consume → return → wash → fill" cycles. No piles of small plastic bottles in the bin.</p>

<h2>How Aqua Element keeps subscription quality consistent</h2>
<p>A subscription is only worth it if every delivery delivers consistent water quality. Aqua Element water comes from an artesian well at roughly 120 m depth in the Quyi Chirchiq District of Tashkent Region and goes through 11 stages of treatment:</p>
<ol>
<li>Quartz filtration — removal of suspended solids and coarse particles.</li>
<li>Activated carbon — removal of chlorine, organics, taste, and odour.</li>
<li>Ion-exchange softener — reduction of hardness salts.</li>
<li>Filter regeneration — restoration of the ion-exchange resin.</li>
<li>5 µm polypropylene membrane — fine mechanical filtration.</li>
<li>1 µm polypropylene membrane — final mechanical polish.</li>
<li>Reverse osmosis (RO) — removal of up to 99% of dissolved impurities.</li>
<li>Membrane CIP wash — keeping the RO clean.</li>
<li>UV disinfection — inactivation of microorganisms.</li>
<li>Mineralisation — restoration of a balanced Ca, Mg, Na, K, HCO₃ profile.</li>
<li>Ozonation — final sanitary treatment before bottling.</li>
</ol>
<p>The output is a stable profile: TDS 30–50, pH 7.5; Ca 10–60, Mg 7–20, Na 5–15, K 1–4, HCO₃ 50–120 mg/L. Each stage is described on <a href="/process/11-step-filtration">/process/11-step-filtration</a>. The operation is overseen by Sanepid.</p>

<h2>Subscription as household and office infrastructure</h2>
<p>When water arrives on schedule, it stops being a task and becomes infrastructure — like electricity or internet. Offices feel this most: HR and facilities get a predictable spend and stop dealing with weekly orders. For families, a subscription is one of those small automations that, taken together, free up hours per month and remove minor but irritating gaps.</p>

<h2>Frequently asked questions</h2>

<h3>How do I pause a subscription?</h3>
<p>In the Telegram bot <a href="https://t.me/aqua_element_bot">@aqua_element_bot</a> or in your website account — section "My subscription", button "Pause". You can also call a manager. Resuming is one click.</p>

<h3>What if I'm travelling for a month?</h3>
<p>Pause the subscription for that period or skip the upcoming deliveries. There are no penalties and no manual re-billing.</p>

<h3>Can I switch from 10 L to 19 L (or back) mid-subscription?</h3>
<p>Yes. The bottle format is changed in subscription settings — the change applies from the next delivery. If you don't have a cooler and want to try 19 L, add a pump to your order.</p>

<h3>What is the minimum subscription length?</h3>
<p>There is no mandatory minimum length. You can start and cancel at any time under the standard terms described on <a href="/subscriptions">/subscriptions</a>.</p>

<h3>Is there a setup or activation fee?</h3>
<p>No, subscription activation is free. A deposit on the returnable 19 L bottles is charged on the first delivery (if you have no bottles yet) and refunded when the bottles are returned.</p>

<h3>Can I order both 19 L and 10 L on the same subscription?</h3>
<p>Yes. The subscription supports mixed line items: for example, 2 bottles of 19 L for the kitchen and 4 bottles of 10 L for the kids' room — all in one delivery.</p>

<h3>Am I billed monthly or per delivery?</h3>
<p>Both options are available: per-delivery billing or a single monthly invoice for offices. The billing mode is chosen at sign-up and can be changed in subscription settings.</p>

<h3>Where can I see my upcoming deliveries?</h3>
<p>In the Telegram bot — the "My deliveries" command shows the schedule for the upcoming weeks. On the website — the "My subscription" section in your account.</p>

<h3>What if no one is home on the delivery day?</h3>
<p>Reschedule the delivery via the bot or the account. The courier will also reach out in advance if anything in the schedule needs clarifying.</p>

<h3>Does the subscription work in Tashkent Region?</h3>
<p>Yes, delivery covers Tashkent and Tashkent Region. Confirm the exact zone for your address at sign-up on <a href="/subscriptions">/subscriptions</a>.</p>

<h2>Ready to start a subscription?</h2>
<p>If you order water on a steady cadence already, a subscription is simply a more convenient and predictable form of the same consumption. Open <a href="/subscriptions">/subscriptions</a>, pick format, quantity, and schedule — and stop thinking about "I need to order water" as a class of task. If you prefer chat, set up and manage your subscription in the Telegram bot <a href="https://t.me/aqua_element_bot">@aqua_element_bot</a>. The water quality behind every delivery is backed by 11-stage treatment of the artesian source — full details on <a href="/process/11-step-filtration">/process/11-step-filtration</a>.</p>

<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "FAQPage",
  "mainEntity": [
    {"@type": "Question", "name": "How do I pause a subscription?", "acceptedAnswer": {"@type": "Answer", "text": "In the Telegram bot @aqua_element_bot or in your website account, section My subscription, button Pause. Resuming is one click."}},
    {"@type": "Question", "name": "What if I am travelling for a month?", "acceptedAnswer": {"@type": "Answer", "text": "Pause the subscription for that period or skip the upcoming deliveries. There are no penalties and no manual re-billing."}},
    {"@type": "Question", "name": "Can I switch from 10 L to 19 L mid-subscription?", "acceptedAnswer": {"@type": "Answer", "text": "Yes. The bottle format is changed in subscription settings and applies from the next delivery."}},
    {"@type": "Question", "name": "What is the minimum subscription length?", "acceptedAnswer": {"@type": "Answer", "text": "There is no mandatory minimum length. You can start and cancel at any time under standard terms."}},
    {"@type": "Question", "name": "Is there a setup fee?", "acceptedAnswer": {"@type": "Answer", "text": "No, activation is free. A deposit on the 19 L returnable bottles is charged on the first delivery and refunded when the bottles are returned."}},
    {"@type": "Question", "name": "Can I order both 19 L and 10 L on the same subscription?", "acceptedAnswer": {"@type": "Answer", "text": "Yes. The subscription supports mixed line items in a single delivery."}},
    {"@type": "Question", "name": "Am I billed monthly or per delivery?", "acceptedAnswer": {"@type": "Answer", "text": "Both options are available: per-delivery billing or a single monthly invoice for offices."}},
    {"@type": "Question", "name": "Where can I see my upcoming deliveries?", "acceptedAnswer": {"@type": "Answer", "text": "In the Telegram bot the My deliveries command and on the website the My subscription section in your account."}}
  ]
}
</script>
"""
        },
    },
}
