"""The try-out pickup overview must render the card that was TAPPED.

WHY THIS FILE EXISTS
--------------------
``show_pickup_overview`` is reached by ``staff_tryout_pickup_back_<task_id>``,
so the callback names its subject. The handler ignored it: it rendered whatever
``context.user_data['tryout_pickup_state']`` happened to hold.

``user_data`` holds ONE pickup state for a driver who can have two try-out cards
on screen. A driver mid-pickup for try-out A who taps Back on try-out B's older
card was therefore shown A's product list and A's outstanding counts under B's
card — and the buttons redrawn there carry A's ids, so the counts they then
record go to A. The recorded quantities are bottles.

``edit_pickup_product``, the very next method in the same class, already does
this correctly: it parses the id off the callback and refuses when the loaded
state is for a different task. This asserts the overview does the same.
"""

from __future__ import annotations

import inspect
import re

from staff_bot.handlers.tryouts import TryoutHandler


def test_the_overview_refuses_a_state_belonging_to_a_different_task():
    source = inspect.getsource(TryoutHandler.show_pickup_overview)

    assert re.search(r"query\.data", source), (
        "show_pickup_overview does not look at the callback at all, so it "
        "cannot tell which card was tapped"
    )
    assert re.search(r"state\.get\(['\"]task_id['\"]\)", source), (
        "show_pickup_overview does not compare the loaded state against the "
        "tapped task — a driver with two pickup cards open can be shown one "
        "try-out's bottle counts under the other's card, and record against it"
    )
