"""Retired delivery wording (plan 2026-11-12, "live price and same-day delivery visibility").

A plain module, not a test: the site-wide guard and the blog guard both import it.
"""

import json
import re
from collections.abc import Iterator

SAME = r"(?:shu kun\w*|o.sha kun\w*|в тот же день|в день заказа|same[- ]day)"
CLOCK = r"(?:\b(?:[01]?\d|2[0-3]):[0-5]\d\b|\b(?:1[0-2]|[1-9])\s?(?:am|pm)\b|до \d{1,2} час|\bsoat \d{1,2}\b)"
DELIVER = r"(?:deliver\w*|достав\w*|привез\w*|привоз\w*|yetkaz\w*)"
HOURS = r"(?:\b\d+(?:\s?[–-]\s?\d+)?[\s-]?(?:hours?|hrs?|minutes?|mins?|час(?:а|ов)?|минут\w*|soat\w*|daqiqa\w*)\b)"
PERCENT = r"(?:\b\d+(?:[.,]\d+)?\s?%)"
GUARANTEED = r"(?:\bis guaranteed\b|kafolatlan(?!ma)\w*)"
_PATTERNS = [
    ("slot_condition", r"open (?:time )?slots?|available slots?|slots remain|next available slot|commits to a time slot|route slot"
                       r"|remaining slots|available delivery windows"
                       r"|свободн\w+ интервал\w*|доступн\w+ временн\w+ интервал\w*|(?<![а-яё])слот\w*"
                       r"|bo.sh vaqt oralig|mavjud vaqt oralig|\bslot(?:ini|ni|lar\w*)\b|aniq slot"),
    ("zone_condition", r"service zones?|many zones|most (?:service )?(?:zones|areas|addresses)"
                       r"|в большинстве (?:зон|районов)|для большинства адресов|ko.p (?:hududlarda|manzillar)"),
    ("rush",           r"\brush\b|(?<![а-яё])срочн(?:о|ую|ая|ой|ый)\b|shoshilinch"),
    ("working_day",    r"next business day|\d\s*[–-]\s*\d (?:working|business) days|следующ\w+ рабоч\w+ день"
                       r"|\d\s*[–-]\s*\d рабочих дн\w+|ertasi ish kuni|\d\s*[–-]\s*\d ish kuni"),
    ("monday",         r"до понедельника|только в понедельник|dushanbagacha|faqat dushanbada|until monday"),
    ("fast",           r"fast delivery|fast and reliable|same day delivery available|tezkor|tez va ishonchli"
                       r"|быстр\w+ (?:и над[её]жн\w+|доставк\w+)|fastest|\beng tez(?![- ]tez)|сам\w+ быстр\w+"),
    ("guarantee",      r"guaranteed? (?:same|delivery)|kafolatlaymiz|гарантир\w+ доставк\w+"
                       r"|" + DELIVER + r"[^.!?]{0,60}" + GUARANTEED + "|" + GUARANTEED + r"[^.!?]{0,60}" + DELIVER),
    ("daytime_uz",     r"kunduzgi soatlarda"),
    ("confirmed",      r"confirmed by an operator|подтвержда\w+ оператор\w*|operator yoki bot tomonidan"),
    ("clock_hour",     SAME + r"[^.!?]{0,120}" + CLOCK + "|" + CLOCK + r"[^.!?]{0,120}" + SAME),
    ("flat_claim",     r"same-day delivery across tashkent"),
    ("express",        r"\bexpress\b|\bekspress\w*|экспресс\w*|(?<![\w-])tez yetkaz\w*"),
    ("hours_figure",   HOURS + r"[^.!?]{0,80}" + DELIVER + "|" + DELIVER + r"[^.!?]{0,80}" + HOURS),
    ("percent",        r"(?:" + DELIVER + "|" + SAME + r")[^.!?]{0,80}" + PERCENT + "|" + PERCENT + r"[^.!?]{0,40}" + SAME
                       + "|" + PERCENT + r"\s?(?:of (?:\w+ )?(?:orders|deliveries)|заказ\w*|доставок)"),
]
RETIRED = [(name, re.compile(pattern, re.IGNORECASE)) for name, pattern in _PATTERNS]
# (?<![а-яё]) keeps «кислота» and «краткосрочную» out; (?![- ]tez) keeps «eng tez-tez» (most often) out;
# the subscription "time slot" picker and "22 working days" are real and never match.
# The last three entries and the guarantee entry's last two alternatives come from the final review (W5):
# - hours_figure: an hours or minutes figure in the same sentence as a delivery word ("8–9 hours in front of a screen"
#   and office hours are not);
# - percent: a percentage after a delivery or same-day word, or shortly before a same-day word, or "of orders": a page
#   runs its tiles together with no full stop, so "100% Satisfaction Rate Free Delivery" and "100% secure transactions"
#   above the block heading must stay clear;
# - express: a whole word ("expressed in mg/L" is not); "tez yetkaz" not after a hyphen («tez-tez yetkazib», how often);
# - guarantee: "is guaranteed" or "kafolatlan" (not its negative «kafolatlanmaydi») beside a delivery word, because the
#   loyalty guide truly says "Your new tier is guaranteed for {days} days".


