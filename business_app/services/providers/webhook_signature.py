"""
Webhook signature and replay-protection verifier.

Extracted from ``business_app.services.payment_service`` as ARCH-002 PR 1.
Owns:
    - Per-provider HMAC/credential validation (via injected callables so the
      low-level verifiers can stay wherever they live today and move later).
    - Timestamp freshness window.
    - Redis-backed nonce replay guard (via ``RedisKeyspace.webhook_replay_nonce``).

PaymentService keeps a thin ``validate_webhook_signature`` delegate so the
public payment-service API remains unchanged during the split.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

from flask import current_app

from business_app.utils.audit_logger import audit_logger, AuditEventType, AuditSeverity
from shared.redis_keyspace import RedisKeyspace


SignatureFn = Callable[[Dict[str, Any]], bool]
IPsProvider = Callable[[], Iterable[str]]


class WebhookSignatureVerifier:
    """Validate payment-webhook signatures and guard against replay."""

    def __init__(
        self,
        *,
        redis_client: Optional[Any],
        verify_payme_signature: SignatureFn,
        verify_click_signature: SignatureFn,
        tolerance_seconds: int = 300,
        nonce_ttl_seconds: int = 3600,
        payme_allowed_ips_provider: Optional[IPsProvider] = None,
        click_allowed_ips_provider: Optional[IPsProvider] = None,
    ):
        self._redis = redis_client
        self._verify_payme = verify_payme_signature
        self._verify_click = verify_click_signature
        self._tolerance = int(tolerance_seconds or 300)
        self._nonce_ttl = int(nonce_ttl_seconds or 3600)
        self._payme_ips_provider = payme_allowed_ips_provider or _default_payme_ips
        self._click_ips_provider = click_allowed_ips_provider or _default_click_ips

    def validate(self, provider: str, request) -> bool:
        """Validate signature + replay protection for ``provider``'s webhook.

        Returns True on success, False on any validation failure. Callers
        should treat False as terminal — the verifier emits its own audit log
        for the rejection reason.
        """
        try:
            provider_lc = provider.lower()
            if provider_lc == "payme":
                signature_valid = self._validate_payme_envelope(request)
            elif provider_lc == "click":
                signature_valid = self._validate_click_envelope(request)
            else:
                current_app.logger.error(f"Unknown webhook provider: {provider}")
                return False

            if not signature_valid:
                audit_logger.log_event(
                    event_type=AuditEventType.SUSPICIOUS_ACTIVITY,
                    action="webhook_signature_validation_failed",
                    severity=AuditSeverity.HIGH,
                    success=False,
                    resource_type="payment_webhook",
                    description=f"Invalid webhook signature from {provider}",
                    additional_data={
                        "provider": provider,
                        "remote_addr": request.remote_addr,
                        "user_agent": request.headers.get("User-Agent"),
                        "content_length": request.headers.get("Content-Length"),
                    },
                )
                return False

            if not self._validate_replay_protection(provider_lc, request):
                audit_logger.log_event(
                    event_type=AuditEventType.SUSPICIOUS_ACTIVITY,
                    action="webhook_replay_attack_detected",
                    severity=AuditSeverity.CRITICAL,
                    resource_type="payment_webhook",
                    description=f"Webhook replay attack detected from {provider}",
                    additional_data={
                        "provider": provider,
                        "remote_addr": request.remote_addr,
                        "user_agent": request.headers.get("User-Agent"),
                        "timestamp": request.headers.get("X-Timestamp"),
                        "nonce": request.headers.get("X-Nonce"),
                    },
                )
                return False

            audit_logger.log_event(
                event_type=AuditEventType.WEBHOOK_RECEIVED,
                action="webhook_validation_successful",
                severity=AuditSeverity.LOW,
                resource_type="payment_webhook",
                description=f"Webhook validation successful for {provider}",
                additional_data={"provider": provider, "remote_addr": request.remote_addr},
            )
            return True
        except Exception as e:
            current_app.logger.exception("Webhook validation error for %s", provider)
            audit_logger.log_event(
                event_type=AuditEventType.SUSPICIOUS_ACTIVITY,
                action="webhook_validation_error",
                severity=AuditSeverity.HIGH,
                resource_type="payment_webhook",
                description=f"Webhook validation error for {provider}: {e}",
                additional_data={"provider": provider, "error": str(e)},
            )
            return False

    def _validate_payme_envelope(self, request) -> bool:
        try:
            data = request.get_json() or {}
            if not self._verify_payme(data):
                return False

            allowed_ips = list(self._payme_ips_provider() or [])
            if allowed_ips and request.remote_addr not in allowed_ips:
                current_app.logger.warning(f"Webhook from unauthorized IP: {request.remote_addr}")
                return False

            content_type = (request.content_type or "").lower()
            if not content_type.startswith("application/json"):
                current_app.logger.warning(f"Invalid content-type for Payme webhook: {request.content_type}")
                return False
            return True
        except Exception:
            current_app.logger.exception("Payme webhook signature validation error")
            return False

    def _validate_click_envelope(self, request) -> bool:
        try:
            data = dict(request.form) if request.form else request.get_json() or {}
            if not self._verify_click(data):
                return False

            allowed_ips = list(self._click_ips_provider() or [])
            if allowed_ips and request.remote_addr not in allowed_ips:
                current_app.logger.warning(f"Webhook from unauthorized IP: {request.remote_addr}")
                return False

            content_type = (request.content_type or "").lower()
            if not (
                content_type.startswith("application/x-www-form-urlencoded")
                or content_type.startswith("application/json")
            ):
                current_app.logger.warning(f"Invalid content-type for Click webhook: {request.content_type}")
                return False
            return True
        except Exception:
            current_app.logger.exception("Click webhook signature validation error")
            return False

    def _validate_replay_protection(self, provider_lc: str, request) -> bool:
        try:
            timestamp, nonce = self._extract_timestamp_and_nonce(provider_lc, request)

            try:
                webhook_time = int(timestamp)
                time_diff = abs(int(time.time()) - webhook_time)
                if time_diff > self._tolerance:
                    current_app.logger.warning(
                        f"Webhook timestamp too old or too new: {time_diff}s difference "
                        f"(tolerance: {self._tolerance}s)"
                    )
                    return False
            except (ValueError, TypeError):
                current_app.logger.warning(f"Invalid webhook timestamp format: {timestamp}")
                return False

            if self._redis and nonce:
                nonce_key = RedisKeyspace.webhook_replay_nonce(provider_lc, nonce)
                if self._redis.exists(nonce_key):
                    current_app.logger.warning(f"Webhook nonce replay detected: {nonce}")
                    return False
                self._redis.setex(nonce_key, self._nonce_ttl, "1")
            return True
        except Exception:
            current_app.logger.exception("Replay protection validation error")
            return False

    @staticmethod
    def _extract_timestamp_and_nonce(provider_lc: str, request) -> Tuple[str, str]:
        if provider_lc == "payme":
            body = request.get_json() or {}
            timestamp = body.get("timestamp") or request.headers.get("X-Timestamp")
            nonce = body.get("nonce") or request.headers.get("X-Nonce")
            if not timestamp:
                timestamp = str(int(time.time()))
            if not nonce:
                content_hash = hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()[:16]
                nonce = f"payme_{content_hash}_{timestamp}"
            return timestamp, nonce

        if provider_lc == "click":
            timestamp = request.form.get("timestamp") or request.headers.get("X-Timestamp")
            nonce = request.form.get("nonce") or request.headers.get("X-Nonce")
            if not timestamp:
                timestamp = str(int(time.time()))
            if not nonce:
                form_data = dict(request.form)
                form_data.pop("timestamp", None)
                form_data.pop("nonce", None)
                content_hash = hashlib.sha256(json.dumps(form_data, sort_keys=True).encode("utf-8")).hexdigest()[:16]
                nonce = f"click_{content_hash}_{timestamp}"
            return timestamp, nonce

        raise ValueError(f"Unknown webhook provider: {provider_lc}")


def _default_payme_ips() -> Iterable[str]:
    return current_app.config.get("PAYME_WEBHOOK_IPS", []) or []


def _default_click_ips() -> Iterable[str]:
    return current_app.config.get("CLICK_CALLBACK_ALLOWLIST") or current_app.config.get("CLICK_WEBHOOK_IPS", []) or []
