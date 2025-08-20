"""
Database timezone utilities for consistent datetime handling
"""
from datetime import datetime
from sqlalchemy import func, and_, text
from sqlalchemy.orm import Query
from typing import Optional, Union, List
import logging

from business_app.utils.timezone_utils import (
    get_utc_now, local_to_utc, utc_to_local, 
    get_user_timezone, parse_user_datetime
)

logger = logging.getLogger(__name__)


class TimezoneAwareQuery:
    """Helper class for timezone-aware database queries"""
    
    @staticmethod
    def filter_by_date_range(query: Query, date_column, start_date=None, end_date=None, 
                           user_timezone=None) -> Query:
        """
        Filter query by date range with timezone conversion
        
        Args:
            query: SQLAlchemy query
            date_column: Column to filter on
            start_date: Start date (string, datetime, or None)
            end_date: End date (string, datetime, or None)
            user_timezone: User's timezone for conversion
        
        Returns:
            Query: Filtered query
        """
        try:
            if start_date:
                if isinstance(start_date, str):
                    start_date = parse_user_datetime(start_date, source_tz=user_timezone)
                elif isinstance(start_date, datetime):
                    start_date = local_to_utc(start_date, source_tz=user_timezone)
                
                query = query.filter(date_column >= start_date)
            
            if end_date:
                if isinstance(end_date, str):
                    end_date = parse_user_datetime(end_date, source_tz=user_timezone)
                elif isinstance(end_date, datetime):
                    end_date = local_to_utc(end_date, source_tz=user_timezone)
                
                # If end_date is just a date (midnight), extend to end of day
                if end_date.time() == datetime.min.time():
                    end_date = end_date.replace(hour=23, minute=59, second=59, microsecond=999999)
                
                query = query.filter(date_column <= end_date)
            
            return query
            
        except Exception as e:
            logger.error(f"Error filtering by date range: {e}")
            return query
    
    @staticmethod
    def filter_by_business_hours(query: Query, date_column, user_timezone=None) -> Query:
        """
        Filter query to only include records created during business hours
        
        Args:
            query: SQLAlchemy query
            date_column: Datetime column to filter on
            user_timezone: User's timezone
        
        Returns:
            Query: Filtered query
        """
        from business_app.utils.timezone_utils import get_business_hours_for_timezone
        
        try:
            business_hours = get_business_hours_for_timezone(user_timezone)
            
            # Create filters for each day of the week
            day_filters = []
            
            for day_num, day_name in enumerate(['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']):
                day_hours = business_hours.get(day_name, {})
                open_time = day_hours.get('open')
                close_time = day_hours.get('close')
                
                if open_time and close_time:
                    # Extract day of week and time from date_column
                    day_of_week_filter = func.extract('dow', date_column) == day_num
                    
                    # Convert business hours to time objects for comparison
                    open_hour, open_minute = map(int, open_time.split(':'))
                    close_hour, close_minute = map(int, close_time.split(':'))
                    
                    time_filter = and_(
                        func.extract('hour', date_column) >= open_hour,
                        func.extract('hour', date_column) <= close_hour
                    )
                    
                    # Handle minute precision for open/close times
                    if func.extract('hour', date_column) == open_hour:
                        time_filter = and_(time_filter, func.extract('minute', date_column) >= open_minute)
                    
                    if func.extract('hour', date_column) == close_hour:
                        time_filter = and_(time_filter, func.extract('minute', date_column) <= close_minute)
                    
                    day_filters.append(and_(day_of_week_filter, time_filter))
            
            if day_filters:
                from sqlalchemy import or_
                query = query.filter(or_(*day_filters))
            
            return query
            
        except Exception as e:
            logger.error(f"Error filtering by business hours: {e}")
            return query
    
    @staticmethod
    def group_by_date_period(query: Query, date_column, period='day', user_timezone=None):
        """
        Group query results by date period (day, week, month, year)
        
        Args:
            query: SQLAlchemy query
            date_column: Date column to group by
            period: Grouping period ('day', 'week', 'month', 'year')
            user_timezone: User's timezone for grouping
        
        Returns:
            Query: Query with grouping applied
        """
        try:
            # Convert UTC times to user timezone for grouping
            if user_timezone:
                # PostgreSQL: AT TIME ZONE for timezone conversion
                local_time = func.timezone(str(user_timezone), date_column)
            else:
                local_time = date_column
            
            if period == 'day':
                date_part = func.date(local_time)
            elif period == 'week':
                date_part = func.date_trunc('week', local_time)
            elif period == 'month':
                date_part = func.date_trunc('month', local_time)
            elif period == 'year':
                date_part = func.date_trunc('year', local_time)
            else:
                raise ValueError(f"Invalid period: {period}")
            
            return query.group_by(date_part), date_part
            
        except Exception as e:
            logger.error(f"Error grouping by date period: {e}")
            return query, date_column
    
    @staticmethod
    def add_timezone_aware_aggregates(query: Query, date_column, user_timezone=None):
        """
        Add timezone-aware aggregate functions to query
        
        Args:
            query: SQLAlchemy query
            date_column: Date column for aggregates
            user_timezone: User's timezone
        
        Returns:
            dict: Dictionary of aggregate functions
        """
        try:
            # Convert to user timezone for aggregation
            if user_timezone:
                local_time = func.timezone(str(user_timezone), date_column)
            else:
                local_time = date_column
            
            aggregates = {
                'earliest': func.min(local_time),
                'latest': func.max(local_time),
                'count': func.count(date_column),
                'count_today': func.sum(
                    func.case(
                        [(func.date(local_time) == func.current_date(), 1)],
                        else_=0
                    )
                ),
                'count_this_week': func.sum(
                    func.case(
                        [(func.date_trunc('week', local_time) == func.date_trunc('week', func.now()), 1)],
                        else_=0
                    )
                ),
                'count_this_month': func.sum(
                    func.case(
                        [(func.date_trunc('month', local_time) == func.date_trunc('month', func.now()), 1)],
                        else_=0
                    )
                )
            }
            
            return aggregates
            
        except Exception as e:
            logger.error(f"Error creating timezone-aware aggregates: {e}")
            return {}


