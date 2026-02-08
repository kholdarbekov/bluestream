"""
Celery tasks for audit log maintenance and retention
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any
from celery import shared_task, current_task
from sqlalchemy import and_

from business_app import db
from business_app.models.audit import AuditLog, AuditSeverity

logger = logging.getLogger(__name__)


@shared_task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 300})
def cleanup_old_audit_logs_task(
    self,
    retention_days: int = 90,
    batch_size: int = 1000,
    preserve_critical: bool = True
) -> Dict[str, Any]:
    """
    Clean up old audit log records based on retention policy

    Performance optimizations:
    - Processes in batches to avoid memory issues
    - Uses bulk delete for efficiency
    - Commits after each batch

    Args:
        retention_days: Number of days to keep audit logs (default: 90)
        batch_size: Number of records to delete per batch (default: 1000)
        preserve_critical: Keep CRITICAL severity logs regardless of age (default: True)

    Returns:
        Dictionary with cleanup statistics:
        {
            'total_deleted': int,
            'batches_processed': int,
            'retention_cutoff_date': str,
            'duration_seconds': float,
            'preserved_critical_count': int
        }

    Example:
        # Delete logs older than 90 days
        cleanup_old_audit_logs_task.delay()

        # Delete logs older than 30 days, including critical
        cleanup_old_audit_logs_task.delay(retention_days=30, preserve_critical=False)
    """
    start_time = datetime.now(timezone.utc)

    try:
        logger.info(
            f"Starting audit log cleanup task "
            f"(retention: {retention_days} days, batch: {batch_size}, "
            f"preserve_critical: {preserve_critical})"
        )

        # Calculate cutoff date
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=retention_days)

        # Statistics
        total_deleted = 0
        batches_processed = 0
        preserved_critical_count = 0

        # Count critical logs that will be preserved
        if preserve_critical:
            preserved_critical_count = AuditLog.query.filter(
                and_(
                    AuditLog.created_at < cutoff_date,
                    AuditLog.severity == AuditSeverity.CRITICAL
                )
            ).count()
            logger.info(f"Preserving {preserved_critical_count} critical severity logs")

        # Build base query for deletion
        if preserve_critical:
            # Delete old logs except critical severity
            base_query = AuditLog.query.filter(
                and_(
                    AuditLog.created_at < cutoff_date,
                    AuditLog.severity != AuditSeverity.CRITICAL
                )
            )
        else:
            # Delete all old logs
            base_query = AuditLog.query.filter(AuditLog.created_at < cutoff_date)

        # Count total to delete
        total_to_delete = base_query.count()
        logger.info(f"Found {total_to_delete} audit logs to delete (older than {cutoff_date.date()})")

        if total_to_delete == 0:
            logger.info("No audit logs to delete")
            return {
                'total_deleted': 0,
                'batches_processed': 0,
                'retention_cutoff_date': cutoff_date.isoformat(),
                'duration_seconds': 0,
                'preserved_critical_count': preserved_critical_count,
                'status': 'nothing_to_delete'
            }

        # Process in batches
        while True:
            # Get batch of IDs to delete
            batch_ids = [
                log.id for log in base_query.limit(batch_size).all()
            ]

            if not batch_ids:
                break

            # Delete batch
            deleted_count = AuditLog.query.filter(AuditLog.id.in_(batch_ids)).delete(
                synchronize_session=False
            )
            db.session.commit()

            total_deleted += deleted_count
            batches_processed += 1

            # Update task progress
            if current_task:
                progress_percentage = min(100, int((total_deleted / total_to_delete) * 100))
                current_task.update_state(
                    state='PROGRESS',
                    meta={
                        'total_deleted': total_deleted,
                        'total_to_delete': total_to_delete,
                        'progress_percentage': progress_percentage,
                        'batches_processed': batches_processed
                    }
                )

            logger.info(
                f"Batch {batches_processed} completed: deleted {deleted_count} logs "
                f"(total: {total_deleted}/{total_to_delete})"
            )

        # Calculate duration
        end_time = datetime.now(timezone.utc)
        duration_seconds = (end_time - start_time).total_seconds()

        result = {
            'total_deleted': total_deleted,
            'batches_processed': batches_processed,
            'retention_cutoff_date': cutoff_date.isoformat(),
            'duration_seconds': round(duration_seconds, 2),
            'preserved_critical_count': preserved_critical_count,
            'status': 'success'
        }

        logger.info(
            f"Audit log cleanup completed: "
            f"deleted {total_deleted} logs in {batches_processed} batches "
            f"({duration_seconds:.2f}s)"
        )

        return result

    except Exception as e:
        logger.error(f"Audit log cleanup task failed: {e}", exc_info=True)
        db.session.rollback()
        raise


@shared_task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 3, 'countdown': 300})
def archive_old_audit_logs_task(
    self,
    retention_days: int = 90,
    batch_size: int = 1000,
    archive_format: str = 'json'
) -> Dict[str, Any]:
    """
    Archive old audit logs to file before deletion

    This task exports old audit logs to JSON/CSV files before deleting them,
    useful for long-term compliance or analysis.

    Args:
        retention_days: Number of days to keep audit logs (default: 90)
        batch_size: Number of records to process per batch (default: 1000)
        archive_format: Format for archive ('json' or 'csv', default: 'json')

    Returns:
        Dictionary with archive statistics

    Note:
        This task should be scheduled BEFORE cleanup_old_audit_logs_task
        to ensure logs are archived before deletion.

    Example:
        # Archive logs older than 90 days
        archive_old_audit_logs_task.delay()
    """
    import json
    import csv
    from pathlib import Path

    start_time = datetime.now(timezone.utc)

    try:
        logger.info(
            f"Starting audit log archive task "
            f"(retention: {retention_days} days, format: {archive_format})"
        )

        # Calculate cutoff date
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=retention_days)

        # Create archive directory
        archive_dir = Path('var/audit_archives')
        archive_dir.mkdir(parents=True, exist_ok=True)

        # Generate archive filename
        archive_timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
        archive_filename = f"audit_logs_archive_{archive_timestamp}.{archive_format}"
        archive_path = archive_dir / archive_filename

        # Query logs to archive
        logs_to_archive = AuditLog.query.filter(
            AuditLog.created_at < cutoff_date
        ).order_by(AuditLog.created_at).all()

        total_archived = len(logs_to_archive)

        if total_archived == 0:
            logger.info("No audit logs to archive")
            return {
                'total_archived': 0,
                'archive_file': None,
                'status': 'nothing_to_archive'
            }

        # Export to file
        if archive_format == 'json':
            with open(archive_path, 'w', encoding='utf-8') as f:
                logs_data = [log.to_dict() for log in logs_to_archive]
                json.dump(logs_data, f, indent=2, default=str)

        elif archive_format == 'csv':
            with open(archive_path, 'w', encoding='utf-8', newline='') as f:
                if logs_to_archive:
                    # Get field names from first log
                    fieldnames = list(logs_to_archive[0].to_dict().keys())
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    for log in logs_to_archive:
                        writer.writerow(log.to_dict())

        # Calculate file size
        file_size_mb = archive_path.stat().st_size / (1024 * 1024)

        # Calculate duration
        end_time = datetime.now(timezone.utc)
        duration_seconds = (end_time - start_time).total_seconds()

        result = {
            'total_archived': total_archived,
            'archive_file': str(archive_path),
            'file_size_mb': round(file_size_mb, 2),
            'archive_format': archive_format,
            'retention_cutoff_date': cutoff_date.isoformat(),
            'duration_seconds': round(duration_seconds, 2),
            'status': 'success'
        }

        logger.info(
            f"Audit log archive completed: "
            f"archived {total_archived} logs to {archive_path} "
            f"({file_size_mb:.2f} MB, {duration_seconds:.2f}s)"
        )

        return result

    except Exception as e:
        logger.error(f"Audit log archive task failed: {e}", exc_info=True)
        raise


@shared_task(bind=True)
def get_audit_log_statistics_task(self) -> Dict[str, Any]:
    """
    Get statistics about audit log storage and growth

    Useful for monitoring storage usage and planning retention policies.

    Returns:
        Dictionary with audit log statistics:
        {
            'total_logs': int,
            'logs_by_severity': dict,
            'logs_by_age': dict,
            'oldest_log_date': str,
            'newest_log_date': str,
            'estimated_size_mb': float
        }
    """
    try:
        logger.info("Calculating audit log statistics")

        # Total count
        total_logs = AuditLog.query.count()

        if total_logs == 0:
            return {
                'total_logs': 0,
                'status': 'no_logs'
            }

        # Count by severity
        logs_by_severity = {}
        for severity in AuditSeverity:
            count = AuditLog.query.filter(AuditLog.severity == severity).count()
            logs_by_severity[severity.value] = count

        # Count by age buckets
        now = datetime.now(timezone.utc)
        logs_by_age = {
            'last_24h': AuditLog.query.filter(
                AuditLog.created_at >= now - timedelta(hours=24)
            ).count(),
            'last_7_days': AuditLog.query.filter(
                AuditLog.created_at >= now - timedelta(days=7)
            ).count(),
            'last_30_days': AuditLog.query.filter(
                AuditLog.created_at >= now - timedelta(days=30)
            ).count(),
            'last_90_days': AuditLog.query.filter(
                AuditLog.created_at >= now - timedelta(days=90)
            ).count(),
            'older_than_90_days': AuditLog.query.filter(
                AuditLog.created_at < now - timedelta(days=90)
            ).count()
        }

        # Oldest and newest logs
        oldest_log = AuditLog.query.order_by(AuditLog.created_at.asc()).first()
        newest_log = AuditLog.query.order_by(AuditLog.created_at.desc()).first()

        # Estimate database size (rough calculation)
        # Average audit log row size ~1-2 KB including JSON fields
        estimated_size_mb = (total_logs * 1.5) / 1024  # 1.5 KB per row average

        result = {
            'total_logs': total_logs,
            'logs_by_severity': logs_by_severity,
            'logs_by_age': logs_by_age,
            'oldest_log_date': oldest_log.created_at.isoformat() if oldest_log else None,
            'newest_log_date': newest_log.created_at.isoformat() if newest_log else None,
            'estimated_size_mb': round(estimated_size_mb, 2),
            'status': 'success'
        }

        logger.info(f"Audit log statistics: {result}")
        return result

    except Exception as e:
        logger.error(f"Failed to get audit log statistics: {e}", exc_info=True)
        raise
