"""Celery liveness heartbeat.

Beat sends `ops.celery_heartbeat` every 5 minutes; the worker records when it ran.
The worker healthcheck and the CeleryHeartbeatStale alert read that timestamp.
"""

import time

from celery import shared_task

from business_app import redis_client

HEARTBEAT_KEY = "bluestream:celery:heartbeat"


@shared_task(name="ops.celery_heartbeat", ignore_result=True)
def celery_heartbeat() -> float:
    now = time.time()
    redis_client.set(HEARTBEAT_KEY, now)
    return now
