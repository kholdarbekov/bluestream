"""The blog seeder (`BLOG_SEED_ONLY` names what runs, no silent create, an existing article gets its text only, the
uz translation row) and the read-only compare tool."""

import importlib
import shutil
from pathlib import Path

import pytest

import scripts.seed_blog_milestone_2 as seeder
from business_app.models.blog import BlogCategory, BlogPost, BlogStatus
from business_app.models.translation import Translation
from business_app.services.public_urls_service import public_blog_paths
from scripts.compare_blog_live_vs_modules import compare_blog, report
from scripts.seed_blog_milestone_2 import ARTICLE_SLUGS, _run_options, seed_milestone_2_blog
from tests.integration.public_page_builders import flat, get_page

BLOG_DIR = Path(__file__).resolve().parents[2] / "scripts" / "blog_content"
OFFICE, GUIDE = "voda-dlya-ofisa-tashkent", "luchshaya-dostavka-vody-tashkent-2026"
COMPARISON = "aqua-element-vs-hayot-vs-nestle-pure-life"  # commented out of ARTICLE_SLUGS; its module exists


def _article(slug):
    return importlib.import_module(f"scripts.blog_content.{slug.replace('-', '_')}").ARTICLE


def _row(slug):
    return BlogPost.query.filter_by(slug=slug).first()


def _baseline(tmp_path, *slugs) -> Path:
    for slug in slugs:
        shutil.copy(BLOG_DIR / f"{slug.replace('-', '_')}.py", tmp_path)
    return tmp_path


def _save_in_admin_editor(post, language, **fields):
    """What a save in the admin blog editor stores (business_app/api/admin.py:11849-11910): a translation row
    per language, and for uz the column as well."""
    from business_app import db

    for field, value in fields.items():
        if language == "uz":
            setattr(post, field, value)
    post.set_translations({field: {language: value} for field, value in fields.items()})
    db.session.commit()


def _set_columns_in_admin_editor(post, **columns):
    """What a save in the admin blog editor stores for the columns besides the text (business_app/api/admin.py:
    11821-11836): each value as sent, the category as its enum. The PUT route itself cannot be driven here: on the
    tests' SQLite it answers 500 after its commit, because `BlogPost.is_published` compares the naive datetime SQLite
    hands back with an aware now."""
    from business_app import db

    for field, value in columns.items():
        setattr(post, field, BlogCategory(value) if field == "category" else value)
    db.session.commit()


def _admin_columns(post) -> dict:
    """The six columns the admin editor writes besides the text (business_app/api/admin.py:11821-11836)."""
    return {
        "category": post.category.value,
        "tags": post.tags,
        "featured_image": post.featured_image,
        "image_alt_text": post.image_alt_text,
        "is_featured": post.is_featured,
        "sort_order": post.sort_order,
    }


@pytest.fixture
def run_main(app, monkeypatch):
    """`main()` as the runbook runs it, on the test database: only the environment differs between runs."""
    monkeypatch.setattr(seeder, "create_app", lambda: app)
    for name in ("BLOG_SEED_ONLY", "BLOG_SEED_ALLOW_CREATE"):
        monkeypatch.delenv(name, raising=False)

    def run(environ: dict[str, str]) -> int:
        for name, value in environ.items():
            monkeypatch.setenv(name, value)
        return seeder.main()

    return run


# ---- the seeder
def test_the_run_options_come_from_the_two_variables():
    assert _run_options({"BLOG_SEED_ONLY": f" {OFFICE} , {GUIDE},", "BLOG_SEED_ALLOW_CREATE": "1"}) == (
        [OFFICE, GUIDE],
        True,
    )
    assert _run_options({"BLOG_SEED_ONLY": " all ", "BLOG_SEED_ALLOW_CREATE": "true"}) == (None, False)


@pytest.mark.parametrize(
    "environ",
    [{}, {"BLOG_SEED_ONLY": ""}, {"BLOG_SEED_ONLY": " , "}, {"BLOG_SEED_OLNY": OFFICE}],
    ids=["unset", "empty", "blank", "misspelt"],
)
def test_a_run_that_names_nothing_is_refused_and_writes_nothing(app, db, run_main, capsys, environ):
    seed_milestone_2_blog(only=[OFFICE], allow_create=True)
    _row(OFFICE).excerpt = "stale"
    db.session.commit()
    capsys.readouterr()

    assert run_main({"BLOG_SEED_ALLOW_CREATE": "1", **environ}) == 2

    assert "BLOG_SEED_ONLY=all" in capsys.readouterr().err
    assert BlogPost.query.count() == 1 and _row(OFFICE).excerpt == "stale"
    assert _run_options(environ) is None


