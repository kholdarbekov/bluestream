"""
Bot schemas package.

Provides Pydantic models for validating bot-related data structures.
"""

from .bot_state import BotState, validate_bot_state, clear_bot_state

__all__ = ['BotState', 'validate_bot_state', 'clear_bot_state']
