"""Unit tests for business_app/tasks/backup_tasks.py (GDRIVE-BACKUP).

Patches are applied to module-local names (business_app.tasks.backup_tasks.*)
because the module does `import subprocess`, `import shutil` and
`from business_app.utils.audit_logger import audit_logger`.
"""
import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from business_app.tasks import backup_tasks


@pytest.mark.unit
class TestOffsiteRetentionDays:
    def test_db_default_is_90(self, app, monkeypatch):
        monkeypatch.delenv("BACKUP_OFFSITE_RETENTION_DAYS_DB", raising=False)
        with app.app_context():
            app.config["BACKUP_OFFSITE_RETENTION_DAYS_DB"] = None
            assert backup_tasks._offsite_retention_days("db") == 90

    def test_uploads_default_is_365(self, app, monkeypatch):
        monkeypatch.delenv("BACKUP_OFFSITE_RETENTION_DAYS_UPLOADS", raising=False)
        with app.app_context():
            app.config["BACKUP_OFFSITE_RETENTION_DAYS_UPLOADS"] = None
            assert backup_tasks._offsite_retention_days("uploads") == 365

    def test_env_override_is_read_and_cast_to_int(self, app, monkeypatch):
        monkeypatch.setenv("BACKUP_OFFSITE_RETENTION_DAYS_DB", "30")
        with app.app_context():
            app.config["BACKUP_OFFSITE_RETENTION_DAYS_DB"] = None
            assert backup_tasks._offsite_retention_days("db") == 30

    def test_unknown_kind_falls_back_to_db_window(self, app, monkeypatch):
        monkeypatch.delenv("BACKUP_OFFSITE_RETENTION_DAYS_DB", raising=False)
        with app.app_context():
            app.config["BACKUP_OFFSITE_RETENTION_DAYS_DB"] = None
            assert backup_tasks._offsite_retention_days("something-else") == 90


@pytest.mark.unit
class TestEnforceOffsiteRetention:
    def test_noop_when_remote_unset(self, app):
        with (
            app.app_context(),
            patch("business_app.tasks.backup_tasks.shutil.which") as which,
            patch("business_app.tasks.backup_tasks.subprocess.run") as run,
        ):
            app.config["BACKUP_RCLONE_REMOTE"] = None
            backup_tasks._enforce_offsite_retention(
                remote_subdir="db", prefix="db-", days=90
            )
        run.assert_not_called()
        which.assert_not_called()

    def test_skips_when_rclone_binary_missing(self, app):
        with (
            app.app_context(),
            patch("business_app.tasks.backup_tasks.shutil.which", return_value=None),
            patch("business_app.tasks.backup_tasks.subprocess.run") as run,
        ):
            app.config["BACKUP_RCLONE_REMOTE"] = "gdrive:bluestream-backups"
            backup_tasks._enforce_offsite_retention(
                remote_subdir="db", prefix="db-", days=90
            )
        run.assert_not_called()

    def test_invokes_rclone_delete_with_correct_argv(self, app):
        completed = MagicMock(returncode=0, stderr="")
        with (
            app.app_context(),
            patch("business_app.tasks.backup_tasks.shutil.which", return_value="/usr/bin/rclone"),
            patch("business_app.tasks.backup_tasks.subprocess.run", return_value=completed) as run,
        ):
            app.config["BACKUP_RCLONE_REMOTE"] = "gdrive:bluestream-backups/"
            backup_tasks._enforce_offsite_retention(
                remote_subdir="db", prefix="db-", days=90
            )
        run.assert_called_once()
        argv = run.call_args.args[0]
        assert argv[:3] == ["rclone", "delete", "gdrive:bluestream-backups/db"]
        assert "--min-age" in argv and "90d" in argv
        assert "--include" in argv and "db-*" in argv

    def test_nonzero_exit_is_swallowed_not_raised(self, app):
        completed = MagicMock(returncode=1, stderr="quota exceeded")
        with (
            app.app_context(),
            patch("business_app.tasks.backup_tasks.shutil.which", return_value="/usr/bin/rclone"),
            patch("business_app.tasks.backup_tasks.subprocess.run", return_value=completed),
        ):
            app.config["BACKUP_RCLONE_REMOTE"] = "gdrive:bluestream-backups"
            backup_tasks._enforce_offsite_retention(
                remote_subdir="db", prefix="db-", days=90
            )

    def test_exception_is_swallowed_not_raised(self, app):
        with (
            app.app_context(),
            patch("business_app.tasks.backup_tasks.shutil.which", return_value="/usr/bin/rclone"),
            patch("business_app.tasks.backup_tasks.subprocess.run", side_effect=OSError("boom")),
        ):
            app.config["BACKUP_RCLONE_REMOTE"] = "gdrive:bluestream-backups"
            backup_tasks._enforce_offsite_retention(
                remote_subdir="db", prefix="db-", days=90
            )


