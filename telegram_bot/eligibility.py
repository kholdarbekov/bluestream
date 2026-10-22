"""Leaf eligibility helpers for the Telegram bot.

- is_loyalty_eligible(telegram_id) — async; delegates to BotUserRepository.
- main_menu_for(telegram_id, language) — async; the bottom keyboard this customer
  should see, with Aqua Club only when they are loyalty-eligible. Its one caller is
  `menu_keyboard.send_menu_keyboard`, the only sender of that keyboard.
"""

from database import BotUserRepository, db_manager


async def is_loyalty_eligible(telegram_id: int) -> bool:
    """Return True if the user is eligible for the loyalty programme.

    Delegates to BotUserRepository which runs LOYALTY_ELIGIBLE_SQL against
    the live DB.  Unknown users (no row) default to True (open).
    """
    return await BotUserRepository(db_manager).get_user_loyalty_eligible(telegram_id)


async def main_menu_for(telegram_id, language: str):
    """The bottom keyboard for this customer: Aqua Club only when they are eligible.

    `telegram_id=None` (no customer to ask about) shows it. Send the result only through
    `menu_keyboard.send_menu_keyboard`, which keeps the install marker true.
    """
    # Lazy import avoids circular-import issues at module load time.
    from keyboards import MenuKeyboards

    if telegram_id is None:
        show_loyalty = True
    else:
        show_loyalty = await is_loyalty_eligible(telegram_id)

    return MenuKeyboards.bottom_menu(language, show_loyalty=show_loyalty)
