"""The blog and the Aqua Club guide after the redesign (group "content", unit blog + loyalty, 2026-11-14).

Driven through the real pages: GET /blog, /blog?category=…, /blog?tag=…, /blog/<slug> and /loyalty-guide in uz, ru
and en, with the rows the seeds ship (the backend seed's landing.* / loyalty_guide.* rows, the shell's and this group's
web.* rows) in the database. The guide's own corrections (‘ and ’ in Uzbek, config-owned numbers, "the website",
the streak sentences) ship in the backend seed's LOYALTY_GUIDE_TRANSLATIONS, the rows' one source.
"""

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest

from business_app.models.blog import BlogCategory, BlogPost, BlogStatus
from business_app.models.loyalty import LoyaltyProgram, LoyaltyStreakRule, LoyaltyTierConfig
from business_app.models.translation import Translation
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS
from scripts.seed_backend_translations import LOYALTY_GUIDE_TRANSLATIONS
from scripts.seed_web_ui_content_translations import CATEGORY
from scripts.seed_web_ui_content_translations import TRANSLATIONS as CONTENT
from scripts.seed_web_ui_integration_translations import TRANSLATIONS as INTEGRATION
from scripts.seed_web_ui_shell_translations import TRANSLATIONS as SHELL
from scripts.seed_blog_milestone_2 import _upsert_post
from tests.integration.retired_phrases import _page_texts
from tests.integration.test_blog_content_is_truthful import STEMS as ARTICLE_STEMS
from tests.integration.test_blog_content_is_truthful import _article
from tests.integration.test_blog_content_is_truthful import _texts as _article_texts
from tests.integration.public_page_builders import (  # noqa: F401 -- `offer_skus` is a fixture
    flat,
    get_page,
    jsonld,
    offer_skus,
    seed_copy,
)

pytestmark = pytest.mark.integration

LANGS = ("uz", "ru", "en")
BOT_URL = "https://t.me/aqua_content_blog_test_bot"
TASHKENT = timedelta(hours=5)
PLACEHOLDER = re.compile(r"\{(\w+)\}")


@pytest.fixture
def copy_rows(app, db, monkeypatch):
    """The rows the seeds ship: landing.* and the month names (backend seed), loyalty_guide.* (backend seed with this
    group's corrections on top), the shell's and this group's web.* rows; and a known bot link."""
    monkeypatch.setitem(app.config, "COMPANY_TELEGRAM_BOT_URL", BOT_URL)
    seed_copy(db)
    present = set(db.session.query(Translation.key, Translation.language).all())

    def add(key, language, value, category):
        if (key, language) not in present:
            db.session.add(Translation(key=key, language=language, value=value, category=category, is_active=True))
            present.add((key, language))

    for key, row in BACKEND_TRANSLATIONS.items():
        if key.startswith("loyalty_guide."):
            for language in LANGS:
                add(key, language, row.get(language) or row["en"], "loyalty_guide")
    for rows_by_lang in (SHELL, CONTENT, INTEGRATION):
        for language, rows in rows_by_lang.items():
            for key, value in rows.items():
                add(key, language, value, CATEGORY)
    db.session.commit()
    app.jinja_env.cache.clear()


def _text(lang, key):
    for rows in (CONTENT, SHELL, INTEGRATION):
        if key in rows[lang]:
            return rows[lang][key]
    row = BACKEND_TRANSLATIONS[key]
    return row.get(lang) or row["en"]


def _lg(lang, key):
    """A loyalty_guide row as the dev/prod database holds it: the backend seed's."""
    row = BACKEND_TRANSLATIONS[key]
    return row.get(lang) or row["en"]


def _post(db, title, category=BlogCategory.LIFESTYLE, days_ago=1, tags=None):
    """A published post with its title in uz (the column), ru and en."""
    post = BlogPost(
        title=f"{title} uz",
        slug=f"{re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')}-{uuid4().hex[:6]}",
        content="<p>Matn</p><h2>Birinchi</h2><p>Matn</p>",
        excerpt=f"{title} excerpt",
        category=category,
        tags=tags,
        status=BlogStatus.PUBLISHED,
        published_at=datetime.now(UTC) - timedelta(days=days_ago),
    )
    db.session.add(post)
    db.session.flush()
    post.set_translated("title", f"{title} ru", "ru")
    post.set_translated("title", title, "en")
    db.session.commit()
    return post


