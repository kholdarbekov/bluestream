"""`CustomerLinkService.notification_recipient`: which linked account a reminder goes to (spec §4, D19).

Pure: members are constructed, never loaded. The ranking is (1) connected, unblocked Telegram by
latest interaction, (2) unblocked with a deliverable email by oldest account, (3) any unblocked by
oldest, (4) blocked accounts last, each tie broken by the lowest id.
"""

from datetime import datetime, timedelta, timezone

import pytest

from business_app.services.customer_link_service import CustomerLinkService
from business_app.services.reorder_reminders.facts import MemberFacts

pytestmark = pytest.mark.unit

T0 = datetime(2025, 1, 1, tzinfo=timezone.utc)


def member(user_id, *, created_days=0, telegram=False, seen_days=None, email=None, blocked=False,
           status="active", staff=False, can_order=True):
    return MemberFacts(
        user_id=user_id,
        status=status,
        is_staff=staff,
        can_order=can_order,
        telegram_id=str(770000000 + user_id) if (telegram or blocked) else None,
        is_bot_active=telegram and not blocked,
        telegram_blocked_at=T0 + timedelta(days=300) if blocked else None,
        last_bot_interaction=T0 + timedelta(days=seen_days) if seen_days is not None else None,
        email=email,
        created_at=T0 + timedelta(days=created_days),
        preferred_language="uz",
        greeting_name="Ali",
    )


CASES = [
    ("a connected Telegram member beats an older member with a real email",
     [member(1, email="ali@example.com"), member(2, created_days=50, telegram=True)], 2),
    ("between Telegram members the latest interaction wins",
     [member(1, telegram=True, seen_days=10), member(2, telegram=True, seen_days=20)], 2),
    ("a Telegram member never seen ranks after one seen",
     [member(1, telegram=True), member(2, telegram=True, seen_days=1)], 2),
    ("equal interactions fall to the lowest id",
     [member(3, telegram=True, seen_days=5), member(2, telegram=True, seen_days=5)], 2),
    ("without Telegram the oldest deliverable email wins; placeholders do not count",
     [member(1, email="telegram_1@bot.internal"), member(2, created_days=5, email="Ali@Example.COM"),
      member(3, created_days=9, email="v@example.com")], 2),
    ("with no channel anywhere the oldest member is still the recipient",
     [member(4, created_days=3), member(5, created_days=1)], 5),
    ("D19: a blocked account loses to a sibling with a real email",
     [member(1, blocked=True, email="ali@example.com"), member(2, created_days=9, email="work@example.com")], 2),
    ("D19: a blocked account loses even to a sibling with no channel",
     [member(1, blocked=True), member(2, created_days=9)], 2),
    ("D19: when every member is blocked the oldest blocked one is returned",
     [member(7, created_days=3, blocked=True), member(6, created_days=1, blocked=True)], 6),
    ("an INACTIVE old primary is never the recipient",
     [member(1, telegram=True, status="inactive"), member(2, created_days=9)], 2),
    ("staff and accounts that cannot order are skipped",
     [member(1, telegram=True, staff=True), member(2, telegram=True, can_order=False), member(3, created_days=9)], 3),
    ("no candidate means no recipient",
     [member(1, status="banned"), member(2, staff=True)], None),
]


@pytest.mark.parametrize("label, members, expected", CASES, ids=[case[0] for case in CASES])
def test_notification_recipient(label, members, expected):
    chosen = CustomerLinkService.notification_recipient(members)

    assert (chosen.user_id if chosen else None) == expected