def test_all_seeds_the_whole_list(app, db, run_main):
    assert run_main({"BLOG_SEED_ONLY": "all", "BLOG_SEED_ALLOW_CREATE": "1"}) == 0
    assert {post.slug for _path, post in public_blog_paths()} == set(ARTICLE_SLUGS)


def test_without_the_flag_nothing_is_created(app, db):
    counters = seed_milestone_2_blog()
    assert BlogPost.query.count() == 0
    assert (counters["created"], counters["updated"], counters["skipped_not_live"]) == (0, 0, len(ARTICLE_SLUGS))


def test_with_the_flag_the_list_is_created_and_published(app, db):
    assert seed_milestone_2_blog(allow_create=True)["created"] == len(ARTICLE_SLUGS) == 7
    assert {post.slug for _path, post in public_blog_paths()} == set(ARTICLE_SLUGS)
    assert _row(COMPARISON) is None


def test_only_limits_the_run_to_the_named_slugs(app, db):
    seed_milestone_2_blog(allow_create=True)
    for slug in (OFFICE, GUIDE):
        _row(slug).excerpt = "stale"
    db.session.commit()
    counters = seed_milestone_2_blog(only=[OFFICE])
    assert (counters["updated"], counters["created"]) == (1, 0)
    assert _row(OFFICE).excerpt == _article(OFFICE)["translations"]["uz"]["excerpt"] and _row(GUIDE).excerpt == "stale"


def test_the_comparison_article_can_be_named_but_is_never_created_without_the_flag(app, db):
    assert seed_milestone_2_blog(only=[COMPARISON])["skipped_not_live"] == 1 and _row(COMPARISON) is None
    assert seed_milestone_2_blog(only=[COMPARISON], allow_create=True)["created"] == 1
    assert seed_milestone_2_blog(only=[COMPARISON])["updated"] == 1


def test_an_unpublished_article_is_left_exactly_as_it_is(app, db):
    seed_milestone_2_blog(only=[OFFICE], allow_create=True)
    post = _row(OFFICE)
    post.unpublish()
    post.excerpt = "owner draft"
    db.session.commit()
    assert seed_milestone_2_blog(only=[OFFICE])["skipped_not_live"] == 1
    assert (_row(OFFICE).status, _row(OFFICE).excerpt) == (BlogStatus.DRAFT, "owner draft")


def test_an_unknown_slug_stops_the_run_before_anything_is_written(app, db):
    seed_milestone_2_blog(only=[OFFICE], allow_create=True)
    _row(OFFICE).excerpt = "stale"
    db.session.commit()
    with pytest.raises(ValueError, match="no-such-article"):
        seed_milestone_2_blog(only=[OFFICE, "no-such-article"])
    assert _row(OFFICE).excerpt == "stale"


def test_a_reseed_reaches_the_uzbek_page_of_an_article_saved_in_the_admin_editor(app, db):
    seed_milestone_2_blog(only=[OFFICE], allow_create=True)
    _save_in_admin_editor(_row(OFFICE), "uz", title="Admin sarlavhasi", content="<p>Admin matni</p>")
    assert "Admin matni" in get_page(app, f"/blog/{OFFICE}", "uz").select_one(".blog-content").get_text()
    seed_milestone_2_blog(only=[OFFICE])
    soup = get_page(app, f"/blog/{OFFICE}", "uz")
    assert flat(soup.select_one("main h1").get_text()) == flat(_article(OFFICE)["translations"]["uz"]["title"])
    assert "Admin matni" not in soup.select_one(".blog-content").get_text()


def test_a_picture_and_tags_changed_in_admin_survive_a_seed(app, db):
    """A new article gets every column from its module; an existing one gets its text only, so what the owner set in
    Admin, Blog (picture, alt text, tags, category, featured flag, order) stays."""
    article = _article(OFFICE)
    seed_milestone_2_blog(only=[OFFICE], allow_create=True)
    post = _row(OFFICE)
    assert _admin_columns(post) == {
        "category": article["category"],
        "tags": article["tags"],
        "featured_image": article["featured_image"],
        "image_alt_text": article["image_alt_text"],
        "is_featured": article["is_featured"],
        "sort_order": article["sort_order"],
    }
    owner = {
        "category": next(c.value for c in BlogCategory if c.value != article["category"]),
        "tags": "owner,tags",
        "featured_image": "/static/images/owner-picture.jpg",
        "image_alt_text": "Owner picture",
        "is_featured": not article["is_featured"],
        "sort_order": article["sort_order"] + 7,
    }
    _set_columns_in_admin_editor(post, **owner)
    _save_in_admin_editor(post, "uz", title="Admin sarlavhasi")

    assert seed_milestone_2_blog(only=[OFFICE])["updated"] == 1

    assert _admin_columns(_row(OFFICE)) == owner
    soup = get_page(app, f"/blog/{OFFICE}", "uz")
    assert flat(soup.select_one("main h1").get_text()) == flat(article["translations"]["uz"]["title"])
    picture = soup.select_one(".blog-details-content figure.image-box img")
    assert (picture["src"], picture["alt"]) == (owner["featured_image"], owner["image_alt_text"])