def create_timezone_aware_indexes(db_session):
    """
    Create database indexes optimized for timezone-aware queries
    
    Args:
        db_session: Database session
    """
    index_statements = [
        # Indexes for date range queries
        "CREATE INDEX IF NOT EXISTS idx_orders_created_at_date ON orders (DATE(created_at))",
        "CREATE INDEX IF NOT EXISTS idx_payments_paid_at_date ON payments (DATE(paid_at))",
        "CREATE INDEX IF NOT EXISTS idx_deliveries_scheduled_date ON deliveries (DATE(scheduled_at))",
        
        # Indexes for business hours queries (extract hour)
        "CREATE INDEX IF NOT EXISTS idx_orders_created_hour ON orders (EXTRACT(hour FROM created_at))",
        "CREATE INDEX IF NOT EXISTS idx_orders_created_dow ON orders (EXTRACT(dow FROM created_at))",
        
        # Composite indexes for timezone-aware filtering
        "CREATE INDEX IF NOT EXISTS idx_orders_status_created_at ON orders (status, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_payments_status_paid_at ON payments (status, paid_at)",
        
        # Partial indexes for active records
        "CREATE INDEX IF NOT EXISTS idx_orders_active_created_at ON orders (created_at) WHERE deleted_at IS NULL",
        "CREATE INDEX IF NOT EXISTS idx_users_active_last_login ON users (last_login) WHERE status = 'active'"
    ]
    
    for statement in index_statements:
        try:
            db_session.execute(text(statement))
            logger.info(f"Created index: {statement}")
        except Exception as e:
            logger.warning(f"Failed to create index {statement}: {e}")
    
    try:
        db_session.commit()
    except Exception as e:
        logger.error(f"Failed to commit timezone indexes: {e}")
        db_session.rollback()


def validate_datetime_consistency(db_session):
    """
    Validate datetime consistency across the database
    
    Args:
        db_session: Database session
    
    Returns:
        dict: Validation results
    """
    validation_results = {
        'total_checks': 0,
        'passed_checks': 0,
        'failed_checks': 0,
        'issues': []
    }
    
    # Check for naive datetimes (should not exist)
    checks = [
        {
            'name': 'Orders created_at timezone',
            'query': "SELECT COUNT(*) FROM orders WHERE created_at AT TIME ZONE 'UTC' = created_at"
        },
        {
            'name': 'Users last_login timezone', 
            'query': "SELECT COUNT(*) FROM users WHERE last_login IS NOT NULL AND last_login AT TIME ZONE 'UTC' = last_login"
        },
        {
            'name': 'Payments paid_at timezone',
            'query': "SELECT COUNT(*) FROM payments WHERE paid_at IS NOT NULL AND paid_at AT TIME ZONE 'UTC' = paid_at"
        }
    ]
    
    for check in checks:
        try:
            result = db_session.execute(text(check['query'])).scalar()
            validation_results['total_checks'] += 1
            
            if result == 0:
                validation_results['passed_checks'] += 1
                logger.info(f"PASS: {check['name']}")
            else:
                validation_results['failed_checks'] += 1
                validation_results['issues'].append({
                    'check': check['name'],
                    'issue': f"Found {result} records with timezone issues"
                })
                logger.warning(f"FAIL: {check['name']} - {result} issues found")
                
        except Exception as e:
            validation_results['failed_checks'] += 1
            validation_results['issues'].append({
                'check': check['name'],
                'issue': f"Query failed: {str(e)}"
            })
            logger.error(f"ERROR: {check['name']} - {e}")
    
    return validation_results


def fix_naive_datetimes(db_session, table_name, datetime_columns):
    """
    Fix naive datetimes in database table
    
    Args:
        db_session: Database session
        table_name: Name of the table
        datetime_columns: List of datetime column names
    
    Returns:
        int: Number of records updated
    """
    updated_count = 0
    
    for column in datetime_columns:
        try:
            # Update naive datetimes to UTC
            update_statement = text(f"""
                UPDATE {table_name} 
                SET {column} = {column} AT TIME ZONE 'UTC'
                WHERE {column} IS NOT NULL 
                AND EXTRACT(timezone FROM {column}) IS NULL
            """)
            
            result = db_session.execute(update_statement)
            count = result.rowcount
            updated_count += count
            
            if count > 0:
                logger.info(f"Updated {count} naive datetime records in {table_name}.{column}")
            
        except Exception as e:
            logger.error(f"Failed to fix naive datetimes in {table_name}.{column}: {e}")
    
    try:
        db_session.commit()
    except Exception as e:
        logger.error(f"Failed to commit datetime fixes: {e}")
        db_session.rollback()
        updated_count = 0
    
    return updated_count