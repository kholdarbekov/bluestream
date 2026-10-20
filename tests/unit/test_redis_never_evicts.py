"""Redis never evicts a key on its own (owner decision R9d, 2026-10-20).

The session cut-offs (`tokens_valid_after:*`), the token blacklist and Celery's queued messages live
in Redis. Under `allkeys-lru` a full Redis silently drops any of them: a signed-out device is signed
in again, a queued task never runs. Under `noeviction` a full Redis refuses writes instead, and the
caller sees the error. Production runs docker-compose.yml + docker-compose.production.yml
(scripts/deploy.sh), so the production command is the one that decides; the other compose files and
redis/redis.conf (docker-compose.secrets.yml) say the same, so no environment behaves differently.
"""

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]


def _redis_command(compose_file):
    command = yaml.safe_load((REPO / compose_file).read_text())["services"]["redis"]["command"]
    return command if isinstance(command, str) else " ".join(command)


@pytest.mark.parametrize(
    "compose_file", ["docker-compose.production.yml", "docker-compose.yml", "docker-compose.local.yml"]
)
def test_redis_runs_with_noeviction(compose_file):
    assert re.findall(r"--maxmemory-policy\s+(\S+)", _redis_command(compose_file)) == ["noeviction"]


def _megabytes(value):
    number, unit = re.fullmatch(r"(\d+)\s*([a-zA-Z]+)", value.strip()).groups()
    return int(number) * {"mb": 1, "m": 1, "gb": 1024, "g": 1024}[unit.lower()]


@pytest.mark.parametrize("compose_file", ["docker-compose.production.yml", "docker-compose.yml"])
def test_redis_maxmemory_sits_below_its_container_limit(compose_file):
    """`noeviction` only holds if Redis reaches `maxmemory` before the kernel's memory limit kills it.

    Production capped the container at 512M while `maxmemory` said 768mb, so Redis would have been
    OOM-killed (losing up to a second of writes, a fresh cut-off included) before noeviction ever
    refused a write. Keep at least 64 MB of room for Redis's own overhead (final review O-1, 2026-10-20).
    """
    service = yaml.safe_load((REPO / compose_file).read_text())["services"]["redis"]
    limit = (((service.get("deploy") or {}).get("resources") or {}).get("limits") or {}).get("memory")
    if limit is None:
        pytest.skip(f"{compose_file} sets no container memory limit for redis")
    (maxmemory,) = re.findall(r"--maxmemory\s+(\S+)", _redis_command(compose_file))
    assert _megabytes(maxmemory) + 64 <= _megabytes(limit)


def test_redis_conf_never_evicts():
    settings = [line.split() for line in (REPO / "redis/redis.conf").read_text().splitlines()]
    assert [words[1] for words in settings if words[:1] == ["maxmemory-policy"]] == ["noeviction"]
