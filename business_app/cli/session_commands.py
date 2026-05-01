"""
CLI commands for session and user cleanup management
"""

import click
from flask.cli import with_appcontext
from business_app.services.session_cleanup_service import SessionCleanupService


@click.group()
def session():
    """Session management commands"""


@session.command()
@click.option("--batch-size", default=1000, help="Batch size for processing sessions")
@click.option("--dry-run", is_flag=True, help="Show what would be cleaned without making changes")
@with_appcontext
def cleanup_sessions(batch_size, dry_run):
    """Clean up expired user sessions"""
    service = SessionCleanupService()

    if dry_run:
        stats = service.get_cleanup_statistics()
        click.echo("=== Session Cleanup Statistics (DRY RUN) ===")
        click.echo(f"Total sessions: {stats.get('total_sessions', 0)}")
        click.echo(f"Active sessions: {stats.get('active_sessions', 0)}")
        click.echo(f"Expired sessions: {stats.get('expired_sessions', 0)}")
        click.echo(f"Old expired sessions to be removed: {stats.get('old_expired_sessions', 0)}")
        click.echo("\nUse --no-dry-run to perform actual cleanup")
    else:
        click.echo("Starting session cleanup...")
        results = service.cleanup_expired_sessions(batch_size)

        click.echo("=== Session Cleanup Results ===")
        click.echo(f"Expired sessions removed: {results.get('expired_sessions_removed', 0)}")
        click.echo(f"Sessions marked inactive: {results.get('inactive_sessions_updated', 0)}")
        click.echo(f"Redis sessions cleaned: {results.get('redis_sessions_cleaned', 0)}")

        if results.get("errors", 0) > 0:
            click.echo(f"Errors encountered: {results['errors']}", err=True)
        else:
            click.echo("Session cleanup completed successfully!")


@session.command()
@click.option("--batch-size", default=500, help="Batch size for processing users")
@click.option("--dry-run", is_flag=True, help="Show what would be cleaned without making changes")
@with_appcontext
def cleanup_users(batch_size, dry_run):
    """Clean up inactive users and their data"""
    service = SessionCleanupService()

    if dry_run:
        stats = service.get_cleanup_statistics()
        click.echo("=== User Cleanup Statistics (DRY RUN) ===")
        click.echo(f"Total users: {stats.get('total_users', 0)}")
        click.echo(f"Active users: {stats.get('active_users', 0)}")
        click.echo(f"Inactive users: {stats.get('inactive_users', 0)}")
        click.echo(f"Users needing cleanup: {stats.get('users_needing_cleanup', 0)}")
        click.echo(f"Expired reset tokens: {stats.get('expired_reset_tokens', 0)}")
        click.echo("\nUse --no-dry-run to perform actual cleanup")
    else:
        click.echo("Starting user cleanup...")
        results = service.cleanup_inactive_users(batch_size)

        click.echo("=== User Cleanup Results ===")
        click.echo(f"Users marked inactive: {results.get('users_marked_inactive', 0)}")
        click.echo(f"Orphaned sessions removed: {results.get('orphaned_sessions_removed', 0)}")
        click.echo(f"Bot states cleared: {results.get('bot_states_cleared', 0)}")

        if results.get("errors", 0) > 0:
            click.echo(f"Errors encountered: {results['errors']}", err=True)
        else:
            click.echo("User cleanup completed successfully!")


@session.command()
@click.option("--dry-run", is_flag=True, help="Show what would be cleaned without making changes")
@with_appcontext
def cleanup_orphaned(dry_run):
    """Clean up orphaned data and invalid records"""
    service = SessionCleanupService()

    if dry_run:
        stats = service.get_cleanup_statistics()
        click.echo("=== Orphaned Data Statistics (DRY RUN) ===")
        click.echo(f"Expired reset tokens: {stats.get('expired_reset_tokens', 0)}")
        click.echo("\nNote: Orphaned sessions count requires database query during cleanup")
        click.echo("Use --no-dry-run to perform actual cleanup and see full results")
    else:
        click.echo("Starting orphaned data cleanup...")
        results = service.cleanup_orphaned_data()

        click.echo("=== Orphaned Data Cleanup Results ===")
        click.echo(f"Orphaned sessions removed: {results.get('orphaned_sessions_removed', 0)}")
        click.echo(f"Invalid tokens cleaned: {results.get('invalid_tokens_cleaned', 0)}")
        click.echo(f"Password reset tokens cleared: {results.get('password_reset_tokens_cleared', 0)}")

        if results.get("errors", 0) > 0:
            click.echo(f"Errors encountered: {results['errors']}", err=True)
        else:
            click.echo("Orphaned data cleanup completed successfully!")