def _title(post, lang):
    return post.get_translated("title", lang)


def _main(soup):
    return soup.select_one("main") or soup


# --- /blog -------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_the_blog_list_reads_in_the_visitors_language_with_a_working_filter(app, db, copy_rows, lang):
    newest = _post(db, "Office water", BlogCategory.LIFESTYLE, days_ago=1)
    _post(db, "Bottle choice", BlogCategory.LIFESTYLE, days_ago=2)
    _post(db, "Minerals", BlogCategory.WATER_BENEFITS, days_ago=3)
    _post(db, "Health", BlogCategory.HEALTH_TIPS, days_ago=4)

    soup = get_page(app, "/blog", lang)
    main = _main(soup)
    text = flat(main.get_text(" "))
    assert "web.content." not in text and "web.shell." not in text
    assert flat(soup.select_one(".page-title h1").get_text()) == _text(lang, "web.content.blog.title")

    chips = soup.select(".ct-filter .ae-chip")
    assert flat(chips[0].get_text()) == _text(lang, "web.content.blog.all")
    assert chips[0].get("aria-current") == "page"
    # Only the categories that have a post, each with its count, labelled like the cards.
    labels = [(flat(c.find(string=True, recursive=False)), flat(c.select_one(".ae-chip__count").get_text())) for c in chips[1:]]
    assert labels == [
        (BlogPost(category=BlogCategory.HEALTH_TIPS).get_category_display(lang), "1"),
        (BlogPost(category=BlogCategory.WATER_BENEFITS).get_category_display(lang), "1"),
        (BlogPost(category=BlogCategory.LIFESTYLE).get_category_display(lang), "2"),
    ]
    # A Latin "'" never stands in for the Uzbek o‘ / g‘ in a label.
    assert "'" not in flat(soup.select_one(".ct-filter").get_text())

    # The newest post leads; every post links to its article; the footer's bot band is the page's only navy band.
    assert _title(newest, lang) in flat(soup.select_one(".ct-lead .ct-post__title").get_text())
    assert len(soup.select(".ct-post")) == 4
    assert soup.select_one("main .ae-band") is None
    item_list = jsonld(soup, "ItemList")[0]
    assert item_list["numberOfItems"] == 4
    assert "noindex" not in soup.select_one('meta[name="robots"]')["content"]


@pytest.mark.parametrize("lang", LANGS)
def test_an_empty_category_shows_the_empty_state_and_stays_out_of_the_index(app, db, copy_rows, lang):
    _post(db, "Office water", BlogCategory.LIFESTYLE)
    _post(db, "Minerals", BlogCategory.WATER_BENEFITS)

    soup = get_page(app, "/blog?category=environment", lang)
    empty = soup.select_one(".ae-empty")
    assert empty is not None and _text(lang, "web.content.blog.empty_title") in flat(empty.get_text(" "))
    assert soup.select_one('meta[name="robots"]')["content"].startswith("noindex")
    assert not soup.select('.ct-filter a[href*="category=environment"]')
    assert soup.select_one(".ct-filter [aria-current]") is None  # neither "All" nor a listed category is this page


def test_an_unknown_category_is_ignored(app, db, copy_rows):
    _post(db, "Office water", BlogCategory.LIFESTYLE)
    soup = get_page(app, "/blog?category=no-such-thing", "en")
    assert len(soup.select(".ct-post")) == 1
    assert "category=" not in soup.select_one('link[rel="canonical"]')["href"]


def test_pagination_keeps_the_category_and_marks_the_current_page(app, db, copy_rows):
    for i in range(11):
        _post(db, f"Post {i}", BlogCategory.LIFESTYLE, days_ago=i + 1)
    _post(db, "Other", BlogCategory.HEALTH_TIPS)

    soup = get_page(app, "/blog?category=lifestyle&page=2", "ru")
    assert len(soup.select(".ct-post")) == 2
    pager = soup.select_one(".ae-pagination")
    assert flat(pager.select_one('[aria-current="page"]').get_text()) == "2"
    prev = pager.select_one('a[rel="prev"]')
    assert prev["aria-label"] == _text("ru", "web.content.blog.prev")
    assert parse_qs(urlsplit(prev["href"]).query)["category"] == ["lifestyle"]
    assert parse_qs(urlsplit(soup.select_one('link[rel="prev"]')["href"]).query)["category"] == ["lifestyle"]
    assert _text("ru", "web.content.blog.page_n").format(n=2) in soup.title.get_text()


