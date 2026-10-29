"""The website's recorded subscription answers, served to the bot (spec 2026-10-27 §16, Task 14).

`tests/fixtures/web_subscription_management/<case>.json` is what the real backend published for each case, the
same files the cabinet's Vitest suite renders. Serving them to the bot holds both clients to one answer: the bot
asserts against the published words, never a copy of them.

Two shapes are recorded. A state case has `requests[<key>]["body"]`. A write case has an `exchanges` list: every
GET the cabinet made, in order, and the write with the body it sent and the answer it got. `serve` replays the GETs
in order (the answers after a write are the ones recorded after it) and answers a write only when the bot's body is
one the website recorded.
"""

from __future__ import annotations

import copy
import json
from urllib.parse import parse_qsl, urlencode
from pathlib import Path

from tests.telegram_bot.ptb_harness import backend_failure

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "web_subscription_management"
SUBSCRIPTIONS_KEY = "GET /api/v1/subscriptions/?per_page=50"
HISTORY_PATH = "/api/v1/subscriptions/1/history"
METHODS_PATH = "/api/v1/payments/methods"
SUBSCRIPTION_PATH = "/api/v1/subscriptions/1"
LIST_PATH = "/api/v1/subscriptions"


def load(case: str) -> dict:
    return json.loads((FIXTURES / f"{case}.json").read_text("utf-8"))


def _split(key: str) -> tuple[str, str]:
    method, target = key.split(" ", 1)
    return method, target


def _reads(case: str) -> dict[str, list]:
    """Every GET the case recorded, `(path, query) -> [body, ...]` in the order the website made them."""
    fixture = load(case)
    reads: dict[tuple, list] = {}
    for key, recorded in (fixture.get("requests") or {}).items():
        method, target = _split(key)
        if method == "GET":
            reads.setdefault(_path_and_query(target), []).append(recorded["body"])
    for recorded in fixture.get("exchanges") or []:
        if recorded["method"] == "GET":
            reads.setdefault(_path_and_query(recorded["path"]), []).append(recorded["response"])
    return reads


def _path_and_query(target: str) -> tuple[str, str]:
    path, _, query = target.partition("?")
    return path.rstrip("/") or "/", query


def subscription(case: str) -> dict:
    """The case's subscription: the first `GET /api/v1/subscriptions/?per_page=50` answer's first item."""
    first = _reads(case)[("/api/v1/subscriptions", "per_page=50")][0]
    return first["data"]["items"][0]


def exchange(case: str, method: str, path: str) -> dict:
    """The first recorded write to `method path`: `{"body", "response", "status"}`."""
    for recorded in load(case)["exchanges"]:
        if recorded["method"] == method and recorded["path"] == path:
            return recorded
    raise KeyError(f"{case} recorded no {method} {path}")


def history_rows(case: str, page: int = 1) -> list:
    return _history(case, page)["data"]["items"]


def _history(case: str, page: int) -> dict:
    return _reads(case)[(HISTORY_PATH, f"page={page}&per_page=5")][0]


def _answer(status: int, body: dict):
    body = copy.deepcopy(body)
    if status < 400:
        return body
    return backend_failure(body.get("message", f"HTTP {status}"), status, body)


def path_is_preview(call) -> bool:
    return str(call.endpoint).rstrip("/").endswith("/preview")


class Served:
    """The backend for one case. `phase` is the number of writes answered so far: it picks which recorded GET
    answers the next read, so the card the bot redraws after a write is the one the website drew."""

    def __init__(self, bot, case: str):
        self.case = case
        self.phase = 0
        self.reads = _reads(case)
        card = self._card_reads()
        bot.backend.route("GET", LIST_PATH, lambda _c: self._pick(card))
        bot.backend.route("GET", SUBSCRIPTION_PATH, lambda _c: self._detail(card))
        routed: set[str] = set()
        for (path, query), answers in self.reads.items():
            if path in (LIST_PATH, SUBSCRIPTION_PATH) or path in routed:
                continue
            routed.add(path)
            if path == HISTORY_PATH:
                bot.backend.route("GET", path, lambda c, p=path: self._history(c, p))
            elif path == METHODS_PATH:
                bot.backend.route("GET", path, lambda _c, a=answers: self._pick(a))
            else:
                # Every other read the website made (time slots, products, addresses): answered by its query.
                bot.backend.route("GET", path, lambda c, p=path: self._read(c, p))
        fixture = load(case)
        writes: dict[tuple, list] = {}
        for recorded in fixture.get("exchanges") or []:
            if recorded["method"] != "GET":
                writes.setdefault((recorded["method"], recorded["path"]), []).append(recorded)
        for (method, path), recorded in writes.items():
            bot.backend.route(method, path, lambda call, r=recorded: self._write(call, r))

    def _card_reads(self) -> list:
        return self.reads[("/api/v1/subscriptions", "per_page=50")]

    def _pick(self, answers: list):
        return copy.deepcopy(answers[min(self.phase, len(answers) - 1)])

    def _detail(self, card: list):
        listed = self._pick(card)
        return {"data": {"subscription": listed["data"]["items"][0]}, "success": True}

    def _history(self, call, path: str):
        page = (call.params or {}).get("page", 1)
        per_page = (call.params or {}).get("per_page")
        answers = self.reads.get((path, f"page={page}&per_page={per_page}"))
        if answers is None:
            return backend_failure(f"the website never asked for history page {page} ({per_page} a page)", 599)
        return self._pick(answers)

    def _read(self, call, path: str):
        asked = sorted((k, str(v)) for k, v in (call.params or {}).items())
        for (recorded_path, query), answers in self.reads.items():
            if recorded_path == path and sorted(parse_qsl(query)) == asked:
                return self._pick(answers)
        return backend_failure(f"the website never asked for {path}?{urlencode(call.params or {})}", 599)

    def _write(self, call, recorded: list):
        sent = {k: v for k, v in (call.data or {}).items() if k != "channel"}
        for candidate in recorded:
            if {k: v for k, v in (candidate["body"] or {}).items() if k != "channel"} == sent:
                # A preview changes nothing: only a write that took effect moves the card to its next answer.
                if not path_is_preview(call):
                    self.phase += 1
                return _answer(candidate["status"], candidate["response"])
        return backend_failure(f"the website recorded no answer to {call.data!r}", 599)


def serve(bot, case: str) -> Served:
    return Served(bot, case)
