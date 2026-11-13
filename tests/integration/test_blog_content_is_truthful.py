"""Every blog module, in every language, carries no retired delivery wording and no false bottle, minimum or price line.

Read as text (the four short fields, the visible body, each FAQPage JSON-LD string) and through `/blog/<slug>`.
The article modules and the seed validator are the real ones; nothing here keeps a copy of an article.
"""

import importlib
import json
import re
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from scripts.seed_backend_translations import BACKEND_TRANSLATIONS
from scripts.seed_blog_milestone_2 import _upsert_post, _validate_article
from shared.constants import get_delivery_statement
from tests.integration.public_page_builders import flat, get_page, offer_skus, seed_copy  # noqa: F401
from tests.integration.retired_phrases import _page_texts, _string_leaves, find_bottle_claims, find_retired, sentences

BLOG_DIR = Path(__file__).resolve().parents[2] / "scripts" / "blog_content"
STEMS = sorted(p.stem for p in BLOG_DIR.glob("*.py") if p.stem != "__init__")     # the comparison article is one of them
LANGS = ("uz", "ru", "en")
FIELDS = ("title", "excerpt", "meta_title", "meta_description", "content")
JSONLD = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.DOTALL)
DELETED_QUESTIONS = re.compile(      # a deleted FAQ entry leaves no question behind, in an <h3> or in the JSON-LD
    r"how quickly|как быстро|qanchalik tez yetkazib|just one 19|только на одну 19|faqat bitta 19", re.IGNORECASE)
RETURNED_BOTTLE = {                  # the subscription article says WHICH bottle goes back (rows P5-P7)
    "uz": ("Bo‘sh 19 l idish keyingi yetkazib berishda olib ketiladi", "bo‘sh 19 l idishlar olib ketiladi", "19 l idishni qaytarish"),
    "ru": ("Пустая бутыль 19 л возвращается", "пустые бутыли 19 л забирают", "возврат бутылей 19 л"),
    "en": ("Empty 19 L bottles are collected", "empty 19 L bottles go back automatically", "returnable 19 L bottles"),
}
BOTTLE_CAUGHT = {
    "deposit": ("Залог за возвратные 19 л бутыли начисляется при первой доставке и возвращается при сдаче тары.",
                "при первой доставке тара выдаётся под залог; при отмене подписки залог возвращается при сдаче бутылей.",
                "A deposit on the returnable 19 L bottles is charged on the first delivery and refunded when the bottles are returned.",
                "the bottles are issued under deposit on the first delivery",
                "Qaytariladigan 19 l idish uchun garov puli birinchi yetkazib berishda hisoblanadi."),
    "uz_deposit_word": ("Idish depoziti — suv narxi emas.", "Tara depoziti", "birinchi yetkazib berishda idish garov asosida beriladi"),
    "ten_litre_returnable": ("Доступные форматы: возвратные бутылки 18,9 л и 10 л.", "возвратной таре 18,9 л и 10 л", "Да (18,9 л и 10 л)",
                             "Mavjud formatlar: qaytariladigan 18,9 l va 10 l.", "Ha (18,9 l va 10 l)",
                             "Available formats: returnable 18.9 L and 10 L.", "Yes (18.9 L and 10 L)",
                             "Its returnability follows current operational policy", "Возвратность зависит от текущей операционной политики",
                             "Qaytarilish joriy operatsion siyosatga bog'liq", "contact /contact for current return policy",
                             "уточнить условия возврата на текущей этикетке", "qaytarish shartlarini joriy yorliqda"),
    "typed_minimum": ("There is no hard minimum — we serve small offices of 5 people too.", "Жёсткого минимума нет, автозаказ по подписке",
                      "Qattiq minimum yo'q — biz 5 kishilik", "The minimum subscription is one bottle per delivery.",
                      "Минимальная подписка — одна бутыль за доставку.", "Minimal obuna — bitta yetkazib berishga bitta butilka."),
    "numbers_kept_out": ("we deliberately keep numbers out of this guide", "Without quoting numbers (current prices live on /shop)",
                         "мы намеренно не приводим цифры в гиде", "Без конкретных цифр (актуальные цены — на странице /shop)",
                         "biz qoʻllanmaga raqamlarni keltirmaymiz", "Aniq raqamlarsiz (joriy narxlar — /shop sahifasida)"),
    "damaged_bottle": ("If the bottle is damaged, it is taken out of circulation.", "Если тара повреждена — она выводится из оборота.",
                       "Tara shikastlangan bo'lsa — muomaladan chiqariladi."),
    "typed_price": ("17 345 so'm", "17 345 сум", "17,345 UZS", "17 ming so‘m"),      # an unusual figure, never the real price
}
BOTTLE_SPARED = (                    # true sentences that sit next to the false ones
    "rich in mineral deposits", "a confined aquifer overlain by impermeable deposits",
    "Мы продаём только два формата: 18,9 л (стандарт «19 литров», возвратная тара) и 10 л.",
    'We sell only two formats: 18.9 L (the "19-litre" returnable standard) and 10 L.',
    "You can order an 18.9 L returnable bottle or a 10 L pack from our shop", "It is available in 18.9 L returnable and 10 L formats",
    "19 л (возвратная, для кулера или помпы), 10 л (одноразовая, удобна для квартир без кулера)",
    "Available formats: 18.9 L (returnable) and 10 L (single-use).", "18,9 l (qaytariladigan) va 10 l (bir martalik)",
    "Is the 10 L bottle returnable?", "The 10 L bottle is yours to keep; there is nothing to return.",
    "Депозит за тару — не цена воды.", "У части операторов депозит вообще отсутствует, потому что тара остаётся на учёте поставщика.",
    "Залог за тару .", "Уточните залог и условия возврата.", "Idish uchun garov puli — suv narxi emas.",
    "Baʼzi operatorlarda garov puli umuman yoʻq.", "Garov puli va qaytarish shartlarini aniqlang.",
    "The bottle deposit is not the price of water.", "Bottle deposit .", "Confirm the deposit and return terms.",
    "Залог за бутыли не берём — ни при первой доставке, ни при оформлении подписки.",
    "Idishlar uchun garov puli olinmaydi — birinchi yetkazib berishda ham, obunani rasmiylashtirishda ham.",
    "We charge no deposit for the bottles, on the first delivery or at sign-up.",
    "Is there a deposit for the bottle?", "If our inspection finds a bottle worn out, it is taken out of circulation.",
)


