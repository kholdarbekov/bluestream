"""visit_rules: the ONE rule for "was the agent really in the shop" (C4, spec §4.1.1).

Every case is an in-memory `Visit`: the rule reads six attributes and needs no table. The
expected reason of each case is the ruling written down, never recomputed from the module under
test, and the threshold edge reads the real `short_visit_threshold()` rather than a copy of 60.

The last two tests are T-VIS-8, the SSOT guard: `SALES_SHORT_VISIT_SECONDS` has one reader in
the whole backend, and no other sales service puts a check-in fact beside a duration of its own.
Plan compliance, the short-visit exception, the average visit length and the pay gate all ask
this module; the day one of them measures a visit itself, two screens disagree about one visit.
"""

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from business_app.models.sales_visits import Visit
from business_app.services.sales.visit_rules import (
    NOT_VERIFIED_REASONS,
    is_short_visit,
    is_verified_visit,
    not_verified_reason,
    presence_seconds,
    short_visit_threshold,
)

pytestmark = pytest.mark.unit

CHECKIN_AT = datetime(2026, 10, 1, 5, 2, tzinfo=timezone.utc)  # 10:02 Tashkent


def _visit(**overrides):
    """A verified visit unless `overrides` say otherwise: completed, checked in, in range, 25 min."""
    fields = {
        "status": "completed",
        "started_at": CHECKIN_AT - timedelta(minutes=2),
        "checkin_at": CHECKIN_AT,
        "ended_at": CHECKIN_AT + timedelta(minutes=25),
        "checkin_skipped": False,
        "in_radius": True,
        **overrides,
    }
    return Visit(**fields)


REASON_CASES = [
    ("abandoned", {"status": "abandoned"}, "not_completed"),
    ("in-progress", {"status": "in_progress", "ended_at": None}, "not_completed"),
    # The FIRST failure is the one named: an abandoned out-of-range visit reads as abandoned.
    ("abandoned-and-out-of-range", {"status": "abandoned", "in_radius": False}, "not_completed"),
    # `VisitService.close` does not require the check-in step, so this row can exist.
    ("closed-without-check-in", {"checkin_at": None, "in_radius": None}, "no_checkin"),
    ("skipped", {"checkin_skipped": True, "in_radius": None}, "skipped"),
    ("out-of-range", {"in_radius": False}, "out_of_range"),
    # Q9 (owner-confirmed): no pin sent, or an outlet with no coordinates.
    ("no-location", {"in_radius": None}, "no_location"),
    ("five-seconds", {"ended_at": CHECKIN_AT + timedelta(seconds=5)}, "short"),
    ("no-end-instant", {"ended_at": None}, "short"),
    ("verified", {}, None),
]


@pytest.mark.parametrize(
    "overrides,reason",
    [case[1:] for case in REASON_CASES],
    ids=[case[0] for case in REASON_CASES],
)
def test_the_first_failure_is_the_reason_and_no_reason_is_verified(overrides, reason):
    visit = _visit(**overrides)

    assert not_verified_reason(visit, min_seconds=60) == reason
    # DEFINED as "no reason": the drill-down's reasons and the predicate cannot drift apart.
    assert is_verified_visit(visit, min_seconds=60) is (reason is None)


def test_the_reasons_are_published_in_the_order_they_are_checked():
    assert NOT_VERIFIED_REASONS == ("not_completed", "no_checkin", "skipped", "out_of_range", "no_location", "short")
    # Every reason the rule can give is reachable from a case above, and nothing else is.
    assert {case[2] for case in REASON_CASES if case[2] is not None} == set(NOT_VERIFIED_REASONS)


def test_exactly_the_threshold_is_verified_and_one_second_under_is_short(app):
    """`>=`, not `>`: an agent who spent exactly the minimum at the counter was there."""
    with app.app_context():
        threshold = short_visit_threshold()
    at_threshold = _visit(ended_at=CHECKIN_AT + timedelta(seconds=threshold))
    one_under = _visit(ended_at=CHECKIN_AT + timedelta(seconds=threshold - 1))

    assert not_verified_reason(at_threshold, min_seconds=threshold) is None
    assert not_verified_reason(one_under, min_seconds=threshold) == "short"
    assert (is_short_visit(at_threshold, min_seconds=threshold), is_short_visit(one_under, min_seconds=threshold)) == (
        False,
        True,
    )