@pytest.mark.unit
class TestBackupDatabaseTaskWiring:
    def _fake_popen(self):
        proc = MagicMock()
        proc.stdout = io.BytesIO(b"PGDMP-fake")
        proc.communicate.return_value = (b"", b"")
        proc.returncode = 0
        return proc

    def test_offsite_prune_invoked_when_remote_set(self, app, tmp_path):
        app.config["BACKUP_LOCAL_DIR"] = str(tmp_path)
        app.config["BACKUP_RCLONE_REMOTE"] = "gdrive:bluestream-backups"
        app.config["BACKUP_OFFSITE_RETENTION_DAYS_DB"] = "90"
        app.config["DATABASE_URL"] = "postgresql://postgres:pw@db:5432/bluestream_db"

        with (
            app.app_context(),
            patch("business_app.tasks.backup_tasks.shutil.which", side_effect=lambda b: "/usr/bin/" + b),
            patch("business_app.tasks.backup_tasks.subprocess.Popen", return_value=self._fake_popen()),
            patch("business_app.tasks.backup_tasks.subprocess.run", return_value=MagicMock(returncode=0, stderr="")) as run,
            patch("business_app.tasks.backup_tasks.audit_logger"),
        ):
            result = backup_tasks.backup_database_task.run()

        assert result["success"] is True
        delete_calls = [
            c for c in run.call_args_list
            if c.args[0][:2] == ["rclone", "delete"]
        ]
        assert len(delete_calls) == 1
        argv = delete_calls[0].args[0]
        assert argv[2] == "gdrive:bluestream-backups/db"
        assert "90d" in argv and "db-*" in argv

    def test_uploads_offsite_prune_invoked_when_remote_set(self, app, tmp_path):
        uploads_dir = tmp_path / "uploads"
        uploads_dir.mkdir()
        app.config["BACKUP_LOCAL_DIR"] = str(tmp_path)
        app.config["BACKUP_RCLONE_REMOTE"] = "gdrive:bluestream-backups"
        app.config["BACKUP_OFFSITE_RETENTION_DAYS_UPLOADS"] = "365"
        app.config["UPLOAD_FOLDER"] = str(uploads_dir)

        def fake_run(cmd, *args, **kwargs):
            # The real `tar` writes the target archive (cmd[2] for `tar -czf <target>`);
            # subprocess.run is mocked, so create it here so os.chmod()/stat() succeed.
            if cmd[:2] == ["tar", "-czf"]:
                Path(cmd[2]).write_bytes(b"fake-tar-gz")
            return MagicMock(returncode=0, stderr="")

        with (
            app.app_context(),
            patch("business_app.tasks.backup_tasks.shutil.which", side_effect=lambda b: "/usr/bin/" + b),
            patch("business_app.tasks.backup_tasks.subprocess.run", side_effect=fake_run) as run,
            patch("business_app.tasks.backup_tasks.audit_logger"),
        ):
            result = backup_tasks.backup_uploads_task.run()

        assert result["success"] is True
        assert not result.get("skipped")
        delete_calls = [
            c for c in run.call_args_list
            if c.args[0][:2] == ["rclone", "delete"]
        ]
        assert len(delete_calls) == 1
        argv = delete_calls[0].args[0]
        assert argv[2] == "gdrive:bluestream-backups/uploads"
        assert "365d" in argv and "uploads-*" in argv

    def test_no_offsite_prune_when_remote_unset(self, app, tmp_path):
        app.config["BACKUP_LOCAL_DIR"] = str(tmp_path)
        app.config["BACKUP_RCLONE_REMOTE"] = None
        app.config["DATABASE_URL"] = "postgresql://postgres:pw@db:5432/bluestream_db"

        with (
            app.app_context(),
            patch("business_app.tasks.backup_tasks.shutil.which", return_value="/usr/bin/pg_dump"),
            patch("business_app.tasks.backup_tasks.subprocess.Popen", return_value=self._fake_popen()),
            patch("business_app.tasks.backup_tasks.subprocess.run") as run,
            patch("business_app.tasks.backup_tasks.audit_logger"),
        ):
            result = backup_tasks.backup_database_task.run()

        assert result["success"] is True
        assert result["remote"] is None
        assert not any(c.args[0][0] == "rclone" for c in run.call_args_list)