def _article(stem):
    return importlib.import_module(f"scripts.blog_content.{stem}").ARTICLE


def _texts(translation: dict) -> dict[str, str]:
    """One language of one module, as a reader and a crawler lift it: the four short fields, the visible body, each JSON-LD string."""
    texts = {field: translation[field] for field in FIELDS if field != "content"}
    content = translation["content"]
    texts["content"] = BeautifulSoup(JSONLD.sub("", content), "html.parser").get_text(" ")
    for number, raw in enumerate(JSONLD.findall(content)):
        for leaf_number, leaf in enumerate(_string_leaves(json.loads(raw))):
            texts[f"json-ld {number}.{leaf_number}"] = leaf
    return texts


@pytest.mark.unit
def test_the_bottle_detector_catches_every_false_line_and_spares_true_copy():
    for name, lines in BOTTLE_CAUGHT.items():
        for line in lines:
            assert name in find_bottle_claims(flat(line)), (name, line)
    shipped = [row[lang] for key, row in BACKEND_TRANSLATIONS.items()
               if key.startswith(("landing.offer.", "landing.terms.")) for lang in LANGS if row.get(lang)]
    for line in list(BOTTLE_SPARED) + shipped:                       # the no-deposit copy of Tasks 5-9 (and 23) must pass
        assert find_bottle_claims(flat(line)) == [], line


@pytest.mark.unit
def test_all_eight_modules_are_read():
    assert len(STEMS) == 8 and "aqua_element_vs_hayot_vs_nestle_pure_life" in STEMS


@pytest.mark.unit
@pytest.mark.parametrize("stem", STEMS)
@pytest.mark.parametrize("lang", LANGS)
def test_no_article_carries_retired_or_false_wording(stem, lang):
    hits = []
    for where, text in _texts(_article(stem)["translations"][lang]).items():
        text = flat(text)
        found = find_retired(text) + find_bottle_claims(text)
        if DELETED_QUESTIONS.search(text):
            found.append("deleted_question")
        if any(sentence in text for sentence in get_delivery_statement(lang)):
            found.append("statement_pasted")                         # the block under the article is the only carrier
        if found:
            where_exactly = [s[:200] for s in sentences(text) if find_retired(s) or find_bottle_claims(s) or DELETED_QUESTIONS.search(s)]
            hits.append(f"[{where}] {found}: {where_exactly or text[:200]}")
    assert hits == [], "\n".join(hits)


@pytest.mark.unit
@pytest.mark.parametrize("stem", STEMS)
def test_every_article_passes_the_seed_validator_and_its_faq_block_parses(stem):
    article = _article(stem)
    assert _validate_article(article, article["slug"]) == []          # 1200 words, meta description 160, excerpt 280, links
    for lang in LANGS:
        blocks = [json.loads(raw) for raw in JSONLD.findall(article["translations"][lang]["content"])]
        assert [block["@type"] for block in blocks] == ["FAQPage"] and blocks[0]["mainEntity"]


@pytest.mark.unit
@pytest.mark.parametrize("lang", LANGS)
def test_the_subscription_article_says_which_bottle_goes_back(lang):
    translation = _article("podpiska_na_vodu_tashkent")["translations"][lang]
    joined = " ".join(translation[field] for field in FIELDS)
    assert [needle for needle in RETURNED_BOTTLE[lang] if needle not in joined] == []


@pytest.mark.integration
@pytest.mark.parametrize("stem", STEMS)
def test_every_article_renders_without_retired_wording(app, db, offer_skus, stem):
    article = _article(stem)
    seed_copy(db)                                                     # once; then GET many
    _upsert_post(article)
    db.session.commit()
    for lang in LANGS:
        soup = get_page(app, f"/blog/{article['slug']}", lang)
        assert flat(soup.select_one("main h1").get_text()) == flat(article["translations"][lang]["title"])
        for where, text in _page_texts(soup).items():                 # title, meta description, main text, every JSON-LD leaf
            assert find_retired(flat(text)) == [], (stem, lang, where, flat(text)[:160])
