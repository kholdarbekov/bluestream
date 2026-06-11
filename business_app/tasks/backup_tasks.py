"""
Scheduled backup tasks (INF-008).

Two Celery beat tasks ship here:

  - ``backup_database_task`` — runs nightly. Streams ``pg_dump --format=custom``
    into a gzipped, timestamped file under ``BACKUP_LOCAL_DIR``. Optional
    offsite copy via the ``rclone`` binary if ``BACKUP_RCLONE_REMOTE`` is set.

  - ``backup_uploads_task`` — runs weekly (Sunday). Tars+gzips the uploads
    directory (paywall: ``UPLOAD_FOLDER`` config) and ships the same way.

Retention is enforced after each run. Local: anything older than
``BACKUP_LOCAL_RETENTION_DAYS`` (default 14) is deleted from the local
backup dir. Offsite: pruned with a longer window via ``rclone delete
--min-age`` — ``BACKUP_OFFSITE_RETENTION_DAYS_DB`` (default 90) and
``BACKUP_OFFSITE_RETENTION_DAYS_UPLOADS`` (default 365) — because Google
Drive has no lifecycle policy of its own. Offsite prune is best-effort:
a failure is logged but never fails the backup.

All operations log structured fields via ``audit_logger`` so each backup
run produces an auditable record. Failures raise so Celery retries kick in
and the failure surfaces in Sentry (INF-005).

Restore: see :doc:`docs/operations/restore.md`.
"""

from __future__ import annotations

import gzip
import logging
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from celery import shared_task
from flask import current_app

from business_app.models.audit import AuditEventType, AuditSeverity
from business_app.utils.audit_logger import audit_logger

logger = logging.getLogger(__name__)


# ---- Config helpers --------------------------------------------------------


def _backup_local_dir() -> Path:
    """Where backups are written on the host. Created if missing (mode 700)."""
    raw = current_app.config.get("BACKUP_LOCAL_DIR") or os.environ.get("BACKUP_LOCAL_DIR", "/var/backups/bluestream")
    p = Path(raw)
    p.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p, 0o700)
    except OSError as exc:  # pragma: no cover — non-fatal
        logger.warning("Could not chmod 700 on %s: %s", p, exc)
    return p


def _retention_days() -> int:
    return int(
        current_app.config.get("BACKUP_LOCAL_RETENTION_DAYS") or os.environ.get("BACKUP_LOCAL_RETENTION_DAYS", "14")
    )


def _offsite_retention_days(kind: str) -> int:
    """Offsite retention window (days) for a backup `kind` ('db' or 'uploads').

    Offsite is kept LONGER than local (``BACKUP_LOCAL_RETENTION_DAYS``, default
    14) because Google Drive has no lifecycle policy of its own — we prune it
    explicitly. Defaults: db=90, uploads=365. Anything that isn't 'uploads'
    uses the db window.
    """
    if kind == "uploads":
        key, default = "BACKUP_OFFSITE_RETENTION_DAYS_UPLOADS", "365"
    else:
        key, default = "BACKUP_OFFSITE_RETENTION_DAYS_DB", "90"
    return int(current_app.config.get(key) or os.environ.get(key, default))


def _rclone_remote() -> Optional[str]:
    """rclone remote+path for offsite copy (e.g. ``s3:bluestream-backups/``).

    Returns None if not configured — backups still work locally, just no
    offsite copy. Recommended in production: always configure a remote.
    """
    return current_app.config.get("BACKUP_RCLONE_REMOTE") or os.environ.get("BACKUP_RCLONE_REMOTE")


# ---- Helpers ---------------------------------------------------------------


def _timestamp() -> str:
    """UTC timestamp suitable for filenames (sortable + filesystem-safe)."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _shell_run(cmd: list[str], *, env: Optional[dict] = None, check: bool = True) -> subprocess.CompletedProcess:
    """Wrapper around subprocess.run with sane defaults for backup ops.

    NEVER logs the env (would leak passwords); only the argv. stderr is
    captured so a failure carries the actual pg_dump error message.
    """
    logger.info("Running: %s", " ".join(cmd))
    return subprocess.run(
        cmd,
        env=env,
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _push_offsite(local_path: Path, remote_subdir: str) -> Optional[str]:
    """Copy a backup file to the configured rclone remote.

    Returns the remote path string on success, None if rclone isn't
    configured. Raises CalledProcessError if rclone is configured but fails —
    we want offsite-failure to surface as a hard error, not silent drop.
    """
    remote = _rclone_remote()
    if not remote:
        logger.info("BACKUP_RCLONE_REMOTE not set; skipping offsite copy of %s", local_path.name)
        return None
    if not shutil.which("rclone"):
        logger.error("BACKUP_RCLONE_REMOTE is set but rclone binary not found on PATH")
        raise RuntimeError("rclone binary missing")

    remote_target = f"{remote.rstrip('/')}/{remote_subdir.strip('/')}/{local_path.name}"
    _shell_run(
        [
            "rclone",
            "copyto",
            str(local_path),
            remote_target,
            "--s3-server-side-encryption=AES256",  # ignored if not S3
        ]
    )
    return remote_target


def _enforce_local_retention(directory: Path, prefix: str, days: int) -> int:
    """Delete files in `directory` matching `prefix*` older than `days` days.

    Returns the number of files deleted.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    deleted = 0
    for f in directory.glob(f"{prefix}*"):
        if not f.is_file():
            continue
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime < cutoff:
            try:
                f.unlink()
                deleted += 1
                logger.info("Deleted expired backup: %s", f.name)
            except OSError as exc:
                logger.warning("Could not delete expired backup %s: %s", f, exc)
    return deleted


