"""API-surface snapshot test (TST-002).

Snapshots the inventory of ``/api/v1/...`` routes from ``app.url_map`` and
fails on any drift. Forces contributors to either keep API contracts stable
or explicitly update the snapshot in the same PR — making accidental
breaking changes impossible to land silently.

Why ``app.url_map`` and not Flasgger ``/apispec_1.json``:
- Flasgger's spec emit currently raises ``TypeError: Object of type date
  is not JSON serializable`` from a non-serializable value in the swagger
  template. Pinning a Flasgger-bug fix into TST-002 would conflate two
  separate concerns.
- ``url_map`` is the canonical source of truth for the route surface;
  Flasgger only documents what the developer remembered to annotate
  (most blueprints don't carry ``@swag_from``). Snapshotting ``url_map``
  guarantees full coverage.
- Shape/payload contracts already live in [tests/contract/test_api_response_contracts.py](
  test_api_response_contracts.py).

Updating the snapshot:
- Run ``UPDATE_API_SNAPSHOT=1 pytest tests/contract/test_openapi_snapshot.py``.
- Review the resulting diff in [snapshots/api_routes.json](snapshots/api_routes.json)
  alongside the route change in the same PR.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Tuple

import pytest


SNAPSHOT_PATH = Path(__file__).parent / 'snapshots' / 'api_routes.json'
HIDDEN_METHODS = {'HEAD', 'OPTIONS'}
API_PREFIX = '/api/v1/'


def _extract_route_inventory(app) -> List[Dict[str, object]]:
    """Return a stable, JSON-serializable inventory of API routes.

    Each entry: ``{rule, endpoint, methods}``. Sorted by (rule, endpoint)
    so the snapshot diff is stable across Flask iteration order.
    """
    rows: List[Dict[str, object]] = []
    for rule in app.url_map.iter_rules():
        path = str(rule.rule)
        # Only track the public API surface — ignore static, frontend, swagger.
        if not path.startswith(API_PREFIX):
            continue
        methods = sorted(m for m in (rule.methods or set()) if m not in HIDDEN_METHODS)
        if not methods:
            continue
        rows.append({
            'rule': path,
            'endpoint': rule.endpoint,
            'methods': methods,
        })

    rows.sort(key=lambda r: (r['rule'], r['endpoint']))
    return rows


@pytest.mark.contract
def test_api_route_surface_matches_snapshot(app):
    """Fail on any drift in the ``/api/v1`` route inventory."""
    current = _extract_route_inventory(app)

    if os.environ.get('UPDATE_API_SNAPSHOT') == '1':
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT_PATH.write_text(json.dumps(current, indent=2, sort_keys=True) + '\n')
        pytest.skip(f"Snapshot updated at {SNAPSHOT_PATH.relative_to(Path.cwd())}")

    if not SNAPSHOT_PATH.exists():
        pytest.fail(
            f"API route snapshot missing at {SNAPSHOT_PATH}.\n"
            f"Create it: UPDATE_API_SNAPSHOT=1 pytest "
            f"tests/contract/test_openapi_snapshot.py"
        )

    expected = json.loads(SNAPSHOT_PATH.read_text())

    # Diff with helpful messages — we want failures to point at the
    # specific rule/method that drifted, not just "snapshots don't match".
    expected_keys: Dict[Tuple[str, str], List[str]] = {
        (e['rule'], e['endpoint']): e['methods'] for e in expected
    }
    current_keys: Dict[Tuple[str, str], List[str]] = {
        (e['rule'], e['endpoint']): e['methods'] for e in current
    }

    added = sorted(current_keys.keys() - expected_keys.keys())
    removed = sorted(expected_keys.keys() - current_keys.keys())
    method_changes: List[str] = []
    for key in sorted(current_keys.keys() & expected_keys.keys()):
        if current_keys[key] != expected_keys[key]:
            method_changes.append(
                f"  {key[0]} ({key[1]}): "
                f"expected={expected_keys[key]} -> actual={current_keys[key]}"
            )

    if not (added or removed or method_changes):
        return  # Snapshot matches exactly.

    parts: List[str] = ["API route surface drift detected:"]
    if added:
        parts.append("\nAdded routes:")
        for rule, endpoint in added:
            parts.append(f"  + {rule} ({endpoint}, {current_keys[(rule, endpoint)]})")
    if removed:
        parts.append("\nRemoved routes:")
        for rule, endpoint in removed:
            parts.append(f"  - {rule} ({endpoint}, {expected_keys[(rule, endpoint)]})")
    if method_changes:
        parts.append("\nMethod changes:")
        parts.extend(method_changes)
    parts.append(
        "\nIf intentional, regenerate the snapshot in the same PR:"
        "\n  UPDATE_API_SNAPSHOT=1 pytest tests/contract/test_openapi_snapshot.py"
    )

    pytest.fail('\n'.join(parts))


@pytest.mark.contract
def test_snapshot_file_is_valid_json(app):
    """The snapshot must be parseable JSON — guards against editor mishaps."""
    if not SNAPSHOT_PATH.exists():
        pytest.skip("Snapshot not yet created")
    json.loads(SNAPSHOT_PATH.read_text())