def test_the_threshold_is_the_deployable_knob(app, monkeypatch):
    """Read per call from the app config, as an int, so an env override is honoured."""
    monkeypatch.setitem(app.config, "SALES_SHORT_VISIT_SECONDS", "90")

    with app.app_context():
        assert short_visit_threshold() == 90


def test_presence_is_check_in_to_end_in_whole_seconds():
    assert presence_seconds(_visit(ended_at=CHECKIN_AT + timedelta(seconds=61, milliseconds=900))) == 61
    # SQLite hands timestamps back without a zone; they are UTC.
    naive = _visit(
        checkin_at=CHECKIN_AT.replace(tzinfo=None),
        ended_at=(CHECKIN_AT + timedelta(minutes=3)).replace(tzinfo=None),
    )
    assert presence_seconds(naive) == 180
    assert presence_seconds(_visit(checkin_at=None)) is None
    assert presence_seconds(_visit(ended_at=None)) is None


def test_a_short_visit_is_the_same_span_inverted_on_completed_visits_only():
    """The exception feed's `short_visit`. Out of range is its own exception; a short
    out-of-range visit is still short. An abandoned visit never is."""
    five_seconds = CHECKIN_AT + timedelta(seconds=5)

    assert is_short_visit(_visit(ended_at=five_seconds), min_seconds=60) is True
    assert is_short_visit(_visit(ended_at=five_seconds, in_radius=False), min_seconds=60) is True
    assert is_short_visit(_visit(ended_at=five_seconds, status="abandoned"), min_seconds=60) is False
    assert is_short_visit(_visit(checkin_at=None), min_seconds=60) is False
    assert is_short_visit(_visit(), min_seconds=60) is False


# --------------------------------------------------------------------------- #
# T-VIS-8: the SSOT guard
# --------------------------------------------------------------------------- #

ROOT = Path(__file__).resolve().parents[2]
BUSINESS_APP = ROOT / "business_app"
SALES_SERVICES = BUSINESS_APP / "services" / "sales"
VISIT_RULES = SALES_SERVICES / "visit_rules.py"
# The config mirror DEFINES the knob (`SALES_SHORT_VISIT_SECONDS = business_config....`); it reads
# nothing, and tests/unit/test_sales_config_mirror.py already pins it.
CONFIG_MIRROR = BUSINESS_APP / "config" / "base.py"
KNOB = "SALES_SHORT_VISIT_SECONDS"
CHECK_IN_FACTS = {"checkin_skipped", "in_radius"}
VISIT_INSTANTS = {"checkin_at", "ended_at"}


def _tree(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _names_the_knob(tree):
    """A config subscript, an attribute or a bare name spelled exactly like the knob.
    A docstring that mentions it is a sentence, not a read, and does not match."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == KNOB:
            return True
        if isinstance(node, ast.Attribute) and node.attr == KNOB:
            return True
        if isinstance(node, ast.Name) and node.id == KNOB:
            return True
    return False


def _attribute_names(tree):
    return {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}


def _measures_a_duration(tree):
    """`.total_seconds()`, or a subtraction with a visit instant on either side."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "total_seconds":
            return True
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub):
            if (_attribute_names(node.left) | _attribute_names(node.right)) & VISIT_INSTANTS:
                return True
    return False


def test_the_short_visit_threshold_has_one_reader():
    readers = sorted(
        path.relative_to(ROOT).as_posix()
        for path in BUSINESS_APP.rglob("*.py")
        if path != CONFIG_MIRROR and _names_the_knob(_tree(path))
    )

    assert readers == ["business_app/services/sales/visit_rules.py"]


def test_no_other_sales_module_measures_a_visit_on_its_own():
    """A module may read `in_radius` / `checkin_skipped` (the discipline counters and the
    out-of-range feed do) or ask `presence_seconds` for a duration, but never do its own
    arithmetic beside them: that is the verified-visit rule written a second time."""
    rules = _tree(VISIT_RULES)
    # The positive control: the guard can see the combination where it legitimately lives.
    assert _attribute_names(rules) & CHECK_IN_FACTS and _measures_a_duration(rules)

    offenders = sorted(
        path.name
        for path in SALES_SERVICES.glob("*.py")
        if path != VISIT_RULES
        and _attribute_names(_tree(path)) & CHECK_IN_FACTS
        and _measures_a_duration(_tree(path))
    )

    assert offenders == []