def _enforce_offsite_retention(remote_subdir: str, prefix: str, days: int) -> None:
    """Delete offsite backups under ``<remote>/<remote_subdir>`` older than `days`.

    The offsite analogue of :func:`_enforce_local_retention`. Google Drive has
    no lifecycle policy, so we prune it ourselves with ``rclone delete
    --min-age``, scoped to ``<prefix>*`` so a misconfigured remote pointed at a
    shared folder can't delete unrelated files.

    NON-FATAL by design: the backup upload already succeeded by the time this
    runs, so a failed prune is logged but never raises (it must not trigger a
    Celery retry of an otherwise-good backup). This differs from
    :func:`_push_offsite`, where an upload failure IS fatal.
    """
    remote = _rclone_remote()
    if not remote:
        return
    if not shutil.which("rclone"):
        logger.warning("rclone not on PATH; skipping offsite retention sweep")
        return

    remote_target = f"{remote.rstrip('/')}/{remote_subdir.strip('/')}"
    try:
        result = _shell_run(
            [
                "rclone",
                "delete",
                remote_target,
                "--min-age",
                f"{days}d",
                "--include",
                f"{prefix}*",
            ],
            check=False,
        )
        if result.returncode != 0:
            logger.warning(
                "Offsite retention sweep on %s exited %s: %s",
                remote_target,
                result.returncode,
                (result.stderr or "")[:1000],
            )
        else:
            logger.info("Offsite retention sweep complete on %s (>%dd)", remote_target, days)
    except Exception:  # noqa: BLE001 — prune failure must never fail the backup
        logger.exception("Offsite retention sweep failed for %s", remote_target)


# ---- Database backup -------------------------------------------------------


def _build_pg_dump_env() -> tuple[list[str], dict]:
    """Build (argv, env) for pg_dump from DATABASE_URL.

    Password is passed via PGPASSWORD env var (NOT in argv where it'd show in
    `ps`). Other connection bits go in argv as -h/-p/-U/-d.
    """
    from urllib.parse import urlparse

    url = current_app.config.get("DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set; cannot run pg_dump")
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    user = parsed.username or "postgres"
    password = parsed.password or ""
    db = (parsed.path or "/").lstrip("/")
    if not db:
        raise RuntimeError(f"DATABASE_URL has no database name: {url}")

    argv = [
        "pg_dump",
        "-h",
        host,
        "-p",
        str(port),
        "-U",
        user,
        "-d",
        db,
        "--format=custom",  # binary, smaller, restorable selectively
        "--no-owner",
        "--no-privileges",
        "--verbose",
    ]
    env = os.environ.copy()
    if password:
        env["PGPASSWORD"] = password
    return argv, env


