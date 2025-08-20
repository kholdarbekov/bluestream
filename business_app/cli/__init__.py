"""
CLI commands package for BlueStream
"""
from .config import init_app as init_config_commands
from .session_commands import register_session_commands


def init_app(app):
    """Initialize all CLI commands"""
    init_config_commands(app)
    register_session_commands(app)