# ---- the comparison
def test_an_untouched_article_is_same_in_three_languages(app, db, tmp_path):
    seed_milestone_2_blog(only=[OFFICE], allow_create=True)
    assert compare_blog(_baseline(tmp_path, OFFICE)) == {OFFICE: {"status": "same", "differs": {}}}


def test_an_edit_in_the_admin_editor_is_reported_by_language_and_field(app, db, tmp_path):
    seed_milestone_2_blog(only=[OFFICE], allow_create=True)
    _save_in_admin_editor(_row(OFFICE), "ru", content="<p>Правка владельца</p>", meta_description="Правка")
    _save_in_admin_editor(_row(OFFICE), "uz", title="Admin sarlavhasi")
    assert compare_blog(_baseline(tmp_path, OFFICE)) == {
        OFFICE: {"status": "differs", "differs": {"uz": ["title"], "ru": ["meta_description", "content"]}}
    }


def test_line_endings_and_indentation_are_not_a_difference(app, db, tmp_path):
    seed_milestone_2_blog(only=[OFFICE], allow_create=True)
    post = _row(OFFICE)
    _save_in_admin_editor(post, "en", content=post.get_translated("content", "en").replace("\n", "\r\n  "))
    assert compare_blog(_baseline(tmp_path, OFFICE))[OFFICE]["status"] == "same"


def test_absent_and_unpublished_articles_are_reported_and_not_compared(app, db, tmp_path):
    seed_milestone_2_blog(only=[OFFICE], allow_create=True)
    _row(OFFICE).unpublish()
    db.session.commit()
    result = compare_blog(_baseline(tmp_path, OFFICE, COMPARISON))
    assert {slug: row["status"] for slug, row in result.items()} == {OFFICE: "not_live", COMPARISON: "absent"}


def test_the_comparison_writes_nothing(app, db, tmp_path):
    seed_milestone_2_blog(only=[OFFICE], allow_create=True)
    state = lambda: (BlogPost.query.count(), Translation.query.count(), _row(OFFICE).view_count, _row(OFFICE).updated_at)
    before = state()
    compare_blog(_baseline(tmp_path, OFFICE, COMPARISON))
    assert not db.session.new and not db.session.dirty and not db.session.deleted
    db.session.rollback()
    assert state() == before


def test_the_report_says_what_to_seed_and_what_to_edit_by_hand():
    lines, code = report(
        {
            "a-slug": {"status": "same", "differs": {}},
            "b-slug": {"status": "differs", "differs": {"ru": ["meta_description", "content"]}},
            "c-slug": {"status": "absent", "differs": {}},
            "d-slug": {"status": "not_live", "differs": {}},
        }
    )
    assert code == 1
    assert lines == [
        "a-slug  uz  same",
        "a-slug  ru  same",
        "a-slug  en  same",
        "b-slug  uz  same",
        "b-slug  ru  differs  meta_description, content",
        "b-slug  en  same",
        "c-slug  -  absent",
        "d-slug  -  not live",
        "SEED (BLOG_SEED_ONLY): a-slug",
        "BY HAND (Admin, Blog): b-slug",
        "ABSENT (left alone): c-slug",
        "NOT LIVE (left alone): d-slug",
    ]
    lines, code = report({"a-slug": {"status": "same", "differs": {}}, "e-slug": {"status": "same", "differs": {}}})
    assert code == 0 and lines[-4:] == [
        "SEED (BLOG_SEED_ONLY): a-slug,e-slug",
        "BY HAND (Admin, Blog): -",
        "ABSENT (left alone): -",
        "NOT LIVE (left alone): -",
    ]


def test_a_directory_with_no_module_is_refused(app, tmp_path):
    with pytest.raises(ValueError):
        compare_blog(tmp_path)
