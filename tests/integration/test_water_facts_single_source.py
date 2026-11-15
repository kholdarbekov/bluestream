"""One source for the water's published facts: the 11 treatment stages and the mineral profile.

The pages (/process/11-step-filtration, /about/sources) print them from the info seed's `web.info.filtration.step_N.name`
and `web.info.mineral.row_N.*` rows; the machine-readable surfaces (/api/public/products.json and /llms.txt) read
shared/water_facts.py. Before 2026-11-14 the feed and llms.txt each typed their own copy, and the three had drifted
apart ("Polypropylene 5-micron sediment filter" in the feed, "…filter, 5-micron cartridge" in llms.txt, "…filter —
5-micron cartridge" on the page; "Membrane CIP (Clean-in-Place)" against the page's "Membrane CIP — Clean-in-Place
system"; "TDS (total dissolved solids)" against "Total dissolved solids (TDS)"). These tests drive the real endpoints a
visitor, a crawler and an assistant fetch, with the rows the seeds ship, and hold all four to one list.
"""

import re

import pytest

from business_app.models.translation import Translation
from scripts.seed_web_ui_info_translations import CATEGORY, TRANSLATIONS as INFO
from scripts.seed_web_ui_shell_translations import TRANSLATIONS as SHELL
from shared.water_facts import MINERAL_PROFILE, TREATMENT_STAGES, WELL_DEPTH_METRES, mineral_profile_feed
from tests.integration.public_page_builders import (  # noqa: F401 -- `offer_skus` is a fixture
    flat,
    get_page,
    offer_skus,
    seed_copy,
)

pytestmark = pytest.mark.integration

STAGES = [stage.name for stage in TREATMENT_STAGES]
MINERALS = [(row.label, row.text) for row in MINERAL_PROFILE]


@pytest.fixture
def page_copy(app, db):
    """The storefront rows the backend seed ships, the shell's rows and the info group's rows."""
    seed_copy(db)
    present = set(db.session.query(Translation.key, Translation.language).all())
    for rows_by_lang in (SHELL, INFO):
        for language, rows in rows_by_lang.items():
            for key, value in rows.items():
                if (key, language) not in present:
                    db.session.add(Translation(key=key, language=language, value=value, category=CATEGORY, is_active=True))
    db.session.commit()
    app.jinja_env.cache.clear()


@pytest.mark.unit
def test_the_info_seed_ships_the_english_facts_the_feed_publishes():
    en = INFO["en"]
    assert [en[f"web.info.filtration.step_{n}.name"] for n in range(1, len(STAGES) + 1)] == STAGES
    assert [
        (en[f"web.info.mineral.row_{n}.param"], en[f"web.info.mineral.row_{n}.value"])
        for n in range(1, len(MINERALS) + 1)
    ] == MINERALS
    assert f"web.info.filtration.step_{len(STAGES) + 1}.name" not in en
    assert f"web.info.mineral.row_{len(MINERALS) + 1}.param" not in en


def test_the_page_the_feed_and_llms_txt_name_the_same_stages_and_minerals(app, db, offer_skus, page_copy):
    soup = get_page(app, "/process/11-step-filtration", "en")
    page_stages = [flat(h.get_text()) for h in soup.select(".ae-steps__title")]
    page_minerals = [
        (flat(tr.th.get_text()), flat(tr.td.get_text())) for tr in soup.select("table.info-minerals tbody tr")
    ]

    feed = app.test_client().get("/api/public/products.json").get_json()

    llms = app.test_client().get("/llms.txt").get_data(as_text=True)
    llms_stages = re.findall(r"^\d+\. \*\*(.+?)\*\* — ", llms, flags=re.M)
    llms_minerals = re.findall(r"^- \*\*(.+?)\*\*: (.+)$", llms.split("## Mineral profile", 1)[1], flags=re.M)

    assert page_stages == STAGES
    assert feed["sourceWater"]["treatmentStages"] == STAGES
    assert llms_stages == STAGES
    assert f"## Our {len(STAGES)}-stage filtration" in llms

    assert page_minerals == MINERALS
    assert [(label, text) for label, text in llms_minerals[: len(MINERALS)]] == MINERALS
    assert feed["mineralProfile"] == mineral_profile_feed()

    # The well depth is one constant (review SITE-31): the feed and llms.txt (twice) print it, no typed digits.
    assert feed["sourceWater"]["wellDepthMetres"] == WELL_DEPTH_METRES
    assert llms.count(f"approximately {WELL_DEPTH_METRES} metres") == 2
    # No claim nothing backs (owner decision 2): the brand line says "artesian", not "premium".
    assert "premium" not in llms.lower()


@pytest.mark.unit
def test_the_feed_shape_is_unchanged():
    """The feed keeps its JSON shape: a {"min", "max"} range per mineral, pH a single number."""
    assert mineral_profile_feed() == {
        "tds_mg_per_l": {"min": 30, "max": 50},
        "ph": 7.5,
        "calcium_mg_per_l": {"min": 10, "max": 60},
        "magnesium_mg_per_l": {"min": 7, "max": 20},
        "sodium_mg_per_l": {"min": 5, "max": 15},
        "potassium_mg_per_l": {"min": 1, "max": 4},
        "bicarbonate_mg_per_l": {"min": 50, "max": 120},
        "sulfate_mg_per_l": {"min": 10, "max": 30},
        "chloride_mg_per_l": {"min": 5, "max": 10},
    }
