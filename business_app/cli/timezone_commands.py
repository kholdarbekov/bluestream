"""
CLI commands for timezone management
"""
import click
from flask import current_app
from flask.cli import with_appcontext
from datetime import datetime
import logging

from business_app import db
from business_app.utils.db_timezone import (
    create_timezone_aware_indexes, 
    validate_datetime_consistency,
    fix_naive_datetimes
)
from business_app.utils.timezone_utils import get_utc_now

logger = logging.getLogger(__name__)


@click.group()
def timezone():
    """Timezone management commands"""
    pass


@timezone.command()
@click.option('--check-only', is_flag=True, help='Only validate, do not fix issues')
@with_appcontext
def validate():
    """Validate timezone consistency in the database"""
    click.echo("🔍 Validating timezone consistency...")
    
    try:
        results = validate_datetime_consistency(db.session)
        
        click.echo(f"\n📊 Validation Results:")
        click.echo(f"   Total checks: {results['total_checks']}")
        click.echo(f"   Passed: {results['passed_checks']}")
        click.echo(f"   Failed: {results['failed_checks']}")
        
        if results['issues']:
            click.echo(f"\n⚠️  Issues found:")
            for issue in results['issues']:
                click.echo(f"   - {issue['check']}: {issue['issue']}")
        else:
            click.echo(f"\n✅ All timezone validation checks passed!")
        
        return results['failed_checks'] == 0
        
    except Exception as e:
        click.echo(f"❌ Error during validation: {e}")
        return False


@timezone.command()
@with_appcontext
def create_indexes():
    """Create timezone-aware database indexes"""
    click.echo("🔧 Creating timezone-aware database indexes...")
    
    try:
        create_timezone_aware_indexes(db.session)
        click.echo("✅ Timezone indexes created successfully!")
        return True
        
    except Exception as e:
        click.echo(f"❌ Error creating indexes: {e}")
        return False


@timezone.command()
@click.option('--table', help='Specific table to fix (optional)')
@click.option('--dry-run', is_flag=True, help='Show what would be fixed without making changes')
@with_appcontext
def fix_naive_datetimes_cmd(table, dry_run):
    """Fix naive datetimes in database tables"""
    
    if dry_run:
        click.echo("🔍 Dry run: Checking for naive datetimes...")
    else:
        click.echo("🔧 Fixing naive datetimes in database...")
    
    # Define tables and their datetime columns
    tables_config = {
        'users': ['created_at', 'updated_at', 'last_login', 'email_verified_at', 'phone_verified_at'],
        'orders': ['created_at', 'updated_at', 'confirmed_at', 'delivered_at', 'cancelled_at'],
        'payments': ['created_at', 'updated_at', 'paid_at', 'refunded_at'],
        'deliveries': ['created_at', 'updated_at', 'scheduled_at', 'completed_at'],
        'loyalty_transactions': ['created_at', 'updated_at', 'expires_at'],
        'notifications': ['created_at', 'updated_at', 'sent_at'],
        'audit_logs': ['created_at']
    }
    
    if table:
        if table not in tables_config:
            click.echo(f"❌ Unknown table: {table}")
            click.echo(f"Available tables: {', '.join(tables_config.keys())}")
            return False
        tables_to_process = {table: tables_config[table]}
    else:
        tables_to_process = tables_config
    
    total_fixed = 0
    
    for table_name, datetime_columns in tables_to_process.items():
        try:
            if dry_run:
                # For dry run, just count the issues
                from sqlalchemy import text
                
                count_query = f"""
                    SELECT COUNT(*) FROM {table_name} 
                    WHERE created_at IS NOT NULL 
                    AND EXTRACT(timezone FROM created_at) IS NULL
                """
                
                result = db.session.execute(text(count_query)).scalar()
                click.echo(f"   {table_name}: {result} naive datetime records found")
                total_fixed += result
            else:
                fixed_count = fix_naive_datetimes(db.session, table_name, datetime_columns)
                click.echo(f"   {table_name}: {fixed_count} records fixed")
                total_fixed += fixed_count
                
        except Exception as e:
            click.echo(f"   ❌ Error processing {table_name}: {e}")
    
    if dry_run:
        click.echo(f"\n📊 Total naive datetime records found: {total_fixed}")
        if total_fixed > 0:
            click.echo("Run without --dry-run to fix these issues")
    else:
        click.echo(f"\n✅ Total records fixed: {total_fixed}")
    
    return True


