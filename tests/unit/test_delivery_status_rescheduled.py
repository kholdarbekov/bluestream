"""The held `rescheduled` delivery status: the state model, every table keyed by status, the labels.

R3 of docs/superpowers/specs/2026-09-23-admin-order-reschedule-design.md: a delivery that was
released to drivers once and then moved to a later day waits in `rescheduled`. It never carries a
driver and is never claimable. `OrderScheduleService` writes it and releases it directly, so no
driver or admin transition may lead into it, and the order-cancel cascade is its only way out
through the transition table.

Each test reads the production table or resolver it is about; none restates one. The Postgres
half (the enum label, the CHECK, the Delivery page over HTTP, the migration's rollback guard) is
tests/integration/test_delivery_rescheduled_pg_e2e.py.
"""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from business_app.services.admin_delivery_service import AdminDeliveryService
from business_app.services.notification_service import NotificationService
from business_app.utils.exceptions import InvalidStateTransition
from business_app.utils.state_validators import (
    DELIVERY_DRIVERLESS_STATES,
    DELIVERY_POOL_UNASSIGNED_STATES,
    DELIVERY_REQUIRES_PERSON_STATES,
    assert_unassigned_for_pool_status,
)
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS
from shared.enums import DeliveryStatus
from shared.staff_constants import DELIVERY_STATUS_TRANSITIONS as STAFF_BOT_TRANSITIONS
from shared.status_transitions import DELIVERY_STATUS_TRANSITIONS

ROOT = Path(__file__).resolve().parents[2]
LANGUAGES = ("en", "uz", "ru")
HELD_LABEL = {"en": "Rescheduled", "uz": "Ko'chirildi", "ru": "Перенесён"}


@pytest.fixture(scope="module")
def staff_seed():
    spec = importlib.util.spec_from_file_location(
        "seed_staff_translations_rescheduled", ROOT / "scripts" / "seed_staff_translations.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestStateModel:
    def test_rescheduled_is_a_delivery_status(self):
        assert DeliveryStatus("rescheduled") is DeliveryStatus.RESCHEDULED

    def test_a_held_delivery_leaves_the_table_only_by_cancellation(self):
        assert DELIVERY_STATUS_TRANSITIONS[DeliveryStatus.RESCHEDULED] == [DeliveryStatus.CANCELLED]
        # The staff bot's string-keyed view is derived from the same table.
        assert STAFF_BOT_TRANSITIONS["rescheduled"] == ["cancelled"]

    def test_no_transition_leads_into_a_held_delivery(self):
        leading_in = sorted(
            current.value
            for current, successors in DELIVERY_STATUS_TRANSITIONS.items()
            if DeliveryStatus.RESCHEDULED in successors
        )

        assert leading_in == []

    def test_the_claimable_pool_is_unchanged_and_the_driverless_set_adds_the_held_status(self):
        assert DELIVERY_POOL_UNASSIGNED_STATES == frozenset({DeliveryStatus.SCHEDULED, DeliveryStatus.PENDING})
        assert DELIVERY_DRIVERLESS_STATES == DELIVERY_POOL_UNASSIGNED_STATES | {DeliveryStatus.RESCHEDULED}
        assert DeliveryStatus.RESCHEDULED not in DELIVERY_REQUIRES_PERSON_STATES

    def test_a_held_row_may_not_keep_its_driver(self):
        stale = SimpleNamespace(id=7, status=DeliveryStatus.IN_TRANSIT, delivery_person_id=22)

        with pytest.raises(InvalidStateTransition) as exc_info:
            assert_unassigned_for_pool_status(stale, DeliveryStatus.RESCHEDULED)

        assert (exc_info.value.to_state, exc_info.value.missing_field) == ("rescheduled", "delivery_person_id")
        stale.delivery_person_id = None
        assert_unassigned_for_pool_status(stale, DeliveryStatus.RESCHEDULED)


class TestAdminDeliveryTables:
    @pytest.mark.parametrize("status", list(DeliveryStatus), ids=lambda s: s.value)
    def test_every_status_is_a_valid_delivery_page_filter(self, status):
        assert AdminDeliveryService._normalize_status(status.value) is status

    def test_the_admin_transition_table_has_a_row_for_every_status(self):
        """A status with no row answers "Current delivery status is not supported" on every
        admin status change instead of listing what it may move to."""
        assert set(AdminDeliveryService.ADMIN_ALLOWED_TRANSITIONS) == set(DeliveryStatus)

    def test_a_held_row_has_no_admin_move_and_no_admin_move_leads_into_it(self):
        table = AdminDeliveryService.ADMIN_ALLOWED_TRANSITIONS

        assert table[DeliveryStatus.RESCHEDULED] == set()
        assert sorted(s.value for s, targets in table.items() if DeliveryStatus.RESCHEDULED in targets) == []


class TestLabels:
    @pytest.mark.parametrize("language", LANGUAGES)
    def test_every_status_has_a_bundled_customer_label(self, language):
        bundled = NotificationService.DELIVERY_STATUS_LABEL_FALLBACKS[language]

        assert sorted(s.value for s in DeliveryStatus if s.value not in bundled) == []

    @pytest.mark.parametrize("language", LANGUAGES)
    def test_the_seed_row_and_the_bundled_fallback_say_the_same(self, db, language):
        """No translation row exists here, so the real resolver falls through to the bundle."""
        label = NotificationService()._get_localized_delivery_status_label("rescheduled", language)

        assert label == HELD_LABEL[language]
        assert BACKEND_TRANSLATIONS["notification.delivery_status.rescheduled"][language] == HELD_LABEL[language]

    @pytest.mark.parametrize("language", LANGUAGES)
    def test_the_staff_bot_seeds_the_held_status_label(self, staff_seed, language):
        assert staff_seed._curated_value("staff.delivery.status.rescheduled", language) == HELD_LABEL[language]
