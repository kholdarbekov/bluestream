# -*- coding: utf-8 -*-
"""Milestone 2 blog content modules.

Each ``<slug>.py`` module exports a single top-level ``ARTICLE`` dict consumed
by ``scripts/seed_blog_milestone_2.py``. Keeping one article per file lets us
regenerate / re-translate individual posts without touching the orchestrator
and keeps reviewable diffs small.

Schema each module must export
------------------------------
ARTICLE = {
    "slug":            str,                              # unique slug (used by /blog/<slug>)
    "category":        str,                              # one of BlogCategory enum *values*
                                                          #   ("health_tips", "water_benefits",
                                                          #    "company_news", "quality_assurance",
                                                          #    "lifestyle", "environment")
    "tags":            str,                              # comma-separated tag list
    "featured_image":  str,                              # absolute or app-relative URL
    "image_alt_text":  str,
    "is_featured":     bool,
    "sort_order":      int,                              # higher = appears earlier in lists
    "translations": {                                    # default language is "uz" — its values
                                                         # are stored on the BlogPost columns
                                                         # directly; ru/en go through
                                                         # set_translations().
        "uz": {"title": str, "excerpt": str, "content": str,
               "meta_title": str, "meta_description": str},
        "ru": {...same keys...},
        "en": {...same keys...},
    },
}

Constraints (enforced by the seed script's content-validation step):
    * content >= 1500 words per language
    * content is well-formed HTML (h2/h3, p, ul/ol, table) and may include a
      trailing ``<script type="application/ld+json">`` FAQPage block
    * meta_title <= 100 chars; meta_description <= 160 chars
    * excerpt <= 280 chars (used by /blog list cards + og:description fallback)
    * Every article must internal-link to /process/11-step-filtration and at
      least one of /shop, /subscriptions, /water-delivery-faq.
"""
