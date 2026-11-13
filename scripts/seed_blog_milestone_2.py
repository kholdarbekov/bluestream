#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Seed Milestone 2 blog cluster: 8 cornerstone posts in ru / uz / en.

This script is part of the AI Search & SEO Visibility plan
(see ``/Users/umar/.claude/plans/we-want-to-improve-wild-patterson.md``,
Milestone 2 — Comparison, buying-guide & local-intent content cluster).

Each article lives in its own module under ``scripts/blog_content/<slug>.py``
exporting a single ``ARTICLE`` dict (schema documented in
``scripts/blog_content/__init__.py``). This module:

1. Imports every article module that exists on disk (missing modules are
   skipped with a warning so partial batches still seed).
2. Validates each article's ``ARTICLE`` dict against the M2 quality bar
   (word counts, HTML well-formed-ness, mandatory internal links, FAQPage
   JSON-LD presence).
3. Writes the five text fields (``title``, ``excerpt``, ``content``,
   ``meta_title``, ``meta_description``) in uz, ru and en through
   ``set_translations``; the uz values also go to the columns (the page reads
   the uz translation row before the column, and the admin editor writes both).
4. A LIVE article is updated: only those text fields are written. Its
   category, tags, featured image, image alt text, featured flag and sort
   order stay as they are, so what the owner set in Admin, Blog survives.
   An article that is not live (no row, or not published) is left alone
   unless ``BLOG_SEED_ALLOW_CREATE=1``: then a missing one is created with
   every field from its module and published now, and an unpublished one
   gets its text and is published again (an earlier ``published_at`` is kept).

Run (``scripts/`` is neither in the image nor mounted, so copy it in; never
pipe it over stdin)::

    $COMPOSE exec -T business_app rm -rf /tmp/scripts /tmp/blog_baseline
    $COMPOSE cp scripts business_app:/tmp/scripts
    $COMPOSE exec -T -e PYTHONPATH=/app -e BLOG_SEED_ONLY=<slugs> business_app python /tmp/scripts/seed_blog_milestone_2.py

``BLOG_SEED_ONLY`` is required: a comma-separated list of slugs, or ``all``
for the whole ``ARTICLE_SLUGS`` list. Unset, empty or blank, the run is
refused (usage, exit code 2) and nothing is written, so a misspelt or empty
variable never reaches every article. ``BLOG_SEED_ALLOW_CREATE=1`` lets the
run create or re-publish an article that is not live; without it such an
article is left alone. The first seeding of an empty database needs
``BLOG_SEED_ONLY=all BLOG_SEED_ALLOW_CREATE=1``.

Idempotent — safe to re-run; a change in an article module's text
overwrites the text of a LIVE article (so editing a module + re-running is
the publish workflow). Compare first with ``scripts/compare_blog_live_vs_modules.py``.

