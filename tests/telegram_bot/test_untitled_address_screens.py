"""An address saved without a title, on the customer bot's address screens (address-label contract, 2026-11-17).

A title is optional where addresses are written (the admin panel's form, `POST /api/v1/addresses`), and
`UserAddress.to_dict` always sends the `title` key, so a NULL title arrives as `"title": None`. Every screen below
read it with `.get('title', fallback)`, which never falls back when the key is there: the customer saw "None" (and
the delete confirmation's English "Untitled"/"N/A" were never translated). The list and its buttons now print the
backend's `display_label` (the title, else the street line); the detail, delete and rename screens, which print the
street line on a line of their own, name an untitled address with the seeded `telegram.address.untitled`.

Driven through the real PTB dispatcher with the real seeded uz copy; the rows are built by the producer's own
serializer (`address_row`), never hand-written.
"""

import pytest

import bot as bot_module  # noqa: F401  (resolves the bot's own modules first)

# AFTER the bot import, for the sys.path reason test_agent_order_callbacks.py documents.
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS  # noqa: E402

from tests.integration.address_row_builders import address_row
from tests.telegram_bot.ptb_harness import build_bot_harness

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

TITLED_ID = 901
UNTITLED_ID = 902
TITLED_STREET = "Bunyodkor 15, Chilonzor"
UNTITLED_STREET = "Amir Temur 7, Mirobod"

# Every seeded `telegram.*` row, in every language: the screens read the real copy.
TRANSLATIONS = {
    (language, key): row[language]
    for key, row in BACKEND_TRANSLATIONS.items()
    if key.startswith("telegram.")
    for language in ("uz", "ru", "en")
    if row.get(language)
}
UNTITLED = BACKEND_TRANSLATIONS["telegram.address.untitled"]["uz"]  # the customer's language: uz


@pytest.fixture
async def bot(monkeypatch):
    harness = await build_bot_harness(monkeypatch, translations=TRANSLATIONS)
    harness.backend.addresses.update({
        TITLED_ID: address_row(TITLED_ID, title="Uy", full_address=TITLED_STREET, city="Toshkent"),
        UNTITLED_ID: address_row(UNTITLED_ID, title=None, full_address=UNTITLED_STREET, city="Toshkent"),
    })
    return harness


@pytest.fixture
def user(bot):
    return bot.updates()


def _never_unnamed(text):
    for leak in ("None", "Untitled", "N/A"):
        assert leak not in text, f"{leak!r} reached the customer: {text!r}"


async def test_the_address_list_names_an_untitled_address_by_its_street_line(bot, user):
    await bot.send(user.tap("manage_addresses"))

    listing = bot.telegram.last_shown()
    _never_unnamed(listing.text)
    assert listing.button_labels()[:2] == ["📍 Uy", f"📍 {UNTITLED_STREET}"]
    assert listing.callback_data()[:2] == [f"view_address_{TITLED_ID}", f"view_address_{UNTITLED_ID}"]
    assert f"📍 Uy\n   {TITLED_STREET}\n" in listing.text
    assert listing.text.count(UNTITLED_STREET) == 1, "the street line is the untitled address's name; once is enough"


async def test_the_detail_delete_and_rename_screens_call_an_untitled_address_untitled(bot, user):
    await bot.send(user.tap(f"view_address_{UNTITLED_ID}"))
    detail = bot.telegram.last_shown()
    _never_unnamed(detail.text)
    assert UNTITLED in detail.text and UNTITLED_STREET in detail.text

    await bot.send(user.tap(f"delete_address_{UNTITLED_ID}"))
    confirm = bot.telegram.last_shown()
    _never_unnamed(confirm.text)
    assert UNTITLED in confirm.text and UNTITLED_STREET in confirm.text

    await bot.send(user.tap(f"edit_title_{UNTITLED_ID}"))
    prompt = bot.telegram.last_shown()
    _never_unnamed(prompt.text)
    assert UNTITLED in prompt.text
