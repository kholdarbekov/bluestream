"""Alembic's ``fileConfig`` must not silence the application's loggers.

``business_app/migrations/env.py`` configures Python logging from
``alembic.ini``. ``logging.config.fileConfig`` defaults to
``disable_existing_loggers=True``, which sets ``disabled = True`` on every logger
NOT named in the ini file. ``alembic.ini`` names only
``root,sqlalchemy,alembic,flask_migrate``, so the default silences every
``business_app.*``, ``bot`` and ``handlers.*`` logger already created in the
process.

Why that is worse than a level or a handler being wrong: ``Logger.handle()`` and
``Logger.isEnabledFor()`` both short-circuit on ``.disabled`` BEFORE levels,
filters, handlers and propagation are consulted. A silenced logger therefore
cannot be rescued by attaching a handler, forcing ``propagate = True``, or
lowering the level — the record is dropped at the source. That is why the
symptom is an EMPTY capture rather than a wrong one.

In production this is latent: migrations run in their own one-shot ``migrate``
container (docker-compose.yml:91-99) and no migration under
``business_app/migrations/versions/`` logs anything today. It stops being latent
the moment a data-backfill migration logs, or calls a service that logs — that
output would go nowhere, in the one container whose failure blocks the web tier.

In the test suite it was never latent. Any in-process ``flask_migrate.upgrade()``
silenced the application loggers for the rest of that xdist worker, which is what
made 25 logging assertions across 9 files fail in a full run while passing when
their own file ran alone. One test had already been patched around this
individually (tests/integration/test_place_i18n_render_e2e.py) before the cause
was fixed at the source.

The behavioural checks run in SUBPROCESSES on purpose: proving the hazard
requires calling ``fileConfig`` with the disabling default, and doing that
in-process would inflict the very contamination this file exists to prevent.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_PY = REPO_ROOT / "business_app" / "migrations" / "env.py"
ALEMBIC_INI = REPO_ROOT / "business_app" / "migrations" / "alembic.ini"

# Sampled across the three families the alembic.ini keys do not cover: the Flask
# app logger, a nested service logger, and a bot logger. All three were observed
# silenced in the full-suite run that motivated this file.
SAMPLE_LOGGERS = ("business_app", "business_app.utils.distance_matrix", "handlers.base")


def _fileconfig_call():
    """The ``fileConfig(...)`` call node as env.py actually ships it."""
    tree = ast.parse(ENV_PY.read_text(), filename=str(ENV_PY))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            getattr(node.func, "id", None) == "fileConfig"
            or getattr(node.func, "attr", None) == "fileConfig"
        )
    ]
    assert len(calls) == 1, f"expected exactly one fileConfig call in {ENV_PY}, found {len(calls)}"
    return calls[0]


def _probe(disable_existing_loggers: str) -> dict:
    """Report each sample logger's ``disabled`` flag after fileConfig, in a fresh
    interpreter so the call cannot leak into this worker."""
    script = f"""
import json, logging
from logging.config import fileConfig

names = {list(SAMPLE_LOGGERS)!r}
for n in names:
    logging.getLogger(n)
fileConfig({str(ALEMBIC_INI)!r}, disable_existing_loggers={disable_existing_loggers})
print(json.dumps({{n: logging.getLogger(n).disabled for n in names}}))
"""
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, f"probe failed:\n{out.stdout}\n{out.stderr}"
    import json

    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.unit
def test_env_py_disables_the_disable_existing_loggers_default():
    """The shipped call must pass ``disable_existing_loggers=False`` explicitly.

    Pinned on the source rather than on behaviour because env.py needs a live
    alembic context to import; this asserts the production line itself, so the
    guard cannot be removed without turning this red.
    """
    call = _fileconfig_call()
    keywords = {kw.arg: kw.value for kw in call.keywords}

    assert "disable_existing_loggers" in keywords, (
        "business_app/migrations/env.py calls fileConfig() without "
        "disable_existing_loggers=False, so it silences every application logger "
        "in the process — see this module's docstring."
    )
    value = keywords["disable_existing_loggers"]
    assert isinstance(value, ast.Constant) and value.value is False, (
        "disable_existing_loggers must be the literal False, got "
        f"{ast.dump(value)}"
    )


@pytest.mark.unit
def test_fileconfig_with_the_guard_leaves_application_loggers_enabled():
    """The guard actually does the job, against the real shipped alembic.ini."""
    disabled = _probe("False")
    still_silenced = [name for name, is_disabled in disabled.items() if is_disabled]
    assert not still_silenced, (
        f"disable_existing_loggers=False should leave these enabled: {still_silenced}"
    )


@pytest.mark.unit
def test_the_default_would_silence_them_so_this_pin_is_not_vacuous():
    """Control: without the guard the hazard is real.

    Without this, the test above would still pass if alembic.ini ever grew
    entries for these loggers, or if fileConfig stopped disabling anything —
    and the pin would be quietly asserting nothing.
    """
    disabled = _probe("True")
    survivors = [name for name, is_disabled in disabled.items() if not is_disabled]
    assert not survivors, (
        "expected the fileConfig default to silence every application logger not "
        f"named in alembic.ini, but these survived: {survivors}. If alembic.ini "
        "now names them, update SAMPLE_LOGGERS to loggers it does not cover."
    )
