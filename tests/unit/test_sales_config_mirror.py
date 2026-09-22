"""Phase 2a's sales tunables live once — in shared/business_config.py.

`shared/business_config.py`'s own docstring states the single-default rule:
each value's default literal lives THERE exactly once,
`business_app/config/base.py` DERIVES its Flask key from the module rather
than re-declaring the literal, and no call site adds a
`.get(KEY, literal)` fallback. Services then read `current_app.config[...]`.

Nothing in the repo currently enforces that rule for a NEW constant — there is
no "every business_config name is mirrored" test anywhere (verified 2026-09-08).
This file is that guard for the ten constants Phase 2a adds, the five Phase 2b
adds and the two Phase 3 adds (plans
`docs/superpowers/plans/2026-09-08-sales-agent-phase2a-visit-loop.md`, "Shared
interfaces → Constants"; spec section "Constants — shared/business_config.py").

It pins four separable facts, because each fails on its own:

1.  the constant EXISTS in `shared.business_config` with the spec default AND
    the spec TYPE. The type half is not pedantry, but the damage lands on the
    first deployed OVERRIDE, not on the default: `_int` and `_float` both return
    an unset default untouched, so `_int(name, 1.5)` would still answer 1.5 today.
    Read through `_int`, `SALES_SUGGEST_SAFETY_FACTOR=1.5` in the environment
    raises `ValueError: invalid literal for int()` at import, and only whole
    numbers load — a deployed `1` cuts every suggested quantity by a third. Fact
    4b below is the assertion that catches exactly that helper swap.
2.  `app.config[NAME]` carries the SAME value. Without the mirror line,
    `current_app.config["SALES_GEOFENCE_RADIUS_M"]` is a `KeyError` at the very
    first visit check-in, and the tempting `.get(NAME, 250)` "fix" plants
    exactly the second literal the module rule forbids.
3.  `config/base.py` derives each key BY NAME (`NAME = business_config.NAME`)
    rather than restating the number — the textual half of (2), which two
    coincidentally-equal literals would otherwise hide until someone changes
    one of them.
4.  the env-override path works for one int and one float, because `_int` /
    `_float` are what make these deployable knobs instead of constants living
    in a different file. `.env` defines none of these keys, so the defaults
    above are what production runs today.

Reload discipline mirrors `tests/unit/test_place_suggestion_radius_config.py:105-119`
and `tests/unit/test_place_cod_collection_gate.py:13-38`: `monkeypatch` restores
`os.environ`, but nothing puts the MODULE back, and the session-scoped `app`
fixture was bound from the true configured values.
"""

import importlib
from pathlib import Path

import pytest

from shared import business_config

# name -> (default literal, exact python type). DATA transcribed from the plan's
# "Constants (Task 2)" block — not a re-implementation of any production rule.
SALES_PHASE_2A_CONSTANTS = {
    "SALES_GEOFENCE_RADIUS_M": (250, int),
    "SALES_SUGGEST_SAFETY_FACTOR": (1.5, float),
    "SALES_DELIVERY_LEAD_DAYS": (1, int),
    "SALES_CADENCE_DAYS_A": (7, int),
    "SALES_CADENCE_DAYS_B": (14, int),
    "SALES_CADENCE_DAYS_C": (30, int),
    "SALES_VISIT_AUTO_ABANDON_HOURS": (6, int),
    "SALES_CONFIRMATION_TTL_HOURS": (3, int),
    "SALES_STOCK_QTY_MAX": (500, int),
    "SALES_RATE_HISTORY_DAYS": (60, int),
}

# Phase 2b (nightly stage job, morning digest, Nearby). DATA transcribed from
# the plan's ruling R12 and spec D23 — not a re-implementation of any rule.
# SALES_AT_RISK_RATIO is a FLOAT for the same reason SALES_SUGGEST_SAFETY_FACTOR
# is, and the damage arrives with the first deployed OVERRIDE rather than with
# the default: read through `_int`, `SALES_AT_RISK_RATIO=1.5` in the environment
# raises `ValueError: invalid literal for int()` at import, so only whole
# multiples load and every at-risk threshold moves by a third of a cycle.
SALES_PHASE_2B_CONSTANTS = {
    "SALES_AT_RISK_RATIO": (1.5, float),
    "SALES_DORMANT_DAYS": (45, int),
    "SALES_UNVISITED_ALERT_DAYS": (21, int),
    "SALES_DIGEST_LOCAL_TIME": ("08:30", str),
    "SALES_NEARBY_LIMIT": (20, int),
}

# Phase 3 (KPIs, the exceptions feed, the metrics windows). DATA transcribed from
# the plan's rulings R18/R12/R6 — not a re-implementation of any rule.
# SALES_SHORT_VISIT_SECONDS is the "was the agent really in the shop" threshold the
# exceptions feed measures `ended_at - checkin_at` against; SALES_METRICS_MAX_RANGE_DAYS
# is the ceiling every dated read route refuses past, so a mistyped year is a 400
# rather than a full-table scan behind an admin's spinner.
SALES_PHASE_3_CONSTANTS = {
    "SALES_SHORT_VISIT_SECONDS": (60, int),
    "SALES_METRICS_MAX_RANGE_DAYS": (92, int),
}

SALES_CONSTANTS = {**SALES_PHASE_2A_CONSTANTS, **SALES_PHASE_2B_CONSTANTS, **SALES_PHASE_3_CONSTANTS}

