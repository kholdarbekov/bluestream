#!/usr/bin/env bash
# How many xdist workers the backend test runner (scripts/precommit-backend-tests.sh) starts.
# The one place this rule lives; tests/unit/test_test_runner_worker_count.py pins it.
#
#   backend-test-workers.sh CPUS TOTAL_MIB [FREE_MIB [REQUESTED]]
#     CPUS       the Docker VM's CPUs (docker info NCPU)
#     TOTAL_MIB  the Docker VM's memory (docker info MemTotal, in MiB)
#     FREE_MIB   the VM's MemAvailable when the run starts, in MiB; empty or non-numeric = unknown
#     REQUESTED  an explicit count (PYTEST_WORKERS): a whole number, 0 = serial; honoured as
#                given up to the cap, anything else refused
#   Prints "<workers> <limit>", limit = what decided the count:
#     cpu | cap | memory-total | memory-free | requested
#
#   backend-test-workers.sh --caller-sets-workers PYTEST_ARGS...
#     Exits 0 when a targeted run's own pytest arguments choose the worker count (-n in any
#     form, also inside a short-flag cluster such as -xn2; --numprocesses; -p no:xdist) or need
#     a serial run (--pdb: xdist 3.6.1 turns pytest.ini's `-n auto` serial for it and refuses an
#     explicit -n; it does not do that for --trace, so --trace gets the sized -n); 1 otherwise.
#     A false positive (a glued option value that happens to contain an n, e.g. -knot) only
#     leaves pytest.ini's own `-n auto` in charge.
#
# Each worker peaks near 1 GB on a full run (0.85-0.94 GB measured 2026-10-17 and 2026-11-23,
# growing ~50 MB per 1000 tests it runs). Too many for the VM's free memory and the VM's kernel
# OOM-kills them mid-run ("[gwN] node down: Not properly terminated"). FREE_MIB is what is free
# NOW, so it already accounts for the live compose stack and Docker Desktop's own services
# (lifecycle-server held ~2.4 GB after 12 days of VM uptime on 2026-11-23; restarting Docker
# Desktop frees it). TOTAL_MIB minus a fixed reserve only bounds the count from above and is the
# fallback when FREE_MIB is unknown. The margin covers the pytest controller (~0.4 GB by the end
# of a run) and Postgres serving the test databases. The cap of 12: tests/conftest.py maps
# worker gwN to Redis DB 15-N, and DBs 0-2 belong to the live stack.
set -euo pipefail

if [[ "${1:-}" == "--caller-sets-workers" ]]; then
    shift
    previous=""
    for arg in "$@"; do
        case "${arg}" in
            --numprocesses|--numprocesses=*|--pdb) exit 0 ;;
            --*) ;;
            -n*|-[!-]*n*) exit 0 ;;  # -n, -n4, -nauto; a cluster such as -xn2; -pno:xdist
        esac
        if [[ "${previous}" == "-p" && "${arg}" == "no:xdist" ]]; then exit 0; fi
        previous="${arg}"
    done
    exit 1
fi

worker_mib=1024
reserve_mib=2560
free_margin_mib=1024
cap=12

cpus="${1:-}"
total_mib="${2:-}"
free_mib="${3:-}"
requested="${4:-}"

if ! [[ "${cpus}" =~ ^[0-9]+$ && "${total_mib}" =~ ^[0-9]+$ ]]; then
    echo "usage: $0 CPUS TOTAL_MIB [FREE_MIB [REQUESTED]] (CPUS and TOTAL_MIB are whole numbers)" >&2
    exit 2
fi

if [[ -n "${requested}" ]]; then
    if ! [[ "${requested}" =~ ^[0-9]+$ ]]; then
        echo "PYTEST_WORKERS must be a whole number (0 = serial), not '${requested}'." >&2
        exit 2
    fi
    workers=$(( 10#${requested} ))
    if (( workers > cap )); then echo "${cap} cap"; else echo "${workers} requested"; fi
    exit 0
fi

workers=$(( 10#${cpus} ))
limit=cpu
if (( cap < workers )); then workers=${cap}; limit=cap; fi
by_total=$(( (10#${total_mib} - reserve_mib) / worker_mib ))
if (( by_total < workers )); then workers=${by_total}; limit=memory-total; fi
if [[ "${free_mib}" =~ ^[0-9]+$ ]]; then
    by_free=$(( (10#${free_mib} - free_margin_mib) / worker_mib ))
    if (( by_free < workers )); then workers=${by_free}; limit=memory-free; fi
fi
if (( workers < 1 )); then workers=1; fi

echo "${workers} ${limit}"