@pytest.mark.unit
class TestPushOffsite:
    """`_push_offsite` ships a local backup to the rclone remote.

    Offsite failure is FATAL by design (the caller retries → Sentry), but the
    error MUST carry rclone's stderr so the root cause is visible.
    """

    def test_noop_returns_none_when_remote_unset(self, app):
        with (
            app.app_context(),
            patch("business_app.tasks.backup_tasks.shutil.which") as which,
            patch("business_app.tasks.backup_tasks.subprocess.run") as run,
        ):
            app.config["BACKUP_RCLONE_REMOTE"] = None
            assert backup_tasks._push_offsite(Path("/var/backups/db-x.dump.gz"), "db") is None
        run.assert_not_called()
        which.assert_not_called()

    def test_raises_when_rclone_binary_missing(self, app):
        with (
            app.app_context(),
            patch("business_app.tasks.backup_tasks.shutil.which", return_value=None),
        ):
            app.config["BACKUP_RCLONE_REMOTE"] = "gdrive:aqua-element-backups"
            with pytest.raises(RuntimeError, match="rclone binary"):
                backup_tasks._push_offsite(Path("/var/backups/db-x.dump.gz"), "db")

    def test_returns_remote_target_on_success(self, app):
        completed = MagicMock(returncode=0, stderr="")
        with (
            app.app_context(),
            patch("business_app.tasks.backup_tasks.shutil.which", return_value="/usr/bin/rclone"),
            patch("business_app.tasks.backup_tasks.subprocess.run", return_value=completed),
        ):
            app.config["BACKUP_RCLONE_REMOTE"] = "gdrive:aqua-element-backups/"
            target = backup_tasks._push_offsite(
                Path("/var/backups/bluestream/db-x.dump.gz"), "db"
            )
        assert target == "gdrive:aqua-element-backups/db/db-x.dump.gz"

    def test_failure_surfaces_rclone_stderr(self, app):
        """Regression for the 2026-06 outage: a 7-day Google OAuth refresh-token
        expiry (consent screen left in 'Testing' status) made `rclone copyto`
        exit 1 with `invalid_grant`. The reason never reached Loki/Sentry because
        ``str(CalledProcessError)`` drops stderr. The raised error must include it.
        """
        stderr = (
            "Failed to copy: couldn't fetch token: oauth2: cannot fetch token: "
            "400 Bad Request\ninvalid_grant: Token has been expired or revoked."
        )
        completed = MagicMock(returncode=1, stderr=stderr)
        with (
            app.app_context(),
            patch("business_app.tasks.backup_tasks.shutil.which", return_value="/usr/bin/rclone"),
            patch("business_app.tasks.backup_tasks.subprocess.run", return_value=completed),
        ):
            app.config["BACKUP_RCLONE_REMOTE"] = "gdrive:aqua-element-backups"
            with pytest.raises(RuntimeError) as exc_info:
                backup_tasks._push_offsite(
                    Path("/var/backups/bluestream/db-x.dump.gz"), "db"
                )
        msg = str(exc_info.value)
        assert "invalid_grant" in msg
        assert "Token has been expired or revoked" in msg