def test_an_old_tag_link_filters_and_offers_a_way_back(app, db, copy_rows):
    tagged = _post(db, "Bottle choice", BlogCategory.LIFESTYLE, tags="19L, 10L")
    _post(db, "Minerals", BlogCategory.WATER_BENEFITS)

    soup = get_page(app, "/blog?tag=19L", "uz")
    assert [flat(a.get_text()) for a in soup.select(".ct-post__title a")] == [_title(tagged, "uz")]
    chip = soup.select_one(".ct-filter__tag")
    # Tags are untranslated English slugs: the chip says a topic filter is on, never the slug, and the listing is
    # kept out of the index (review C-18).
    assert flat(chip.get_text()) == _text("uz", "web.content.blog.tag_filter")
    assert "19L" not in soup.title.get_text() and "19L" not in chip.get_text()
    assert "noindex" in soup.select_one('meta[name="robots"]')["content"]
    assert "tag=" not in chip["href"]


@pytest.mark.parametrize("query", ["page=2", "page=999", "page=0", "category=lifestyle&page=2"])
def test_a_page_past_the_last_one_is_not_found(app, db, copy_rows, query):
    """Review C-17: one page of posts exists, so ?page=2 (or 999, or 0) is a 404, not a 200 empty listing."""
    _post(db, "Bottle choice", BlogCategory.LIFESTYLE)
    app.jinja_env.cache.clear()
    assert app.test_client().get(f"/blog?lang=ru&{query}", follow_redirects=True).status_code == 404
    assert app.test_client().get("/blog?lang=ru&page=1", follow_redirects=True).status_code == 200


def test_an_empty_category_is_still_its_empty_state_not_a_404(app, db, copy_rows):
    _post(db, "Bottle choice", BlogCategory.LIFESTYLE)
    app.jinja_env.cache.clear()
    response = app.test_client().get("/blog?lang=ru&category=environment", follow_redirects=True)
    assert response.status_code == 200 and b"noindex" in response.data


# --- /blog/<slug> ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_the_article_page_has_a_reading_layout_and_ends_with_the_price_the_order_actions_and_sharing(
    app, db, offer_skus, copy_rows, lang
):
    post = _post(db, "Water & health #1", BlogCategory.LIFESTYLE, tags="water")
    soup = get_page(app, f"/blog/{post.slug}", lang)
    main = _main(soup)
    assert "web.content." not in flat(main.get_text(" "))

    title = _title(post, lang)
    assert flat(soup.select_one(".page-title h1").get_text()) == title
    crumbs = [flat(li.get_text()) for li in soup.select(".page-title .bread-crumb li")]
    assert crumbs[-1] == _text(lang, "web.content.blog.article")  # a long title never wraps the trail

    cat = soup.select_one(".ct-article__cat")
    assert parse_qs(urlsplit(cat["href"]).query)["category"] == ["lifestyle"]
    assert flat(cat.get_text()) == post.get_category_display(lang)
    published = (post.published_at.replace(tzinfo=UTC) + TASHKENT).date().isoformat()
    assert soup.select_one(".ct-article__meta time")["datetime"] == published
    shown = flat(soup.select_one(".ct-article__meta time").get_text())   # the site's one date format (review C-21)
    assert "web." not in shown and str(int(published[8:])) in shown and published[:4] in shown

    # The editor's HTML stays in .blog-content; the price block, the order actions and the share row follow it.
    assert soup.select_one(".text > .blog-content.ae-prose") is not None
    assert soup.select_one(".blog-content [data-offer-facts], .blog-content .ct-order") is None
    order = soup.select_one(".text ~ [data-offer-facts] ~ .ct-order")
    assert order is not None and soup.select_one(".ct-order ~ .post-share-option") is not None
    hrefs = [a["href"] for a in order.select("a.ae-btn")]
    assert hrefs[0] == BOT_URL and urlsplit(hrefs[1]).path == "/shop"
    assert soup.select_one(".post-tags") is None  # raw tag slugs are gone

    # Share links carry the canonical URL and the title intact, encoded, and open safely in a new tab.
    canonical = soup.select_one('link[rel="canonical"]')["href"]
    telegram = soup.select_one('.ct-share a[href^="https://t.me/share/url"]')
    query = parse_qs(urlsplit(telegram["href"]).query)
    assert query["url"] == [canonical] and query["text"] == [title]
    for link in soup.select(".ct-share a"):
        assert link["rel"] == ["noopener", "noreferrer"] and link["target"] == "_blank"

    article = jsonld(soup, "BlogPosting")[0]
    assert article["headline"] == title and article["url"] == canonical


