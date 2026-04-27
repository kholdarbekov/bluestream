"""
Card tokenization, verification, and saved-card management.

Extracted from PaymentService as part of ARCH-002 PR 3 (2026-04-19).
Owns: Payme card tokenization flow, SMS verification + attempt tracking,
verified-card persistence, saved-card CRUD (list/default/delete), card-brand
detection, provider routing by brand.

Depends on PaymeProvider for Payme JSON-RPC calls (cards.create, cards.verify,
cards.get_verify_code) and on the shared webhook Redis client for verification
attempt tracking.
"""

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from flask import current_app

from business_app import db
from business_app.models.payment import CreditCard
from business_app.models.user import User
from business_app.utils.audit_logger import (
    AuditEventType,
    AuditSeverity,
    audit_logger,
)
from business_app.utils.card_validation import CardSecurityValidator, CardValidator
from business_app.utils.exceptions import (
    NotFoundError,
    PaymentError,
    ValidationError,
)
from business_app.utils.translations import get_translation


class CardTokenService:
    """Owns card tokenization, verification, and saved-card lifecycle."""

    MAX_VERIFICATION_ATTEMPTS = 3
    VERIFICATION_ATTEMPTS_TTL = 600  # 10 minutes

    def __init__(self, *, payme_provider, redis_client):
        self._payme_provider = payme_provider
        self.redis_client = redis_client

    # ------------------------------------------------------------------ #
    # Tokenization + SMS verification                                     #
    # ------------------------------------------------------------------ #

    def create_card_token_with_verification(self, card_number: str, expiry: str, save: bool = True) -> Dict[str, Any]:
        """
        Create card token via Payme cards.create and request verification code.

        This is the main entry point for tokenizing a new card. It:
        1. Calls Payme cards.create to get a token
        2. If card needs verification (verify: false), auto-requests SMS code

        Args:
            card_number: 16-digit card number (no spaces)
            expiry: Expiry in MMYY format (e.g., "0325" for March 2025)
            save: True for permanent token (recurring), False for one-time

        Returns:
            Dict containing:
            - token: Card token from Payme
            - masked_number: e.g., "860006******6311"
            - expire: e.g., "03/25"
            - recurrent: Whether card supports recurring payments
            - needs_verification: True if SMS verification required
            - masked_phone: Phone number for SMS (if verification needed)
            - wait_seconds: Seconds until code expires (if verification needed)
            - verification_sent: True if SMS was sent

        Raises:
            PaymentError: If card creation fails
            ValidationError: If card data is invalid
        """
        clean_number = card_number.replace(" ", "").replace("-", "")
        if not clean_number.isdigit() or len(clean_number) != 16:
            raise ValidationError("Card number must be 16 digits")

        clean_expiry = expiry.replace("/", "")
        if not clean_expiry.isdigit() or len(clean_expiry) != 4:
            raise ValidationError("Expiry must be in MMYY format")

        month = int(clean_expiry[:2])
        if month < 1 or month > 12:
            raise ValidationError("Invalid expiry month")

        create_response = self._payme_provider._payme_request(
            "cards.create", {"card": {"number": clean_number, "expire": clean_expiry}, "save": save}
        )

        if "error" in create_response:
            error_msg = self._payme_provider._extract_payme_error_message(create_response["error"])
            current_app.logger.error(f"Payme cards.create failed: {error_msg}")
            raise PaymentError(f"Card tokenization failed: {error_msg}")

        card_data = create_response.get("result", {}).get("card", {})
        token = card_data.get("token")

        if not token:
            raise PaymentError("Payme did not return a card token")

        result = {
            "token": token,
            "masked_number": card_data.get("number", ""),
            "expire": card_data.get("expire", ""),
            "recurrent": card_data.get("recurrent", False),
            "needs_verification": not card_data.get("verify", False),
        }

        # Step 2: If verification needed, request SMS code automatically
        # if result['needs_verification']:
        #     try:
        #         verify_result = self.request_card_verification_code(token)
        #         result.update({
        #             'masked_phone': verify_result['phone'],
        #             'wait_seconds': verify_result['wait'] // 1000,  # Convert ms to seconds
        #             'verification_sent': verify_result['sent']
        #         })
        #     except PaymentError as e:
        #         # Log but don't fail - user can manually request code
        #         current_app.logger.warning(f"Auto-verification request failed: {e}")
        #         result['verification_sent'] = False
        #         result['masked_phone'] = None
        #         result['wait_seconds'] = 60  # Default

        audit_logger.log_event(
            event_type=AuditEventType.SENSITIVE_DATA_ACCESS,
            action="card_tokenized",
            severity=AuditSeverity.MEDIUM,
            resource_type="credit_card",
            description=f"Card tokenized via Payme: {result['masked_number']}",
            additional_data={
                "masked_number": result["masked_number"],
                "needs_verification": result["needs_verification"],
                "recurrent": result["recurrent"],
            },
        )

        return result

    def request_card_verification_code(self, token: str) -> Dict[str, Any]:
        """
        Request SMS verification code for a card token via Payme cards.get_verify_code.

        Args:
            token: Card token from cards.create

        Returns:
            Dict containing:
            - sent: True if SMS was sent successfully
            - phone: Masked phone number (e.g., "99890*****31")
            - wait: Validity period in milliseconds

        Raises:
            PaymentError: If request fails
        """
        if not token:
            raise ValidationError("Card token is required")

        response = self._payme_provider._payme_request("cards.get_verify_code", {"token": token})

        if "error" in response:
            error_msg = self._payme_provider._extract_payme_error_message(response["error"])
            current_app.logger.error(f"Payme cards.get_verify_code failed: {error_msg}")
            raise PaymentError(f"Failed to send verification code: {error_msg}")

        result = response.get("result", {})

        if not result.get("sent"):
            raise PaymentError("Verification code was not sent by Payme")

        self.reset_verification_attempts(token)

        current_app.logger.info(f"Verification code sent to {result.get('phone', 'unknown')}")

        return {"sent": result.get("sent", False), "phone": result.get("phone", ""), "wait": result.get("wait", 60000)}

    def _get_verification_attempts_key(self, token: str) -> str:
        """Generate Redis key for tracking verification attempts."""
        token_hash = hashlib.sha256(token.encode()).hexdigest()[:16]
        return f"payme:verify_attempts:{token_hash}"

    def get_verification_attempts_remaining(self, token: str) -> int:
        """
        Get remaining verification attempts for a token.

        Args:
            token: Card token

        Returns:
            int: Number of attempts remaining (0-3)
        """
        if not self.redis_client:
            current_app.logger.warning("Redis not available for verification attempt tracking")
            return self.MAX_VERIFICATION_ATTEMPTS

        try:
            key = self._get_verification_attempts_key(token)
            attempts = self.redis_client.get(key)
            if attempts is None:
                return self.MAX_VERIFICATION_ATTEMPTS
            return max(0, self.MAX_VERIFICATION_ATTEMPTS - int(attempts))
        except Exception:
            current_app.logger.exception("Error getting verification attempts")
            return self.MAX_VERIFICATION_ATTEMPTS

    def increment_verification_attempts(self, token: str) -> int:
        """
        Increment failed verification attempts for a token.

        Args:
            token: Card token

        Returns:
            int: Number of attempts remaining after increment
        """
        if not self.redis_client:
            current_app.logger.warning("Redis not available for verification attempt tracking")
            return self.MAX_VERIFICATION_ATTEMPTS - 1

        try:
            key = self._get_verification_attempts_key(token)
            pipe = self.redis_client.pipeline()
            pipe.incr(key)
            pipe.expire(key, self.VERIFICATION_ATTEMPTS_TTL)
            results = pipe.execute()
            attempts = results[0]
            return max(0, self.MAX_VERIFICATION_ATTEMPTS - int(attempts))
        except Exception:
            current_app.logger.exception("Error incrementing verification attempts")
            return self.MAX_VERIFICATION_ATTEMPTS - 1

    def reset_verification_attempts(self, token: str) -> None:
        """
        Reset verification attempts for a token (call after successful verify or new code request).

        Args:
            token: Card token
        """
        if not self.redis_client:
            return

        try:
            key = self._get_verification_attempts_key(token)
            self.redis_client.delete(key)
        except Exception:
            current_app.logger.exception("Error resetting verification attempts")

    def verify_card(self, token: str, code: str) -> Dict[str, Any]:
        """
        Verify card with SMS code via Payme cards.verify.

        Args:
            token: Card token from cards.create
            code: Verification code from SMS (4-8 alphanumeric characters)

        Returns:
            Dict containing:
            - verified: True if verification successful
            - card: Card data from Payme (masked_number, expire, token, recurrent)

        Raises:
            ValidationError: If code format is invalid or wrong code entered
            PaymentError: If verification fails for other reasons
        """
        if not token:
            raise ValidationError("Card token is required")

        code = str(code).strip().upper()
        if not code or len(code) < 4 or len(code) > 8:
            raise ValidationError("Verification code must be 4-8 characters")

        if not code.isalnum():
            raise ValidationError("Verification code must be alphanumeric")

        attempts_remaining = self.get_verification_attempts_remaining(token)
        if attempts_remaining <= 0:
            raise ValidationError("Too many failed attempts. Please request a new code.")

        response = self._payme_provider._payme_request("cards.verify", {"token": token, "code": code})

        if "error" in response:
            error_code = response["error"].get("code")
            error_msg = self._payme_provider._extract_payme_error_message(response["error"])

            if error_code == -31103:
                attempts_remaining = self.increment_verification_attempts(token)
                current_app.logger.warning(
                    f"Invalid verification code entered. Attempts remaining: {attempts_remaining}"
                )
                raise ValidationError("Invalid verification code")

            if error_code == -31104:
                current_app.logger.warning("Verification code expired for token")
                raise ValidationError("Verification code has expired. Please request a new code.")

            current_app.logger.error(f"Payme cards.verify failed: {error_msg}")
            raise PaymentError(f"Card verification failed: {error_msg}")

        card_result = response.get("result", {}).get("card", {})

        if not card_result.get("verify"):
            raise PaymentError("Card verification was not confirmed by Payme")

        self.reset_verification_attempts(token)

        current_app.logger.info(f"Card verified successfully: {card_result.get('number', 'unknown')}")

        audit_logger.log_event(
            event_type=AuditEventType.PAYMENT_VERIFICATION_CODE_VERIFED,
            action="card_verified",
            severity=AuditSeverity.MEDIUM,
            resource_type="credit_card",
            description=f"Card verified via SMS: {card_result.get('number', '')}",
            additional_data={
                "masked_number": card_result.get("number"),
                "recurrent": card_result.get("recurrent", False),
            },
        )

        return {
            "verified": True,
            "card": {
                "masked_number": card_result.get("number", ""),
                "expire": card_result.get("expire", ""),
                "token": card_result.get("token", token),
                "recurrent": card_result.get("recurrent", False),
            },
        }

    # ------------------------------------------------------------------ #
    # Card brand detection + verified-card persistence                    #
    # ------------------------------------------------------------------ #

    def detect_card_brand(self, masked_number: str) -> str:
        """
        Detect card brand from masked card number.

        Args:
            masked_number: Masked card number (e.g., "860006******6311")

        Returns:
            str: Card brand (uzcard, humo, visa, mastercard, unknown)
        """
        if not masked_number:
            return "unknown"

        clean = masked_number.replace("*", "").replace(" ", "")
        if len(clean) < 4:
            return "unknown"

        prefix = clean[:4]

        if masked_number.startswith("8600"):
            return "uzcard"
        if masked_number.startswith("9860"):
            return "humo"

        if prefix.startswith("4"):
            return "visa"
        if prefix.startswith("5") or prefix.startswith("2"):
            return "mastercard"

        return "unknown"

    def save_or_update_verified_card(self, user_id: int, token: str, card_metadata: Dict[str, Any]) -> CreditCard:
        """
        Save a verified card to database or update existing card's verification status.

        Args:
            user_id: User ID
            token: Verified card token
            card_metadata: Dict with masked_number, expire, cardholder_name, recurrent

        Returns:
            CreditCard: The saved or updated card object
        """
        existing_card = CreditCard.query.filter_by(user_id=user_id, card_token=token).first()

        if existing_card:
            existing_card.is_verified = True
            existing_card.last_used_at = datetime.now(timezone.utc)
            existing_card.usage_count = (existing_card.usage_count or 0) + 1
            existing_card.payme_recurrent = card_metadata.get("recurrent", False)
            return existing_card

        expire = card_metadata.get("expire", "")
        try:
            if "/" in expire:
                parts = expire.split("/")
                expiry_month = int(parts[0])
                expiry_year = int("20" + parts[1]) if len(parts[1]) == 2 else int(parts[1])
            else:
                expiry_month = int(expire[:2]) if len(expire) >= 2 else 1
                expiry_year = int("20" + expire[2:4]) if len(expire) >= 4 else 2099
        except (ValueError, IndexError):
            expiry_month = 1
            expiry_year = 2099

        masked_number = card_metadata.get("masked_number", "")
        clean_digits = masked_number.replace("*", "").replace(" ", "")
        last_four = clean_digits[-4:] if len(clean_digits) >= 4 else "0000"

        card_brand = self.detect_card_brand(masked_number)

        card = CreditCard(
            user_id=user_id,
            card_token=token,
            card_brand=card_brand,
            last_four_digits=last_four,
            expiry_month=expiry_month,
            expiry_year=expiry_year,
            cardholder_name=card_metadata.get("cardholder_name", "Card Holder"),
            is_verified=True,
            is_active=True,
            is_default=False,
            provider="payme",
            payme_recurrent=card_metadata.get("recurrent", True),
            last_used_at=datetime.now(timezone.utc),
            usage_count=1,
        )

        db.session.add(card)

        audit_logger.log_event(
            event_type=AuditEventType.SENSITIVE_DATA_ACCESS,
            action="verified_card_saved",
            severity=AuditSeverity.MEDIUM,
            resource_type="credit_card",
            description=f"Verified card saved: {masked_number}",
            additional_data={
                "card_brand": card_brand,
                "last_four": last_four,
                "recurrent": card_metadata.get("recurrent", True),
                "user_id": user_id,
            },
        )

        return card

    # ------------------------------------------------------------------ #
    # Saved-card CRUD                                                     #
    # ------------------------------------------------------------------ #

    def save_card(self, card_data: Dict[str, Any]) -> CreditCard:
        """
        Save a credit card with comprehensive validation and security

        Args:
            card_data: Dictionary containing card information
                - user_id: User ID
                - card_number: Credit card number
                - expiry_month: Expiry month (1-12)
                - expiry_year: Expiry year (YYYY)
                - cardholder_name: Cardholder name
                - cvv: CVV code (optional, for validation only)
                - is_default: Whether this should be the default card

        Returns:
            CreditCard: Saved card object

        Raises:
            ValidationError: If card validation fails
            NotFoundError: If user not found
        """
        user_id = card_data.get("user_id")
        try:
            user = User.query.get(user_id)
            if not user:
                raise NotFoundError(get_translation("error.not_found"))

            card_token = card_data.get("card_token")
            if not card_token:
                raise ValidationError("Secure card token is required. Please use client-side tokenization.")

            card_number = card_data.get("card_number", "")
            is_tokenized = CardValidator._is_masked_card_number(card_number)

            validation_result = CardValidator.validate_complete_card(card_data)
            if not validation_result.is_valid:
                error_details = ", ".join(validation_result.errors)
                current_app.logger.warning(f"Card validation failed: {error_details}")
                raise ValidationError(get_translation("error.validation.card_invalid"))

            if is_tokenized:
                last_four_digits = CardValidator._extract_last_four_from_masked(card_number)
            else:
                cleaned_number = CardValidator._clean_card_number(card_number)
                last_four_digits = cleaned_number[-4:] if len(cleaned_number) >= 4 else "0000"

                if not CardSecurityValidator.validate_no_sequential_numbers(cleaned_number):
                    raise ValidationError(get_translation("error.validation.card_invalid"))

                if not CardSecurityValidator.validate_not_test_card(cleaned_number):
                    raise ValidationError(get_translation("error.validation.test_card_not_allowed"))

            if is_tokenized:
                fingerprint = CardValidator.generate_tokenized_card_fingerprint(
                    card_token, last_four_digits, card_data.get("expiry_month"), card_data.get("expiry_year")
                )
            else:
                fingerprint = CardValidator.generate_card_fingerprint(
                    cleaned_number, card_data.get("expiry_month"), card_data.get("expiry_year")
                )

            existing_card = CreditCard.query.filter_by(user_id=user_id, fingerprint=fingerprint, is_active=True).first()

            if existing_card:
                raise ValidationError(get_translation("error.validation.card_already_saved"))

            card_brand = validation_result.card_brand or "unknown"

            credit_card = CreditCard(
                user_id=user_id,
                card_token=card_token,
                card_brand=card_brand,
                last_four_digits=last_four_digits,
                expiry_month=card_data.get("expiry_month"),
                expiry_year=card_data.get("expiry_year"),
                cardholder_name=card_data.get("cardholder_name", "").strip(),
                is_default=card_data.get("is_default", False),
                provider="payme",
                fingerprint=fingerprint,
                is_verified=False,
            )

            if credit_card.is_default:
                CreditCard.query.filter_by(user_id=user_id, is_default=True, is_active=True).update(
                    {"is_default": False}
                )

            db.session.add(credit_card)
            db.session.commit()

            audit_logger.log_event(
                event_type=AuditEventType.SENSITIVE_DATA_ACCESS,
                action="credit_card_saved",
                severity=AuditSeverity.MEDIUM,
                resource_type="credit_card",
                resource_id=str(credit_card.id),
                description=f"Credit card saved for user {user_id}",
                additional_data={
                    "card_brand": card_brand,
                    "last_four_digits": last_four_digits,
                    "is_default": credit_card.is_default,
                    "fingerprint": fingerprint[:8],
                    "is_tokenized": is_tokenized,
                    "user_id": user_id,
                },
            )

            current_app.logger.info(f"Credit card saved successfully for user {user_id}")
            return credit_card

        except (ValidationError, NotFoundError):
            raise
        except Exception:
            current_app.logger.exception("Unexpected error saving card for user %s", user_id)
            db.session.rollback()
            raise PaymentError(get_translation("error.payment.card_save_failed"))

    def get_user_cards(self, user_id: int, include_expired: bool = False) -> List[CreditCard]:
        """
        Get all active credit cards for a user

        Args:
            user_id: User ID
            include_expired: Whether to include expired cards (default: False)

        Returns:
            List of CreditCard objects ordered by default status then creation date
        """
        query = CreditCard.query.filter_by(user_id=user_id, is_active=True)

        if not include_expired:
            current_date = datetime.now(timezone.utc)
            query = query.filter(
                db.or_(
                    CreditCard.expiry_year > current_date.year,
                    db.and_(CreditCard.expiry_year == current_date.year, CreditCard.expiry_month >= current_date.month),
                )
            )

        return query.order_by(CreditCard.is_default.desc(), CreditCard.created_at.desc()).all()

    def get_default_card(self, user_id: int) -> Optional[CreditCard]:
        """
        Get user's default payment card

        Args:
            user_id: User ID

        Returns:
            CreditCard object or None if no default card
        """
        current_date = datetime.now(timezone.utc)

        card = (
            CreditCard.query.filter_by(user_id=user_id, is_default=True, is_active=True)
            .filter(
                db.or_(
                    CreditCard.expiry_year > current_date.year,
                    db.and_(CreditCard.expiry_year == current_date.year, CreditCard.expiry_month >= current_date.month),
                )
            )
            .first()
        )

        if not card:
            card = (
                CreditCard.query.filter_by(user_id=user_id, is_active=True)
                .filter(
                    db.or_(
                        CreditCard.expiry_year > current_date.year,
                        db.and_(
                            CreditCard.expiry_year == current_date.year, CreditCard.expiry_month >= current_date.month
                        ),
                    )
                )
                .order_by(CreditCard.created_at.desc())
                .first()
            )

        return card

    def set_default_card(self, card_id: int, user_id: int) -> CreditCard:
        """
        Set a card as the default payment method

        Args:
            card_id: Card ID
            user_id: User ID (for security)

        Returns:
            CreditCard: Updated card object

        Raises:
            NotFoundError: If card not found
            ValidationError: If card is expired
        """
        card = CreditCard.query.filter_by(id=card_id, user_id=user_id, is_active=True).first()

        if not card:
            raise NotFoundError(get_translation("error.not_found"))

        current_date = datetime.now(timezone.utc)
        is_expired = card.expiry_year < current_date.year or (
            card.expiry_year == current_date.year and card.expiry_month < current_date.month
        )

        if is_expired:
            raise ValidationError(get_translation("error.validation.card_expired"))

        CreditCard.query.filter_by(user_id=user_id, is_default=True, is_active=True).update({"is_default": False})

        card.is_default = True
        db.session.commit()

        audit_logger.log_event(
            event_type=AuditEventType.SENSITIVE_DATA_ACCESS,
            action="credit_card_set_as_default",
            severity=AuditSeverity.LOW,
            resource_type="credit_card",
            resource_id=str(card.id),
            description=f"Card ending in {card.last_four_digits} set as default",
            additional_data={
                "card_brand": card.card_brand,
                "last_four_digits": card.last_four_digits,
                "user_id": user_id,
            },
        )

        return card

    def delete_card(self, card_id: int, user_id: int) -> bool:
        """
        Delete (deactivate) a credit card

        Args:
            card_id: Card ID
            user_id: User ID (for security)

        Returns:
            bool: True if successful

        Raises:
            NotFoundError: If card not found
            ValidationError: If card cannot be deleted
        """
        card = CreditCard.query.filter_by(id=card_id, user_id=user_id, is_active=True).first()

        if not card:
            raise NotFoundError(get_translation("error.not_found"))

        if card.is_default:
            other_cards_count = (
                CreditCard.query.filter_by(user_id=user_id, is_active=True).filter(CreditCard.id != card_id).count()
            )

            if other_cards_count == 0:
                raise ValidationError(get_translation("error.validation.cannot_delete_last_card"))

            if other_cards_count > 0:
                next_card = (
                    CreditCard.query.filter_by(user_id=user_id, is_active=True).filter(CreditCard.id != card_id).first()
                )
                next_card.is_default = True

        # Call Payme verify API if needed to remove token invalidation
        # For now, we assume removal is successful and just deactivate locally
        # Ideally: self._payme_provider._payme_request('cards.remove', {'token': card.card_token})

        card.is_active = False
        card.deleted_at = datetime.now(timezone.utc)

        db.session.commit()

        audit_logger.log_event(
            event_type=AuditEventType.SENSITIVE_DATA_ACCESS,
            action="credit_card_deleted",
            severity=AuditSeverity.MEDIUM,
            resource_type="credit_card",
            resource_id=str(card.id),
            description=f"Credit card deleted for user {user_id}",
            additional_data={
                "card_brand": card.card_brand,
                "last_four_digits": card.last_four_digits,
                "user_id": user_id,
            },
        )

        return True

    def create_card_token(self, number: str, expire: str, save: bool = False) -> Dict[str, Any]:
        """
        Create card token via Payme API

        Args:
            number: Card number (16 digits)
            expire: Expiration date (MMYY or similar)
            save: Whether to save the card

        Returns:
            Dict containing token and card metadata
        """
        response = self._payme_provider._payme_request(
            "cards.create", {"card": {"number": number, "expire": expire}, "save": save}
        )

        if "error" in response:
            raise PaymentError(f"Payme tokenization failed: {response['error'].get('message')}, response: {response}")

        result = response.get("result", {}).get("card", {})
        if not result.get("token"):
            raise PaymentError("Payme did not return a token")

        return result

    def get_provider_for_brand(self, card_brand: str) -> str:
        """
        Get payment provider for card brand

        Args:
            card_brand: Card brand

        Returns:
            str: Provider name
        """
        provider_mapping = {"uzcard": "uzcard", "humo": "humo", "visa": "payme", "mastercard": "payme"}

        return provider_mapping.get(card_brand, "payme")
