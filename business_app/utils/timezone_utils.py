"""
Timezone utilities for consistent datetime handling across the application
"""
from datetime import datetime, timezone
from typing import Optional, Union
import pytz
from flask import current_app, g, request
import logging

logger = logging.getLogger(__name__)


def get_utc_now() -> datetime:
    """
    Get current UTC datetime with timezone info
    
    Returns:
        datetime: Current UTC datetime
    """
    return datetime.now(timezone.utc)


def get_user_timezone() -> pytz.BaseTzInfo:
    """
    Get user's preferred timezone from session, headers, or default
    
    Returns:
        pytz.BaseTzInfo: User's timezone
    """
    try:
        # Try to get timezone from user session/profile
        if hasattr(g, 'current_user') and g.current_user and hasattr(g.current_user, 'timezone'):
            user_tz = g.current_user.timezone
            if user_tz and user_tz in current_app.config.get('ALLOWED_TIMEZONES', []):
                return pytz.timezone(user_tz)
        
        # Try to get from request headers (e.g., X-Timezone)
        if request and hasattr(request, 'headers'):
            tz_header = request.headers.get('X-Timezone')
            if tz_header and tz_header in current_app.config.get('ALLOWED_TIMEZONES', []):
                return pytz.timezone(tz_header)
        
        # Fall back to configured display timezone
        display_tz = current_app.config.get('DISPLAY_TIMEZONE', 'Asia/Tashkent')
        return pytz.timezone(display_tz)
        
    except Exception as e:
        logger.warning(f"Error getting user timezone, using default: {e}")
        return pytz.timezone('Asia/Tashkent')


def utc_to_local(dt: datetime, target_tz: Optional[Union[str, pytz.BaseTzInfo]] = None) -> datetime:
    """
    Convert UTC datetime to local timezone
    
    Args:
        dt: UTC datetime (should be timezone-aware)
        target_tz: Target timezone (string or pytz timezone object)
    
    Returns:
        datetime: Datetime in target timezone
    """
    try:
        # Ensure datetime is timezone-aware UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        elif dt.tzinfo != timezone.utc:
            dt = dt.astimezone(timezone.utc)
        
        # Determine target timezone
        if target_tz is None:
            target_tz = get_user_timezone()
        elif isinstance(target_tz, str):
            target_tz = pytz.timezone(target_tz)
        
        # Convert to target timezone
        return dt.astimezone(target_tz)
        
    except Exception as e:
        logger.error(f"Error converting UTC to local time: {e}")
        return dt


def local_to_utc(dt: datetime, source_tz: Optional[Union[str, pytz.BaseTzInfo]] = None) -> datetime:
    """
    Convert local datetime to UTC
    
    Args:
        dt: Local datetime (may be naive or timezone-aware)
        source_tz: Source timezone (string or pytz timezone object)
    
    Returns:
        datetime: UTC datetime
    """
    try:
        # If datetime is naive, assume it's in the source timezone
        if dt.tzinfo is None:
            if source_tz is None:
                source_tz = get_user_timezone()
            elif isinstance(source_tz, str):
                source_tz = pytz.timezone(source_tz)
            
            # Localize the naive datetime
            dt = source_tz.localize(dt)
        
        # Convert to UTC
        return dt.astimezone(timezone.utc)
        
    except Exception as e:
        logger.error(f"Error converting local to UTC time: {e}")
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def format_datetime_for_user(dt: datetime, format_string: Optional[str] = None, 
                           timezone_tz: Optional[Union[str, pytz.BaseTzInfo]] = None) -> str:
    """
    Format datetime for user display in their timezone
    
    Args:
        dt: Datetime to format (should be UTC)
        format_string: Custom format string
        timezone_tz: Target timezone for display
    
    Returns:
        str: Formatted datetime string
    """
    try:
        # Convert to user timezone
        local_dt = utc_to_local(dt, timezone_tz)
        
        # Use default format if none specified
        if format_string is None:
            format_string = current_app.config.get('DATETIME_FORMAT', '%Y-%m-%d %H:%M:%S %Z')
        
        return local_dt.strftime(format_string)
        
    except Exception as e:
        logger.error(f"Error formatting datetime for user: {e}")
        return str(dt)


def parse_user_datetime(dt_string: str, format_string: Optional[str] = None,
                       source_tz: Optional[Union[str, pytz.BaseTzInfo]] = None) -> datetime:
    """
    Parse user input datetime string to UTC datetime
    
    Args:
        dt_string: Datetime string from user
        format_string: Format string for parsing
        source_tz: Source timezone (user's timezone)
    
    Returns:
        datetime: UTC datetime
    """
    try:
        # Use default format if none specified
        if format_string is None:
            # Try common formats
            formats = [
                '%Y-%m-%d %H:%M:%S',
                '%Y-%m-%d %H:%M',
                '%Y-%m-%d',
                '%Y-%m-%dT%H:%M:%S',
                '%Y-%m-%dT%H:%M:%S.%f',
                '%Y-%m-%dT%H:%M:%S.%fZ'
            ]
            
            dt = None
            for fmt in formats:
                try:
                    dt = datetime.strptime(dt_string, fmt)
                    break
                except ValueError:
                    continue
            
            if dt is None:
                raise ValueError(f"Unable to parse datetime string: {dt_string}")
        else:
            dt = datetime.strptime(dt_string, format_string)
        
        # Convert to UTC
        return local_to_utc(dt, source_tz)
        
    except Exception as e:
        logger.error(f"Error parsing user datetime: {e}")
        raise ValueError(f"Invalid datetime format: {dt_string}")