def test_the_related_row_is_full_and_starts_with_the_same_category(app, db, copy_rows):
    current = _post(db, "Current", BlogCategory.LIFESTYLE, days_ago=1)
    same = _post(db, "Same category, older", BlogCategory.LIFESTYLE, days_ago=9)
    newest_other = _post(db, "Other, newest", BlogCategory.HEALTH_TIPS, days_ago=2)
    second_other = _post(db, "Other, second", BlogCategory.WATER_BENEFITS, days_ago=3)
    _post(db, "Other, oldest", BlogCategory.HEALTH_TIPS, days_ago=20)

    soup = get_page(app, f"/blog/{current.slug}", "en")
    related = soup.select(".ct--related .ct-post__title a")
    assert [flat(a.get_text()) for a in related] == [_title(p, "en") for p in (same, newest_other, second_other)]
    assert flat(soup.select_one(".ct--related h2").get_text()) == _text("en", "web.content.blog.related")


def test_reading_an_article_counts_the_view_but_does_not_move_its_date_modified(app, db, copy_rows):
    """Review C-03: GET /blog/<slug> bumps the view counter only. `updated_at` (the BlogPosting dateModified) stays the
    article's last edit, however often it is read."""
    post = _post(db, "Bottle choice")
    edited = datetime(2026, 5, 8, 10, 0, tzinfo=UTC)
    BlogPost.query.filter(BlogPost.id == post.id).update(
        {BlogPost.updated_at: edited, BlogPost.view_count: 0}, synchronize_session=False)
    db.session.commit()

    modified = []
    for _ in range(2):
        soup = get_page(app, f"/blog/{post.slug}", "ru")
        modified.append(jsonld(soup, "BlogPosting")[0]["dateModified"])
    db.session.expire_all()
    row = db.session.get(BlogPost, post.id)
    assert row.view_count == 2
    assert row.updated_at.replace(tzinfo=UTC) == edited
    assert [m[:19] for m in modified] == ["2026-05-08T10:00:00", "2026-05-08T10:00:00"]


@pytest.fixture
def published_at_read_back_aware():
    """Postgres hands `published_at` back aware (timestamptz); the test SQLite hands it back naive, and the admin
    serializer's `is_published` compares it with an aware "now". Load it aware, as production does, without marking
    the row dirty (a flush would otherwise write it back and stamp `updated_at`)."""
    from sqlalchemy import event
    from sqlalchemy.orm.attributes import set_committed_value

    def aware(target, *_):
        value = target.__dict__.get("published_at")
        if value is not None and value.tzinfo is None:
            set_committed_value(target, "published_at", value.replace(tzinfo=UTC))

    event.listen(BlogPost, "load", aware)
    event.listen(BlogPost, "refresh", aware)
    yield
    event.remove(BlogPost, "load", aware)
    event.remove(BlogPost, "refresh", aware)


