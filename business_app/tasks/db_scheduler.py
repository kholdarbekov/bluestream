"""Custom Celery beat scheduler that overlays one DB-backed entry on the static
``beat_schedule`` dictionary.

Only the ``pre-register-marking-codes`` entry is overridden; every other
scheduled task continues to use the hard-coded ``crontab`` defined in
:mod:`business_app.tasks.celery_app`.

Reload mechanism
----------------
At startup, ``setup_schedule`` reads the singleton ``MarkingCodeTaskConfig``
row and replaces the marking-code entry. The current ``schedule_version`` is
cached. On every ``sync()`` tick (default once per minute), we re-read just
that integer; if it has been bumped by an admin via the API, we ``os._exit``
the beat process. The ``celery_beat`` container's ``restart: unless-stopped``
policy in ``docker-compose.yml`` immediately respawns the process, which then
loads the new schedule on its next boot.

No new dependency (django-celery-beat, redbeat, etc.) — this is sufficient
because only one task needs runtime-editable scheduling.
"""

from __future__ import annotations

import logging
import os

from celery.beat import PersistentScheduler

from business_app.tasks.celery_app import _flask_app

logger = logging.getLogger(__name__)


_MARKING_CODE_ENTRY_KEY = "pre-register-marking-codes"
_MARKING_CODE_TASK_NAME = "business_app.tasks.marking_code_tasks.pre_register_marking_codes_daily"


class DBBackedScheduler(PersistentScheduler):
    """``PersistentScheduler`` + DB override for the marking-code task."""

    def __init__(self, *args, **kwargs):
        self._loaded_schedule_version: int | None = None
        super().__init__(*args, **kwargs)

    # -- Lifecycle ----------------------------------------------------

    def setup_schedule(self):
        super().setup_schedule()
        self._apply_db_overlay(initial=True)

    def sync(self):
        # Check whether the admin bumped schedule_version since we loaded.
        try:
            current_version = self._read_schedule_version()
        except Exception:
            logger.warning("DBBackedScheduler: failed to read schedule_version", exc_info=True)
            current_version = None

        if (
            current_version is not None
            and self._loaded_schedule_version is not None
            and current_version != self._loaded_schedule_version
        ):
            logger.info(
                "DBBackedScheduler: schedule_version bumped (%s -> %s), exiting for reload",
                self._loaded_schedule_version,
                current_version,
            )
            # super().sync() flushes the schedule file before we exit so we
            # don't fire the same task twice after restart.
            try:
                super().sync()
            except Exception:
                logger.warning("DBBackedScheduler: sync flush failed before exit", exc_info=True)
            # The celery_beat container restart policy will respawn us.
            os._exit(0)
        super().sync()

    # -- Internals ----------------------------------------------------

    def _read_schedule_version(self) -> int | None:
        """Cheap read — pull only id + schedule_version, no other columns."""
        if _flask_app is None:
            return None
        from business_app import db
        from business_app.models.marking_code_config import MarkingCodeTaskConfig

        with _flask_app.app_context():
            row = (
                db.session.query(MarkingCodeTaskConfig.id, MarkingCodeTaskConfig.schedule_version)
                .filter(MarkingCodeTaskConfig.id == 1)
                .one_or_none()
            )
            return int(row.schedule_version) if row else None

    def _apply_db_overlay(self, initial: bool = False) -> None:
        """Replace the marking-code entry in ``self.schedule`` from the DB row."""
        if _flask_app is None:
            return
        from business_app.services.marking_code_config_service import (
            MarkingCodeConfigService,
        )

        with _flask_app.app_context():
            try:
                cfg = MarkingCodeConfigService().get_config()
                cron = MarkingCodeConfigService().to_crontab(cfg)
            except Exception:
                logger.warning(
                    "DBBackedScheduler: failed to load marking-code config, keeping default",
                    exc_info=True,
                )
                return

        existing = self.schedule.get(_MARKING_CODE_ENTRY_KEY)
        entry_kwargs = {
            "name": _MARKING_CODE_ENTRY_KEY,
            "task": _MARKING_CODE_TASK_NAME,
            "schedule": cron,
            "app": self.app,
        }
        if existing is not None:
            # Preserve any operational fields celery added on top.
            entry_kwargs["args"] = getattr(existing, "args", ()) or ()
            entry_kwargs["kwargs"] = getattr(existing, "kwargs", {}) or {}
            entry_kwargs["options"] = getattr(existing, "options", {}) or {}

        self.schedule[_MARKING_CODE_ENTRY_KEY] = self.Entry(**entry_kwargs)
        self._loaded_schedule_version = int(cfg.schedule_version)
        if initial:
            logger.info(
                "DBBackedScheduler: loaded marking-code schedule version=%s cron=%s",
                self._loaded_schedule_version,
                cron,
            )
