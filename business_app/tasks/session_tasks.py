"""
Celery tasks for session and user maintenance
"""
import logging
from datetime import datetime, timezone
from celery import shared_task, current_task
from business_app.services.session_cleanup_service import SessionCleanupService

logger = logging.getLogger(__name__)


@shared_task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 60})
def cleanup_expired_sessions_task(self, batch_size=1000):
    """
    Celery task to clean up expired sessions
    
    Args:
        batch_size: Number of sessions to process in each batch
        
    Returns:
        Dictionary with cleanup results
    """
    try:
        logger.info(f"Starting scheduled session cleanup task with batch size {batch_size}")
        
        service = SessionCleanupService()
        results = service.cleanup_expired_sessions(batch_size)
        
        # Update task progress
        if current_task:
            current_task.update_state(
                state='SUCCESS',
                meta={'results': results, 'completed_at': datetime.now(timezone.utc).isoformat()}
            )
        
        logger.info(f"Session cleanup task completed: {results}")
        return results
        
    except Exception as e:
        logger.error(f"Session cleanup task failed: {e}")
        raise


@shared_task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 60})
def cleanup_inactive_users_task(self, batch_size=500):
    """
    Celery task to clean up inactive users
    
    Args:
        batch_size: Number of users to process in each batch
        
    Returns:
        Dictionary with cleanup results
    """
    try:
        logger.info(f"Starting scheduled inactive user cleanup task with batch size {batch_size}")
        
        service = SessionCleanupService()
        results = service.cleanup_inactive_users(batch_size)
        
        # Update task progress
        if current_task:
            current_task.update_state(
                state='SUCCESS',
                meta={'results': results, 'completed_at': datetime.now(timezone.utc).isoformat()}
            )
        
        logger.info(f"Inactive user cleanup task completed: {results}")
        return results
        
    except Exception as e:
        logger.error(f"Inactive user cleanup task failed: {e}")
        raise


@shared_task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 60})
def cleanup_orphaned_data_task(self):
    """
    Celery task to clean up orphaned data
    
    Returns:
        Dictionary with cleanup results
    """
    try:
        logger.info("Starting scheduled orphaned data cleanup task")
        
        service = SessionCleanupService()
        results = service.cleanup_orphaned_data()
        
        # Update task progress
        if current_task:
            current_task.update_state(
                state='SUCCESS',
                meta={'results': results, 'completed_at': datetime.now(timezone.utc).isoformat()}
            )
        
        logger.info(f"Orphaned data cleanup task completed: {results}")
        return results
        
    except Exception as e:
        logger.error(f"Orphaned data cleanup task failed: {e}")
        raise


@shared_task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 2, 'countdown': 300})
def full_session_cleanup_task(self, batch_size=1000):
    """
    Comprehensive session and user cleanup task
    
    Args:
        batch_size: Batch size for processing
        
    Returns:
        Dictionary with comprehensive cleanup results
    """
    try:
        logger.info(f"Starting comprehensive session cleanup task with batch size {batch_size}")
        
        service = SessionCleanupService()
        results = service.full_cleanup(batch_size)
        
        # Update task progress
        if current_task:
            current_task.update_state(
                state='SUCCESS',
                meta={'results': results, 'completed_at': datetime.now(timezone.utc).isoformat()}
            )
        
        logger.info(f"Comprehensive cleanup task completed: {results}")
        return results
        
    except Exception as e:
        logger.error(f"Comprehensive cleanup task failed: {e}")
        raise


@shared_task(bind=True)
def session_cleanup_health_check(self):
    """
    Health check task for session cleanup functionality
    
    Returns:
        Dictionary with health status
    """
    try:
        logger.info("Running session cleanup health check")
        
        service = SessionCleanupService()
        stats = service.get_cleanup_statistics()
        
        # Check for concerning conditions
        warnings = []
        errors = []
        
        # Check if too many sessions are accumulating
        total_sessions = stats.get('total_sessions', 0)
        expired_sessions = stats.get('expired_sessions', 0)
        old_expired_sessions = stats.get('old_expired_sessions', 0)
        
        if total_sessions > 10000:
            warnings.append(f"High number of total sessions: {total_sessions}")
        
        if expired_sessions > 1000:
            warnings.append(f"High number of expired sessions: {expired_sessions}")
        
        if old_expired_sessions > 500:
            errors.append(f"Too many old expired sessions not cleaned: {old_expired_sessions}")
        
        # Check for inactive users needing cleanup
        users_needing_cleanup = stats.get('users_needing_cleanup', 0)
        if users_needing_cleanup > 100:
            warnings.append(f"Many users need cleanup: {users_needing_cleanup}")
        
        health_status = {
            'status': 'error' if errors else ('warning' if warnings else 'healthy'),
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'statistics': stats,
            'warnings': warnings,
            'errors': errors
        }
        
        if current_task:
            current_task.update_state(
                state='SUCCESS',
                meta=health_status
            )
        
        if errors:
            logger.error(f"Session cleanup health check found errors: {errors}")
        elif warnings:
            logger.warning(f"Session cleanup health check found warnings: {warnings}")
        else:
            logger.info("Session cleanup health check passed")
        
        return health_status
        
    except Exception as e:
        logger.error(f"Session cleanup health check failed: {e}")
        raise


# Periodic task registration (if using Celery Beat)
# These can be configured in celerybeat-schedule.py or similar configuration

def register_periodic_tasks():
    """
    Register periodic session cleanup tasks
    This should be called from celery configuration
    """
    from celery.schedules import crontab
    from business_app.tasks.celery_app import celery

    # Daily session cleanup at 2 AM
    celery.conf.beat_schedule.update({
        'cleanup-expired-sessions-daily': {
            'task': 'business_app.tasks.session_tasks.cleanup_expired_sessions_task',
            'schedule': crontab(hour=2, minute=0),
            'args': (1000,)  # batch_size
        },
        'cleanup-inactive-users-weekly': {
            'task': 'business_app.tasks.session_tasks.cleanup_inactive_users_task',
            'schedule': crontab(hour=3, minute=0, day_of_week=1),  # Monday at 3 AM
            'args': (500,)  # batch_size
        },
        'cleanup-orphaned-data-weekly': {
            'task': 'business_app.tasks.session_tasks.cleanup_orphaned_data_task',
            'schedule': crontab(hour=4, minute=0, day_of_week=1),  # Monday at 4 AM
        },
        'session-cleanup-health-check': {
            'task': 'business_app.tasks.session_tasks.session_cleanup_health_check',
            'schedule': crontab(minute=0),  # Every hour
        }
    })
    
    logger.info("Registered periodic session cleanup tasks")