def test_reading_an_article_does_not_reorder_the_admin_blog_list(
    app, db, copy_rows, admin_user, published_at_read_back_aware
):
    """The one other reader of `updated_at`: Admin, Blog lists posts by their last edit (GET
    /api/v1/admin/blog/posts, newest edit first). A read used to stamp `updated_at`, so the most-read post jumped to the
    top; after C-03 only an edit moves it. The sitemap's lastmod reads `published_at` and never depended on it."""
    from flask_jwt_extended import create_access_token

    older = _post(db, "Edited in May", days_ago=3)
    newer = _post(db, "Edited in June", days_ago=2)
    ids = (older.id, newer.id)
    for post_id, edited in zip(ids, (datetime(2026, 5, 8, 10, 0, tzinfo=UTC), datetime(2026, 6, 8, 10, 0, tzinfo=UTC))):
        BlogPost.query.filter(BlogPost.id == post_id).update({BlogPost.updated_at: edited}, synchronize_session=False)
    db.session.commit()
    token = create_access_token(identity=str(admin_user.id), additional_claims={"role": "admin"})

    def admin_order():
        response = app.test_client().get(
            "/api/v1/admin/blog/posts?per_page=100", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200, response.get_data(as_text=True)
        return [item["id"] for item in response.get_json()["data"]["items"] if item["id"] in ids]

    assert admin_order() == [newer.id, older.id]
    assert get_page(app, f"/blog/{older.slug}", "ru").select_one("main h1") is not None   # a reader opens the older one
    db.session.expire_all()
    assert db.session.get(BlogPost, older.id).view_count == 1
    assert admin_order() == [newer.id, older.id]


@pytest.mark.parametrize("meta_title, expected", [
    ("19 л или 10 л — Aqua Element", "19 л или 10 л — Aqua Element"),   # the editor's title already names the brand
    ("19 л или 10 л", "19 л или 10 л — Aqua Element"),  # the site's one title separator (review SITE-14)
])
def test_the_article_title_names_the_brand_once(app, db, copy_rows, monkeypatch, meta_title, expected):
    """Review C-05: the brand is appended to the article's <title> only when its meta title lacks it."""
    monkeypatch.setitem(app.config, "COMPANY_NAME", "Aqua Element")
    post = _post(db, "Bottle choice")
    post.set_translated("meta_title", meta_title, "ru")
    db.session.commit()
    soup = get_page(app, f"/blog/{post.slug}", "ru")
    assert flat(soup.title.get_text()) == expected
    assert flat(soup.title.get_text()).count("Aqua Element") == 1


def test_an_article_with_no_other_post_has_no_related_row(app, db, copy_rows):
    only = _post(db, "Only one")
    soup = get_page(app, f"/blog/{only.slug}", "ru")
    assert soup.select_one(".ct--related") is None


# --- The article modules (scripts/blog_content) -------------------------------------------------------------------------

# Customer copy never sends anyone to the phone (owner decision 3; audit 17, review C-02): no phone channel, no "call".
PHONE = re.compile(r"телефон|позвон|перезвон|звонк|\bphone\b|\bcalls?\b(?![\s-]*(?:cent|mark|центр))|qo.ng.iroq|telefon", re.IGNORECASE)
# Aqua Element delivers water; the articles promised cooler/pump rental and sales nothing backs.
EQUIPMENT = re.compile(r"аренду и продажу|rental and sales|ijaraga olish va sotish|продаёте ли вы кулеры|do you sell coolers|"
                       r"kulerlarni sotasizmi|оборудование \(кулер|equipment \(cooler|equipment and a recurring|"
                       r"jihoz va muntazam|и оборудование оформляются|va jihoz obuna", re.IGNORECASE)
BARE_LINK = re.compile(r'<a href="(/[^"]*)">\1</a>')          # a path as its own link text ("/shop")


@pytest.mark.unit
@pytest.mark.parametrize("stem", ARTICLE_STEMS)
@pytest.mark.parametrize("lang", LANGS)
def test_no_article_tells_the_reader_to_phone_or_promises_equipment(stem, lang):
    """Every module, every language: the four short fields, the visible body and each FAQPage JSON-LD answer."""
    translation = _article(stem)["translations"][lang]
    hits = [(where, match.group(0)) for where, text in _article_texts(translation).items()
            for rx in (PHONE, EQUIPMENT) for match in rx.finditer(flat(text))]
    assert hits == []
    assert BARE_LINK.findall(translation["content"]) == []
    if lang == "uz":
        uz = " ".join(translation.values())
        assert re.findall(r"[A-Za-z]'[A-Za-z]|[ʻʼ]", uz) == []    # o‘ g‘ with U+2018, the tutuq with U+2019


@pytest.mark.parametrize("lang", LANGS)
def test_the_published_article_page_and_its_faq_jsonld_never_offer_the_phone(app, db, offer_skus, copy_rows, lang):
    """Through GET /blog/<slug> after the real seed upsert: the page text and every JSON-LD string (the article's
    FAQPage included) name no phone; ordering goes to the shop and the Telegram bot."""
    article = _article("19l_ili_10l_kakuyu_butyl_vybrat")
    _upsert_post(article)
    db.session.commit()
    soup = get_page(app, f"/blog/{article['slug']}", lang)
    texts = _page_texts(soup)
    assert any(key.startswith("json-ld") for key in texts)
    assert [(where, PHONE.findall(flat(text)) + EQUIPMENT.findall(flat(text))) for where, text in texts.items()
            if PHONE.search(flat(text)) or EQUIPMENT.search(flat(text))] == []
    assert soup.select_one('main .blog-content a[href="/shop"]') is not None
    assert BARE_LINK.findall(str(soup.select_one("main .blog-content"))) == []


# --- Covers (review C-12: stock photos of people are not Aqua Element's) ----------------------------------------------

BRAND_MARK = "/static/images/brand/aqua-element-mark-512.png"
BRAND_CARD = "/static/images/brand/og-default.png"


@pytest.mark.unit
@pytest.mark.parametrize("stem", ARTICLE_STEMS)
def test_no_article_module_ships_a_stock_cover(stem):
    """The theme's /static/images/news/ pictures (stock models drinking water) are no article's cover: a module names a
    real photo or none, and none shows the brand cover."""
    cover = _article(stem)["featured_image"]
    assert cover is None or "/images/news/" not in cover, cover


@pytest.mark.parametrize("lang", LANGS)
def test_a_post_without_a_cover_shows_the_brand_cover_and_a_real_cover_shows_itself(app, db, copy_rows, lang):
    """GET /blog and GET /blog/<slug>: a post with no picture gets the designed brand cover on its card (decorative,
    tinted by its category), no figure in the article and the brand card as its share image; a post with a real
    picture keeps it everywhere."""
    bare = _post(db, "No picture yet", BlogCategory.HEALTH_TIPS, days_ago=1)
    shot = _post(db, "Real delivery photo", BlogCategory.LIFESTYLE, days_ago=2)
    shot.featured_image = "/uploads/blog/real-delivery-2026.jpg"
    shot.image_alt_text = "A courier carries an Aqua Element bottle"
    db.session.commit()

    cards = {flat(card.select_one(".ct-post__title").get_text()): card for card in get_page(app, "/blog", lang).select(".ct-post")}
    bare_media = cards[_title(bare, lang)].select_one(".ct-post__media")
    assert bare_media["class"] == ["ct-post__media", "ct-post__media--brand", "ct-post__media--health_tips"]
    assert bare_media["aria-hidden"] == "true"
    assert (bare_media.img["src"], bare_media.img["alt"]) == (BRAND_MARK, "")
    shot_img = cards[_title(shot, lang)].select_one(".ct-post__media img")
    assert (shot_img["src"], shot_img["alt"]) == (shot.featured_image, shot.image_alt_text)
    assert "ct-post__media--brand" not in cards[_title(shot, lang)].select_one(".ct-post__media")["class"]

    article = get_page(app, f"/blog/{bare.slug}", lang)
    assert article.select_one(".ct-article figure") is None
    shared = [m["content"] for m in article.select('meta[property="og:image"]')]
    # One Open Graph set (review SITE-22): one og:type (article), one og:image, one og:title.
    assert len(shared) == 1 and urlsplit(shared[0]).path == BRAND_CARD
    assert [m["content"] for m in article.select('meta[property="og:type"]')] == ["article"]
    assert len(article.select('meta[property="og:title"]')) == 1
    assert urlsplit(jsonld(article, "BlogPosting")[0]["image"]).path == BRAND_CARD

    article = get_page(app, f"/blog/{shot.slug}", lang)
    assert article.select_one(".ct-article figure img")["src"] == shot.featured_image
    assert urlsplit(jsonld(article, "BlogPosting")[0]["image"]).path == shot.featured_image


# --- /loyalty-guide ----------------------------------------------------------------------------------------------------


@pytest.fixture
def program(db):
    program = LoyaltyProgram(
        name="Aqua Club", is_active=True, is_default=True, uzs_per_point=250, signup_bonus=200, referral_bonus=500,
        birthday_bonus=200, points_expiry_days=365, min_redemption_points=200, surprise_enabled=False,
    )
    db.session.add(program)
    db.session.flush()
    for order, (name, low, mult, pct) in enumerate(
        [("Bronze", 0, 1.0, 0), ("Silver", 1500, 1.15, 2), ("Gold", 5000, 1.3, 3), ("Platinum", 12000, 1.5, 6)]
    ):
        db.session.add(LoyaltyTierConfig(program_id=program.id, name=name, display_order=order, min_points=low,
                                         points_multiplier=mult, discount_percentage=pct, is_active=True))
    db.session.add(LoyaltyStreakRule(program_id=program.id, name="Order streak", required_orders=4, window_days=30,
                                     bonus_points=400, is_active=True, display_order=0))
    db.session.commit()
    return program


@pytest.mark.parametrize("lang", LANGS)
def test_the_guide_reads_as_sentences_in_the_visitors_language(app, db, copy_rows, program, lang):
    soup = get_page(app, "/loyalty-guide", lang)
    main = _main(soup)
    text = flat(main.get_text(" "))
    assert "web.content." not in text and "loyalty_guide." not in text
    assert "→" not in text  # no "4 / 30 → +400" formulas, no arrows glued to buttons
    assert not PLACEHOLDER.search(text)  # every {placeholder} a row names is one the page fills
    assert not main.select('[class*="wow"], [class*="fadeIn"], [class*="kicker"], [class*="eyebrow"]')

    # The streak line is the corrected sentence, filled with the rule's own numbers.
    unit = _lg(lang, "loyalty_guide.unit.points")
    line = _lg(lang, "loyalty_guide.earn.streak_line")
    assert flat(line.format(orders=4, days=30, pts="400", unit=unit)) in text

    # The two callouts under the rewards share one shape: a bold lead, then the sentence.
    notes = soup.select(".lg-notes .ae-alert")
    assert [flat(n.select_one("strong").get_text()) for n in notes] == [
        _lg(lang, "loyalty_guide.expiry.title"),
        _text(lang, "web.content.lg.delivery_title"),
    ]
    if lang == "uz":
        assert "ʻ" not in text and "ʼ" not in text


def test_the_tier_window_is_the_tier_rule_not_the_points_expiry(app, db, copy_rows, program):
    """Review C-07: AquaCoins expire after the program's points_expiry_days; the tier counts the last
    TIER_QUALIFYING_WINDOW_DAYS and locks a new tier for as long. Different settings, different numbers on the page."""
    from business_app.services.loyalty_service import LoyaltyService

    program.points_expiry_days = 180
    db.session.commit()
    window = LoyaltyService.TIER_QUALIFYING_WINDOW_DAYS
    assert window != 180
    for lang in LANGS:
        soup = get_page(app, "/loyalty-guide", lang)
        text = flat(_main(soup).get_text(" "))
        for key in ("loyalty_guide.promo.s2_title", "loyalty_guide.promo.s2_desc", "loyalty_guide.rolling.title",
                    "loyalty_guide.rolling.body", "loyalty_guide.twopoints.status_desc"):
            assert flat(_lg(lang, key).format(days=window)) in text, (lang, key)
        assert flat(_lg(lang, "loyalty_guide.expiry.body").format(days=180)) in text
        answers = {q["name"]: q["acceptedAnswer"]["text"] for q in jsonld(soup, "FAQPage")[0]["mainEntity"]}
        assert answers[_lg(lang, "loyalty_guide.faq.q3")] == _lg(lang, "loyalty_guide.faq.a3").format(days=window)
        assert answers[_lg(lang, "loyalty_guide.faq.q1")] == _lg(lang, "loyalty_guide.faq.a1").format(days=180)


@pytest.mark.parametrize("fee, free", [(0, True), (5000, False)])
def test_the_guide_says_delivery_is_free_only_while_the_offer_does(app, db, copy_rows, program, monkeypatch, fee, free):
    """Review C-08: the delivery note, the FAQ "is delivery free?" and the Bronze delivery perk follow the public
    offer's delivery fact (DEFAULT_DELIVERY_FEE), like the delivery page."""
    monkeypatch.setitem(app.config, "DEFAULT_DELIVERY_FEE", fee)
    for lang in LANGS:
        soup = get_page(app, "/loyalty-guide", lang)
        text = flat(_main(soup).get_text(" "))
        questions = [q["name"] for q in jsonld(soup, "FAQPage")[0]["mainEntity"]]
        bronze = [flat(li.get_text(" ")) for li in soup.select("article.lg-tier--bronze .lg-tier__perks li")]
        assert (_text(lang, "web.content.lg.delivery_title") in text) is free, lang
        assert (_lg(lang, "loyalty_guide.faq.q6") in questions) is free, lang
        assert (flat(_lg(lang, "loyalty_guide.tier.0.benefit2")) in bronze) is free, lang
        assert flat(_lg(lang, "loyalty_guide.tier.0.benefit1")) in bronze


@pytest.mark.parametrize("lang", LANGS)
def test_a_streak_rule_named_after_its_condition_is_printed_once(app, db, copy_rows, program, lang):
    """Review C-15: a rule whose name is its condition ("4 orders in 30 days") shows the sentence alone; a rule with
    a name of its own keeps the name above the sentence."""
    unit = _lg(lang, "loyalty_guide.unit.points")
    line = flat(_lg(lang, "loyalty_guide.earn.streak_line").format(orders=6, days=60, pts="700", unit=unit))
    db.session.add(LoyaltyStreakRule(program_id=program.id, name=line[:15].strip(), required_orders=6, window_days=60,
                                     bonus_points=700, is_active=True, display_order=1))
    db.session.commit()
    items = get_page(app, "/loyalty-guide", lang).select(".lg-streak-list li")
    named, plain = items
    assert flat(named.select_one("strong").get_text()) == "Order streak"     # the fixture's rule: a name of its own
    assert plain.select_one("strong") is None
    assert flat(plain.get_text(" ")) == line


def test_the_guide_closes_with_one_quiet_panel_and_the_site_title_separator(app, db, copy_rows, program):
    """Review C-23: the footer's bot band closes the page; the guide's own close is a tinted panel with the shop
    action, and the title ends with the site's one separator, " — " (review SITE-14), never " - " or " · "."""
    soup = get_page(app, "/loyalty-guide", "ru")
    close = soup.select_one(".lg-close")
    assert close is not None and soup.select_one("main .ae-band") is None
    assert [a["href"].split("?")[0].rstrip("/").rsplit("/", 1)[-1] for a in close.select("a")] == ["shop"]
    title = soup.title.get_text()
    assert " · " not in title and " - " not in title and " — " in title


# --- The seed ----------------------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_every_guide_row_names_the_same_placeholders_in_each_language_and_uses_uzbek_typography():
    """The backend seed is the guide rows' one source (the content seed no longer rewrites them): every language fills
    the same numbers from the page, no row glues a "→" formula, and Uzbek writes ‘ and ’ (never ʻ, ʼ or ')."""
    rows = {key: row for key, row in LOYALTY_GUIDE_TRANSLATIONS.items() if key.startswith("loyalty_guide.")}
    assert rows
    for key, row in rows.items():
        values = {lang: row.get(lang) or row["en"] for lang in LANGS}
        assert len({frozenset(PLACEHOLDER.findall(value)) for value in values.values()}) == 1, key
        assert not any("→" in value for value in values.values()), key
        assert not re.search("[ʻʼ']", values["uz"]), key


@pytest.mark.unit
def test_the_birthday_line_claims_no_tier_bonus():
    """grant_birthday_bonuses awards the program's one flat birthday_bonus: no "more at higher tiers"."""
    row = LOYALTY_GUIDE_TRANSLATIONS["loyalty_guide.earn.birthday_desc"]
    assert row["en"] == "Celebrate with {pts} bonus AquaCoins on your birthday."
    assert all("{pts}" in row[lang] and "daraja" not in row[lang] and "уровн" not in row[lang] for lang in LANGS)


@pytest.mark.unit
def test_the_units_new_keys_ship_in_three_languages():
    keys = ("web.content.blog.article", "web.content.blog.related", "web.content.lg.delivery_title",
            "web.content.lg.delivery_text", "web.content.cta.title", "web.content.cta.text", "web.content.cta.shop")
    for key in keys:
        assert all(CONTENT[lang].get(key) for lang in LANGS), key
