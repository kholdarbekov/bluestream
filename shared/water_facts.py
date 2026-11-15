"""The bottled water's published facts: the 11 treatment stages and the mineral profile of the finished water.

One source for the machine-readable surfaces: /api/public/products.json (`sourceWater.treatmentStages`,
`mineralProfile`) and /llms.txt. The pages (/process/11-step-filtration, /about/sources, /quality-standards) print the
same facts in three languages from the `web.info.filtration.step_N.name` and `web.info.mineral.row_N.*` rows of
scripts/seed_web_ui_info_translations.py; tests/integration/test_water_facts_single_source.py holds the seed's English
rows, the rendered page, the feed and llms.txt to these values, so a change here or there fails until both agree.
"""

from typing import NamedTuple, Optional, Union

Number = Union[int, float]

# The artesian well's depth (approximately; the feed's `sourceWater.wellDepthMetres` and llms.txt both print it).
WELL_DEPTH_METRES = 120


class TreatmentStage(NamedTuple):
    name: str  # as the filtration page names it (its English row)
    summary: str  # the one line llms.txt gives it


class MineralRow(NamedTuple):
    field: str  # the feed's key
    label: str  # as the pages print it (the English row)
    low: Number
    high: Optional[Number] = None  # a range ends here; None for a single value (pH)

    @property
    def text(self) -> str:
        """The value as the pages print it: "30–50 mg/L", or "7.5"."""
        return f"{self.low}" if self.high is None else f"{self.low}–{self.high} mg/L"

    @property
    def feed_value(self):
        """The value as the feed publishes it: {"min": 30, "max": 50}, or 7.5."""
        return self.low if self.high is None else {"min": self.low, "max": self.high}


TREATMENT_STAGES = (
    TreatmentStage(
        "Quartz filter",
        "removes coarse suspended particles (sand, rust, mud, visible impurities) from the raw water; reduces "
        "turbidity and protects downstream stages from clogging.",
    ),
    TreatmentStage(
        "Activated carbon filter",
        "adsorbs chlorine, off-odours, off-tastes, and many organic compounds; also protects sensitive equipment such "
        "as the reverse-osmosis membrane.",
    ),
    TreatmentStage(
        "Ion exchanger / softener",
        "reduces water hardness by removing calcium and magnesium ions that cause scale formation; protects pipes, "
        "filters, pumps, and the RO membrane from mineral build-up.",
    ),
    TreatmentStage(
        "Regeneration block",
        "works alongside the ion exchanger by restoring the softener resin once it becomes saturated, keeping "
        "softening performance stable over time.",
    ),
    TreatmentStage(
        "Polypropylene sediment filter — 5-micron cartridge",
        "captures fine particles that pass through the earlier stages (small sediment, rust, mechanical impurities) "
        "before the water reaches finer filters.",
    ),
    TreatmentStage(
        "Polypropylene sediment filter — 1-micron cartridge",
        "provides a finer mechanical-filtration step; removes very small particles and prepares the water for reverse "
        "osmosis.",
    ),
    TreatmentStage(
        "Reverse osmosis",
        "water passes through a semi-permeable membrane that reduces dissolved salts, heavy metals, microorganisms, "
        "and many other unwanted impurities. This is the principal purification stage.",
    ),
    TreatmentStage(
        "Membrane CIP — Clean-in-Place system",
        "cleans and maintains the RO membrane, removing scale, mineral deposits, and organic build-up to keep "
        "efficiency high and extend service life.",
    ),
    TreatmentStage(
        "UV filter",
        "exposes the water to ultraviolet light, inactivating bacteria, viruses, and other microorganisms without "
        "adding any chemicals.",
    ),
    TreatmentStage(
        "Mineralisation",
        "re-introduces beneficial minerals (calcium and magnesium) after RO; improves taste and helps balance the pH.",
    ),
    TreatmentStage(
        "Ozone-generator treatment",
        "final disinfection step before storage and bottling; suppresses microbial growth then naturally breaks down "
        "to oxygen.",
    ),
)

MINERAL_PROFILE = (
    MineralRow("tds_mg_per_l", "Total dissolved solids (TDS)", 30, 50),
    MineralRow("ph", "pH", 7.5),
    MineralRow("calcium_mg_per_l", "Calcium (Ca²⁺)", 10, 60),
    MineralRow("magnesium_mg_per_l", "Magnesium (Mg²⁺)", 7, 20),
    MineralRow("sodium_mg_per_l", "Sodium (Na⁺)", 5, 15),
    MineralRow("potassium_mg_per_l", "Potassium (K⁺)", 1, 4),
    MineralRow("bicarbonate_mg_per_l", "Bicarbonate (HCO₃⁻)", 50, 120),
    MineralRow("sulfate_mg_per_l", "Sulfate (SO₄²⁻)", 10, 30),
    MineralRow("chloride_mg_per_l", "Chloride (Cl⁻)", 5, 10),
)


def mineral_profile_feed() -> dict:
    """The feed's `mineralProfile`: {"tds_mg_per_l": {"min": 30, "max": 50}, "ph": 7.5, …}."""
    return {row.field: row.feed_value for row in MINERAL_PROFILE}
