"""What Celery and Sentry show of a task's secret arguments: never the value.

Celery prints a task's arguments wherever it reports the task: the worker's "Task ...[id] received" line and its failure
line (the message's `argsrepr`/`kwargsrepr` headers), the `task-sent` event Flower keeps (built from the same `argsrepr`
before any publish signal runs), and Sentry's Celery integration attaches them raw to every event raised while the task
runs. Loki held live sign-up codes with full numbers (2026-11-15), and password-reset and email-verification links,
enough to take over the account.

A task declares its secret parameters once, each with the mask it is shown by:

    @shared_task(bind=True, ..., **secret_arguments(otp_code=hidden, phone=mask_phone_number))

and then every publish (.delay, .apply_async, a signature, the retry Celery re-sends itself) shows them masked, and a
Sentry event raised while a worker runs the task carries none of their values. Of a dict or a list (a webhook's payload
and headers) those are the strings under a secret name (`shared.secret_scrub.names_a_secret`: Click's `sign_string`,
Payme's `Authorization`); the rest of it (the order number, the method, the content type) is what the event is read
for, and stays. The task itself still receives the real values. What a task returns is printed on its "succeeded"
line, so it must not return a secret either.
"""

import inspect

import sentry_sdk
from celery import current_task
from celery.signals import task_prerun
from celery.utils.saferepr import saferepr

from shared.secret_scrub import names_a_secret, worth_following

HIDDEN = "******"
# Shorter values are not looked for in Sentry events: an empty one would match everywhere. A code has six digits.
_SHORTEST_SCRUBBED_VALUE = 4


def hidden(_value):
    """The mask of a value no log may show at all: a code, a token."""
    return HIDDEN


def secret_arguments(**masks):
    """Options for the task decorator: which parameters are secret, and the function each is shown through.

    Celery sets every decorator option on the task's class, the documented way to customise one task (see
    `Task.shadow_name`), so the task keeps its app's own base class: `ContextTask` in the worker, Celery's `Task` where
    the API publishes through the default app.
    """
    return {"secret_arguments": masks, "apply_async": _apply_async_with_secrets_masked}


def _parameters(task):
    """The names the task's arguments bind to, in order (`self` is not one for a bound task)."""
    return list(inspect.signature(task.run).parameters)


def masked_arguments(task, args, kwargs):
    """`args` and `kwargs` of a call to `task`, every secret one replaced by its mask."""
    masks = task.secret_arguments

    def shown(name, value):
        mask = masks.get(name)
        return value if mask is None or value is None else mask(value)

    return (
        tuple(shown(name, value) for name, value in zip(_parameters(task), args or ())),
        {name: shown(name, value) for name, value in (kwargs or {}).items()},
    )


def _apply_async_with_secrets_masked(self, args=None, kwargs=None, *more, **options):
    """`Task.apply_async`, with the `argsrepr`/`kwargsrepr` Celery puts in the message headers and the task-sent event.

    Every dispatch through the task ends here, the worker's own retry too (`Task.retry` re-sends through the task's
    `apply_async`), and the declaration wins over an `argsrepr` a caller passes. `app.send_task` by name does not pass
    through the task; nothing sends these tasks that way.
    """
    shown_args, shown_kwargs = masked_arguments(self, args, kwargs)
    options.update(argsrepr=saferepr(shown_args), kwargsrepr=saferepr(shown_kwargs))
    return super(type(self), self).apply_async(args, kwargs, *more, **options)


def _secrets_of_running_task():
    """(value, mask) for each secret string of the task this thread is running, longest value first. An argument
    that is itself a string is looked for with and without its plus (a phone number: Eskiz is sent the digits)."""
    task = current_task
    masks = getattr(task, "secret_arguments", None) if task else None
    if not masks:
        return []
    request = task.request
    values = {**dict(zip(_parameters(task), request.args or ())), **(request.kwargs or {})}
    found = {}
    for name, mask in masks.items():
        value = values.get(name)
        if isinstance(value, str):  # a declared secret: the whole string is one, whatever its letters
            written = {string for string in (value, value.lstrip("+")) if len(string) >= _SHORTEST_SCRUBBED_VALUE}
        else:  # a payload: only what sits under a secret name, by the same rule as a frame's locals
            written = {string for string in _strings_under_secret_names(value) if worth_following(string)}
        for string in written:
            found[string] = mask(value)
    return sorted(found.items(), key=lambda pair: -len(pair[0]))


def _strings_under_secret_names(value, under_a_secret_name=False):
    """The strings of a dict or list argument that sit, at any depth, under a secret name."""
    if isinstance(value, str):
        return [value] if under_a_secret_name else []
    if isinstance(value, dict):
        return [
            string
            for key, item in value.items()
            for string in _strings_under_secret_names(
                item, under_a_secret_name or (isinstance(key, str) and names_a_secret(key))
            )
        ]
    if isinstance(value, (list, tuple)):
        return [string for item in value for string in _strings_under_secret_names(item, under_a_secret_name)]
    return []


def _without(value, secrets):
    if isinstance(value, str):
        for secret, mask in secrets:
            value = value.replace(secret, mask)
        return value
    if isinstance(value, dict):
        return {key: _without(item, secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_without(item, secrets) for item in value]
    return value


def _scrub_secrets_of_running_task(event, _hint):
    """Sentry event processor: an event raised while the task runs carries none of its secret arguments' values.

    Value by value, not by key, because the values travel: Sentry's Celery integration attaches the raw arguments
    (`extra["celery-job"]`), the task's frames and Celery's tracer frames hold them in their locals, and a letter's link
    carries its token inside the template data.
    """
    secrets = _secrets_of_running_task()
    if not secrets:
        return event
    event = _without(event, secrets)
    if event.get("type") != "transaction" and not any(key in event for key in ("exception", "stacktrace", "threads")):
        # With `attach_stacktrace` (production) Sentry adds the thread's stack, frame locals and all, only AFTER every
        # processor has run, where none can mask it. An empty list stops it: a logged error during such a task is
        # reported without the stack of the line that logged it.
        event["threads"] = {"values": []}
    return event


@task_prerun.connect
def _hide_secrets_from_sentry(task=None, **_kwargs):
    """Scrub every event of a task with secret arguments, in the scope Sentry's Celery integration opens per task.

    The integration attaches the raw arguments from a processor it adds to that scope before the task starts; added to
    the same scope here, at the start, this one runs after it, and goes when the scope closes with the task. Without
    the integration (no Sentry, as in development) there is no such scope, and nothing is added.
    """
    if getattr(task, "secret_arguments", None) and sentry_sdk.get_client().get_integration("celery"):
        sentry_sdk.get_isolation_scope().add_event_processor(_scrub_secrets_of_running_task)