The script never touches Translation rows for fields that aren't in
``BlogPost._translatable_fields``; we only seed ``title``, ``excerpt``,
``content``, ``meta_title``, ``meta_description``.
"""
from __future__ import annotations

import importlib
import os
import re
import sys
from datetime import datetime, UTC
from pathlib import Path
from collections.abc import Mapping, Sequence
from typing import Any

# Add project root to import path so ``business_app`` resolves when run
# directly (matches the pattern in scripts/seed_data.py).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from business_app import create_app, db  # noqa: E402
from business_app.models.blog import BlogPost, BlogCategory, BlogStatus  # noqa: E402
from business_app.services.public_urls_service import public_blog_paths  # noqa: E402


# Article module slugs in publish order. The list is the source of truth —
# missing modules are reported but do not abort the run, so a partial M2
# rollout (e.g. 4 articles ready, 4 still drafting) still seeds cleanly.
ARTICLE_SLUGS: list[str] = [
    "luchshaya-dostavka-vody-tashkent-2026",
    # "aqua-element-vs-hayot-vs-nestle-pure-life",
    "19l-ili-10l-kakuyu-butyl-vybrat",
    "voda-dlya-ofisa-tashkent",
    "voda-dlya-detey-i-beremennykh",
    "mineralizatsiya-vody-zachem-i-skolko",
    "artezianskaya-voda-otlichiya",
    "podpiska-na-vodu-tashkent",
]

DEFAULT_LANGUAGE = "uz"
NON_DEFAULT_LANGUAGES = ("ru", "en")
ALL_LANGUAGES = (DEFAULT_LANGUAGE, *NON_DEFAULT_LANGUAGES)

# Soft validation thresholds — log warnings, don't abort. M2 plan target is
# 1500+ words per language; we don't want a 1450-word article to block the
# whole batch.
MIN_BODY_WORDS = 1200  # warn-floor; plan target is 1500+
MAX_META_TITLE = 100
MAX_META_DESCRIPTION = 160
MAX_EXCERPT = 280

REQUIRED_INTERNAL_LINK_TARGETS = [
    "/process/11-step-filtration",
]
SECONDARY_INTERNAL_LINK_TARGETS = [
    "/shop",
    "/subscriptions",
    "/water-delivery-faq",
]


def _slug_to_module(slug: str) -> str:
    """Module slugs use hyphens on disk for filename readability; Python
    module names must be valid identifiers, so we swap ``-`` for ``_`` for
    the import path only."""
    return f"scripts.blog_content.{slug.replace('-', '_')}"


def _word_count(html: str) -> int:
    """Crude word count — strips HTML tags and JSON-LD scripts before
    counting. Good enough to catch articles that are obviously short."""
    no_scripts = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    plain = re.sub(r"<[^>]+>", " ", no_scripts)
    plain = re.sub(r"\s+", " ", plain).strip()
    return len(plain.split()) if plain else 0


def _has_faq_jsonld(html: str) -> bool:
    return bool(re.search(r'"@type"\s*:\s*"FAQPage"', html))


def _validate_article(article: dict[str, Any], slug: str) -> list[str]:
    """Return a list of warnings (empty == clean). Hard errors raise."""
    warnings: list[str] = []

    # Hard requirements — raise so a malformed module doesn't silently seed.
    for required in ("slug", "category", "translations", "featured_image"):
        if required not in article:
            raise ValueError(f"[{slug}] ARTICLE dict missing required key: {required!r}")

    if article["slug"] != slug:
        raise ValueError(f"[{slug}] module slug mismatch: ARTICLE['slug'] = {article['slug']!r}")

    valid_categories = {c.value for c in BlogCategory}
    if article["category"] not in valid_categories:
        raise ValueError(
            f"[{slug}] invalid category {article['category']!r}; expected one of {sorted(valid_categories)}"
        )

    translations = article["translations"]
    for lang in ALL_LANGUAGES:
        if lang not in translations:
            raise ValueError(f"[{slug}] missing translations[{lang!r}]")
        for field in ("title", "excerpt", "content", "meta_title", "meta_description"):
            if not translations[lang].get(field):
                raise ValueError(f"[{slug}] empty translations[{lang!r}][{field!r}]")

    # Soft warnings — just log so partial-quality batches still seed.
    for lang in ALL_LANGUAGES:
        t = translations[lang]
        wc = _word_count(t["content"])
        if wc < MIN_BODY_WORDS:
            warnings.append(f"[{slug}] {lang}: body is {wc} words (target 1500+)")
        if not _has_faq_jsonld(t["content"]):
            warnings.append(f"[{slug}] {lang}: content does not embed FAQPage JSON-LD")
        if len(t["meta_title"]) > MAX_META_TITLE:
            warnings.append(f"[{slug}] {lang}: meta_title is {len(t['meta_title'])} chars (max {MAX_META_TITLE})")
        if len(t["meta_description"]) > MAX_META_DESCRIPTION:
            warnings.append(
                f"[{slug}] {lang}: meta_description is {len(t['meta_description'])} chars (max {MAX_META_DESCRIPTION})"
            )
        if len(t["excerpt"]) > MAX_EXCERPT:
            warnings.append(f"[{slug}] {lang}: excerpt is {len(t['excerpt'])} chars (max {MAX_EXCERPT})")

    # Mandatory pillar-page link in at least one language's body.
    bodies_concat = " ".join(translations[lang]["content"] for lang in ALL_LANGUAGES)
    for required_link in REQUIRED_INTERNAL_LINK_TARGETS:
        if required_link not in bodies_concat:
            warnings.append(f"[{slug}] missing internal link to {required_link} (required by M2 plan)")
    if not any(link in bodies_concat for link in SECONDARY_INTERNAL_LINK_TARGETS):
        warnings.append(
            f"[{slug}] missing internal link to any of {SECONDARY_INTERNAL_LINK_TARGETS} (at least one required)"
        )

    return warnings


def _upsert_post(article: dict[str, Any]) -> tuple[BlogPost, bool]:
    """Upsert a BlogPost by slug. Returns (post, created)."""
    slug = article["slug"]
    post = BlogPost.query.filter_by(slug=slug).first()
    created = False

    default_t = article["translations"][DEFAULT_LANGUAGE]

    if post is None:
        post = BlogPost(
            slug=slug,
            title=default_t["title"],
            excerpt=default_t["excerpt"],
            content=default_t["content"],
            meta_title=default_t["meta_title"],
            meta_description=default_t["meta_description"],
            category=BlogCategory(article["category"]),
            tags=article.get("tags", "") or None,
            featured_image=article.get("featured_image"),
            image_alt_text=article.get("image_alt_text"),
            is_featured=bool(article.get("is_featured", False)),
            sort_order=int(article.get("sort_order", 0)),
            status=BlogStatus.PUBLISHED,
            published_at=datetime.now(UTC),
        )
        db.session.add(post)
        db.session.flush()  # need post.id before set_translations
        created = True
    else:
        # Update default-language column values (uz lives on the columns). Only the text: category, tags,
        # featured image, image alt text, featured flag and sort order are the admin editor's once the row exists.
        post.title = default_t["title"]
        post.excerpt = default_t["excerpt"]
        post.content = default_t["content"]
        post.meta_title = default_t["meta_title"]
        post.meta_description = default_t["meta_description"]
        # Promote to PUBLISHED if it was previously DRAFT/ARCHIVED. Preserve
        # the original published_at so listings keep their stable order.
        if post.status != BlogStatus.PUBLISHED:
            post.status = BlogStatus.PUBLISHED
        if post.published_at is None:
            post.published_at = datetime.now(UTC)

    # All languages go through set_translations (uz is on the columns as well).
    # set_translations expects {field_name: {language: value}} — see
    # Translation.bulk_set_entity_translations — so we transpose the article's
    # natural {language: {field: value}} authoring shape here.
    # The uz row is written too: the page reads a uz translation row before the
    # column, and the admin editor writes that row on every save.
    translations: dict[str, dict[str, str]] = {}
    for field in ("title", "excerpt", "content", "meta_title", "meta_description"):
        translations[field] = {lang: article["translations"][lang][field] for lang in ALL_LANGUAGES}
    post.set_translations(translations)

    return post, created


SEED_ALL = "all"
USAGE = (
    "usage: BLOG_SEED_ONLY=<slug>[,<slug>...] | BLOG_SEED_ONLY=all  [BLOG_SEED_ALLOW_CREATE=1]  "
    "python seed_blog_milestone_2.py\n"
    "BLOG_SEED_ONLY is required; the whole list is only ever BLOG_SEED_ONLY=all. Nothing was written."
)


def _run_options(environ: Mapping[str, str]) -> tuple[list[str] | None, bool] | None:
    """(slugs to seed or None for the whole list, whether a missing/unpublished article may be created).

    The whole list is only ``BLOG_SEED_ONLY=all``. None when ``BLOG_SEED_ONLY`` names nothing (unset, empty or
    blank): the run is refused, so a misspelt or empty variable never seeds every article."""
    raw = environ.get("BLOG_SEED_ONLY", "").strip()
    allow_create = environ.get("BLOG_SEED_ALLOW_CREATE") == "1"
    if raw == SEED_ALL:
        return None, allow_create
    only = [part.strip() for part in raw.split(",") if part.strip()]
    return (only, allow_create) if only else None


def seed_milestone_2_blog(only: Sequence[str] | None = None, allow_create: bool = False) -> dict[str, int]:
    """Main entry. Returns counters for created/updated/skipped/warnings.

    An article that is not live (no row, or not published) is written only with ``allow_create``.
    """
    print("\n=== Milestone 2 — blog cluster seeding ===\n")

    counters = {
        "created": 0,
        "updated": 0,
        "skipped_missing_module": 0,
        "skipped_not_live": 0,
        "warnings": 0,
    }
    pending_warnings: list[str] = []

    slugs = list(only) if only is not None else ARTICLE_SLUGS
    if only is not None:
        content_dir = Path(__file__).resolve().parent / "blog_content"
        unknown = [slug for slug in slugs if not (content_dir / f"{slug.replace('-', '_')}.py").is_file()]
        if unknown:
            raise ValueError(f"no article module for: {', '.join(unknown)}")
    live_slugs = {post.slug for _path, post in public_blog_paths()}

    for slug in slugs:
        if slug not in live_slugs and not allow_create:
            state = "not published" if BlogPost.query.filter_by(slug=slug).first() else "absent"
            print(f"  ⏭  {slug}: {state} — left alone (set BLOG_SEED_ALLOW_CREATE=1 to create it)")
            counters["skipped_not_live"] += 1
            continue
        module_path = _slug_to_module(slug)
        try:
            module = importlib.import_module(module_path)
        except ModuleNotFoundError:
            print(f"  ⏭  {slug}: module not yet drafted ({module_path}.py) — skipped")
            counters["skipped_missing_module"] += 1
            continue
        # Allow re-importing if the module changed since process start
        importlib.reload(module)

        article = getattr(module, "ARTICLE", None)
        if article is None:
            print(f"  ✗  {slug}: module {module_path} has no ARTICLE dict — skipped")
            counters["skipped_missing_module"] += 1
            continue

        warnings = _validate_article(article, slug)
        if warnings:
            for w in warnings:
                pending_warnings.append(w)
                counters["warnings"] += 1

        try:
            post, created = _upsert_post(article)
            if created:
                counters["created"] += 1
                action = "✚ created"
            else:
                counters["updated"] += 1
                action = "↻ updated"
            db.session.commit()
            print(f"  {action}  {slug}  (id={post.id}, category={post.category.value})")
        except Exception as exc:
            db.session.rollback()
            print(f"  ✗  {slug}: upsert failed — {exc}")

    if pending_warnings:
        print("\nWarnings (non-fatal):")
        for w in pending_warnings:
            print(f"  ⚠  {w}")

    print(
        "\nSummary: "
        f"{counters['created']} created, "
        f"{counters['updated']} updated, "
        f"{counters['skipped_missing_module']} skipped (missing module), "
        f"{counters['skipped_not_live']} skipped (not live), "
        f"{counters['warnings']} warnings"
    )
    return counters


def main() -> int:
    options = _run_options(os.environ)
    if options is None:
        print(USAGE, file=sys.stderr)
        return 2
    only, allow_create = options
    print(f"Seeding: {', '.join(only) if only is not None else 'the whole list'}; allow create: {allow_create}")
    app = create_app()
    with app.app_context():
        try:
            seed_milestone_2_blog(only=only, allow_create=allow_create)
        except Exception as exc:
            db.session.rollback()
            print(f"\nFATAL: {exc}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
