"""Every UPDATE_* fixture-regeneration flag the tests read must reach the container.

`docker run` inherits nothing from the calling shell, so a flag the tests read
but the runner does not forward (`-e NAME="${NAME:-}"`) is silently unset and
the fixture is never regenerated through scripts/precommit-backend-tests.sh.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER = REPO_ROOT / "scripts" / "precommit-backend-tests.sh"
TESTS_DIR = REPO_ROOT / "tests"
THIS_FILE = Path(__file__).resolve()

# An environment READ of an UPDATE_* name: os.environ.get("X"), os.environ["X"],
# os.getenv("X"), in either quote style. Bare constants such as UPDATE_SECONDS
# are not environment reads and do not match.
_ENV_READ = re.compile(
    r"""(?:os\.environ\.get\(|os\.getenv\(|os\.environ\[)\s*['"](UPDATE_[A-Z0-9_]+)['"]"""
)


def _update_flags_the_tests_read() -> set[str]:
    names: set[str] = set()
    for path in TESTS_DIR.rglob("*.py"):
        if path.resolve() == THIS_FILE:
            continue
        names.update(_ENV_READ.findall(path.read_text(encoding="utf-8")))
    return names


def _update_flags_the_runner_forwards() -> set[str]:
    script = RUNNER.read_text(encoding="utf-8")
    return set(re.findall(r'-e\s+(UPDATE_[A-Z0-9_]+)="\$\{\1:-\}"', script))


def test_every_update_flag_the_tests_read_is_forwarded():
    read = _update_flags_the_tests_read()
    forwarded = _update_flags_the_runner_forwards()

    assert read, "no UPDATE_* environment reads found; the scan itself is broken"
    missing = sorted(read - forwarded)
    assert not missing, (
        "scripts/precommit-backend-tests.sh does not forward these UPDATE_* flags "
        f"into the test container (add `-e NAME=\"${{NAME:-}}\"` for each): {missing}"
    )
