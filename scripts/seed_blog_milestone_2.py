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
3. Upserts a ``BlogPost`` row by slug, writing the default-language (``uz``)
   values directly to the columns and the ``ru`` / ``en`` values through
   ``set_translations`` (matches how product/category seeding works
   elsewhere in the codebase — see ``business_app/models/translatable.py``).
4. Marks the post ``PUBLISHED`` with ``published_at = now`` (or preserves
   an earlier ``published_at`` if the row already existed) so blog_list and
   the sitemap pick it up immediately.

Run inside the business_app container::

    docker compose exec business_app python scripts/seed_blog_milestone_2.py

Idempotent — safe to re-run; any change in the article module overwrites
the DB content (so editing a module + re-running is the publish workflow).

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
from typing import Any

# Add project root to import path so ``business_app`` resolves when run
# directly (matches the pattern in scripts/seed_data.py).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from business_app import create_app, db  # noqa: E402
from business_app.models.blog import BlogPost, BlogCategory, BlogStatus  # noqa: E402


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
        # Update default-language column values (uz lives on the columns).
        post.title = default_t["title"]
        post.excerpt = default_t["excerpt"]
        post.content = default_t["content"]
        post.meta_title = default_t["meta_title"]
        post.meta_description = default_t["meta_description"]
        post.category = BlogCategory(article["category"])
        post.tags = article.get("tags", "") or None
        post.featured_image = article.get("featured_image")
        post.image_alt_text = article.get("image_alt_text")
        post.is_featured = bool(article.get("is_featured", False))
        post.sort_order = int(article.get("sort_order", 0))
        # Promote to PUBLISHED if it was previously DRAFT/ARCHIVED. Preserve
        # the original published_at so listings keep their stable order.
        if post.status != BlogStatus.PUBLISHED:
            post.status = BlogStatus.PUBLISHED
        if post.published_at is None:
            post.published_at = datetime.now(UTC)

    # Non-default languages go through set_translations (uz already on cols).
    non_default_translations: dict[str, dict[str, str]] = {}
    for lang in NON_DEFAULT_LANGUAGES:
        non_default_translations[lang] = {
            field: article["translations"][lang][field]
            for field in ("title", "excerpt", "content", "meta_title", "meta_description")
        }
    post.set_translations(non_default_translations)

    return post, created


def seed_milestone_2_blog() -> dict[str, int]:
    """Main entry. Returns counters for created/updated/skipped/warnings."""
    print("\n=== Milestone 2 — blog cluster seeding ===\n")

    counters = {"created": 0, "updated": 0, "skipped_missing_module": 0, "warnings": 0}
    pending_warnings: list[str] = []

    for slug in ARTICLE_SLUGS:
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
        f"{counters['warnings']} warnings"
    )
    return counters


def main() -> int:
    app = create_app()
    with app.app_context():
        try:
            seed_milestone_2_blog()
        except Exception as exc:
            db.session.rollback()
            print(f"\nFATAL: {exc}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
