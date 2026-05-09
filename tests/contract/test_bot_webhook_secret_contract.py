"""Contract test: backend → main-bot webhook HMAC pact.

Locks in the regression we just fixed — customers were not receiving payment
success notifications because the backend signed `/internal/payment-success`
with `BOT_WEBHOOK_SECRET` while the main `telegram_bot` verified with
`WEBHOOK_SECRET` (and a JWT_SECRET_KEY fallback). They named different env
vars for the same secret, so any deployment that set them to different values
silently broke the customer-facing notification path.

These tests don't import telegram_bot internals (the bot lives on its own
sys.path inside its container) — they re-implement the bot's verifier
algorithm against the backend's signer to assert the HMAC pact at the bytes
level, plus assert the env-var name pact via static inspection of the bot's
config source.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from pathlib import Path

# Backend's signer (canonical implementation lives in
# business_app/utils/bot_webhook.py and business_app/utils/decorators.py — both
# use the same algorithm).
from business_app.utils.bot_webhook import _get_webhook_signature


REPO_ROOT = Path(__file__).resolve().parents[2]
BOT_CONFIG_PATH = REPO_ROOT / "telegram_bot" / "config.py"
BOT_WEBHOOK_PATH = REPO_ROOT / "telegram_bot" / "webhook_server.py"


def _bot_verify_signature(body: bytes, signature: str, secret: str) -> bool:
    """Re-implementation of telegram_bot/webhook_server.py::verify_webhook_signature.

    Kept in lockstep with the bot's actual verifier — if the bot ever changes
    its hash algorithm, header semantics, or comparison rule, this test must
    be updated, which is the point.
    """
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def test_backend_signature_is_accepted_by_bot_with_same_secret():
    """End-to-end byte-level HMAC pact across the trust boundary."""
    secret = "shared-bot-webhook-secret-do-not-reuse"
    payload = {
        "user_id": 42,
        "telegram_id": 90801796,
        "order_id": 297,
        "order_number": "TG_000108_26",
        "amount": 15000,
        "currency": "UZS",
    }
    body = json.dumps(payload).encode("utf-8")

    signature = _get_webhook_signature(body, secret)

    assert _bot_verify_signature(body, signature, secret), (
        "Backend signature must verify with the bot's algorithm when both "
        "sides use the same secret. If this fails, the HMAC pact is broken."
    )


def test_bot_rejects_signature_built_with_a_different_secret():
    """The bug's failure mode: different secrets ⇒ rejection (no notification)."""
    payload = {"order_id": 1}
    body = json.dumps(payload).encode("utf-8")

    backend_signature = _get_webhook_signature(body, "backend-secret")

    assert not _bot_verify_signature(body, backend_signature, "bot-different-secret"), (
        "Mismatched secrets must NOT verify — this is the security property "
        "the contract relies on."
    )


def test_bot_reads_BOT_WEBHOOK_SECRET_env_var():
    """Static check: the bot config reads the env var the backend signs with.

    The original bug: bot read os.environ.get('WEBHOOK_SECRET') while backend
    signed with BOT_WEBHOOK_SECRET. They named different env vars for the
    same logical secret, so even with both sides "configured" the HMAC failed.
    """
    src = BOT_CONFIG_PATH.read_text(encoding="utf-8")

    # The webhook_secret field in SecurityConfig must be sourced from
    # BOT_WEBHOOK_SECRET (matching backend's BOT_WEBHOOK_SECRET property).
    assert re.search(
        r"webhook_secret\s*=\s*os\.environ\.get\(\s*['\"]BOT_WEBHOOK_SECRET['\"]",
        src,
    ), (
        "telegram_bot/config.py must read BOT_WEBHOOK_SECRET (matching the "
        "backend's signer). Reading WEBHOOK_SECRET silently breaks payment "
        "success notifications."
    )

    # And the *old* incorrect read must be gone — guards against accidental revert.
    assert not re.search(
        r"webhook_secret\s*=\s*os\.environ\.get\(\s*['\"]WEBHOOK_SECRET['\"]",
        src,
    ), (
        "telegram_bot/config.py must NOT read WEBHOOK_SECRET (that's the "
        "staff_bot's secret — different trust boundary)."
    )


def test_bot_verifier_has_no_jwt_secret_fallback():
    """Cross-domain fallback would let a JWT-secret leak forge webhooks."""
    src = BOT_WEBHOOK_PATH.read_text(encoding="utf-8")

    # The fixed line reads only `config.security.webhook_secret`. If the JWT
    # fallback creeps back, this catches it. The match is intentionally
    # narrow so it doesn't trip on prose in comments.
    assert not re.search(
        r"webhook_secret\s*=\s*config\.security\.webhook_secret\s*or\s*config\.security\.jwt_secret_key",
        src,
    ), (
        "telegram_bot/webhook_server.py must not fall back to jwt_secret_key — "
        "webhook trust and auth-token trust are separate domains."
    )


def test_backend_BOT_WEBHOOK_SECRET_does_not_default_to_SECRET_KEY():
    """Cross-domain fallback in backend config — same class of bug."""
    base_path = REPO_ROOT / "business_app" / "config" / "base.py"
    src = base_path.read_text(encoding="utf-8")

    # The property must require its own value, not silently inherit SECRET_KEY.
    # Match the get_secret call for bot_webhook_secret and assert default=
    # is not present (we replaced default=self.SECRET_KEY with required=True).
    match = re.search(
        r'get_secret\(\s*"bot_webhook_secret"\s*,\s*"BOT_WEBHOOK_SECRET"[^)]*\)',
        src,
    )
    assert match, "BOT_WEBHOOK_SECRET property must call get_secret with the canonical names"
    assert "default=self.SECRET_KEY" not in match.group(0), (
        "BOT_WEBHOOK_SECRET must not silently default to SECRET_KEY — that "
        "collapses three trust domains (Flask sessions, auth, webhooks) into one."
    )
    assert "required=True" in match.group(0), (
        "BOT_WEBHOOK_SECRET must be required so missing config fails loudly "
        "at first webhook send instead of silently signing with SECRET_KEY."
    )
