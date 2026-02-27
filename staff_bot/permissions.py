"""
Role-based permission checks for the Staff Bot.
Decorator-based system to restrict handler access by staff role.
"""
import logging
from functools import wraps
from typing import List

from telegram import Update
from telegram.ext import ContextTypes

from shared.enums import UserRole
from shared.staff_constants import STAFF_BOT_ROLES

logger = logging.getLogger(__name__)


def _get_user_staff_roles(context: ContextTypes.DEFAULT_TYPE) -> List[str]:
    """Extract staff roles from context user_data."""
    return context.user_data.get('staff_roles', [])


def _get_user_language(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Get user language from context."""
    from i18n import i18n

    return i18n.normalize_language(context.user_data.get('language'))


async def _send_unauthorized(update: Update, message: str):
    """Send unauthorized message to user."""
    if update.callback_query:
        await update.callback_query.answer(message, show_alert=True)
    elif update.message:
        await update.message.reply_text(message)


def require_auth(func):
    """Require user to be authenticated (has valid token in context)."""
    @wraps(func)
    async def wrapper(self_or_update, *args, **kwargs):
        # Support both class methods (self, update, context) and functions (update, context)
        if isinstance(self_or_update, Update):
            update = self_or_update
            context = args[0] if args else kwargs.get('context')
        else:
            update = args[0] if args else kwargs.get('update')
            context = args[1] if len(args) > 1 else kwargs.get('context')

        if not context.user_data.get('authenticated'):
            from i18n import i18n
            lang = _get_user_language(context)
            await _send_unauthorized(update, i18n.get('staff.session_expired', lang))
            return

        return await func(self_or_update, *args, **kwargs)
    return wrapper


def require_role(*roles: str):
    """
    Decorator to restrict handler access by staff role.

    Usage:
        @require_role('delivery_driver')
        async def handle_pick_order(update, context):
            ...

        @require_role('delivery_driver', 'operator')
        async def handle_shared_action(update, context):
            ...
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(self_or_update, *args, **kwargs):
            # Support both class methods and functions
            if isinstance(self_or_update, Update):
                update = self_or_update
                context = args[0] if args else kwargs.get('context')
            else:
                update = args[0] if args else kwargs.get('update')
                context = args[1] if len(args) > 1 else kwargs.get('context')

            user_roles = _get_user_staff_roles(context)
            if not any(role in user_roles for role in roles):
                from i18n import i18n
                lang = _get_user_language(context)
                await _send_unauthorized(
                    update,
                    i18n.get('staff.unauthorized', lang)
                )
                logger.warning(
                    f"User {update.effective_user.id} attempted action "
                    f"requiring roles {roles} but has {user_roles}"
                )
                return

            return await func(self_or_update, *args, **kwargs)
        return wrapper
    return decorator


def require_delivery_driver(func):
    """Shorthand: require delivery_driver role."""
    return require_role('delivery_driver')(func)


def require_operator(func):
    """Shorthand: require operator role."""
    return require_role('operator')(func)


def require_any_staff_role(func):
    """Shorthand: any staff role (delivery_driver, operator)."""
    return require_role(*STAFF_BOT_ROLES)(func)


def has_role(context: ContextTypes.DEFAULT_TYPE, role: str) -> bool:
    """Check if user has a specific staff role."""
    return role in _get_user_staff_roles(context)


def is_delivery_driver(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if user is a delivery driver."""
    return has_role(context, 'delivery_driver')


def is_operator(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if user is an operator."""
    return has_role(context, 'operator')
