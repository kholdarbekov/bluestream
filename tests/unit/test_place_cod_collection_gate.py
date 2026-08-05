"""The Plan E gate.

Plan E is the first plan permitted to change COD collection behaviour. The gate
exists so that behaviour ships dark: with it off, every Plan E branch is
short-circuited and the money suite provably cannot have moved. See the plan's C0.
"""

import importlib

import pytest


@pytest.fixture(autouse=True)
def _restore_business_config():
    """Undo the process-wide damage `importlib.reload` does.

    `monkeypatch` restores `os.environ` at teardown, but nothing puts the MODULE
    back: `importlib.reload` rebinds `PLACE_COD_COLLECTION_ENABLED` on the live
    module object, so without this fixture the last-executed row's value leaks
    into every later test in the same xdist worker. C0 requires the bots to read
    the gate as a module attribute, so the leak is not theoretical — under a
    gate-OFF run it silently turns a run labelled OFF into a partly-ON one and
    destroys the evidence Task 8 measures. It also breaks
    `test_flask_config_mirrors_the_shared_literal`, whose session-scoped `app`
    was bound from the true configured value.

    Teardown reloads (so every other constant is recomputed from the env
    `monkeypatch` has by then restored) and then re-pins the gate attribute to
    the value captured before this test ran — the re-pin is what makes
    correctness independent of fixture teardown ORDER rather than dependent on
    autouse-before-`monkeypatch` sequencing.
    """
    import shared.business_config as bc

    original = bc.PLACE_COD_COLLECTION_ENABLED
    yield
    importlib.reload(bc)
    bc.PLACE_COD_COLLECTION_ENABLED = original


@pytest.mark.unit
def test_gate_defaults_to_on(monkeypatch):
    """Owner ruling A2, 2026-08-04: the gate ships ON.

    This is deliberately the opposite of the usual fail-closed convention, and
    the assertion exists so that a later "safety" edit flipping the literal back
    to False is caught immediately rather than silently disabling the owner's
    model in production. Q2 is closed — see C0.
    """
    monkeypatch.delenv("PLACE_COD_COLLECTION_ENABLED", raising=False)
    import shared.business_config as bc

    reloaded = importlib.reload(bc)
    assert reloaded.PLACE_COD_COLLECTION_ENABLED is True


@pytest.mark.unit
def test_gate_can_be_turned_off_by_env(monkeypatch):
    """Rollback path: setting the variable to a falsy spelling must restore
    Plan D behaviour without a code change (Task 8's runbook depends on it)."""
    monkeypatch.setenv("PLACE_COD_COLLECTION_ENABLED", "false")
    import shared.business_config as bc

    reloaded = importlib.reload(bc)
    assert reloaded.PLACE_COD_COLLECTION_ENABLED is False


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("true", True), ("True", True), ("1", True), ("yes", True), ("on", True),
        ("false", False), ("0", False), ("nonsense", False), ("  ", False),
        # ⚠️ EMPTY IS NOT FALSE. `_bool` treats unset/empty as "no opinion" and
        # returns the DEFAULT — which is now True (A2). A whitespace-only value
        # is an opinion and parses to False. These two rows differ on purpose;
        # do not "fix" them to match.
        ("", True),
    ],
)
def test_gate_parses_env_var(monkeypatch, raw, expected):
    monkeypatch.setenv("PLACE_COD_COLLECTION_ENABLED", raw)
    import shared.business_config as bc

    reloaded = importlib.reload(bc)
    assert reloaded.PLACE_COD_COLLECTION_ENABLED is expected


@pytest.mark.unit
def test_flask_config_mirrors_the_shared_literal(app):
    """base.py derives from shared.business_config — the single-default rule
    that module's own docstring states. No second literal anywhere."""
    from shared import business_config

    assert (
        app.config["PLACE_COD_COLLECTION_ENABLED"]
        is business_config.PLACE_COD_COLLECTION_ENABLED
    )
