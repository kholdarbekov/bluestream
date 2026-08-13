"""`PerChatSerialUpdateProcessor` must deliver BOTH halves of its promise.

Half one is the bug fix: two different staff members must not block each
other (the 2026-08-13 "staff bot is slow" report, where one slow handler
stalled every driver because PTB processes updates one at a time by default).

Half two is the safety property that lets us turn concurrency on at all:
a SINGLE chat's updates must still run strictly in order, because both bots
lean on `ConversationHandler` and PTB warns that concurrent processing is
unsafe with stateful handlers. If half two ever breaks we have traded a
bot-wide stall for corrupted conversation state in flows that move money and
bottles — strictly worse.

Both are tested by observing real interleaving of real coroutines, not by
asserting a lock object exists.
"""

import asyncio
from types import SimpleNamespace

import pytest

from shared.telegram_update_processor import PerChatSerialUpdateProcessor


def _update(chat_id, user_id=None):
    """Minimal stand-in exposing what the processor actually reads."""
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=user_id if user_id is not None else chat_id),
    )


@pytest.mark.unit
class TestDifferentChatsRunConcurrently:
    def test_a_slow_update_does_not_block_another_chat(self):
        """The actual bug: driver B waited on driver A."""
        processor = PerChatSerialUpdateProcessor(max_concurrent_updates=8)
        order = []

        async def slow():
            order.append("A-start")
            await asyncio.sleep(0.20)
            order.append("A-end")

        async def quick():
            order.append("B-start")
            await asyncio.sleep(0.01)
            order.append("B-end")

        async def scenario():
            await asyncio.gather(
                processor.do_process_update(_update(chat_id=1), slow()),
                processor.do_process_update(_update(chat_id=2), quick()),
            )

        asyncio.run(scenario())

        # B must finish while A is still running — that is the whole point.
        assert order == ["A-start", "B-start", "B-end", "A-end"], order


@pytest.mark.unit
class TestSameChatStaysStrictlyOrdered:
    def test_one_chats_updates_never_interleave(self):
        """ConversationHandler safety: no overlap within a single chat."""
        processor = PerChatSerialUpdateProcessor(max_concurrent_updates=8)
        order = []

        async def first():
            order.append("1-start")
            await asyncio.sleep(0.10)
            order.append("1-end")

        async def second():
            order.append("2-start")
            await asyncio.sleep(0.01)
            order.append("2-end")

        async def scenario():
            await asyncio.gather(
                processor.do_process_update(_update(chat_id=7), first()),
                processor.do_process_update(_update(chat_id=7), second()),
            )

        asyncio.run(scenario())

        assert order == ["1-start", "1-end", "2-start", "2-end"], order

    def test_the_ordering_guard_would_notice_interleaving(self):
        """Negative control.

        Without the per-chat lock the two coroutines above interleave. Proven
        here by running the same pair through the processor's parent
        behaviour (no lock at all), so the assertion in the test above is
        known to be load-bearing rather than accidentally true.
        """
        order = []

        async def first():
            order.append("1-start")
            await asyncio.sleep(0.10)
            order.append("1-end")

        async def second():
            order.append("2-start")
            await asyncio.sleep(0.01)
            order.append("2-end")

        asyncio.run(_gather(first(), second()))

        assert order == ["1-start", "2-start", "2-end", "1-end"], order


async def _gather(*coros):
    return await asyncio.gather(*coros)


@pytest.mark.unit
class TestBookkeepingDoesNotLeak:
    def test_locks_are_released_after_use(self):
        """One dict entry per chat ever seen would be a slow leak; this repo
        has already flagged that exact pattern once."""
        processor = PerChatSerialUpdateProcessor(max_concurrent_updates=4)

        async def noop():
            return None

        async def scenario():
            for chat_id in range(25):
                await processor.do_process_update(_update(chat_id=chat_id), noop())

        asyncio.run(scenario())

        assert processor._locks == {}, processor._locks
        assert processor._waiters == {}, processor._waiters

    def test_lock_survives_an_exception_and_is_still_cleaned_up(self):
        """A handler that raises must not wedge that chat forever."""
        processor = PerChatSerialUpdateProcessor(max_concurrent_updates=4)

        async def boom():
            raise RuntimeError("handler exploded")

        async def ok():
            return "fine"

        async def scenario():
            with pytest.raises(RuntimeError):
                await processor.do_process_update(_update(chat_id=99), boom())
            # the same chat must still be usable afterwards
            await processor.do_process_update(_update(chat_id=99), ok())

        asyncio.run(scenario())
        assert processor._locks == {}


@pytest.mark.unit
class TestUpdatesWithoutAChat:
    def test_chatless_updates_are_not_serialised_against_each_other(self):
        """Poll/service updates share no conversation, so funnelling them
        through one lock would reintroduce the very head-of-line blocking
        this class exists to remove."""
        processor = PerChatSerialUpdateProcessor(max_concurrent_updates=8)
        order = []

        async def slow():
            order.append("A-start")
            await asyncio.sleep(0.15)
            order.append("A-end")

        async def quick():
            order.append("B-start")
            await asyncio.sleep(0.01)
            order.append("B-end")

        chatless = SimpleNamespace(effective_chat=None, effective_user=None)

        async def scenario():
            await asyncio.gather(
                processor.do_process_update(chatless, slow()),
                processor.do_process_update(chatless, quick()),
            )

        asyncio.run(scenario())
        assert order == ["A-start", "B-start", "B-end", "A-end"], order
