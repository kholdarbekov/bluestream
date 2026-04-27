#!/usr/bin/env python3
"""
Manual backup trigger (INF-008).

The actual backup logic lives in
``business_app/tasks/backup_tasks.py`` so it can run on the Celery worker
under the same Flask app context as everything else (Sentry, audit logger,
config). This file is a thin CLI shim for ad-hoc / manual runs:

    # Trigger a database backup right now (returns immediately; runs on worker):
    python scripts/backup.py db

    # Trigger an uploads backup:
    python scripts/backup.py uploads

    # Run synchronously inside the current process (no worker required):
    python scripts/backup.py db --sync

Restore: see docs/operations/restore.md.
"""
from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description='Trigger BlueStream backup tasks.')
    parser.add_argument('target', choices=['db', 'database', 'uploads'],
                        help='What to back up.')
    parser.add_argument('--sync', action='store_true',
                        help='Run in-process (no Celery worker). Useful for one-off / on-demand backups.')
    args = parser.parse_args()

    target = 'db' if args.target == 'database' else args.target

    if args.sync:
        # Inline run — needs a Flask app context.
        from business_app import create_app
        app = create_app()
        with app.app_context():
            if target == 'db':
                from business_app.tasks.backup_tasks import backup_database_task
                result = backup_database_task.run()
            else:
                from business_app.tasks.backup_tasks import backup_uploads_task
                result = backup_uploads_task.run()
        print(result)
        return 0 if result.get('success') else 1

    # Async — enqueue on the worker.
    from business_app.tasks.backup_tasks import (
        backup_database_task,
        backup_uploads_task,
    )
    if target == 'db':
        result = backup_database_task.delay()
    else:
        result = backup_uploads_task.delay()
    print(f"Enqueued task {result.id}; check Celery / Flower for completion.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
