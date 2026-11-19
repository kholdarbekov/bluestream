"""What a bot's `before_send` lets out of the process, for events the real Sentry SDK builds.

A `sentry_sdk.Client` with no DSN and no transport runs in an isolation scope of its own: the SDK builds each event as
in production (frame locals, logging breadcrumbs), hands it to the bot's `before_send`, and the result is kept here.
Nothing is sent, and the process's global client is never touched.
"""

import sentry_sdk
from sentry_sdk.integrations.logging import LoggingIntegration


def events_after(before_send, act, **options):
    """Every event `act()` reports, as `before_send` returns it."""
    seen = []

    def _keep(event, hint):
        seen.append(before_send(event, hint))
        return None

    client = sentry_sdk.Client(
        before_send=_keep,
        integrations=[LoggingIntegration()],
        default_integrations=False,
        auto_enabling_integrations=False,
        send_default_pii=False,
        **options,
    )
    assert client.dsn is None and client.transport is None  # kept, never sent
    with sentry_sdk.isolation_scope():
        sentry_sdk.get_current_scope().set_client(client)
        act()
    client.close()
    return seen


def frames_of(event):
    for holder in [
        event,
        *(event.get("threads") or {}).get("values", []),
        *(event.get("exception") or {}).get("values", []),
    ]:
        yield from ((holder or {}).get("stacktrace") or {}).get("frames") or []


def vars_of(event, function):
    [frame] = [frame for frame in frames_of(event) if frame.get("function") == function]
    return frame["vars"]
