"""nginx and the app see the real customer behind the Cloudflare Tunnel (owner decision 2026-10-29).

Production's front door is a Cloudflare Tunnel: cloudflared on the host connects to nginx's published port, so every
request reached nginx from the Docker bridge gateway (172.18.0.1). Every `limit_req` zone and, behind ProxyFix, every
Flask per-IP limit and logged `ip_address` was one bucket for the whole site. Now:
- nginx takes the client from CF-Connecting-IP (Cloudflare's edge sets it to the client and overwrites any value a
  client sends), trusted only from the Docker bridge range and loopback, and taken as sent (no recursion);
- the port is published on loopback only, so nothing but the host's cloudflared can reach nginx and set that header;
- the nginx exporter, which scrapes /nginx_status over the compose network with no such header, keeps its own bridge
  address and stays allowed, while tunnel traffic now carries the customer's address and is refused.
The checks read the real files; `nginx -t` and a probe in a throwaway nginx container were run by hand (task report).
"""

import re
from pathlib import Path

import pytest
import yaml

from tests.unit.test_nginx_telegram_login_logging import NGINX, NGINX_CONF, _blocks, _directives, _location

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
REAL_IP_DIRECTIVES = ("set_real_ip_from", "real_ip_header", "real_ip_recursive")


def _real_ip(directives):
    return [line for line in directives if line.split()[0] in REAL_IP_DIRECTIVES]


def _nginx_ports(compose_file):
    services = yaml.safe_load((REPO / compose_file).read_text(encoding="utf-8"))["services"]
    return (services.get("nginx") or {}).get("ports")


def test_nginx_takes_the_client_from_cf_connecting_ip_and_only_from_the_bridge_and_loopback():
    [http] = _blocks(NGINX_CONF, "http")

    assert sorted(_real_ip(_directives(http))) == sorted(
        [
            "set_real_ip_from 172.16.0.0/12;",
            "set_real_ip_from 127.0.0.1;",
            "real_ip_header CF-Connecting-IP;",
            "real_ip_recursive off;",
        ]
    )


def test_no_included_config_trusts_another_address_or_header():
    """A server or location with its own real_ip directives would replace the http block's for its requests."""
    for conf in sorted((NGINX / "conf.d").rglob("*.conf")):
        text = re.sub(r"#[^\n]*", "", conf.read_text(encoding="utf-8"))
        assert not re.search(r"\b(set_real_ip_from|real_ip_header|real_ip_recursive)\b", text), conf.name


def test_production_publishes_nginx_on_loopback_only():
    """Production runs docker-compose.yml + docker-compose.production.yml (scripts/deploy.sh). Compose appends an
    overlay's `ports` to the base's (a second "127.0.0.1:81:80" there would keep the base's all-interfaces binding and
    clash with it), so the base's binding is production's and the overlay adds none. The deploy smoke check reaches
    nginx at localhost:81, as cloudflared does."""
    deploy = (REPO / "scripts" / "deploy.sh").read_text(encoding="utf-8")

    assert "COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.production.yml)" in deploy
    assert _nginx_ports("docker-compose.production.yml") is None
    assert _nginx_ports("docker-compose.yml") == ["127.0.0.1:81:80"]
    assert "http://localhost:81/health" in deploy


@pytest.mark.parametrize(
    "compose_file",
    ["docker-compose.yml", "docker-compose.production.yml", "docker-compose.local.yml", "docker-compose.secrets.yml"],
)
def test_no_compose_file_publishes_nginx_beyond_loopback(compose_file):
    assert all(str(port).startswith("127.0.0.1:") for port in _nginx_ports(compose_file) or [])


def test_the_nginx_exporter_stays_allowed_and_everyone_else_is_refused():
    """The whole list, so an inserted `allow all;` or a widened range fails here. 172.16.0.0/12 is the exporter's
    own compose-network address (it sends no CF-Connecting-IP); tunnel traffic now carries the customer's."""
    assert _location("00-stub-status.conf", "_", "/nginx_status") == [
        "stub_status;",
        "access_log off;",
        "allow 127.0.0.0/8;",
        "allow 172.16.0.0/12;",
        "allow 10.0.0.0/8;",
        "deny all;",
    ]
