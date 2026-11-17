"""The COD debt-limit Telegram warning speaks the customer's language and says so'm / сум, never UZS (Task X2).

Real path: `StaffService._notify_customer_cod_debt_limit` -> the real `NotificationService.send_notification`
(language resolution included) -> the Telegram channel. Only the last hop (the Telegram HTTP call) is replaced
by a recorder with the real signature, which renders the template the way the real sender does.
"""

from decimal import Decimal

import pytest

from business_app.models.translation import Translation
from business_app.services.cash_collection_service import CashCollectionService
from business_app.services.notification_service import NotificationService
from business_app.services.staff_service import StaffService
from business_app.utils.translations import get_translation
from scripts.seed_prepayment_translations import PREPAYMENT_TRANSLATIONS
from shared.business_config import COD_DEBT_AMOUNT_THRESHOLD
from tests.unit._scope_money_helpers import delivered_cod_order

pytestmark = pytest.mark.integration

_LIMIT = CashCollectionService.COD_ACTIVE_DEBT_LIMIT
_SUBJECT_KEY = "telegram.orders.cod_restricted_subject"
_BODY_KEY = "telegram.orders.cod_restricted_person"
_WORD = {"en": "so'm", "uz": "so'm", "ru": "сум"}


@pytest.fixture
def seeded_copy(db):
    """The seeded rows, as the seed script writes them (the DB is the copy's only home)."""
    for key in (_SUBJECT_KEY, _BODY_KEY):
        for language, value in PREPAYMENT_TRANSLATIONS[key].items():
            db.session.add(
                Translation(key=key, language=language, value=value, category="telegram", is_active=True)
            )
    db.session.commit()


@pytest.fixture
def sent(monkeypatch):
    """Records what `send_notification` would send, with its real signature.

    The suite's autouse guard already replaces `send_notification` (no real delivery); this recorder replaces
    that stand-in. It resolves the language the way the real method does (the user's stored preference through
    the real `_normalize_language_code`) and renders the override's subject and body for it.
    """
    out = []

    def record(
        self,
        user_id,
        notification_type,
        channels=None,
        template_data=None,
        priority="normal",
        template_override=None,
        campaign_id=None,
        email_headers=None,
        language=None,
    ):
        user = db_user(user_id)
        resolved = self._normalize_language_code(language or getattr(user, "preferred_language", "en"))
        out.append(
            {
                "user_id": user_id,
                "channels": [c.value for c in channels],
                "language": resolved,
                "subject": template_override.get_translated("subject", resolved),
                "body": template_override.get_translated("content", resolved),
            }
        )
        return {"telegram": {"success": True}}

    def db_user(user_id):
        from business_app.models.user import User

        return User.query.get(user_id)

    monkeypatch.setattr(NotificationService, "send_notification", record)
    return out


def _restricted_customer(db, user, language):
    user.telegram_id = 424242
    user.preferred_language = language
    db.session.commit()
    each = Decimal(COD_DEBT_AMOUNT_THRESHOLD) + Decimal("1000.00")
    for _ in range(_LIMIT):
        delivered_cod_order(db, user, total=each)
    return each * _LIMIT


@pytest.mark.parametrize("language", ["en", "uz", "ru"])
def test_the_warning_speaks_the_customers_language(db, sample_user, seeded_copy, sent, language):
    net_total = _restricted_customer(db, sample_user, language)

    StaffService._notify_customer_cod_debt_limit(sample_user.id)

    assert len(sent) == 1
    message = sent[0]
    assert message["language"] == language
    assert message["user_id"] == sample_user.id
    assert message["channels"] == ["telegram"]
    assert message["subject"] == get_translation(_SUBJECT_KEY, language)
    assert message["body"] == get_translation(
        _BODY_KEY,
        language,
        net_debt_total=f"{net_total:,.0f}",
        threshold=f"{COD_DEBT_AMOUNT_THRESHOLD:,.0f}",
    )
    assert _WORD[language] in message["body"]
    assert "UZS" not in message["subject"] + message["body"]
    assert "{" not in message["body"]


def test_a_regional_language_code_still_reads_in_its_language(db, sample_user, seeded_copy, sent):
    _restricted_customer(db, sample_user, "uz-UZ")

    StaffService._notify_customer_cod_debt_limit(sample_user.id)

    assert sent[0]["subject"] == get_translation(_SUBJECT_KEY, "uz")


def test_no_warning_without_a_person_restriction(db, sample_user, seeded_copy, sent):
    sample_user.telegram_id = 424242
    db.session.commit()
    for _ in range(_LIMIT):
        delivered_cod_order(db, sample_user, total=Decimal("280.00"))

    StaffService._notify_customer_cod_debt_limit(sample_user.id)

    assert sent == []