@timezone.command()
@click.option('--timezone', default='Asia/Tashkent', help='User timezone for testing')
@with_appcontext
def test():
    """Test timezone functionality"""
    click.echo(f"🧪 Testing timezone functionality...")
    
    try:
        from business_app.utils.timezone_utils import (
            get_utc_now, utc_to_local, local_to_utc, 
            format_datetime_for_user, parse_user_datetime
        )
        import pytz
        
        # Test 1: UTC now
        utc_now = get_utc_now()
        click.echo(f"   UTC now: {utc_now}")
        
        # Test 2: Convert to user timezone
        user_tz = pytz.timezone('Asia/Tashkent')
        local_time = utc_to_local(utc_now, user_tz)
        click.echo(f"   Local time (Asia/Tashkent): {local_time}")
        
        # Test 3: Convert back to UTC
        back_to_utc = local_to_utc(local_time, user_tz)
        click.echo(f"   Back to UTC: {back_to_utc}")
        
        # Test 4: Format for user
        formatted = format_datetime_for_user(utc_now, timezone_tz=user_tz)
        click.echo(f"   Formatted for user: {formatted}")
        
        # Test 5: Parse user input
        user_input = "2024-01-01 12:00:00"
        parsed = parse_user_datetime(user_input, source_tz=user_tz)
        click.echo(f"   Parsed user input '{user_input}': {parsed}")
        
        # Test 6: Database query test
        from business_app.models.user import User
        user_count = User.query.count()
        click.echo(f"   Database query test: {user_count} users found")
        
        click.echo("✅ All timezone tests passed!")
        return True
        
    except Exception as e:
        click.echo(f"❌ Timezone test failed: {e}")
        return False


@timezone.command()
@with_appcontext
def info():
    """Show timezone configuration information"""
    click.echo("ℹ️  Timezone Configuration:")
    
    try:
        config_items = [
            ('USE_TZ', current_app.config.get('USE_TZ')),
            ('TIMEZONE', current_app.config.get('TIMEZONE')),
            ('DISPLAY_TIMEZONE', current_app.config.get('DISPLAY_TIMEZONE')),
            ('ALLOWED_TIMEZONES', current_app.config.get('ALLOWED_TIMEZONES')),
            ('DATETIME_FORMAT', current_app.config.get('DATETIME_FORMAT')),
            ('BABEL_DEFAULT_TIMEZONE', current_app.config.get('BABEL_DEFAULT_TIMEZONE'))
        ]
        
        for key, value in config_items:
            click.echo(f"   {key}: {value}")
        
        # Show current UTC time
        utc_now = get_utc_now()
        click.echo(f"\n⏰ Current UTC time: {utc_now}")
        
        # Show time in default timezone
        from business_app.utils.timezone_utils import utc_to_local
        default_tz = current_app.config.get('DISPLAY_TIMEZONE', 'Asia/Tashkent')
        local_now = utc_to_local(utc_now, default_tz)
        click.echo(f"   Current {default_tz} time: {local_now}")
        
        return True
        
    except Exception as e:
        click.echo(f"❌ Error getting timezone info: {e}")
        return False


@timezone.command()
@click.option('--user-id', type=int, help='User ID to update timezone for')
@click.option('--timezone-name', required=True, help='Timezone name (e.g., Asia/Tashkent)')
@with_appcontext
def set_user_timezone(user_id, timezone_name):
    """Set timezone for a specific user or all users"""
    
    try:
        # Validate timezone
        import pytz
        try:
            pytz.timezone(timezone_name)
        except pytz.UnknownTimeZoneError:
            click.echo(f"❌ Unknown timezone: {timezone_name}")
            return False
        
        from business_app.models.user import User
        
        if user_id:
            # Update specific user
            user = User.query.get(user_id)
            if not user:
                click.echo(f"❌ User {user_id} not found")
                return False
            
            user.timezone = timezone_name
            db.session.commit()
            click.echo(f"✅ Updated timezone for user {user_id} to {timezone_name}")
        else:
            # Update all users
            updated_count = User.query.update({'timezone': timezone_name})
            db.session.commit()
            click.echo(f"✅ Updated timezone for {updated_count} users to {timezone_name}")
        
        return True
        
    except Exception as e:
        click.echo(f"❌ Error setting user timezone: {e}")
        return False


def register_timezone_commands(app):
    """Register timezone CLI commands with the Flask app"""
    app.cli.add_command(timezone)