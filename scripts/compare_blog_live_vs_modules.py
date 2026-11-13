#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""READ-ONLY comparison of the live blog against a baseline set of article modules.

It writes nothing: it never adds, flushes or commits, and rolls back at the end.

The baseline directory is another version of ``scripts/blog_content``, for example::

    git archive <commit> scripts/blog_content | tar -x -C /tmp/blog_baseline_src

For every baseline article it compares what the page prints (``get_translated``) in uz, ru and en,
for title, excerpt, meta_title, meta_description and content, ignoring whitespace. Statuses:

- ``same``     - live and equal to the baseline: safe to seed. Its slug is listed under ``SEED``.
- ``differs``  - live, but edited since (in the admin editor): never seed it; edit by hand
                 (Admin, Blog, Edit). Listed under ``BY HAND``.
- ``absent``   - no ``blog_posts`` row: left alone.
- ``not_live`` - a row that is not published (or dated in the future): left alone.

``SEED`` is the value for ``BLOG_SEED_ONLY`` of ``scripts/seed_blog_milestone_2.py``.
Exit code: 0 when no article differs, 1 when one does, 2 on a bad baseline directory.

Run (``scripts/`` is not in the image; copy it in)::

    $COMPOSE exec -T business_app rm -rf /tmp/scripts /tmp/blog_baseline
    $COMPOSE cp scripts business_app:/tmp/scripts
    $COMPOSE cp <baseline dir> business_app:/tmp/blog_baseline
    $COMPOSE exec -T -e PYTHONPATH=/app business_app python /tmp/scripts/compare_blog_live_vs_modules.py --baseline /tmp/blog_baseline
"""

from __future__ import annotations

import argparse
import os
import runpy
import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from business_app import create_app, db  # noqa: E402
from business_app.models.blog import BlogPost  # noqa: E402
from business_app.services.public_urls_service import public_blog_paths  # noqa: E402

LANGUAGES = ("uz", "ru", "en")
FIELDS = ("title", "excerpt", "meta_title", "meta_description", "content")


def load_baseline(baseline_dir) -> dict[str, dict]:
    """slug -> ARTICLE for every module file of the directory (loaded by path, never imported)."""
    articles: dict[str, dict] = {}
    for path in sorted(Path(baseline_dir).glob("*.py")):
        if path.name == "__init__.py":
            continue
        article = runpy.run_path(str(path)).get("ARTICLE")
        if article is not None:
            articles[article["slug"]] = article
    if not articles:
        raise ValueError(f"no article module found in {baseline_dir}")
    return articles


def _same(a: str | None, b: str | None) -> bool:
    return " ".join((a or "").split()) == " ".join((b or "").split())


def compare_blog(baseline_dir) -> dict[str, dict]:
    articles = load_baseline(baseline_dir)
    live = {post.slug: post for _path, post in public_blog_paths()}
    result: dict[str, dict] = {}
    for slug in sorted(articles):
        if slug in live:
            post = live[slug]
        elif BlogPost.query.filter_by(slug=slug).first() is None:
            result[slug] = {"status": "absent", "differs": {}}
            continue
        else:
            result[slug] = {"status": "not_live", "differs": {}}
            continue
        differs: dict[str, list[str]] = {}
        for lang in LANGUAGES:
            baseline = articles[slug]["translations"][lang]
            fields = [f for f in FIELDS if not _same(post.get_translated(f, lang), baseline.get(f))]
            if fields:
                differs[lang] = fields
        result[slug] = {"status": "differs" if differs else "same", "differs": differs}
    return result


def report(result: dict[str, dict]) -> tuple[list[str], int]:
    lines: list[str] = []
    for slug, row in result.items():
        if row["status"] == "absent":
            lines.append(f"{slug}  -  absent")
        elif row["status"] == "not_live":
            lines.append(f"{slug}  -  not live")
        else:
            for lang in LANGUAGES:
                fields = row["differs"].get(lang)
                lines.append(f"{slug}  {lang}  differs  {', '.join(fields)}" if fields else f"{slug}  {lang}  same")

    def slugs(status: str) -> str:
        return ",".join(sorted(s for s, r in result.items() if r["status"] == status)) or "-"

    lines += [
        f"SEED (BLOG_SEED_ONLY): {slugs('same')}",
        f"BY HAND (Admin, Blog): {slugs('differs')}",
        f"ABSENT (left alone): {slugs('absent')}",
        f"NOT LIVE (left alone): {slugs('not_live')}",
    ]
    return lines, 1 if any(r["status"] == "differs" for r in result.values()) else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare the live blog with a baseline of article modules (read-only).")
    parser.add_argument("--baseline", required=True, help="directory of baseline article modules")
    args = parser.parse_args(argv)
    app = create_app()
    with app.app_context():
        try:
            lines, code = report(compare_blog(args.baseline))
        except ValueError as exc:
            print(exc)
            return 2
        finally:
            db.session.rollback()
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
