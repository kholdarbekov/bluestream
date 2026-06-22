"""Leaf eligibility helpers for the Telegram bot.

Provides two thin functions consumed by menu-rendering and handler guard code:

- is_loyalty_eligible(telegram_id)  — async; delegates to BotUserRepository
- main_menu_for(telegram_id, language) — async; returns the correct main-menu
  keyboard depending on eligibility.  The show_loyalty parameter it passes to
  MenuKeyboards.main_menu is added in Task 8; do not call this function until
  Task 8 has landed.
"""

from database import BotUserRepository, db_manager


async def is_loyalty_eligible(telegram_id: int) -> bool:
    """Return True if the user is eligible for the loyalty programme.

    Delegates to BotUserRepository which runs LOYALTY_ELIGIBLE_SQL against
    the live DB.  Unknown users (no row) default to True (open).
    """
    return await BotUserRepository(db_manager).get_user_loyalty_eligible(telegram_id)


async def main_menu_for(telegram_id, language: str):
    """Return the correct main-menu keyboard for this user.

    show_loyalty is set to the user's eligibility result, or True when
    telegram_id is None (unauthenticated context).

    NOTE: MenuKeyboards.main_menu must accept a show_loyalty keyword argument
    (added in Task 8).  Do not call this function before Task 8 lands.
    """
    # Lazy import avoids circular-import issues at module load time.
    from keyboards import MenuKeyboards

    if telegram_id is None:
        show_loyalty = True
    else:
        show_loyalty = await is_loyalty_eligible(telegram_id)

    return MenuKeyboards.main_menu(language, show_loyalty=show_loyalty)
