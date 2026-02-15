"""
Staff Bot Handlers
"""
from handlers.start import StartHandler
from handlers.menu import menu_handler, main_menu_handler
from handlers.language import LanguageHandler

start_handler = StartHandler()
language_handler = LanguageHandler()

__all__ = [
    'start_handler',
    'menu_handler',
    'main_menu_handler',
    'language_handler',
]
