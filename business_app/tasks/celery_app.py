"""
Celery application configuration for the Water Business Platform
This file should be placed in business_app/tasks/celery_app.py
"""
from celery import Celery
from celery.schedules import crontab
from flask import current_app
import os


def make_celery(app=None):
    """Create and configure Celery app"""
    if app is None:
        from .. import create_app
        app = create_app()
    
    celery = Celery(
        app.import_name,
        backend=app.config['CELERY']['result_backend'],
        broker=app.config['CELERY']['broker_url'],
        include=[
            'business_app.tasks.payment_tasks',
            'business_app.tasks.notification_tasks',
            'business_app.tasks.delivery_tasks',
            'business_app.tasks.analytics_tasks',
            'business_app.tasks.subscription_tasks',
            'business_app.tasks.order_tasks'
        ]
    )
    
    # Update configuration from Flask config
    celery.conf.update(app.config['CELERY'])
    
    # Configure periodic tasks
    celery.conf.beat_schedule = {
        # Process subscription billing daily at 9 AM
        'process-subscription-billing': {
            'task': 'business_app.tasks.subscription_tasks.process_daily_subscription_billing',
            'schedule': crontab(hour=9, minute=0),
        },
        
        # Expire loyalty points daily at midnight
        'expire-loyalty-points': {
            'task': 'business_app.tasks.loyalty_tasks.expire_loyalty_points',
            'schedule': crontab(hour=0, minute=0),
        },
        
        # Send delivery reminders every 30 minutes
        'delivery-reminders': {
            'task': 'business_app.tasks.delivery_tasks.send_delivery_reminders',
            'schedule': crontab(minute='*/30'),
        },
        
        # Process payment confirmations every 5 minutes
        'process-payment-confirmations': {
            'task': 'business_app.tasks.payment_tasks.process_pending_payments',
            'schedule': crontab(minute='*/5'),
        },
        
        # Generate daily analytics report
        'daily-analytics-report': {
            'task': 'business_app.tasks.analytics_tasks.generate_daily_analytics_report',
            'schedule': crontab(hour=23, minute=0),
        },
        
        # Clean up old notifications weekly
        'cleanup-old-notifications': {
            'task': 'business_app.tasks.notification_tasks.cleanup_old_notifications',
            'schedule': crontab(hour=2, minute=0, day_of_week=1),  # Monday at 2 AM
        },
        
        # Auto-confirm orders after 10 minutes
        'auto-confirm-orders': {
            'task': 'business_app.tasks.order_tasks.auto_confirm_pending_orders',
            'schedule': crontab(minute='*/10'),
        },
        
        # Send subscription renewal reminders
        'subscription-renewal-reminders': {
            'task': 'business_app.tasks.subscription_tasks.send_renewal_reminders',
            'schedule': crontab(hour=10, minute=0),  # Daily at 10 AM
        },
        
        # Optimize delivery routes daily
        'optimize-delivery-routes': {
            'task': 'business_app.tasks.delivery_tasks.optimize_daily_delivery_routes',
            'schedule': crontab(hour=7, minute=0),  # Daily at 7 AM
        },
        
        # Process failed payments retry
        'retry-failed-payments': {
            'task': 'business_app.tasks.payment_tasks.retry_failed_payments',
            'schedule': crontab(hour=12, minute=0),  # Daily at noon
        },
        
        # Generate weekly business reports
        'weekly-business-report': {
            'task': 'business_app.tasks.analytics_tasks.generate_weekly_business_report',
            'schedule': crontab(hour=8, minute=0, day_of_week=1),  # Monday at 8 AM
        },
        
        # Update customer segments monthly
        'update-customer-segments': {
            'task': 'business_app.tasks.analytics_tasks.update_customer_segments',
            'schedule': crontab(hour=3, minute=0, day_of_month=1),  # 1st day of month at 3 AM
        }
    }
    
    # Set timezone
    celery.conf.timezone = 'Asia/Tashkent'
    
    class ContextTask(celery.Task):
        """Make celery tasks work with Flask app context"""
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)
    
    celery.Task = ContextTask
    return celery


# Create default celery app
celery_app = make_celery()


# Task routing configuration
celery_app.conf.task_routes = {
    'business_app.tasks.payment_tasks.*': {'queue': 'payment'},
    'business_app.tasks.notification_tasks.*': {'queue': 'notifications'},
    'business_app.tasks.delivery_tasks.*': {'queue': 'delivery'},
    'business_app.tasks.analytics_tasks.*': {'queue': 'analytics'},
    'business_app.tasks.subscription_tasks.*': {'queue': 'subscriptions'},
    'business_app.tasks.order_tasks.*': {'queue': 'orders'},
}


# Task priority configuration
celery_app.conf.task_default_priority = 5
celery_app.conf.worker_prefetch_multiplier = 1
celery_app.conf.task_acks_late = True
celery_app.conf.worker_disable_rate_limits = False


# Error handling configuration
celery_app.conf.task_reject_on_worker_lost = True
celery_app.conf.task_ignore_result = False
celery_app.conf.result_expires = 3600  # 1 hour


# Monitoring configuration
celery_app.conf.worker_send_task_events = True
celery_app.conf.task_send_sent_event = True


if __name__ == '__main__':
    celery_app.start()