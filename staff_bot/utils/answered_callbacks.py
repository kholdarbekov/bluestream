"""Which callback queries this bot has already answered.

Telegram shows a user only the FIRST ``answerCallbackQuery`` for a tap. A second
answer is accepted (HTTP 200, ``True``) and dropped by the client — measured in
prod on 2026-09-10: a driver re-tapped a refused "Picked up" 20 times, the bot
logged no rejection, sent no fallback, and showed nothing. Most staff handlers
answer bare on entry to stop the spinner, so by the time an API error is known
the popup slot is usually spent.

``StaffExtBot`` is the one place every answer passes through (bare
``query.answer()`` included: ``CallbackQuery.answer`` delegates to
``Bot.answer_callback_query``). ``BaseHandler._notify_user`` asks it before
spending text on a popup nobody will see.
"""
from collections import OrderedDict

from telegram.ext import ExtBot

# A tap is answered within seconds of arriving; this only has to outlive one
# update. Bounded so a long-running bot does not grow without limit.
ANSWERED_CALLBACK_IDS_MAX = 4096


class StaffExtBot(ExtBot):
    """ExtBot that remembers the ids of callback queries it answered."""

    __slots__ = ("_answered_callback_ids",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # TelegramObjects are frozen after __init__; ExtBot sets its own
        # attributes the same way.
        with self._unfrozen():
            self._answered_callback_ids = OrderedDict()

    async def answer_callback_query(self, callback_query_id, *args, **kwargs):
        try:
            return await super().answer_callback_query(callback_query_id, *args, **kwargs)
        finally:
            # Also when Telegram refused ("query is too old"): a refused answer
            # leaves no popup slot either.
            self._remember(str(callback_query_id))

    def _remember(self, callback_query_id: str) -> None:
        ids = self._answered_callback_ids
        ids[callback_query_id] = None
        ids.move_to_end(callback_query_id)
        while len(ids) > ANSWERED_CALLBACK_IDS_MAX:
            ids.popitem(last=False)

    def was_answered(self, callback_query_id) -> bool:
        return str(callback_query_id) in self._answered_callback_ids


def build_staff_bot(token: str, *, request, get_updates_request) -> StaffExtBot:
    """The staff bot's Bot, for production and for the test harness alike."""
    return StaffExtBot(token=token, request=request, get_updates_request=get_updates_request)


def callback_already_answered(callback_query) -> bool:
    """True only when a ``StaffExtBot`` owns this tap and has answered it.

    Anything else — a unit test's MagicMock, a bot built elsewhere, a query with
    no bot — reads as NOT answered, which is the pre-existing behaviour.
    """
    try:
        bot = callback_query.get_bot()
    except RuntimeError:
        return False
    return isinstance(bot, StaffExtBot) and bot.was_answered(callback_query.id)
