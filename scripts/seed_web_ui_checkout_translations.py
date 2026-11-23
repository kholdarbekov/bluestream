"""Seed the checkout group's strings (website redesign step 3, group "checkout": My addresses and the address wizard
first; checkout, order confirmation, tracking and the payment pages as their units add theirs).

The pattern every redesign group's seed follows (scripts/seed_web_ui_shell_translations.py):
- one self-contained script per group, keys under the group's own namespace (`web.checkout.*` here);
- en / ru / uz for every key: natural Russian, native Uzbek Latin with ‘ in o‘/g‘ and ’ for the tutuq belgisi;
- customer copy in plain verbs and perk tone, never telling the customer to phone (the Telegram bot is the channel);
- `Translation.bulk_create_or_update` inserts the missing rows, updates only the keys listed here, and drops their
  Redis cache entries, so a re-run is safe and a changed value shows at once.

Units of the group ADD rows to ROWS; never drop a row another unit added.
Placeholders: {title} (address name), {n} / {total} (wizard step); checkout's are noted at its section.
`web.checkout.addr.in_use` is also read by the backend (AuthService.delete_user_address).

Run on the dev database (scripts/ is not mounted in the container, so pipe it in):
    docker compose exec -T business_app python - < scripts/seed_web_ui_checkout_translations.py
"""

from business_app import create_app, db
from business_app.models.translation import Translation

CATEGORY = "web"

