"""CLI commands for subscriptions (spec 2026-10-27 §6.5, §17 Q7)."""

import click
from flask.cli import with_appcontext

from business_app import db
from business_app.services.subscription_service import SubscriptionService


@click.group()
def subscriptions():
    """Subscription commands"""


@subscriptions.command("reconcile-delivery-days")
@click.option("--dry-run", is_flag=True, help="Count what would change, change nothing and tell nobody")
@with_appcontext
def reconcile_delivery_days(dry_run):
    """Once at deploy: move every live subscription whose weekly day or next delivery is no longer a delivery day to
    the next one, and tell each customer. Daily subscribers are never told of added days here."""
    logs = SubscriptionService.reconcile_with_slots()
    count = len(logs)
    if dry_run:
        db.session.rollback()
        click.echo(f"{count} subscriptions would change")
        return
    db.session.commit()
    SubscriptionService._deliver(logs)
    click.echo(f"{count} subscriptions changed")


def register_subscription_commands(app):
    """Register subscription CLI commands"""
    app.cli.add_command(subscriptions)
