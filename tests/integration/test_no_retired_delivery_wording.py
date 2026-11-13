"""Site-wide guard: no public page, in any language, and neither `/llms.txt` nor the two JSON feeds, carries a retired
delivery claim."""

import json
from pathlib import Path

import pytest

from business_app.services.public_urls_service import STATIC_PUBLIC_PATHS, public_product_paths
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS
from shared.constants import get_delivery_statement
from tests.integration.public_page_builders import flat, get_page, offer_skus, page, seed_copy, shipped  # noqa: F401
from tests.integration.retired_phrases import _page_texts, _string_leaves, find_retired

LANGS = ("uz", "ru", "en")
GUARD_PREFIXES = ("web.display_labels", "landing.", "loyalty_guide.")
REPO = Path(__file__).resolve().parents[2]

CAUGHT = {   # the sentences this plan retires, by the entry that must catch them
    "slot_condition": (
        "Most service zones support same-day delivery when an open time slot is available.",
        "В большинстве зон доставка доступна в тот же день при наличии свободного интервала.",
        "Ko‘p hududlarda bo‘sh vaqt oralig‘i mavjud bo‘lsa, shu kunning o‘zida yetkazib berish mumkin.",
        "Most areas are served the same day within available delivery windows.",
        "В большинстве районов доставляем в тот же день в доступные временные интервалы.",
        "Ko‘p hududlarda buyurtmalar mavjud vaqt oralig‘ida shu kunning o‘zida yetkaziladi.",
        "Same-day delivery is available for many zones when slots remain open.",
        "it will quote the next available slot for your district", "он подскажет ближайший слот доставки",
        "eng yaqin yetkazib berish slotini aytib beradi", "it commits to a time slot and keeps it"),
    "zone_condition": (
        "Delivery windows are shown at checkout and depend on service zone and available slots.",
        "Delivery terms follow the current delivery policy and available service zones.",
        "Для большинства адресов в черте Ташкента доступна доставка в день заказа", "Toshkentdagi koʻp manzillar uchun"),
    "rush": ("place a same-day rush order", "заказать «срочно сегодня»", '"shoshilinch bugun" buyurtma berish',
             "можно оформить срочную доставку"),
    "working_day": (
        "remaining slots are scheduled for the next business day", "на остальные слоты — следующий рабочий день",
        "qolgan slotlar uchun ertasi ish kuni", "The first delivery typically lands within 1–2 working days.",
        "Первая доставка обычно в течение 1–2 рабочих дней.", "Birinchi yetkazib berish odatda 1–2 ish kuni ichida."),
    "monday": ("офис без воды до понедельника", "ofis dushanbagacha suvsiz", "nothing arriving until Monday",
               "доставка начнётся только в понедельник", "yetkazib berish esa faqat dushanbada boshlanadi"),
    "fast": ("Fast Delivery", "Same day delivery available", "Fast and reliable water delivery to your doorstep",
             "Быстрая и надежная доставка воды к вашей двери", "Moslashuvchan jadval bilan tezkor va ishonchli suv yetkazib berish.",
             "the fastest water delivery in Tashkent", "самая быстрая доставка", "eng tez yetkazib berish"),
    "express": ("Express delivery.", "Экспресс-доставка.", "Ekspress yetkazib berish.", "Tez yetkazib berish"),
    "hours_figure": ("Delivery within 2 hours.", "Доставим за 2 часа.", "2 soat ichida yetkazamiz.",
                     "We deliver in 30 minutes.", "Доставка за 40 минут.", "30 daqiqada yetkazib beramiz.",
                     "2-hour delivery anywhere in the city."),
    "percent": ("93% of orders arrive the same day.", "95% заказов доставляем в тот же день.",
                "Buyurtmalarning 90% i shu kuni yetkaziladi.", "We deliver 98 % of orders on time."),
    "guarantee": ("Guaranteed same-day delivery", "o‘z vaqtida yetkazishni kafolatlaymiz", "гарантируем доставку в срок",
                  "Delivery is guaranteed.", "Kafolatlangan yetkazib berish."),
    "daytime_uz": ("kunduzgi soatlarda buyurtma qilinganda",),
    "confirmed": ("The exact slot is confirmed by an operator or by the bot.", "Точный слот подтверждается оператором или ботом.",
                  "Aniq slot operator yoki bot tomonidan tasdiqlanadi."),
    "clock_hour": ("Order before 14:00 and we deliver the same day.", "Закажите до 14:00 — привезём в тот же день.",
                   "Soat 14 gacha buyurtma bering — shu kuni yetkazamiz.", "Same-day delivery for orders placed by 2 pm"),
    "flat_claim": ("via subscription, with same-day delivery across Tashkent.",),
}
IGNORED = (   # true sentences that sit next to the retired ones
    "Select a time slot", "Vaqt oralig'ini tanlang", "Выберите временной интервал", "Yetkazib berish kuni",
    "22 рабочих дня", "22 ish kuni", "К концу рабочего дня", "Mon - Sat: 9AM - 6PM.", "Dush - Shan: 9:00 - 18:00.",
    "eng tez-tez uchraydigan", "Qanchalik tez-tez yetkazib beraylik?", "кислотности", "краткосрочную память",
    "the faster every future AquaCoin adds up", "Delivery: every day",
    "A comfortable subscription cadence is 6–8 bottles per week, with two deliveries: Monday and Thursday.",
    "Product listings, stock availability, and delivery windows can change.",
    "Choose your delivery frequency (daily, weekly, biweekly, or monthly) and preferred delivery time slots that fit your lifestyle.",
    "For Tashkent, a reasonable benchmark is same-day delivery to the major residential and business districts.",
    # The entries the final fix round added (W5) need a delivery or same-day word in the same sentence (hours,
    # percent, "is guaranteed"/"kafolatlan"), a whole word (express) and the positive form (kafolatlan, not
    # kafolatlanmaydi): these true blog sentences stay clear (retired_phrases.py says why each is narrowed).
    "In an office, where staff spend 8–9 hours in front of a screen, dehydration creeps in.",
    "Ofisda xodim ekran oldida 8–9 soat o'tkazadi.", "В офисе, где сотрудник проводит 8–9 часов перед экраном.",
    "Расход растёт на 40–60% относительно весны. Закладывайте увеличение частоты доставок.",
    "TDS stands for total dissolved solids, expressed in mg/L.",
    "lekin yuzaki ifloslanishdan tasdiqlangan himoyasiz xavfsizligi kafolatlanmaydi.",
    "Aqua Element guarantees the sterility of the water up to the moment the bottle is installed.",
)


