"""How many xdist workers the backend test runner starts.

Too many workers for the Docker VM's free memory get OOM-killed by the VM's
kernel mid-run ("[gwN] node down: Not properly terminated"): on 2026-11-23 every
full run lost three workers because the old rule assumed the rest of the VM
needed only a fixed 2.5 GB. The count is decided in one place,
scripts/backend-test-workers.sh, from the VM's CPUs, its total memory and the
memory it has free when the run starts; the runner only asks it. The same file
decides whether a targeted run's own pytest arguments already choose a worker
count (then the runner adds none).
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SIZER = REPO_ROOT / "scripts" / "backend-test-workers.sh"
RUNNER = REPO_ROOT / "scripts" / "precommit-backend-tests.sh"
TARGETED_RUNNER = REPO_ROOT / "scripts" / "run-backend-tests.sh"


def _sizer(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SIZER), *args], capture_output=True, text=True, timeout=10, check=False
    )


@pytest.mark.parametrize(
    "cpus, total_mib, free_mib, expected",
    [
        # The VM on 2026-11-23: 9.9 GB, ~5.7 GB free at idle. Memory decides.
        ("8", "9937", "5826", "4 memory-free"),
        # The same VM just after a Docker Desktop restart: still memory-bound, one more worker.
        ("8", "9937", "6758", "5 memory-free"),
        # Enough free for what the total allows: the total-based count stands.
        ("8", "9937", "9000", "7 memory-total"),
        # A 4-CPU VM with plenty of memory: the CPUs decide.
        ("4", "16384", "7936", "4 cpu"),
        # A big host: capped at 12 (conftest maps gwN to Redis DB 15-N; DBs 0-2 are the live stack).
        ("16", "32768", "30000", "12 cap"),
        # Almost nothing free: never below one worker.
        ("8", "9937", "1500", "1 memory-free"),
        # Free memory unreadable: fall back to the total-based count.
        ("8", "9937", "", "7 memory-total"),
        ("8", "9937", "n/a", "7 memory-total"),
    ],
)
def test_the_worker_count_fits_the_vm(cpus, total_mib, free_mib, expected):
    result = _sizer(cpus, total_mib, free_mib)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


@pytest.mark.parametrize(
    "requested, expected",
    [
        ("6", "6 requested"),  # PYTEST_WORKERS wins over memory: the caller asked for it
        ("0", "0 requested"),  # a serial run (`-n 0`), as PYTEST_WORKERS=0 always meant
        ("08", "8 requested"),  # a leading zero is still the number eight
        ("20", "12 cap"),  # ...but never past the Redis DB mapping's 12
    ],
)
def test_an_explicit_worker_count_is_honoured_up_to_the_redis_cap(requested, expected):
    result = _sizer("8", "9937", "5826", requested)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


@pytest.mark.parametrize(
    "bad",
    [
        ("x", "9937", "5826"),
        ("8", "", "5826"),
        ("8",),
        ("8", "9937", "5826", "auto"),  # PYTEST_WORKERS=auto would be memory-blind: refused, not ignored
        ("8", "9937", "5826", "-1"),
    ],
)
def test_unusable_figures_are_refused_loudly(bad):
    result = _sizer(*bad)

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.strip()


@pytest.mark.parametrize(
    "pytest_args, caller_sets_workers",
    [
        (["tests/unit/test_x.py"], False),
        (["tests/unit/test_x.py::test_a[-n]"], False),  # a node id is not an option
        (["tests/unit/test_x.py", "-v", "-x"], False),
        (["-k", "not slow", "tests/"], False),
        (["-p", "no:cacheprovider", "tests/"], False),
        (["tests/", "-n", "2"], True),
        (["tests/", "-n4"], True),
        (["tests/", "-nauto"], True),
        (["tests/", "-xn2"], True),  # a short-flag cluster: -x, then -n 2
        (["tests/", "--numprocesses=3"], True),
        (["tests/", "--numprocesses", "3"], True),
        (["tests/", "-p", "no:xdist"], True),
        (["tests/", "-pno:xdist"], True),
        (["tests/", "--pdb"], True),  # pdb needs a serial run; pytest.ini's -n auto turns serial for it
        # xdist 3.6.1 reads only --pdb (usepdb); --trace leaves -n auto at one worker per CPU,
        # so it gets the memory-sized -n like any other run.
        (["tests/", "--trace"], False),
    ],
)
def test_a_targeted_run_that_chooses_its_own_workers_is_left_alone(pytest_args, caller_sets_workers):
    result = _sizer("--caller-sets-workers", *pytest_args)

    assert result.returncode == (0 if caller_sets_workers else 1), result.stderr
    assert result.stdout == ""


def test_the_runner_asks_the_sizer_instead_of_re_deriving_the_rule():
    script = RUNNER.read_text(encoding="utf-8")

    assert re.search(r'bash "\$\{sizer\}" "\$\{docker_cpus\}" "\$\{docker_mem_mib\}" "\$\{vm_free_mib\}" "\$\{PYTEST_WORKERS:-\}"', script)
    assert re.search(r'bash "\$\{sizer\}" --caller-sets-workers "\$@"', script)
    # The reserve, the per-worker figure and the flag patterns live only in the sizer.
    assert not re.search(r"\b2560\b", script)
    assert not re.search(r"workers\s*=\s*\$\(\(", script)
    assert "numprocesses" not in script


def test_an_args_run_gets_the_sized_worker_count_unless_the_caller_sets_one():
    """pytest.ini's addopts carries `-n auto` (one worker per CPU), so a targeted run that passes
    no -n of its own must get the memory-sized `-n` too, or `tests/integration/` OOMs the VM."""
    script = RUNNER.read_text(encoding="utf-8")

    assert re.search(r'pytest_args="\$\{quoted\}--no-cov --no-header -n \$\{workers\}"', script)


def test_the_targeted_runner_is_the_same_runner():
    """scripts/run-backend-tests.sh had drifted into a second, weaker copy of the runner: no
    memory sizing (-n auto), the live Celery broker, the reachable bot webhook URLs and no
    one-run-at-a-time wait. It now hands its arguments to the one runner."""
    script = TARGETED_RUNNER.read_text(encoding="utf-8")

    assert re.search(r'exec bash "\$\{repo_root\}/scripts/precommit-backend-tests\.sh" "\$@"', script)
    assert "docker run" not in script