@shared_task(bind=True, name="backup.database", max_retries=2, default_retry_delay=600)
def backup_database_task(self):
    """Nightly Postgres backup — pg_dump → gzip → optional offsite ship."""
    try:
        backup_dir = _backup_local_dir()
        ts = _timestamp()
        target = backup_dir / f"db-{ts}.dump.gz"

        if not shutil.which("pg_dump"):
            raise RuntimeError("pg_dump binary not found on PATH")

        argv, env = _build_pg_dump_env()

        # Stream pg_dump's stdout through gzip into the target file. Avoids
        # writing an uncompressed intermediate to disk (could be many GB).
        logger.info("Starting database backup → %s", target)
        with gzip.open(target, "wb") as out_fh:
            proc = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            assert proc.stdout is not None
            shutil.copyfileobj(proc.stdout, out_fh)
            _, stderr_data = proc.communicate()
        if proc.returncode != 0:
            target.unlink(missing_ok=True)
            raise RuntimeError(f"pg_dump exited {proc.returncode}: {stderr_data[:2000]}")

        os.chmod(target, 0o600)
        size_mb = target.stat().st_size / (1024 * 1024)
        logger.info("Database backup complete: %s (%.1f MB)", target.name, size_mb)

        # Offsite ship
        remote_path = _push_offsite(target, remote_subdir="db")

        # Retention sweeps — local filesystem + offsite remote. Drive has no
        # lifecycle policy, so we prune it explicitly with a longer window.
        deleted = _enforce_local_retention(backup_dir, prefix="db-", days=_retention_days())
        _enforce_offsite_retention(remote_subdir="db", prefix="db-", days=_offsite_retention_days("db"))

        audit_logger.log_event(
            event_type=AuditEventType.SYSTEM_MAINTENANCE,
            action="backup_database_completed",
            severity=AuditSeverity.LOW,
            resource_type="backup",
            resource_id=target.name,
            description="Nightly database backup complete",
            additional_data={
                "local_path": str(target),
                "size_mb": round(size_mb, 1),
                "remote_path": remote_path,
                "expired_local_files_deleted": deleted,
            },
        )
        return {"success": True, "local": str(target), "remote": remote_path, "size_mb": size_mb}

    except Exception as exc:
        logger.exception("backup_database_task failed")
        audit_logger.log_event(
            event_type=AuditEventType.SYSTEM_MAINTENANCE,
            action="backup_database_failed",
            severity=AuditSeverity.HIGH,
            resource_type="backup",
            description=f"Database backup failed: {exc}",
            success=False,
            additional_data={"error": str(exc)},
        )
        # Re-raise so Celery retries (default_retry_delay=600 → 10 min, twice)
        raise self.retry(exc=exc)


# ---- Uploads backup --------------------------------------------------------


@shared_task(bind=True, name="backup.uploads", max_retries=2, default_retry_delay=600)
def backup_uploads_task(self):
    """Weekly tar+gzip backup of UPLOAD_FOLDER → optional offsite ship."""
    try:
        upload_folder = current_app.config.get("UPLOAD_FOLDER", "uploads/")
        # Resolve relative to project root if not absolute (matches the same
        # logic in business_app/__init__.py::uploaded_file).
        if not os.path.isabs(upload_folder):
            project_root = Path(current_app.root_path).parent
            uploads_path = (project_root / upload_folder).resolve()
        else:
            uploads_path = Path(upload_folder).resolve()

        if not uploads_path.exists() or not uploads_path.is_dir():
            logger.warning("Upload folder %s does not exist; skipping backup", uploads_path)
            return {"success": True, "skipped": True, "reason": "upload_folder_missing"}

        backup_dir = _backup_local_dir()
        ts = _timestamp()
        target = backup_dir / f"uploads-{ts}.tar.gz"

        # Stream tar | gzip directly to disk. shutil.make_archive would also
        # work, but we want explicit control over the gzip level + permissions.
        logger.info("Starting uploads backup → %s", target)
        argv = ["tar", "-czf", str(target), "-C", str(uploads_path.parent), uploads_path.name]
        result = _shell_run(argv, check=False)
        if result.returncode != 0:
            target.unlink(missing_ok=True)
            raise RuntimeError(f"tar exited {result.returncode}: {result.stderr[:2000]}")

        os.chmod(target, 0o600)
        size_mb = target.stat().st_size / (1024 * 1024)
        logger.info("Uploads backup complete: %s (%.1f MB)", target.name, size_mb)

        remote_path = _push_offsite(target, remote_subdir="uploads")

        deleted = _enforce_local_retention(backup_dir, prefix="uploads-", days=_retention_days())
        _enforce_offsite_retention(remote_subdir="uploads", prefix="uploads-", days=_offsite_retention_days("uploads"))

        audit_logger.log_event(
            event_type=AuditEventType.SYSTEM_MAINTENANCE,
            action="backup_uploads_completed",
            severity=AuditSeverity.LOW,
            resource_type="backup",
            resource_id=target.name,
            description="Weekly uploads backup complete",
            additional_data={
                "local_path": str(target),
                "size_mb": round(size_mb, 1),
                "remote_path": remote_path,
                "expired_local_files_deleted": deleted,
            },
        )
        return {"success": True, "local": str(target), "remote": remote_path, "size_mb": size_mb}

    except Exception as exc:
        logger.exception("backup_uploads_task failed")
        audit_logger.log_event(
            event_type=AuditEventType.SYSTEM_MAINTENANCE,
            action="backup_uploads_failed",
            severity=AuditSeverity.HIGH,
            resource_type="backup",
            description=f"Uploads backup failed: {exc}",
            success=False,
            additional_data={"error": str(exc)},
        )
        raise self.retry(exc=exc)
