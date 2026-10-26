"""Blog posts must not promise a subscription discount the backend never gives.

Owner decision 2026-10-23: subscriptions give NO discount; delivery is always
free; AquaCoins are earned at the user's tier rate; the real benefit is
auto-order. Imports the real article modules and the real seed validator.
"""

import importlib
import json
import re
from pathlib import Path

import pytest

BLOG_DIR = Path(__file__).resolve().parents[2] / "scripts" / "blog_content"
SLUGS = sorted(p.stem for p in BLOG_DIR.glob("*.py") if p.stem != "__init__")
LANGS = ("ru", "uz", "en")

SUBSCRIPTION = re.compile(r"подписк|subscri|obuna", re.IGNORECASE)
DISCOUNT = re.compile(
    r"скидк|скидок|выгодн|дешевл|экономи|discount|cheaper|cost-effective|chegirma|arzonroq|arzon\b|"
    r"foydali|tejam|"
    r"цена за литр.{0,40}(?:ниже|выгодн)|per-litre.{0,40}lower|litr narxi.{0,40}past",
    re.IGNORECASE,
)
PERCENT_PRICE = re.compile(r"\d\s*%.{0,30}(?:дешевл|cheaper|arzon)", re.IGNORECASE)
SPLIT = re.compile(r"(?<=[.!?])\s+|</?(?:p|li|h\d|ul|ol|div)[^>]*>|\{\"@type\"")


def _module(slug):
    return importlib.import_module(f"scripts.blog_content.{slug}")


def _sentences(html):
    return [s for s in SPLIT.split(html) if s and s.strip()]


@pytest.mark.parametrize("slug", SLUGS)
@pytest.mark.parametrize("lang", LANGS)
def test_no_subscription_discount_claim(slug, lang):
    article = _module(slug).ARTICLE
    offenders = [
        s.strip()[:200]
        for s in _sentences(article["translations"][lang]["content"])
        if SUBSCRIPTION.search(s) and (DISCOUNT.search(s) or PERCENT_PRICE.search(s))
    ]
    assert offenders == []


@pytest.mark.parametrize("slug", SLUGS)
def test_subscription_savings_tag_removed(slug):
    assert "savings" not in _module(slug).ARTICLE["tags"].split(",")


@pytest.mark.parametrize("slug", ["voda_dlya_ofisa_tashkent", "podpiska_na_vodu_tashkent"])
@pytest.mark.parametrize("lang", LANGS)
def test_faq_jsonld_still_parses_and_is_truthful(slug, lang):
    html = _module(slug).ARTICLE["translations"][lang]["content"]
    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL)
    assert blocks, "FAQPage JSON-LD missing"
    for raw in blocks:
        data = json.loads(raw)
        if data.get("@type") != "FAQPage":
            continue
        assert data["mainEntity"]
        for q in data["mainEntity"]:
            assert not DISCOUNT.search(q["acceptedAnswer"]["text"])


@pytest.mark.parametrize("lang,needles", [
    ("ru", ["автозаказ", "бесплатн", "AquaCoins"]),
    ("uz", ["avtobuyurtma", "bepul", "AquaCoins"]),
    ("en", ["auto-order", "free", "AquaCoins"]),
])
def test_subscription_post_carries_the_real_benefits(lang, needles):
    html = _module("podpiska_na_vodu_tashkent").ARTICLE["translations"][lang]["content"].lower()
    for n in needles:
        assert n.lower() in html


@pytest.mark.parametrize("slug", ["voda_dlya_ofisa_tashkent", "podpiska_na_vodu_tashkent"])
def test_seed_validator_still_clean(slug):
    from scripts.seed_blog_milestone_2 import _validate_article

    article = _module(slug).ARTICLE
    warnings = _validate_article(article, article["slug"])
    assert warnings == []


# No monthly statement exists: every scheduled delivery is its own order, paid on its own rail
# (spec 2026-10-23 §2.1, §4.7 Copy). The post may not promise a monthly invoice, VAT statement or
# "monthly billing" in any language, in the article or in its FAQ JSON-LD.
MONTHLY_INVOICE = re.compile(
    r"monthly invoice|invoice for offices|ежемесячн\w+ счёт|ежемесячным счётом|месячн\w+ счёт|"
    r"oylik hisob-kitob|QQS bilan oylik",
    re.IGNORECASE,
)
INVOICE_DENIALS = ("не выставляется", "chiqarilmaydi", "no monthly invoice")


@pytest.mark.parametrize("lang", LANGS)
def test_subscription_post_promises_no_monthly_invoice(lang):
    html = _module("podpiska_na_vodu_tashkent").ARTICLE["translations"][lang]["content"]
    offenders = [
        s.strip()[:200]
        for s in _sentences(html)
        if MONTHLY_INVOICE.search(s) and not any(denial in s.lower() for denial in INVOICE_DENIALS)
    ]
    assert offenders == []


@pytest.mark.parametrize("lang,needle", [
    ("ru", "Ежемесячный счёт не выставляется"),
    ("uz", "Oylik hisob-faktura chiqarilmaydi"),
    ("en", "No monthly invoice is issued"),
])
def test_subscription_post_says_each_delivery_is_its_own_paid_order(lang, needle):
    html = _module("podpiska_na_vodu_tashkent").ARTICLE["translations"][lang]["content"]
    assert needle in html


# Every delivery is its own order, paid on its own rail: nothing is "one periodic payment" (final review M6).
ONE_PERIODIC_PAYMENT = re.compile(
    r"периодическ\w* платёж|davriy to.lov|one (?:periodic|recurring) payment|single (?:periodic|recurring) payment",
    re.IGNORECASE,
)
# Customer copy points to the bot chat, never to the phone (no "call a manager").
CALL_US = re.compile(
    r"позвонит\w*|позвоните|звоните|qo.ng.iroq qil\w*|\bcall (?:a manager|us|our)\b",
    re.IGNORECASE,
)


@pytest.mark.parametrize("lang", LANGS)
def test_subscription_post_promises_no_single_periodic_payment_and_never_says_call(lang):
    translation = _module("podpiska_na_vodu_tashkent").ARTICLE["translations"][lang]
    text = " ".join(str(value) for value in translation.values())
    assert ONE_PERIODIC_PAYMENT.findall(text) == []
    assert CALL_US.findall(text) == []


@pytest.mark.parametrize("lang,needle", [
    ("ru", "напишите нам в чат Telegram-бота"),
    ("uz", "Telegram-bot chatida bizga yozing"),
    ("en", "message us in the Telegram bot chat"),
])
def test_subscription_post_points_help_to_the_bot_chat(lang, needle):
    html = _module("podpiska_na_vodu_tashkent").ARTICLE["translations"][lang]["content"]
    assert needle in html