# key: (en, ru, uz)
ROWS = {
    # ---- My addresses (/addresses) -------------------------------------------------------------------------
    "web.checkout.addr.lead": (
        "Saved addresses fill in by themselves when you order on the website or in the bot.",
        "Сохранённые адреса подставляются сами, когда вы заказываете на сайте или в боте.",
        "Saqlangan manzil saytda yoki botda buyurtma berganingizda o‘zi qo‘yiladi.",
    ),
    "web.checkout.addr.add": ("Add address", "Добавить адрес", "Manzil qo‘shish"),
    "web.checkout.addr.list_label": ("Saved addresses", "Сохранённые адреса", "Saqlangan manzillar"),
    "web.checkout.addr.empty_title": ("No addresses yet", "Адресов пока нет", "Hozircha manzil yo‘q"),
    "web.checkout.addr.empty_text": (
        "Save your address once, and it fills in by itself when you order on the website or in the bot.",
        "Сохраните адрес один раз, и он подставится сам при заказе на сайте или в боте.",
        "Manzilni bir marta saqlang, saytda yoki botda buyurtma berganingizda u o‘zi qo‘yiladi.",
    ),
    "web.checkout.addr.load_failed_title": (
        "Couldn’t load your addresses",
        "Не удалось загрузить адреса",
        "Manzillarni yuklab bo‘lmadi",
    ),
    "web.checkout.addr.load_failed_text": (
        "Check the internet connection and try again.",
        "Проверьте подключение к интернету и попробуйте ещё раз.",
        "Internet aloqasini tekshirib, qayta urinib ko‘ring.",
    ),
    "web.checkout.addr.retry": ("Try again", "Повторить", "Qayta urinish"),
    "web.checkout.addr.default_badge": ("Main", "Основной", "Asosiy"),
    "web.checkout.addr.business_badge": ("Business", "Организация", "Tashkilot"),
    "web.checkout.addr.district": ("District", "Район", "Tuman"),
    "web.checkout.addr.floor": ("Floor", "Этаж", "Qavat"),
    "web.checkout.addr.apartment": ("Apt / office", "Кв. / офис", "Xonadon / ofis"),
    "web.checkout.addr.instructions": ("Note for the courier", "Подсказка курьеру", "Kuryer uchun izoh"),
    "web.checkout.addr.edit": ("Edit", "Изменить", "Tahrirlash"),
    "web.checkout.addr.make_default": ("Make main", "Сделать основным", "Asosiy qilish"),
    "web.checkout.addr.delete": ("Delete", "Удалить", "O‘chirish"),
    "web.checkout.addr.delete_title": ("Delete this address?", "Удалить адрес?", "Manzil o‘chirilsinmi?"),
    "web.checkout.addr.delete_text": (
        "“{title}” will be removed from your saved addresses.",
        "Адрес «{title}» пропадёт из сохранённых.",
        "«{title}» saqlangan manzillardan o‘chiriladi.",
    ),
    "web.checkout.addr.delete_cancel": ("Keep it", "Оставить", "Qoldirish"),
    "web.checkout.addr.deleted": ("Address deleted", "Адрес удалён", "Manzil o‘chirildi"),
    "web.checkout.addr.delete_failed": (
        "Couldn’t delete the address. Please try again.",
        "Не удалось удалить адрес. Попробуйте ещё раз.",
        "Manzilni o‘chirib bo‘lmadi. Qayta urinib ko‘ring.",
    ),
    "web.checkout.addr.in_use": (
        "This address is already used in orders, so it can’t be deleted. You can edit it instead.",
        "Этот адрес уже есть в заказах, поэтому удалить его нельзя. Его можно изменить.",
        "Bu manzil buyurtmalarda ishlatilgan, shuning uchun uni o‘chirib bo‘lmaydi. Uni tahrirlash mumkin.",
    ),
    "web.checkout.addr.default_updated": (
        "Main address updated",
        "Основной адрес изменён",
        "Asosiy manzil o‘zgartirildi",
    ),
    "web.checkout.addr.default_failed": (
        "Couldn’t change the main address. Please try again.",
        "Не удалось сменить основной адрес. Попробуйте ещё раз.",
        "Asosiy manzilni o‘zgartirib bo‘lmadi. Qayta urinib ko‘ring.",
    ),
    "web.checkout.addr.network_error": (
        "No connection. Check the internet and try again.",
        "Нет связи. Проверьте интернет и попробуйте ещё раз.",
        "Aloqa yo‘q. Internetni tekshirib, qayta urinib ko‘ring.",
    ),
    "web.checkout.addr.close": ("Close", "Закрыть", "Yopish"),
    # ---- Address wizard (partials/address_wizard.html, js/address-wizard.js) --------------------------------
    "web.checkout.wizard.title_new": ("New address", "Новый адрес", "Yangi manzil"),
    "web.checkout.wizard.title_edit": ("Edit address", "Изменить адрес", "Manzilni tahrirlash"),
    "web.checkout.wizard.step_map": ("Point on the map", "Точка на карте", "Xaritadagi nuqta"),
    "web.checkout.wizard.step_details": ("Address details", "Детали адреса", "Manzil tafsilotlari"),
    "web.checkout.wizard.step_confirm": ("Review", "Проверка", "Tekshirish"),
    "web.checkout.wizard.step_count": ("Step {n} of {total}", "Шаг {n} из {total}", "{total} qadamdan {n}-qadam"),
    "web.checkout.wizard.search_label": ("Find an address", "Найти адрес", "Manzilni topish"),
    "web.checkout.wizard.search_placeholder": ("Street or landmark", "Улица или ориентир", "Ko‘cha yoki mo‘ljal"),
    "web.checkout.wizard.search_button": ("Find", "Найти", "Topish"),
    "web.checkout.wizard.my_location": ("My location", "Моё местоположение", "Mening joylashuvim"),
    "web.checkout.wizard.locating": ("Finding you…", "Определяем…", "Aniqlanmoqda…"),
    "web.checkout.wizard.map_label": (
        "Map. Tap to mark the delivery point",
        "Карта. Нажмите, чтобы отметить точку доставки",
        "Xarita. Yetkazish nuqtasini belgilash uchun bosing",
    ),
    "web.checkout.wizard.map_hint": (
        "Tap the map where the courier should come. You can drag the pin. The blue outline is our delivery zone.",
        "Нажмите на карту там, куда приехать курьеру. Метку можно перетащить. Синим обведена зона доставки.",
        "Kuryer keladigan joyni xaritada bosing. Belgini surib to‘g‘rilash mumkin. Ko‘k chiziq ichi — "
        "yetkazib berish hududi.",
    ),
    "web.checkout.wizard.getting_address": ("Looking up the address…", "Определяем адрес…", "Manzil aniqlanmoqda…"),
    "web.checkout.wizard.point_selected": ("Point selected", "Точка выбрана", "Nuqta tanlandi"),
    "web.checkout.wizard.point_no_address": (
        "Point selected. Type the address on the next step.",
        "Точка выбрана. Адрес впишите на следующем шаге.",
        "Nuqta tanlandi. Manzilni keyingi qadamda yozing.",
    ),
    "web.checkout.wizard.outside_zone": (
        "We don’t deliver to this point yet. Mark a spot inside the blue outline.",
        "Сюда мы пока не доставляем. Отметьте точку внутри синей зоны.",
        "Bu yerga hozircha yetkazib bermaymiz. Ko‘k hudud ichidan nuqta tanlang.",
    ),
    "web.checkout.wizard.zone_link": (
        "See the delivery zone",
        "Посмотреть зону доставки",
        "Yetkazib berish hududini ko‘rish",
    ),
    "web.checkout.wizard.not_found": (
        "Nothing found. Try other words or tap the map.",
        "Ничего не нашли. Попробуйте иначе или отметьте точку на карте.",
        "Hech narsa topilmadi. Boshqacha yozib ko‘ring yoki xaritada belgilang.",
    ),
    "web.checkout.wizard.search_empty": (
        "Type a street or a landmark first.",
        "Сначала введите улицу или ориентир.",
        "Avval ko‘cha yoki mo‘ljalni yozing.",
    ),
    "web.checkout.wizard.search_failed": (
        "Search isn’t available right now. Tap the map instead.",
        "Поиск сейчас недоступен. Отметьте точку на карте.",
        "Qidiruv hozir ishlamayapti. Nuqtani xaritada belgilang.",
    ),
    "web.checkout.wizard.geo_unsupported": (
        "This browser can’t share your location. Tap the map instead.",
        "Браузер не может передать местоположение. Отметьте точку на карте.",
        "Brauzer joylashuvni yubora olmaydi. Nuqtani xaritada belgilang.",
    ),
    "web.checkout.wizard.geo_denied": (
        "Location access is off. Allow it in the browser or tap the map.",
        "Доступ к местоположению выключен. Разрешите его в браузере или отметьте точку на карте.",
        "Joylashuvga ruxsat berilmagan. Brauzerda ruxsat bering yoki nuqtani xaritada belgilang.",
    ),
    "web.checkout.wizard.geo_failed": (
        "Couldn’t find your location. Tap the map instead.",
        "Не удалось определить местоположение. Отметьте точку на карте.",
        "Joylashuvni aniqlab bo‘lmadi. Nuqtani xaritada belgilang.",
    ),
    "web.checkout.wizard.pick_point": (
        "Tap the map to mark the delivery point.",
        "Отметьте точку доставки на карте.",
        "Yetkazish nuqtasini xaritada belgilang.",
    ),
    "web.checkout.wizard.map_failed": (
        "The map didn’t load. Check the internet and open the form again.",
        "Карта не загрузилась. Проверьте интернет и откройте форму ещё раз.",
        "Xarita yuklanmadi. Internetni tekshirib, oynani qayta oching.",
    ),
    "web.checkout.wizard.chips_label": ("Quick name", "Быстрое название", "Tezkor nom"),
    "web.checkout.wizard.chip_home": ("Home", "Дом", "Uy"),
    "web.checkout.wizard.chip_work": ("Work", "Работа", "Ish"),
    "web.checkout.wizard.chip_other": ("Other", "Другое", "Boshqa"),
    "web.checkout.wizard.f_title": ("Name", "Название", "Nomi"),
    "web.checkout.wizard.f_title_ph": (
        "For example, Home or Office",
        "Например, Дом или Офис",
        "Masalan, Uy yoki Ofis",
    ),
    "web.checkout.wizard.f_title_required": (
        "Give the address a name.",
        "Дайте адресу название.",
        "Manzilga nom bering.",
    ),
    "web.checkout.wizard.f_address": ("Address", "Адрес", "Manzil"),
    "web.checkout.wizard.f_address_help": (
        "Filled in from the map point. Correct it if something is off.",
        "Подставили по точке на карте. Поправьте, если что-то не так.",
        "Xaritadagi nuqtadan to‘ldirildi. Noto‘g‘ri joyi bo‘lsa, tuzating.",
    ),
    "web.checkout.wizard.suggest": ("From the map:", "По карте:", "Xarita bo‘yicha:"),
    "web.checkout.wizard.suggest_use": ("Use it", "Подставить", "Qo‘yish"),
    "web.checkout.wizard.f_district_ph": ("Choose a district", "Выберите район", "Tumanni tanlang"),
    "web.checkout.wizard.f_street": ("Street and house", "Улица и дом", "Ko‘cha va uy"),
    "web.checkout.wizard.f_street_ph": (
        "For example, Navoi street, 12",
        "Например, ул. Навои, 12",
        "Masalan, Navoiy ko‘chasi, 12",
    ),
    "web.checkout.wizard.f_apartment": ("Apartment or office", "Квартира или офис", "Xonadon yoki ofis"),
    "web.checkout.wizard.f_landmark": ("Landmark", "Ориентир", "Mo‘ljal"),
    "web.checkout.wizard.f_landmark_ph": (
        "For example, next to the school",
        "Например, рядом со школой",
        "Masalan, maktab yonida",
    ),
    "web.checkout.wizard.f_instructions_ph": (
        "Entrance, intercom code, how to find the door",
        "Подъезд, код домофона, как найти вход",
        "Podyezd, domofon kodi, eshikni qanday topish",
    ),
    "web.checkout.wizard.summary_title": ("Check the address", "Проверьте адрес", "Manzilni tekshiring"),
    "web.checkout.wizard.not_set": ("Not set", "Не указано", "Ko‘rsatilmagan"),
    "web.checkout.wizard.is_default": ("Make this my main address", "Сделать основным адресом", "Asosiy manzil qilish"),
    "web.checkout.wizard.is_business": ("This is a business address", "Это адрес организации", "Bu tashkilot manzili"),
    "web.checkout.wizard.back": ("Back", "Назад", "Orqaga"),
    "web.checkout.wizard.next": ("Next", "Далее", "Keyingi"),
    "web.checkout.wizard.cancel": ("Cancel", "Отмена", "Bekor qilish"),
    "web.checkout.wizard.save": ("Save address", "Сохранить адрес", "Manzilni saqlash"),
    "web.checkout.wizard.saving": ("Saving…", "Сохраняем…", "Saqlanmoqda…"),
    "web.checkout.wizard.saved": ("Address saved", "Адрес сохранён", "Manzil saqlandi"),
    # The duplicate-address guard's pane (spec 2026-11-18 §6.11). {distance} is whole metres.
    "web.checkout.wizard.duplicate_title": (
        "You already have this address",
        "Этот адрес у вас уже есть",
        "Bu manzil sizda allaqachon bor",
    ),
    "web.checkout.wizard.duplicate_body": (
        "Choose the saved one and your orders and bottle count stay in one place.",
        "Выберите сохранённый — заказы и учёт бутылей останутся в одном месте.",
        "Saqlanganini tanlang — buyurtmalar va idishlar hisobi bir joyda qoladi.",
    ),
    "web.checkout.wizard.duplicate_distance": ("~{distance} m away", "~{distance} м отсюда", "~{distance} m narida"),
    "web.checkout.wizard.duplicate_use": ("Use this address", "Выбрать этот адрес", "Shu manzilni tanlash"),
    "web.checkout.wizard.duplicate_move": (
        "Move it to the new pin",
        "Перенести на новую точку",
        "Yangi nuqtaga ko‘chirish",
    ),
    "web.checkout.wizard.duplicate_move_hint": (
        "Its name and saved door details stay; what you just typed fills in anything missing.",
        "Название и сохранённые детали останутся; то, что вы ввели, дополнит только пустые поля.",
        "Nomi va saqlangan tafsilotlari qoladi; hozir yozganingiz faqat bo‘sh joylarni to‘ldiradi.",
    ),
    "web.checkout.wizard.duplicate_new": (
        "Save as a new address",
        "Сохранить как новый адрес",
        "Yangi manzil sifatida saqlash",
    ),
    "web.checkout.wizard.duplicate_save_anyway": ("Save anyway", "Всё равно сохранить", "Baribir saqlash"),
    "web.checkout.wizard.save_failed": (
        "Couldn’t save the address. Please try again.",
        "Не удалось сохранить адрес. Попробуйте ещё раз.",
        "Manzilni saqlab bo‘lmadi. Qayta urinib ko‘ring.",
    ),
    # ---- Checkout (/checkout), unit "checkout-flow" ------------------------------------------------------------
    # Placeholders: {minimum} / {remaining} (money words), {name} / {min} (product, its smallest order).
    "web.checkout.co.title": ("Checkout", "Оформление заказа", "Buyurtmani rasmiylashtirish"),
    "web.checkout.co.address_title": ("Delivery address", "Адрес доставки", "Yetkazib berish manzili"),
    "web.checkout.co.address_empty_title": (
        "Add a delivery address",
        "Добавьте адрес доставки",
        "Yetkazib berish manzilini qo‘shing",
    ),
    "web.checkout.co.address_empty_text": (
        "Mark the place on the map once, and next time it fills in by itself.",
        "Отметьте место на карте один раз, и в следующий раз адрес подставится сам.",
        "Joyni xaritada bir marta belgilang, keyingi safar manzil o‘zi qo‘yiladi.",
    ),
    "web.checkout.co.address_error": (
        "Choose where to deliver the order.",
        "Выберите, куда доставить заказ.",
        "Buyurtmani qayerga yetkazishni tanlang.",
    ),
    "web.checkout.co.untitled_address": ("Address", "Адрес", "Manzil"),
    "web.checkout.co.when_title": ("Delivery day and time", "День и время доставки", "Yetkazib berish kuni va vaqti"),
    "web.checkout.co.date_label": ("Delivery day", "День доставки", "Yetkazib berish kuni"),
    "web.checkout.co.date_error": ("Choose a delivery day.", "Выберите день доставки.", "Yetkazib berish kunini tanlang."),
    # The quote's or the order's schedule refusal (`error_code` SCHEDULE_*), said at the field it is about.
    # {date}: the last day the schedule rule takes (display_labels.day_month).
    "web.checkout.co.schedule_title": (
        "Check the delivery day and time",
        "Проверьте день и время доставки",
        "Yetkazib berish kuni va vaqtini tekshiring",
    ),
    "web.checkout.co.schedule_date_past": (
        "That day has passed. Choose today or a later day.",
        "Этот день уже прошёл. Выберите сегодня или более поздний день.",
        "Bu kun o‘tib ketdi. Bugunni yoki keyingi kunni tanlang.",
    ),
    "web.checkout.co.schedule_date_far": (
        "You can plan a delivery up to {date}. Choose that day or an earlier one.",
        "Доставку можно запланировать до {date} включительно. Выберите день не позже.",
        "Yetkazib berishni {date} kunigacha rejalashtirish mumkin. Shu kunni yoki undan oldingisini tanlang.",
    ),
    "web.checkout.co.schedule_window_passed": (
        "This time has already passed today. Choose a later time or another day.",
        "Это время на сегодня уже прошло. Выберите время позже или другой день.",
        "Bugun bu vaqt o‘tib ketdi. Kechroq vaqtni yoki boshqa kunni tanlang.",
    ),
    "web.checkout.co.schedule_other": (
        "Choose another delivery day or time.",
        "Выберите другой день или время доставки.",
        "Boshqa yetkazib berish kuni yoki vaqtini tanlang.",
    ),
    "web.checkout.co.window_label": ("Convenient time", "Удобное время", "Qulay vaqt"),
    "web.checkout.co.pay_title": ("Payment", "Оплата", "To‘lov"),
    "web.checkout.co.method_note.cash": (
        "Pay the courier on delivery",
        "Курьеру при доставке",
        "Yetkazib berishda kuryerga",
    ),
    "web.checkout.co.method_note.click": (
        "Click wallet or a linked card",
        "Кошелёк Click или привязанная карта",
        "Click hamyoni yoki ulangan karta",
    ),
    "web.checkout.co.method_note.business_account": (
        "From your company’s prepaid balance",
        "С предоплаченного баланса компании",
        "Kompaniyaning oldindan to‘langan balansidan",
    ),
    "web.checkout.co.notes_title": ("Comment and terms", "Комментарий и условия", "Izoh va shartlar"),
    "web.checkout.co.notes_label": ("Comment on the order", "Комментарий к заказу", "Buyurtmaga izoh"),
    "web.checkout.co.optional": ("optional", "необязательно", "ixtiyoriy"),
    "web.checkout.co.notes_ph": (
        "Anything the courier should know about this delivery",
        "Что курьеру важно знать об этой доставке",
        "Kuryer bu yetkazib berish haqida nimani bilishi kerak",
    ),
    "web.checkout.co.terms_before": ("I accept the", "Я принимаю", "Men"),
    "web.checkout.co.terms_link": ("terms of use", "условия использования", "foydalanish shartlari"),
    "web.checkout.co.terms_after": (".", ".", "ga roziman."),
    "web.checkout.co.terms_error": (
        "Accept the terms to place the order.",
        "Чтобы оформить заказ, примите условия.",
        "Buyurtma berish uchun shartlarni qabul qiling.",
    ),
    "web.checkout.co.subtotal": ("Items", "Товары", "Mahsulotlar"),
    "web.checkout.co.delivery": ("Delivery", "Доставка", "Yetkazib berish"),
    "web.checkout.co.free": ("Free", "Бесплатно", "Bepul"),
    "web.checkout.co.total": ("Total", "Итого", "Jami"),
    "web.checkout.co.calculating": ("Calculating…", "Считаем…", "Hisoblanmoqda…"),
    "web.checkout.co.min_amount_title": (
        "The minimum order is {minimum}",
        "Минимальная сумма заказа — {minimum}",
        "Eng kam buyurtma summasi — {minimum}",
    ),
    "web.checkout.co.min_amount_text": (
        "Add {remaining} more in the cart to place the order.",
        "Добавьте в корзину товаров ещё на {remaining}, чтобы оформить заказ.",
        "Buyurtma berish uchun savatga yana {remaining}lik mahsulot qo‘shing.",
    ),
    "web.checkout.co.min_qty_title": (
        "Some items are below their smallest order",
        "Некоторых товаров меньше минимального количества",
        "Ayrim mahsulotlar eng kam miqdordan kam",
    ),
    "web.checkout.co.min_qty_line": (
        "{name}: at least {min}, add {remaining} more in the cart",
        "{name}: от {min} шт., добавьте в корзину ещё {remaining}",
        "{name}: kamida {min} dona, savatga yana {remaining} dona qo‘shing",
    ),
    "web.checkout.co.quote_failed_title": (
        "We couldn’t work out the total",
        "Не удалось рассчитать сумму",
        "Summani hisoblab bo‘lmadi",
    ),
    "web.checkout.co.quote_failed_text": (
        "Check your cart, or refresh the page in a minute.",
        "Проверьте корзину или обновите страницу через минуту.",
        "Savatni tekshiring yoki bir daqiqadan so‘ng sahifani yangilang.",
    ),
    "web.checkout.co.place": ("Place order", "Подтвердить заказ", "Buyurtmani tasdiqlash"),
    "web.checkout.co.back_to_cart": ("Back to cart", "Вернуться в корзину", "Savatga qaytish"),
    "web.checkout.co.placing_title": ("Placing your order…", "Оформляем заказ…", "Buyurtma rasmiylashtirilmoqda…"),
    "web.checkout.co.placing_text": (
        "This takes a few seconds.",
        "Это займёт несколько секунд.",
        "Bu bir necha soniya davom etadi.",
    ),
    "web.checkout.co.place_failed": (
        "Couldn’t place the order. Please try again.",
        "Не удалось оформить заказ. Попробуйте ещё раз.",
        "Buyurtmani rasmiylashtirib bo‘lmadi. Qayta urinib ko‘ring.",
    ),
    "web.checkout.co.price_changed": (
        "Your order total changed while this page was open. Here is the new total: check it and place the order again.",
        "Сумма заказа изменилась, пока страница была открыта. Вот новая сумма: проверьте её и оформите заказ ещё раз.",  # NATIVE-READ
        "Sahifa ochiq turganda buyurtma summasi o‘zgardi. Mana yangi summa: tekshirib, buyurtmani qaytadan bering.",  # NATIVE-READ
    ),
    # ---- Payment return pages (/payment/success, /payment/cancel), unit "checkout-flow" ---------------------------
    "web.checkout.pay.crumb": ("Payment", "Оплата", "To‘lov"),
    "web.checkout.pay.pending_title": ("Checking your payment", "Проверяем оплату", "To‘lovni tekshiryapmiz"),
    "web.checkout.pay.pending_text": (
        "This usually takes under a minute. As soon as the payment comes through, we’ll open your order.",
        "Обычно это занимает меньше минуты. Как только оплата пройдёт, откроем ваш заказ.",
        "Odatda bu bir daqiqadan kam vaqt oladi. To‘lov o‘tishi bilan buyurtmangizni ochamiz.",
    ),
    "web.checkout.pay.auto_caption": (
        "The page checks by itself",
        "Страница проверяет сама",
        "Sahifa o‘zi tekshiradi",
    ),
    "web.checkout.pay.timeout": (
        "This is taking longer than usual. We’ll message you in the bot or by email as soon as the payment is confirmed.",
        "Проверка затянулась. Мы напишем в боте или на почту, как только оплата подтвердится.",
        "Tekshiruv odatdagidan uzoqroq cho‘zildi. To‘lov tasdiqlanishi bilan botda yoki elektron pochta orqali xabar beramiz.",
    ),
    "web.checkout.pay.failed": ("The payment didn’t go through.", "Оплата не прошла.", "To‘lov o‘tmadi."),
    "web.checkout.pay.check": ("Check now", "Проверить сейчас", "Hozir tekshirish"),
    # After "Check now", while Click has not answered yet.
    "web.checkout.pay.still_pending": (
        "Click hasn’t confirmed it yet. The page keeps checking.",
        "Click ещё не подтвердил оплату. Страница продолжает проверять.",
        "Click to‘lovni hali tasdiqlamadi. Sahifa tekshirishda davom etadi.",
    ),
    "web.checkout.pay.order_number": ("Order number", "Номер заказа", "Buyurtma raqami"),
    "web.checkout.pay.amount": ("Amount", "Сумма", "Summa"),
    "web.checkout.pay.method": ("Payment method", "Способ оплаты", "To‘lov usuli"),
    "web.checkout.pay.status": ("Order status", "Статус заказа", "Buyurtma holati"),
    "web.checkout.pay.cancel_title": ("Payment not completed", "Оплата не завершена", "To‘lov yakunlanmadi"),
    "web.checkout.pay.cancel_text": ("No money was taken.", "Деньги не списаны.", "Pul yechilmadi."),
    "web.checkout.pay.cancel_retry_note": (
        "Your order is saved. You can pay for it again.",
        "Заказ сохранён, его можно оплатить ещё раз.",
        "Buyurtma saqlangan, uni qayta to‘lash mumkin.",
    ),
    "web.checkout.pay.cancel_closed_note": (
        "This order can no longer be paid online. If anything is still owed on it, our team will settle it with you.",
        "Этот заказ больше нельзя оплатить онлайн. Если по нему осталась сумма к оплате, мы договоримся с вами отдельно.",
        "Bu buyurtmani endi onlayn to‘lab bo‘lmaydi. Agar u bo‘yicha to‘lanmagan summa qolgan bo‘lsa, siz bilan alohida kelishib olamiz.",
    ),
    "web.checkout.pay.my_orders": ("My orders", "Мои заказы", "Buyurtmalarim"),
    "web.checkout.pay.continue": ("Continue shopping", "Продолжить покупки", "Xaridni davom ettirish"),
    # ---- Order confirmation and tracking (/order-confirmation, /order-tracking), unit "order-pages" ----------------
    # {number}: the order number; {date}: display_labels.day_month ("5 июля"); {time}: 24-hour "22:40"; {year}: 2025;
    # {when}: an `at` / `at_year` label; {name}: the courier's name.
    "web.checkout.order.done_title": ("Order placed", "Заказ оформлен", "Buyurtma rasmiylashtirildi"),
    "web.checkout.order.thanks": (
        "Thank you! We’ve got your order. You can follow it on the order page.",
        "Спасибо! Мы получили ваш заказ. Следить за ним можно на странице заказа.",
        "Rahmat! Buyurtmangizni qabul qildik. Uni buyurtma sahifasida kuzatib borishingiz mumkin.",
    ),
    "web.checkout.order.number": ("Order {number}", "Заказ {number}", "Buyurtma {number}"),
    "web.checkout.order.placed": ("Placed", "Оформлен", "Rasmiylashtirilgan"),
    "web.checkout.order.at": ("{date}, {time}", "{date}, {time}", "{date}, {time}"),
    "web.checkout.order.at_year": ("{date} {year}, {time}", "{date} {year} г., {time}", "{year}-yil {date}, {time}"),
    "web.checkout.order.address_title": ("Delivery address", "Адрес доставки", "Yetkazib berish manzili"),
    "web.checkout.order.items_title": ("Your order", "Состав заказа", "Buyurtma tarkibi"),
    "web.checkout.order.tax": ("Tax", "Налог", "Soliq"),
    "web.checkout.order.track": ("Track order", "Следить за заказом", "Buyurtmani kuzatish"),
    "web.checkout.order.map_label": (
        "Map with the delivery address",
        "Карта с адресом доставки",
        "Yetkazib berish manzili ko‘rsatilgan xarita",
    ),
    "web.checkout.order.not_found_title": ("Order not found", "Заказ не найден", "Buyurtma topilmadi"),
    "web.checkout.order.not_found_crumb": ("Order", "Заказ", "Buyurtma"),
    "web.checkout.order.not_found_heading": (
        "We couldn’t find this order",
        "Такого заказа нет среди ваших",
        "Bunday buyurtma sizning buyurtmalaringiz orasida yo‘q",
    ),
    "web.checkout.order.not_found_text": (
        "All your orders are listed in My orders.",
        "Все ваши заказы — в разделе «Мои заказы».",
        "Barcha buyurtmalaringiz «Buyurtmalarim» bo‘limida.",
    ),
    "web.checkout.track.placed_lead": ("Placed on {when}", "Оформлен {when}", "Rasmiylashtirilgan: {when}"),
    "web.checkout.track.status_title": ("Order status", "Статус заказа", "Buyurtma holati"),
    "web.checkout.track.live": (
        "The page updates by itself",
        "Страница обновляется сама",
        "Sahifa o‘zi yangilanadi",
    ),
    "web.checkout.track.placed": ("Order placed", "Заказ оформлен", "Buyurtma rasmiylashtirildi"),
    "web.checkout.track.placed_text": ("We’ve got your order.", "Мы получили ваш заказ.", "Buyurtmangizni qabul qildik."),
    "web.checkout.track.confirmed": ("Order confirmed", "Заказ подтверждён", "Buyurtma tasdiqlandi"),
    "web.checkout.track.confirmed_text": (
        "We’ve confirmed your order.",
        "Мы подтвердили ваш заказ.",
        "Buyurtmangizni tasdiqladik.",
    ),
    "web.checkout.track.confirm_wait": (
        "Waiting for confirmation.",
        "Ждёт подтверждения.",
        "Tasdiqlanishi kutilmoqda.",
    ),
    "web.checkout.track.preparing": ("Being prepared", "Готовим заказ", "Tayyorlanmoqda"),
    "web.checkout.track.preparing_text": (
        "We’re getting your order ready for the courier.",
        "Собираем заказ для курьера.",
        "Buyurtmani kuryer uchun tayyorlayapmiz.",
    ),
    "web.checkout.track.preparing_soon": (
        "We’ll start preparing it soon.",
        "Скоро начнём собирать.",
        "Tez orada tayyorlashni boshlaymiz.",
    ),
    "web.checkout.track.on_the_way": ("On the way", "В пути", "Yo‘lda"),
    "web.checkout.track.on_the_way_text": (
        "The courier is bringing your order.",
        "Курьер везёт ваш заказ.",
        "Kuryer buyurtmangizni olib kelyapti.",
    ),
    "web.checkout.track.courier": ("Courier: {name}", "Курьер: {name}", "Kuryer: {name}"),
    "web.checkout.track.start_soon": (
        "Delivery will start soon.",
        "Доставка скоро начнётся.",
        "Yetkazib berish tez orada boshlanadi.",
    ),
    "web.checkout.track.delivery_done": ("Delivery completed.", "Доставка завершена.", "Yetkazib berish yakunlandi."),
    "web.checkout.track.delivered": ("Delivered", "Доставлен", "Yetkazib berildi"),
    "web.checkout.track.delivered_text": (
        "Your order has been delivered.",
        "Заказ доставлен.",
        "Buyurtmangiz yetkazib berildi.",
    ),
    "web.checkout.track.cancelled": ("Order cancelled", "Заказ отменён", "Buyurtma bekor qilindi"),
    "web.checkout.track.returned": ("Order returned", "Заказ возвращён", "Buyurtma qaytarildi"),
}

TRANSLATIONS = {
    language: {key: values[index] for key, values in ROWS.items()} for index, language in enumerate(("en", "ru", "uz"))
}

if __name__ == "__main__":
    # Guarded so tests can import TRANSLATIONS without writing to a database.
    app = create_app()
    with app.app_context():
        Translation.bulk_create_or_update(TRANSLATIONS, category=CATEGORY)
        db.session.commit()
        print(f"Seeded {sum(len(v) for v in TRANSLATIONS.values())} rows into category '{CATEGORY}'")
