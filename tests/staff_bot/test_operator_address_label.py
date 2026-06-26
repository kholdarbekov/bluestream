"""Operator add-address must persist the typed label under the 'title' key.

The backend (StaffService.add_client_address) reads address_data.get('title',
'Home') and the GET serializer exposes the address label from `title`. The bot
stored the typed label under 'label', so the backend defaulted every operator-
created address to 'Home' and the confirm screen (which reads 'title') showed
a blank label.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from staff_bot.handlers.operator.manage_address import (
    ENTER_ADDRESS,
    ManageAddressHandler,
)


def _handler():
    h = ManageAddressHandler.__new__(ManageAddressHandler)
    h._get_language = AsyncMock(return_value="en")
    return h


def _update(text):
    update = MagicMock()
    update.message = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.effective_user = MagicMock(id=5)
    update.callback_query = None
    return update


@pytest.mark.unit
def test_receive_label_stores_under_title_key():
    handler = _handler()
    update = _update("Office")
    ctx = MagicMock()
    ctx.user_data = {
        "language": "en", "authenticated": True, "staff_roles": ["operator"],
        "new_address": {},
    }

    state = asyncio.run(handler.receive_label(update, ctx))

    assert state == ENTER_ADDRESS
    # Must persist under 'title' (the key the backend + GET serializer use).
    assert ctx.user_data["new_address"].get("title") == "Office"
    # And must NOT leave a 'label' key the backend ignores (→ defaults to 'Home').
    assert "label" not in ctx.user_data["new_address"]
