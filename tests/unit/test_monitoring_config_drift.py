"""scripts/monitoring-config-drift.sh: which services still run a monitoring config older than the repo's.

Runs the real script against a fake `docker` on PATH, so no container is touched.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "monitoring-config-drift.sh"

FAKE_DOCKER = """#!/usr/bin/env bash
# Test double: `ps` lists $FAKE_DOCKER/ids and records its args; `inspect` prints $FAKE_DOCKER/<id>.
case "$1" in
  ps) echo "$*" > "$FAKE_DOCKER/ps_args"; cat "$FAKE_DOCKER/ids" ;;
  inspect) cat "$FAKE_DOCKER/${@: -1}" ;;
  *) echo "unexpected docker call: $*" >&2; exit 99 ;;
esac
"""


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000000000Z", time.gmtime(epoch))


@pytest.fixture
def stack(tmp_path):
    """A throwaway repo (script copy + monitoring/) and a fake docker daemon."""
    repo = tmp_path / "bluestream"
    (repo / "scripts").mkdir(parents=True)
    (repo / "monitoring").mkdir()
    shutil.copy(SCRIPT, repo / "scripts" / SCRIPT.name)

    fake = tmp_path / "fake"
    (fake / "bin").mkdir(parents=True)
    docker = fake / "bin" / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    containers = {}

    def add_container(cid, service, started_epoch, sources):
        containers[cid] = True
        (fake / cid).write_text(f"{service}|{_iso(started_epoch)}" + "".join(f"|{s}" for s in sources) + "\n")
        (fake / "ids").write_text("\n".join(containers) + "\n")

    def run():
        env = {**os.environ, "PATH": f"{fake / 'bin'}:{os.environ['PATH']}", "FAKE_DOCKER": str(fake)}
        env.pop("COMPOSE_PROJECT_NAME", None)
        result = subprocess.run(
            ["bash", str(repo / "scripts" / SCRIPT.name)], env=env, capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.split()

    def config(name, text="x: 1\n"):
        path = repo / "monitoring" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return str(path)

    (fake / "ids").write_text("")
    return type("Stack", (), {"repo": repo, "fake": fake, "add": staticmethod(add_container),
                              "run": staticmethod(run), "config": staticmethod(config)})


def test_service_whose_config_changed_after_it_started_is_reported(stack):
    rules = stack.config("alert_rules.yml")
    stack.add("c1", "prometheus", time.time() - 3600, [rules])

    assert stack.run() == ["prometheus"]


def test_service_started_after_its_config_changed_is_not_reported(stack):
    rules = stack.config("alert_rules.yml")
    stack.add("c1", "prometheus", time.time() + 3600, [rules])

    assert stack.run() == []


def test_mounts_outside_monitoring_are_ignored(stack):
    secret = stack.repo / "secrets" / "slack_webhook_url.txt"
    secret.parent.mkdir()
    secret.write_text("https://hooks.example/x\n")
    stack.add("c1", "alertmanager", time.time() - 3600, [str(secret)])

    assert stack.run() == []


def test_directory_mounts_are_ignored(stack):
    rules_dir = stack.repo / "monitoring" / "loki" / "rules"
    rules_dir.mkdir(parents=True)
    stack.add("c1", "loki", time.time() - 3600, [str(rules_dir)])

    assert stack.run() == []


def test_each_stale_service_is_listed_once(stack):
    rules = stack.config("alert_rules.yml")
    scrape = stack.config("prometheus.yml")
    stack.add("c1", "prometheus", time.time() - 3600, [rules, scrape])
    stack.add("c2", "alloy", time.time() - 3600, [stack.config("alloy/config.alloy")])

    assert stack.run() == ["alloy", "prometheus"]


def test_only_this_compose_project_is_inspected(stack):
    stack.run()

    assert "label=com.docker.compose.project=bluestream" in (stack.fake / "ps_args").read_text()
