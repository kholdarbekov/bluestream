"""`telegram_bot.api_client` must be safe to enter concurrently.

This guards a bug that enabling concurrent update processing would otherwise
have INTRODUCED, rather than one that already existed.

`api_client` is a module-level singleton. The old context manager built a
fresh `httpx.AsyncClient` in `__aenter__` (assigning `self._client`) and
closed it in `__aexit__`. That was safe only because PTB processed updates
strictly one at a time. The moment two handlers can run at once:

    handler A: __aenter__  -> self._client = A
    handler B: __aenter__  -> self._client = B      (A's reference is gone)
    handler A: __aexit__   -> closes B's client
    handler B: mid-request -> RuntimeError: client has been closed

The fix makes the client process-wide (`start()` / `aclose()`), turns
`__aenter__` into "ensure it exists" and `__aexit__` into a no-op. These
tests pin both halves: the client survives a nested/overlapping context, and
`aclose()` is the only thing that tears it down.
"""

import asyncio
import importlib.util
import pathlib
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

BOT_DIR = pathlib.Path(__file__).resolve().parents[2] / "telegram_bot"


@pytest.fixture
def client_module(monkeypatch):
    """Import the real client with its HTTP construction stubbed out.

    `telegram_bot/*.py` run with WORKDIR=/app/telegram_bot and use bare
    imports (`from config import config`, `from database import db_manager`).
    A plain `sys.path` insert is not enough on its own: the repo root also
    ships `config.py`, and whichever module lands in `sys.modules` first wins
    for the entire worker process — which under `pytest -n auto` depends on
    test distribution and would make this file pass or fail at random.

    So: put the bot dir first on the path, evict every bare name it shadows,
    import normally (letting Python resolve the whole chain), then restore
    `sys.modules` exactly. Nothing leaks into business_app tests that share
    the worker.
    """
    shadowed = {p.stem for p in BOT_DIR.glob("*.py")} | {"api_client"}
    saved_modules = {n: sys.modules[n] for n in list(sys.modules) if n in shadowed}
    saved_path = list(sys.path)

    for name in shadowed:
        sys.modules.pop(name, None)
    sys.path.insert(0, str(BOT_DIR))

    try:
        import api_client as module  # noqa: PLC0415 — deliberate, see docstring

        yield from _stub_and_yield(module, monkeypatch)
    finally:
        sys.path[:] = saved_path
        for name in shadowed:
            sys.modules.pop(name, None)
        sys.modules.update(saved_modules)


def _stub_and_yield(module, monkeypatch):
    """Stub httpx + the SSL smoke check, then hand the module to the test."""

    fake_clients = []

    def _fake_async_client(*args, **kwargs):
        fake = MagicMock(name=f"httpx_client_{len(fake_clients)}")
        fake.aclose = AsyncMock()
        fake.is_closed = False

        async def _aclose():
            fake.is_closed = True

        fake.aclose.side_effect = _aclose
        fake_clients.append(fake)
        return fake

    monkeypatch.setattr(module.httpx, "AsyncClient", _fake_async_client)
    # Never touch the network for the SSL smoke check.
    monkeypatch.setattr(
        module.BusinessAPIClient, "_test_ssl_connection", AsyncMock(return_value=None)
    )
    yield module, fake_clients


def _fresh(module):
    client = module.BusinessAPIClient()
    return client


@pytest.mark.unit
class TestSharedClientSurvivesConcurrentUse:
    def test_exiting_one_context_does_not_close_another_in_flight(self, client_module):
        """The exact race concurrency would have caused."""
        module, fake_clients = client_module
        api = _fresh(module)

        async def scenario():
            async with api:               # handler A enters
                async with api:           # handler B enters (overlapping)
                    pass                  # handler B exits
                # handler A is still inside — its client must still be usable
                assert api._client is not None
                assert api._client.is_closed is False
            # A has exited too; the shared client is STILL alive, because the
            # bot lifecycle owns it, not the handler.
            assert api._client is not None
            assert api._client.is_closed is False

        asyncio.run(scenario())

    def test_only_one_http_client_is_built_for_many_entries(self, client_module):
        """Per-flow client construction was also a per-flow connection setup."""
        module, fake_clients = client_module
        api = _fresh(module)

        async def scenario():
            for _ in range(5):
                async with api:
                    pass

        asyncio.run(scenario())
        assert len(fake_clients) == 1, f"built {len(fake_clients)} clients, expected 1"

    def test_concurrent_start_builds_exactly_one_client(self, client_module):
        """Two handlers entering simultaneously must not both construct one."""
        module, fake_clients = client_module
        api = _fresh(module)

        async def scenario():
            await asyncio.gather(*(api.start() for _ in range(8)))

        asyncio.run(scenario())
        assert len(fake_clients) == 1, f"built {len(fake_clients)} clients, expected 1"


@pytest.mark.unit
class TestExplicitLifecycle:
    def test_aclose_is_what_actually_closes_it(self, client_module):
        module, fake_clients = client_module
        api = _fresh(module)

        async def scenario():
            await api.start()
            built = api._client
            await api.aclose()
            assert built.is_closed is True
            assert api._client is None

        asyncio.run(scenario())

    def test_start_is_idempotent_and_reusable_after_close(self, client_module):
        """A restart mid-process must get a working client again."""
        module, fake_clients = client_module
        api = _fresh(module)

        async def scenario():
            await api.start()
            await api.start()
            await api.aclose()
            await api.start()
            assert api._client is not None
            assert api._client.is_closed is False

        asyncio.run(scenario())
        assert len(fake_clients) == 2, "expected one client before close and one after"

    def test_aclose_on_a_never_started_client_is_a_noop(self, client_module):
        module, _ = client_module
        api = _fresh(module)
        asyncio.run(api.aclose())
        assert api._client is None
