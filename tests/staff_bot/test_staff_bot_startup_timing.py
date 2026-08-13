"""Regression test for the staff_bot startup crash fixed on 2026-08-13.

`_install_update_timing()` used to do `application.process_update =
timed_process_update` to wrap the whole-update timing instrumentation.
`telegram.ext.Application` defines `__slots__`, so that instance-attribute
assignment is impossible and raised on every single startup:

    AttributeError: 'Application' object attribute 'process_update' is
    read-only

Nothing in `tests/` constructed a real `telegram.ext.Application`, so this
guaranteed-fatal bug shipped and the container crash-looped in production.

The fix (`staff_bot/bot.py`, class `TimedApplication`) subclasses
`Application` and overrides `process_update`, wiring the subclass in via
`ApplicationBuilder.application_class(TimedApplication)` -- the same builder
chain `StaffBot.initialize()` uses. This test builds a REAL `Application`
through that exact call (dummy token, no network I/O) and proves the timing
wrapper is actually installed and executes around the *whole* update,
including when the wrapped call raises.

Do NOT assert on caplog records here: importing `staff_bot.bot` disables log
propagation process-wide, so caplog would never see anything regardless of
whether the code is correct. Instead this monkeypatches the module-level
`logger` object in `staff_bot.bot` directly and asserts on the calls made to
it -- a behavioural assertion on what the code actually did, not a capture of
propagated log records.
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from telegram import Chat, Message, Update, User
from telegram.ext import Application

from staff_bot import bot as staff_bot_module
from staff_bot.bot import TimedApplication

# Not a real bot token -- shaped like one so PTB's token validation accepts
# it, but it is never used to make a network call anywhere in this file.
DUMMY_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"


def _build_application() -> Application:
    """Mirrors the exact production wiring in `StaffBot.initialize()`:
    `Application.builder().application_class(TimedApplication)....build()`.
    Building an Application does no network I/O by itself (the HTTP client
    is created lazily on first use), so this is safe to call directly in a
    unit test.
    """
    return (
        Application.builder()
        .application_class(TimedApplication)
        .token(DUMMY_TOKEN)
        .build()
    )


def _real_update(text: str = "hello") -> Update:
    """A real telegram.Update, not a Mock, so attribute access inside the
    timing wrapper (`update.effective_user`, `update.callback_query`, ...)
    exercises the real PTB object model."""
    chat = Chat(id=1, type="private")
    user = User(id=42, is_bot=False, first_name="Driver")
    msg = Message(
        message_id=1, date=datetime.now(timezone.utc), chat=chat,
        from_user=user, text=text,
    )
    return Update(update_id=1, message=msg)


@pytest.mark.unit
class TestTimedApplicationBuildsThroughRealBuilder:
    """Pins the fix: building through `.application_class(TimedApplication)`
    must succeed and produce an object whose class genuinely overrides
    `process_update` -- as opposed to the old code, which tried (and always
    failed) to assign an instance attribute of the same name."""

    def test_builder_produces_a_timed_application_instance(self):
        app = _build_application()
        assert isinstance(app, TimedApplication)
        assert isinstance(app, Application)

    def test_process_update_is_a_real_class_override_not_an_instance_shim(self):
        # This is the exact distinction that crashed production: the broken
        # code never got as far as "overridden", it died trying to shadow
        # process_update per-instance on a __slots__ class.
        assert TimedApplication.process_update is not Application.process_update
        app = _build_application()
        assert type(app).process_update is TimedApplication.process_update

    def test_instance_attribute_assignment_is_the_bug_this_replaced(self):
        """Documents *why* a subclass was necessary, by reproducing the exact
        crash on a PLAIN (non-instrumented) `Application` -- the class the
        old code actually assigned onto. `Application` declares `__slots__`
        without `__dict__`, so `process_update` (a plain method, not itself a
        slot) can't be shadowed per-instance; note this is specifically a
        property of `Application`, not of `TimedApplication` -- subclassing
        without redeclaring `__slots__ = ()` gives the *subclass* a `__dict__`,
        which is exactly why the subclass route (this fix) works at all."""
        plain_app = (
            Application.builder().token(DUMMY_TOKEN).build()
        )
        with pytest.raises(AttributeError, match="read-only"):
            plain_app.process_update = lambda update: None


@pytest.mark.unit
class TestTimedApplicationWrapsWholeUpdate:
    """Proves the wrapper covers the WHOLE update and always reports from a
    `finally:`, per the load-bearing reasoning in TimedApplication's
    docstring: a last-group handler is skipped on ApplicationHandlerStop or
    an earlier group's error, so only wrapping the outermost process_update
    call guarantees every update -- including failed ones -- is measured."""

    def test_reports_even_when_the_wrapped_call_raises(self, monkeypatch):
        """finally: must still run and the wrapper must not swallow the
        original exception (a swallowed exception would silently break PTB's
        own error handling)."""
        monkeypatch.setenv("STAFF_BOT_SLOW_UPDATE_SECONDS", "1000")
        mock_logger = MagicMock()
        monkeypatch.setattr(staff_bot_module, "logger", mock_logger)

        async def boom(self, update):
            raise RuntimeError("handler exploded")

        monkeypatch.setattr(Application, "process_update", boom)

        app = _build_application()

        with pytest.raises(RuntimeError, match="handler exploded"):
            asyncio.run(app.process_update(_real_update()))

        # The finally: block ran and reported despite the failure -- this is
        # the "failed update is still measured" guarantee from the docstring.
        assert mock_logger.info.called or mock_logger.warning.called

    def test_slow_update_logs_warning_above_threshold(self, monkeypatch):
        monkeypatch.setenv("STAFF_BOT_SLOW_UPDATE_SECONDS", "0")
        mock_logger = MagicMock()
        monkeypatch.setattr(staff_bot_module, "logger", mock_logger)

        async def instant(self, update):
            return None

        monkeypatch.setattr(Application, "process_update", instant)

        app = _build_application()

        asyncio.run(app.process_update(_real_update()))

        mock_logger.warning.assert_called_once()
        message = mock_logger.warning.call_args.args[0]
        assert message.startswith("slow_update elapsed=")
        mock_logger.info.assert_not_called()

    def test_fast_update_logs_info_below_threshold(self, monkeypatch):
        monkeypatch.setenv("STAFF_BOT_SLOW_UPDATE_SECONDS", "1000")
        mock_logger = MagicMock()
        monkeypatch.setattr(staff_bot_module, "logger", mock_logger)

        async def instant(self, update):
            return None

        monkeypatch.setattr(Application, "process_update", instant)

        app = _build_application()

        asyncio.run(app.process_update(_real_update()))

        mock_logger.info.assert_called_once()
        message = mock_logger.info.call_args.args[0]
        assert message.startswith("update_processed elapsed=")
        mock_logger.warning.assert_not_called()

    def test_uses_configured_env_threshold_not_a_hardcoded_default(self, monkeypatch):
        """Regression guard for the threshold itself: STAFF_BOT_SLOW_UPDATE_SECONDS
        must actually gate the branch, not just exist."""
        mock_logger = MagicMock()
        monkeypatch.setattr(staff_bot_module, "logger", mock_logger)

        async def instant(self, update):
            return None

        monkeypatch.setattr(Application, "process_update", instant)


        monkeypatch.setenv("STAFF_BOT_SLOW_UPDATE_SECONDS", "0")
        app = _build_application()
        asyncio.run(app.process_update(_real_update()))
        assert mock_logger.warning.called
        mock_logger.reset_mock()

        monkeypatch.setenv("STAFF_BOT_SLOW_UPDATE_SECONDS", "1000")
        asyncio.run(app.process_update(_real_update()))
        assert mock_logger.info.called
        assert not mock_logger.warning.called


@pytest.mark.unit
class TestTimedApplicationWiredIntoProduction:
    """Static guard, same style as TestConversationMenuEscapeWiring elsewhere
    in this package: makes sure the class this test exercises is actually
    the one StaffBot.initialize() wires in, and that the old, always-broken
    instance-assignment pattern hasn't crept back in."""

    def test_initialize_wires_timed_application_via_application_class(self):
        import inspect
        source = inspect.getsource(staff_bot_module)
        assert ".application_class(TimedApplication)" in source

    def test_no_instance_attribute_assignment_of_process_update(self):
        """AST-based, not a text grep: the fix's own docstring legitimately
        *mentions* the old broken line as documentation
        (`` `application.process_update = ...` raises ... ``), so a plain
        substring search would false-positive on the explanation of the bug.
        What must never reappear is an actual `Assign` statement whose target
        is an attribute named `process_update` -- that is the statement that
        always raised AttributeError in production."""
        import ast
        import inspect

        source = inspect.getsource(staff_bot_module)
        tree = ast.parse(source)

        offending = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Attribute) and target.attr == "process_update"
        ]
        assert not offending, (
            "found an instance-attribute assignment to process_update at "
            f"line(s) {[n.lineno for n in offending]} -- this is the exact "
            "pattern that crashed staff_bot on startup"
        )