def find_retired(text: str) -> list[str]:
    """The names of the entries that match `text`, in list order. Call it on `flat()` text."""
    return [name for name, pattern in RETIRED if pattern.search(text)]


def _string_leaves(obj) -> Iterator[str]:
    """Each str inside a parsed JSON value, recursing into dict values and list items."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from _string_leaves(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _string_leaves(item)


def _page_texts(soup) -> dict[str, str]:
    """What a crawler lifts, as separate strings: title, meta description, main text, every JSON-LD string leaf."""
    meta = soup.select_one('meta[name="description"]')
    texts = {"title": soup.title.get_text(), "meta description": meta["content"] if meta else "",
             "main": soup.select_one("main").get_text(" ")}
    for number, tag in enumerate(soup.select('script[type="application/ld+json"]')):
        for leaf_number, leaf in enumerate(_string_leaves(json.loads(tag.string))):
            texts[f"json-ld {number}.{leaf_number}"] = leaf
    return texts


# ---- blog articles only (Task 18): bottle, minimum and price wording. find_retired() above does not change. ----
DEPOSIT = re.compile(r"deposit(?!s\b)|garov|залог|депозит|depozit", re.IGNORECASE)          # "deposits" is geology
NO_DEPOSIT = re.compile(
    r"no deposit|без залога|залог\w*[^.!?]{0,40}\b(?:нет|не бер\w+)|garov puli(?:siz|\s+yo.q|\s+olinmaydi)", re.IGNORECASE)
DEPOSIT_ADVICE = re.compile(      # advice about any supplier (buyer guide, office article): true as advice, kept
    r"прозрачен депозит|депозит за тару — не цена воды|в чеке заложен возвратный депозит|у части операторов депозит"
    r"|^залог за тару\W*$|уточните залог"
    r"|garov puli qanchalik shaffof|garov puli — suv narxi emas|qaytariladigan garov puli bo.lsa"
    r"|operatorlarda garov puli umuman yo.q|^idish uchun garov puli\W*$|garov puli va qaytarish shartlarini aniqlang"
    r"|bottle deposit, if any|bottle deposit is not the price of water|bottle has a deposit, that amount is refunded"
    r"|^bottle deposit\W*$|confirm the deposit and return terms", re.IGNORECASE)
_BOTTLE_PATTERNS = [
    ("uz_deposit_word",      r"depozit|garov(?!\s+puli)"),                       # the one Uzbek word is "garov puli"
    ("ten_litre_returnable", r"(?:returnable|возвратн\w+|qaytariladigan)[^<()]{0,25}18[.,]9\s?[lл]\s(?:and|и|va)\s10\s?[lл]\b"
                             r"|\(18[.,]9\s?[lл]\s(?:and|и|va)\s10\s?[lл]\)"
                             r"|operational policy|операционной политики|operatsion siyosatga|return policy"
                             r"|условия возврата на текущей|qaytarish shartlarini joriy"),
    ("typed_minimum",        r"no hard minimum|жёсткого минимума нет|qattiq minimum yo.q"
                             r"|minimum subscription is|минимальная подписка|minimal obuna"),
    ("numbers_kept_out",     r"keep numbers out|without quoting numbers|aniq raqamlarsiz|raqamlarni keltirmaymiz"
                             r"|без конкретных цифр|не приводим цифры"),
    ("damaged_bottle",       r"if the bottle is damaged|тара повреждена|tara shikastlangan"),
    ("typed_price",          r"\d[\d\s.,]*(?:(?:ming|тыс\.?|thousand)\s*)?(?:so.m|сум|сўм|uzs)\b"),
]
BOTTLE_CLAIMS = [(name, re.compile(pattern, re.IGNORECASE)) for name, pattern in _BOTTLE_PATTERNS]
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_END.split(text) if part.strip()]


def find_bottle_claims(text: str) -> list[str]:
    """Names of the false bottle, minimum and price wording in one flat() string of a blog article.
    A sentence with a deposit word is a hit unless it is a question, says there is no deposit, or is market advice."""
    found = [name for name, pattern in BOTTLE_CLAIMS if pattern.search(text)]
    if any(DEPOSIT.search(s) and not s.endswith("?") and not NO_DEPOSIT.search(s) and not DEPOSIT_ADVICE.search(s)
           for s in sentences(text)):
        found.append("deposit")
    return found
