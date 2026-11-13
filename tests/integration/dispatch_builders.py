"""Builders of the trigger suites: the session listeners that flag a transaction and drain it after the commit
(`business_app/utils/eta_recompute_dispatch.py`, `business_app/utils/indexnow_dispatch.py`).

Each builder wraps a module-level detector by name with `monkeypatch`, so the listener, which looks the detector up at
call time, runs the wrapper. The real detector still runs; the builder only records what it saw.
"""

from types import ModuleType
from typing import Any, List

from sqlalchemy import event

from business_app import db as _db


def sql_inside(monkeypatch, module: ModuleType, attr: str) -> List[List[str]]:
    """The SQL statements each call of `module.<attr>` ran, one list per call, in order. The real body runs; the
    statements are read off the engine while it does. A detector must run none."""
    real_detector = getattr(module, attr)
    calls: List[List[str]] = []

    def watched_detector(session):
        statements: List[str] = []

        def on_statement(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
            statements.append(statement)

        engine = _db.engine
        event.listen(engine, "before_cursor_execute", on_statement)
        try:
            return real_detector(session)
        finally:
            event.remove(engine, "before_cursor_execute", on_statement)
            calls.append(statements)

    monkeypatch.setattr(module, attr, watched_detector)
    return calls


def calls_to(monkeypatch, module: ModuleType, attr: str) -> List[Any]:
    """The session of each call of `module.<attr>`, in order. The real body runs. Empty when the listener never
    consulted the detector, as with the trigger's switch off."""
    real_detector = getattr(module, attr)
    looked: List[Any] = []

    def watched_detector(session):
        looked.append(session)
        return real_detector(session)

    monkeypatch.setattr(module, attr, watched_detector)
    return looked