@session.command()
@click.option("--batch-size", default=1000, help="Batch size for processing")
@click.option("--dry-run", is_flag=True, help="Show what would be cleaned without making changes")
@with_appcontext
def full_cleanup(batch_size, dry_run):
    """Perform comprehensive cleanup of all session and user data"""
    service = SessionCleanupService()

    if dry_run:
        stats = service.get_cleanup_statistics()
        click.echo("=== Full Cleanup Statistics (DRY RUN) ===")
        click.echo(f"Total users: {stats.get('total_users', 0)}")
        click.echo(f"Active users: {stats.get('active_users', 0)}")
        click.echo(f"Inactive users: {stats.get('inactive_users', 0)}")
        click.echo(f"Total sessions: {stats.get('total_sessions', 0)}")
        click.echo(f"Active sessions: {stats.get('active_sessions', 0)}")
        click.echo(f"Expired sessions: {stats.get('expired_sessions', 0)}")
        click.echo(f"Old expired sessions: {stats.get('old_expired_sessions', 0)}")
        click.echo(f"Users needing cleanup: {stats.get('users_needing_cleanup', 0)}")
        click.echo(f"Expired reset tokens: {stats.get('expired_reset_tokens', 0)}")
        click.echo("\nUse --no-dry-run to perform actual cleanup")
    else:
        click.echo("Starting full cleanup...")
        results = service.full_cleanup(batch_size)

        click.echo("=== Full Cleanup Results ===")
        click.echo("\nInitial State:")
        initial = results.get("initial_state", {})
        click.echo(f"  Total users: {initial.get('total_users', 0)}")
        click.echo(f"  Total sessions: {initial.get('total_sessions', 0)}")
        click.echo(f"  Active sessions: {initial.get('active_sessions', 0)}")

        click.echo("\nFinal State:")
        final = results.get("final_state", {})
        click.echo(f"  Total users: {final.get('total_users', 0)}")
        click.echo(f"  Total sessions: {final.get('total_sessions', 0)}")
        click.echo(f"  Active sessions: {final.get('active_sessions', 0)}")

        click.echo("\nOperations Summary:")
        ops = results.get("operations", {})

        session_ops = ops.get("session_cleanup", {})
        click.echo(f"  Expired sessions removed: {session_ops.get('expired_sessions_removed', 0)}")
        click.echo(f"  Sessions marked inactive: {session_ops.get('inactive_sessions_updated', 0)}")

        user_ops = ops.get("user_cleanup", {})
        click.echo(f"  Users marked inactive: {user_ops.get('users_marked_inactive', 0)}")
        click.echo(f"  Bot states cleared: {user_ops.get('bot_states_cleared', 0)}")

        orphaned_ops = ops.get("orphaned_cleanup", {})
        click.echo(f"  Orphaned sessions removed: {orphaned_ops.get('orphaned_sessions_removed', 0)}")
        click.echo(f"  Reset tokens cleared: {orphaned_ops.get('password_reset_tokens_cleared', 0)}")

        total_errors = results.get("total_errors", 0)
        if total_errors > 0:
            click.echo(f"\nTotal errors: {total_errors}", err=True)
        else:
            click.echo("\nFull cleanup completed successfully!")


@session.command()
@with_appcontext
def stats():
    """Show session and user statistics"""
    service = SessionCleanupService()
    stats = service.get_cleanup_statistics()

    click.echo("=== Session and User Statistics ===")
    click.echo(f"Total users: {stats.get('total_users', 0)}")
    click.echo(f"Active users: {stats.get('active_users', 0)}")
    click.echo(f"Inactive users: {stats.get('inactive_users', 0)}")
    click.echo(f"Users needing cleanup: {stats.get('users_needing_cleanup', 0)}")
    click.echo("")
    click.echo(f"Total sessions: {stats.get('total_sessions', 0)}")
    click.echo(f"Active sessions: {stats.get('active_sessions', 0)}")
    click.echo(f"Expired sessions: {stats.get('expired_sessions', 0)}")
    click.echo(f"Old expired sessions: {stats.get('old_expired_sessions', 0)}")
    click.echo("")
    click.echo(f"Expired reset tokens: {stats.get('expired_reset_tokens', 0)}")


def register_session_commands(app):
    """Register session management CLI commands"""
    app.cli.add_command(session)