@pytest.mark.unit
def test_the_detector_catches_every_retired_sentence_and_spares_true_copy():
    for name, sentences in CAUGHT.items():
        for sentence in sentences:
            assert name in find_retired(flat(sentence)), (name, sentence)
    spared = list(IGNORED) + [s for lang in LANGS for s in get_delivery_statement(lang)]
    spared += [row[lang] for key, row in BACKEND_TRANSLATIONS.items()
               if key.startswith(GUARD_PREFIXES) for lang in LANGS if row.get(lang)]
    for sentence in spared:
        assert find_retired(flat(sentence)) == [], sentence


@pytest.mark.integration
@pytest.mark.parametrize("lang", LANGS)
def test_no_public_page_carries_retired_delivery_wording(app, db, offer_skus, lang):
    seed_copy(db, GUARD_PREFIXES)                        # once; then GET many
    paths = list(STATIC_PUBLIC_PATHS) + [path for path, _product in public_product_paths()]
    assert len(paths) == len(STATIC_PUBLIC_PATHS) + 2    # both fixture products are listed
    hits = []
    for path in paths:
        for where, text in _page_texts(get_page(app, path, lang)).items():
            found = find_retired(flat(text))
            if found:
                hits.append(f"{path}?lang={lang} [{where}] {found}: {flat(text)[:160]}")
    assert hits == [], "\n".join(hits)


@pytest.mark.integration
@pytest.mark.parametrize("path", ["/llms.txt", "/api/public/products.json", "/api/public/loyalty.json"])
def test_no_machine_readable_surface_carries_retired_delivery_wording(app, db, offer_skus, path):
    """What an assistant ingests whole: the `/llms.txt` body, and every string leaf of the two JSON feeds."""
    seed_copy(db, GUARD_PREFIXES)
    response = app.test_client().get(path)
    assert response.status_code == 200, response.get_data(as_text=True)[:300]
    body = response.get_data(as_text=True)
    if path == "/llms.txt":
        texts = {"body": body}
    else:
        texts = {f"leaf {number}": leaf for number, leaf in enumerate(_string_leaves(json.loads(body)))}
    assert len(texts) > 1 or len(body) > 1000                     # the scan read the real document, not an empty one
    hits = []
    for where, text in texts.items():
        if found := find_retired(flat(text)):
            lines = [line for line in text.splitlines() if find_retired(flat(line))]   # where, in the llms.txt body
            hits.append(f"{path} [{where}] {found}: {lines or flat(text)[:160]}")
    assert hits == [], "\n".join(hits)


@pytest.mark.parametrize("lang", LANGS)
def test_the_services_card_makes_no_speed_claim(app, db, lang):
    text = flat(page(app, db, "/services", lang).select_one("main").get_text(" "))
    assert flat(shipped("landing.services.home_delivery.description", lang)) in text


def test_the_terms_page_names_the_coverage_area_not_service_zones(app, db):
    text = flat(page(app, db, "/terms", "en").select_one("main").get_text(" "))
    assert "Delivery terms follow the current delivery policy and our delivery coverage area." in text


def test_the_dead_cutoff_setting_is_gone(app, db, admin_claim_headers):
    client = app.test_client()
    for url in ("/api/v1/admin/system-settings", "/api/v1/admin/system-settings/categories"):
        response = client.get(url, headers=admin_claim_headers)
        assert response.status_code == 200, response.get_data(as_text=True)[:300]
        assert "cutoff" not in response.get_data(as_text=True).lower(), url
    source = (REPO / "business_app" / "api" / "admin.py").read_text(encoding="utf-8")
    assert "same_day_delivery_cutoff" not in source.lower()       # the PUT validation and the reset default
