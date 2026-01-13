"""
Pydantic schemas for bot state validation.

Bot state is stored as JSON in the users.bot_state column.
These schemas provide validation, IDE support, and clear documentation
of the expected state structure.
"""

from typing import Optional, Literal
from pydantic import BaseModel, Field, validator
from datetime import datetime


class BotState(BaseModel):
    """
    Schema for validating bot conversation state.
    
    The bot stores temporary conversation context in users.bot_state as JSON.
    This schema defines the valid structure and provides type safety.
    
    States are typically cleared after handler completion, cancel, or error.
    """
    
    # Input awaiting type - determines which handler processes user input
    awaiting_input: Optional[Literal[
        'profile_edit',           # Editing user profile
        'address_location',       # Waiting for location share
        'address_title',          # Naming a new address
        'edit_address_title',     # Editing address name
        'edit_address_instructions',  # Editing delivery instructions
        'search_products',        # Product search query
        'support_message',        # Support message content
    ]] = None
    
    # Address-related state
    address_id: Optional[int] = Field(None, description="ID of address being edited")
    
    class Config:
        extra = 'allow'  # Allow additional fields for forward compatibility


def validate_bot_state(state_dict: dict) -> Optional[BotState]:
    """
    Validate a bot state dictionary against the schema.
    
    Returns validated BotState or None if validation fails (with graceful fallback).
    Invalid states are logged but not rejected to prevent user disruption.
    
    Args:
        state_dict: Dictionary loaded from JSON bot_state
        
    Returns:
        BotState if valid, None if invalid/empty
    """
    if not state_dict:
        return None
    
    try:
        return BotState(**state_dict)
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.warning(f"Invalid bot state schema: {e}. State: {state_dict}")
        # Return a minimal valid state to allow recovery
        return BotState()


def clear_bot_state() -> dict:
    """Return empty state dict for clearing bot state."""
    return {}
