"""Tax Committee (xTrace / Asl Belgisi) API integration for marking code utilisation."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional

import requests
from flask import current_app

from business_app import db
from business_app.models.payment import TaxCommitteeApiToken
from business_app.models.product import Product
from business_app.utils.exceptions import ValidationError


class TaxCommitteeService:
    """Manages Tax Committee API interactions: token lifecycle and marking code utilisation."""

    def _log_step(self, step: str, *, level: str = "info", **context: Any) -> None:
        payload = {"flow": "tax_committee_utilisation", "step": step, **context}
        log_fn = getattr(current_app.logger, level, current_app.logger.info)
        log_fn("Tax Committee flow step: %s", step, extra=payload)

    @property
    def _base_url(self) -> str:
        return current_app.config["TAX_COMMITTEE_API_URL"]

    @property
    def _company_tin(self) -> str:
        return current_app.config["COMPANY_TIN"]

    @property
    def _timeout(self) -> int:
        return current_app.config.get("TAX_COMMITTEE_API_TIMEOUT_SECONDS", 30)

    def _headers(self, token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json;charset=UTF-8",
        }

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    def get_active_token(self) -> Optional[TaxCommitteeApiToken]:
        """Return the current active token row, or None."""
        return (
            TaxCommitteeApiToken.query.filter_by(is_active=True)
            .order_by(TaxCommitteeApiToken.created_at.desc())
            .first()
        )

    def _seed_token_from_config(self) -> TaxCommitteeApiToken:
        """Create initial token row from the config/env seed value."""
        seed = current_app.config.get("TAX_COMMITTEE_API_TOKEN")
        if not seed:
            raise ValidationError(
                "No Tax Committee API token available. " "Set TAX_COMMITTEE_API_TOKEN env var or seed via admin."
            )
        token_row = TaxCommitteeApiToken(token=seed, is_active=True)
        db.session.add(token_row)
        db.session.flush()
        self._log_step("token_seeded_from_config")
        return token_row

    def check_token_validity(self, token: str) -> Dict[str, Any]:
        """GET check endpoint. Returns parsed JSON response."""
        url = f"{self._base_url}/public/api/v1/party/parties/{self._company_tin}/api-keys/check"
        self._log_step("check_token_validity_started")
        resp = requests.get(url, headers=self._headers(token), timeout=self._timeout)

        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict) and data.get("isTinCorrect"):
                self._log_step("check_token_validity_valid", expires_on=data.get("expiresOn"))
                return data
        # Any non-200 or invalid-token response
        self._log_step("check_token_validity_invalid", level="warning", status_code=resp.status_code)
        return {"valid": False, "status_code": resp.status_code, "body": resp.text}

    def refresh_token(self, old_token: str) -> str:
        """POST refresh endpoint. Returns the new token string."""
        url = f"{self._base_url}/public/api/v1/party/parties/{self._company_tin}/api-keys/refresh"
        self._log_step("refresh_token_started")
        resp = requests.post(
            url,
            headers=self._headers(old_token),
            json={"apiKey": old_token},
            timeout=self._timeout,
        )
        if resp.status_code != 200:
            self._log_step(
                "refresh_token_failed",
                level="error",
                status_code=resp.status_code,
                body=resp.text,
            )
            raise ValidationError(f"Failed to refresh Tax Committee API token: HTTP {resp.status_code}")

        data = resp.json()
        new_token_value = data if isinstance(data, str) else data.get("apiKey") or data.get("token")
        if not new_token_value:
            raise ValidationError("Tax Committee token refresh returned empty token")

        # Deactivate old row(s)
        TaxCommitteeApiToken.query.filter_by(is_active=True).update({"is_active": False})

        new_row = TaxCommitteeApiToken(
            token=new_token_value,
            is_active=True,
            last_refreshed_at=datetime.now(timezone.utc),
        )
        db.session.add(new_row)
        db.session.flush()

        self._log_step("refresh_token_completed")
        return new_token_value

    def _ensure_valid_token(self) -> str:
        """Get an active, validated token — refreshing if needed.

        Uses SELECT ... FOR UPDATE to prevent concurrent refresh races.
        """
        token_row = TaxCommitteeApiToken.query.filter_by(is_active=True).with_for_update().first()
        if not token_row:
            token_row = self._seed_token_from_config()

        check_result = self.check_token_validity(token_row.token)
        if isinstance(check_result, dict) and check_result.get("isTinCorrect"):
            token_row.last_checked_at = datetime.now(timezone.utc)
            expires_on = check_result.get("expiresOn")
            if expires_on:
                try:
                    token_row.expires_at = datetime.fromisoformat(expires_on.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    pass
            db.session.flush()
            return token_row.token

        # Token is invalid — refresh
        self._log_step("token_invalid_refreshing", level="warning")
        return self.refresh_token(token_row.token)

    # ------------------------------------------------------------------
    # Status check
    # ------------------------------------------------------------------

    # Tax Committee marking code statuses
    STATUS_RECEIVED = "RECEIVED"
    STATUS_APPLIED = "APPLIED"
    STATUS_INTRODUCED = "INTRODUCED"
    STATUS_WITHDRAWN = "WITHDRAWN"
    STATUS_WRITTEN_OFF = "WRITTEN_OFF"

    # Codes in these statuses are already in circulation — no utilisation needed
    ALREADY_UTILISED_STATUSES = {STATUS_APPLIED, STATUS_INTRODUCED}
    # Codes in these statuses are invalid and must be replaced
    INVALID_STATUSES = {STATUS_WITHDRAWN, STATUS_WRITTEN_OFF}

    def check_marking_code_statuses(self, identification_codes: List[str]) -> Dict[str, str]:
        """Check current statuses of marking codes in the Tax Committee system.

        Args:
            identification_codes: List of identification code parts (before ASCII 29).

        Returns:
            Dict mapping each identification code to its Tax Committee status
            (e.g. RECEIVED, APPLIED, INTRODUCED, WITHDRAWN, WRITTEN_OFF).
        """
        if not identification_codes:
            return {}

        token = self._ensure_valid_token()
        url = f"{self._base_url}/public/api/cod/private/codes"

        self._log_step(
            "check_marking_code_statuses_started",
            codes_count=len(identification_codes),
        )

        resp = requests.post(
            url,
            headers=self._headers(token),
            json={"codes": identification_codes},
            timeout=self._timeout,
        )

        if resp.status_code != 200:
            self._log_step(
                "check_marking_code_statuses_failed",
                level="error",
                status_code=resp.status_code,
                body=resp.text,
            )
            raise ValidationError(f"Tax Committee status check failed: HTTP {resp.status_code} — {resp.text}")

        data = resp.json()
        results = data.get("results", [])

        status_map: Dict[str, str] = {}
        for entry in results:
            code_data = entry.get("codeData", {})
            code = code_data.get("code", "")
            status = code_data.get("status", "")
            if code:
                status_map[code] = status

        self._log_step(
            "check_marking_code_statuses_completed",
            codes_count=len(identification_codes),
            statuses_found=len(status_map),
        )
        return status_map

    # ------------------------------------------------------------------
    # Utilisation
    # ------------------------------------------------------------------

    def utilise_marking_codes(self, sntins: List[str], product: Product) -> Dict[str, Any]:
        """Change marking codes from 'Received' to 'Applied' in Tax Committee system.

        Args:
            sntins: Full marking code strings (including ASCII 29 separators).
            product: The Product, used for expire_days calculation.

        Returns:
            Parsed response with ``reportId``.
        """
        if not sntins:
            return {"reportId": None, "skipped": True}

        token = self._ensure_valid_token()

        product_group = current_app.config.get("TAX_COMMITTEE_PRODUCT_GROUP", "water")
        url = f"{self._base_url}/api/utilisation?productGroup={product_group}"

        now = datetime.now(ZoneInfo("Asia/Tashkent"))
        expire_days = product.expire_days if product.expire_days is not None else 180
        expiration = now + timedelta(days=expire_days)

        body = {
            "sntins": sntins,
            "businessPlaceId": int(current_app.config["TAX_COMMITTEE_BUSINESS_PLACE_ID"]),
            "releaseType": current_app.config.get("TAX_COMMITTEE_RELEASE_TYPE", "PRODUCTION"),
            "manufacturerCountry": current_app.config.get("TAX_COMMITTEE_MANUFACTURER_COUNTRY", "UZ"),
            "productionDate": now.strftime("%Y-%m-%dT%H:%M:%S+05:00"),
            "expirationDate": expiration.strftime("%Y-%m-%dT%H:%M:%S+05:00"),
        }

        self._log_step(
            "utilise_marking_codes_started",
            product_id=product.id,
            codes_count=len(sntins),
        )

        resp = requests.post(
            url,
            headers=self._headers(token),
            json=body,
            timeout=self._timeout,
        )

        if resp.status_code != 200:
            self._log_step(
                "utilise_marking_codes_failed",
                level="error",
                status_code=resp.status_code,
                body=resp.text,
                product_id=product.id,
            )
            raise ValidationError(f"Tax Committee utilisation failed: HTTP {resp.status_code} — {resp.text}")

        data = resp.json()
        self._log_step(
            "utilise_marking_codes_completed",
            product_id=product.id,
            report_id=data.get("reportId"),
        )
        return data
