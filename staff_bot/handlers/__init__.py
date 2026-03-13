"""
Staff Bot Handlers
"""
from staff_bot.handlers.start import StartHandler
from staff_bot.handlers.menu import menu_handler, main_menu_handler
from staff_bot.handlers.language import LanguageHandler

start_handler = StartHandler()
language_handler = LanguageHandler()

__all__ = [
    'start_handler',
    'menu_handler',
    'main_menu_handler',
    'language_handler',
]