CONSTANT_NAMES = sorted(SALES_CONSTANTS)

# tests/unit/<this file> -> parents[2] is the repo root.
BASE_CONFIG_PATH = Path(__file__).resolve().parents[2] / "business_app" / "config" / "base.py"


@pytest.fixture
def restore_business_config():
    """Undo the process-wide damage `importlib.reload` does.

    `importlib.reload` rebinds the constants on the LIVE module object, which
    every bot imports by name, so a leaked override would outlive this test in
    the same xdist worker and break
    `test_flask_config_mirrors_the_shared_constant`, whose session-scoped `app`
    was created from the true configured values. Teardown reloads (recomputing
    every other constant from the environment `monkeypatch` has restored by
    then) and then re-pins the captured values, so correctness does not depend
    on fixture teardown ORDER.
    """
    originals = {
        name: getattr(business_config, name) for name in CONSTANT_NAMES if hasattr(business_config, name)
    }
    yield
    importlib.reload(business_config)
    for name, value in originals.items():
        setattr(business_config, name, value)


@pytest.mark.unit
@pytest.mark.parametrize("name", CONSTANT_NAMES)
def test_business_config_defines_the_constant_with_its_spec_default(monkeypatch, restore_business_config, name):
    """Fact 1: the literal exists here, once, with the spec's value and type."""
    default, expected_type = SALES_CONSTANTS[name]
    monkeypatch.delenv(name, raising=False)

    reloaded = importlib.reload(business_config)

    assert hasattr(reloaded, name), f"{name} is missing from shared/business_config.py"
    value = getattr(reloaded, name)
    assert value == default, f"{name} default is {value!r}, spec says {default!r}"
    assert type(value) is expected_type, (
        f"{name} must be read through _{expected_type.__name__}(...) — got {type(value).__name__}"
    )


@pytest.mark.unit
@pytest.mark.parametrize("name", CONSTANT_NAMES)
def test_flask_config_mirrors_the_shared_constant(app, name):
    """Fact 2: services read current_app.config[...] and get the same number."""
    assert name in app.config, f"business_app/config/base.py never mirrors {name}"
    assert app.config[name] == getattr(business_config, name)


@pytest.mark.unit
def test_base_config_derives_each_constant_instead_of_restating_the_literal():
    """Fact 3: the mirror is a derivation by name, never a second literal."""
    source = BASE_CONFIG_PATH.read_text(encoding="utf-8")

    missing = [name for name in CONSTANT_NAMES if f"{name} = business_config.{name}" not in source]

    assert missing == [], f"config/base.py must derive these from shared.business_config: {missing}"


@pytest.mark.unit
def test_geofence_radius_is_env_overridable(monkeypatch, restore_business_config):
    """Fact 4a — the int knob. A wider geofence must not need a code change."""
    monkeypatch.setenv("SALES_GEOFENCE_RADIUS_M", "400")

    reloaded = importlib.reload(business_config)

    assert reloaded.SALES_GEOFENCE_RADIUS_M == 400
    assert type(reloaded.SALES_GEOFENCE_RADIUS_M) is int


@pytest.mark.unit
def test_suggest_safety_factor_is_env_overridable(monkeypatch, restore_business_config):
    """Fact 4b — the float knob. `2.25` is deliberately non-integral: read
    through `_int` this line raises `ValueError: invalid literal for int()`,
    which is the failure that catches a helper swap the default-value test
    above cannot see (an unset var returns the declared default untouched)."""
    monkeypatch.setenv("SALES_SUGGEST_SAFETY_FACTOR", "2.25")

    reloaded = importlib.reload(business_config)

    assert reloaded.SALES_SUGGEST_SAFETY_FACTOR == 2.25
    assert type(reloaded.SALES_SUGGEST_SAFETY_FACTOR) is float


@pytest.mark.unit
def test_digest_local_time_is_env_overridable(monkeypatch, restore_business_config):
    """Fact 4c — the STRING knob, and the only one whose reader is new.

    `_str` strips, because a compose `.env` line carries whatever whitespace it
    was typed with and `"08:30 "` is not a time `crontab(hour=..., minute=...)`
    can be built from: Task 3 splits this value on ":" at import, so a stray
    space reaches beat as `int(" 30")`-shaped breakage at worker start, not at
    08:30.
    """
    monkeypatch.setenv("SALES_DIGEST_LOCAL_TIME", " 07:15 ")

    reloaded = importlib.reload(business_config)

    assert reloaded.SALES_DIGEST_LOCAL_TIME == "07:15"
    assert type(reloaded.SALES_DIGEST_LOCAL_TIME) is str


@pytest.mark.unit
def test_an_empty_digest_time_is_no_opinion_not_an_empty_string(monkeypatch, restore_business_config):
    """`SALES_DIGEST_LOCAL_TIME=` in a compose file must mean the default.

    `_int` and `_float` already treat unset AND empty as "no opinion"; a `_str`
    that returned `""` here would hand Task 3 an unparseable digest time from a
    variable an operator merely left blank.
    """
    monkeypatch.setenv("SALES_DIGEST_LOCAL_TIME", "   ")

    reloaded = importlib.reload(business_config)

    assert reloaded.SALES_DIGEST_LOCAL_TIME == "08:30"
