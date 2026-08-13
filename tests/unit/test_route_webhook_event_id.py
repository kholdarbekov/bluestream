"""notify_route_updated must carry a unique event_id so the bot's dedup keys
on the actual event instead of falling back to a constant in-memory key that
swallows every push after the first (spec §2.3).

⚠️ This behaviour must only ever ship together with or after the Task 7
materiality gate — see the plan's ordering warning."""

from unittest.mock import patch

import pytest

from business_app.utils.bot_webhook import notify_route_updated


@pytest.fixture
def _telegram_resolved():
    with patch(
        "business_app.utils.bot_webhook._resolve_driver_telegram_id", return_value=777000081
    ):
        yield


@pytest.mark.unit
def test_event_id_present_and_formatted(app, _telegram_resolved):
    with app.app_context():
        with patch(
            "business_app.utils.bot_webhook._send_staff_bot_webhook", return_value=True
        ) as hook:
            notify_route_updated(42, sound=True, materiality={}, trigger="auto")
    payload = hook.call_args.args[1]
    event_id = payload["event_id"]
    prefix, _, suffix = event_id.partition(":")
    assert prefix == "route_updated"
    assert len(suffix) == 32
    int(suffix, 16)  # hex or raise


@pytest.mark.unit
def test_event_id_unique_per_call(app, _telegram_resolved):
    with app.app_context():
        with patch(
            "business_app.utils.bot_webhook._send_staff_bot_webhook", return_value=True
        ) as hook:
            notify_route_updated(42, sound=True, materiality={}, trigger="auto")
            notify_route_updated(42, sound=True, materiality={}, trigger="auto")
    ids = [c.args[1]["event_id"] for c in hook.call_args_list]
    assert len(set(ids)) == 2


@pytest.mark.unit
def test_explicit_event_id_is_used_verbatim_not_overridden(app, _telegram_resolved):
    """Task 8 review fix 1: `optimize_driver_route_task` derives event_id
    from its own Celery task id (stable across a `self.retry()` re-run) and
    hands it to `notify_route_updated` explicitly. That only works if an
    explicit `event_id` wins over the random per-call fallback -- prove the
    contract at the unit level, independent of Celery machinery."""
    with app.app_context():
        with patch(
            "business_app.utils.bot_webhook._send_staff_bot_webhook", return_value=True
        ) as hook:
            notify_route_updated(
                42, sound=True, materiality={}, trigger="auto", event_id="route_updated:my-task-id"
            )
    payload = hook.call_args.args[1]
    assert payload["event_id"] == "route_updated:my-task-id"


@pytest.mark.unit
def test_explicit_event_id_repeated_across_calls_stays_identical(app, _telegram_resolved):
    """The retry-stability half of the same contract: passing the SAME
    explicit event_id on two separate calls (simulating an original push and
    a retry of it) must NOT be perturbed into two different ids -- that is
    exactly the "too unique" bug the review flagged."""
    with app.app_context():
        with patch(
            "business_app.utils.bot_webhook._send_staff_bot_webhook", return_value=True
        ) as hook:
            notify_route_updated(
                42, sound=True, materiality={}, trigger="auto", event_id="route_updated:retry-id"
            )
            notify_route_updated(
                42, sound=True, materiality={}, trigger="auto", event_id="route_updated:retry-id"
            )
    ids = [c.args[1]["event_id"] for c in hook.call_args_list]
    assert ids == ["route_updated:retry-id", "route_updated:retry-id"]
