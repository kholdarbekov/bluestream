"""`BotUserRepository.get_telegram_blocked_at`: the recovery middleware's one read (spec 2026-10-09
§7.2 b). Read-only: the backend's TelegramReachabilityService is the column's only writer."""

from datetime import datetime, timezone

import pytest

from database import BotUserRepository
from ptb_harness import DEFAULT_USER_ID, FakeDatabase

pytestmark = pytest.mark.anyio

BLOCKED_AT = datetime(2026, 10, 14, 9, 30, tzinfo=timezone.utc)


class RecordingDatabase(FakeDatabase):
    """The harness's fake `users` row, recording every scalar read the repository issues."""

    def __init__(self):
        super().__init__()
        self.reads = []

    async def fetchval(self, query, *args):
        self.reads.append((" ".join(query.split()), args))
        return await super().fetchval(query, *args)


async def test_a_blocked_customer_reads_back_the_block_instant():
    database = RecordingDatabase()
    database.user["telegram_blocked_at"] = BLOCKED_AT

    assert await BotUserRepository(database).get_telegram_blocked_at(DEFAULT_USER_ID) == BLOCKED_AT
    assert database.reads == [
        ("SELECT telegram_blocked_at FROM users WHERE telegram_id = $1", (str(DEFAULT_USER_ID),))
    ]


async def test_a_customer_not_held_as_blocked_reads_none():
    assert await BotUserRepository(RecordingDatabase()).get_telegram_blocked_at(DEFAULT_USER_ID) is None