def ensure_utc(dt: datetime) -> datetime:
    """
    Ensure datetime is in UTC timezone
    
    Args:
        dt: Datetime object
    
    Returns:
        datetime: UTC datetime
    """
    if dt.tzinfo is None:
        # Naive datetime - assume UTC
        return dt.replace(tzinfo=timezone.utc)
    elif dt.tzinfo != timezone.utc:
        # Convert to UTC
        return dt.astimezone(timezone.utc)
    else:
        # Already UTC
        return dt


def is_aware(dt: datetime) -> bool:
    """
    Check if datetime is timezone-aware
    
    Args:
        dt: Datetime object
    
    Returns:
        bool: True if timezone-aware
    """
    return dt.tzinfo is not None and dt.tzinfo.utcoffset(dt) is not None


def make_aware(dt: datetime, tz: Optional[Union[str, pytz.BaseTzInfo]] = None) -> datetime:
    """
    Make naive datetime timezone-aware
    
    Args:
        dt: Naive datetime
        tz: Timezone to assign
    
    Returns:
        datetime: Timezone-aware datetime
    """
    if is_aware(dt):
        return dt
    
    if tz is None:
        tz = get_user_timezone()
    elif isinstance(tz, str):
        tz = pytz.timezone(tz)
    
    return tz.localize(dt)


def get_business_hours_for_timezone(tz: Optional[Union[str, pytz.BaseTzInfo]] = None) -> dict:
    """
    Get business hours configuration for a specific timezone
    
    Args:
        tz: Target timezone
    
    Returns:
        dict: Business hours configuration
    """
    # Default business hours (in local time)
    default_hours = {
        'monday': {'open': '09:00', 'close': '18:00'},
        'tuesday': {'open': '09:00', 'close': '18:00'},
        'wednesday': {'open': '09:00', 'close': '18:00'},
        'thursday': {'open': '09:00', 'close': '18:00'},
        'friday': {'open': '09:00', 'close': '18:00'},
        'saturday': {'open': '10:00', 'close': '16:00'},
        'sunday': {'open': None, 'close': None}  # Closed
    }
    
    return default_hours


def is_business_hours(dt: Optional[datetime] = None, 
                     tz: Optional[Union[str, pytz.BaseTzInfo]] = None) -> bool:
    """
    Check if given datetime is within business hours
    
    Args:
        dt: Datetime to check (defaults to now)
        tz: Timezone to check against
    
    Returns:
        bool: True if within business hours
    """
    try:
        if dt is None:
            dt = get_utc_now()
        
        # Convert to target timezone
        local_dt = utc_to_local(dt, tz)
        
        # Get business hours
        business_hours = get_business_hours_for_timezone(tz)
        
        # Get day of week (0=Monday, 6=Sunday)
        weekday = local_dt.weekday()
        day_names = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
        day_name = day_names[weekday]
        
        day_hours = business_hours.get(day_name, {})
        open_time = day_hours.get('open')
        close_time = day_hours.get('close')
        
        # Check if business is open on this day
        if not open_time or not close_time:
            return False
        
        # Parse business hours
        open_hour, open_minute = map(int, open_time.split(':'))
        close_hour, close_minute = map(int, close_time.split(':'))
        
        # Check if current time is within business hours
        current_time = local_dt.time()
        open_time_obj = local_dt.replace(hour=open_hour, minute=open_minute, second=0, microsecond=0).time()
        close_time_obj = local_dt.replace(hour=close_hour, minute=close_minute, second=0, microsecond=0).time()
        
        return open_time_obj <= current_time <= close_time_obj
        
    except Exception as e:
        logger.error(f"Error checking business hours: {e}")
        return True  # Default to allowing operations


# Template filters for Jinja2
def setup_timezone_filters(app):
    """
    Setup timezone-related template filters
    
    Args:
        app: Flask application instance
    """
    
    @app.template_filter('user_datetime')
    def user_datetime_filter(dt, format_string=None):
        """Format datetime for user display"""
        if not dt:
            return ''
        return format_datetime_for_user(dt, format_string)
    
    @app.template_filter('user_date')
    def user_date_filter(dt):
        """Format date for user display"""
        if not dt:
            return ''
        return format_datetime_for_user(dt, app.config.get('DATE_FORMAT', '%Y-%m-%d'))
    
    @app.template_filter('user_time')
    def user_time_filter(dt):
        """Format time for user display"""
        if not dt:
            return ''
        return format_datetime_for_user(dt, app.config.get('TIME_FORMAT', '%H:%M:%S'))
    
    @app.template_filter('relative_time')
    def relative_time_filter(dt):
        """Format relative time (e.g., '2 hours ago')"""
        if not dt:
            return ''
        
        try:
            now = get_utc_now()
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            
            diff = now - dt
            
            if diff.days > 0:
                return f"{diff.days} day{'s' if diff.days != 1 else ''} ago"
            elif diff.seconds > 3600:
                hours = diff.seconds // 3600
                return f"{hours} hour{'s' if hours != 1 else ''} ago"
            elif diff.seconds > 60:
                minutes = diff.seconds // 60
                return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
            else:
                return "Just now"
                
        except Exception as e:
            logger.error(f"Error formatting relative time: {e}")
            return str(dt)