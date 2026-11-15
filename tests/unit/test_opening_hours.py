"""The office's opening hours formatter (business_app/utils/opening_hours.py; owner, 2026-11-14: Mon–Sat 9–18 is the
office's working time, and COMPANY_OPENING_HOURS is the one value the JSON-LD and the footer read).

The localized text is pinned as visitors read it, in en / ru / uz, with the day names and the line wording the seeds
ship (`web.display_labels.weekday_short.*` from the backend seed, `web.shell.footer.office_hours` from the shell seed).
"""

from datetime import time
from unittest.mock import Mock, call

import pytest

from business_app.models.translation import Translation
from business_app.utils import opening_hours
from business_app.utils.opening_hours import (
    LINE_KEY,
    office_hours_line,
    opening_hours_specs,
    opening_hours_text,
    parse_opening_hours,
)
from scripts.seed_web_ui_shell_translations import TRANSLATIONS as SHELL
from tests.integration.public_page_builders import seed_copy

pytestmark = pytest.mark.unit

WARNING = "COMPANY_OPENING_HOURS: %r is not '<Mo|Mo-Sa|Tu,Th> HH:MM-HH:MM', so it is not published"


@pytest.fixture
def logger(monkeypatch):
    """The module's logger, recorded; the parse cache emptied so each test's value is parsed (and logged) afresh."""
    recorded = Mock()
    monkeypatch.setattr(opening_hours, "logger", recorded)
    parse_opening_hours.cache_clear()
    yield recorded
    parse_opening_hours.cache_clear()


@pytest.fixture
def labels(app, db):
    """The weekday short names the backend seed ships, and the shell seed's office-hours line, in en / ru / uz."""
    seed_copy(db, ("web.display_labels",))
    for language, rows in SHELL.items():
        db.session.add(
            Translation(key=LINE_KEY, language=language, value=rows[LINE_KEY], category="web", is_active=True)
        )
    db.session.commit()


# --- Parsing: what the JSON-LD publishes ----------------------------------------------------------------------------


def test_the_owners_value_reads_as_monday_to_saturday_nine_to_six():
    [rule] = parse_opening_hours("Mo-Sa 09:00-18:00")
    assert rule.spec == "Mo-Sa 09:00-18:00"
    assert rule.weekdays == (0, 1, 2, 3, 4, 5)
    assert (rule.opens, rule.closes) == (time(9, 0), time(18, 0))
    assert opening_hours_specs("Mo-Sa 09:00-18:00") == ["Mo-Sa 09:00-18:00"]


@pytest.mark.parametrize(
    "raw, specs",
    [
        ("Mo-Fr 09:00-18:00,Sa 10:00-16:00", ["Mo-Fr 09:00-18:00", "Sa 10:00-16:00"]),
        ("Mo-Fr 09:00-18:00; Sa 10:00-16:00", ["Mo-Fr 09:00-18:00", "Sa 10:00-16:00"]),  # OSM's separator too
        ("  Mo-Sa   09:00-18:00 ,  Su 10:00-14:00 ", ["Mo-Sa 09:00-18:00", "Su 10:00-14:00"]),  # spaces folded
        ("Mo-Sa 09:00-21:00,Su 10:00-19:00", ["Mo-Sa 09:00-21:00", "Su 10:00-19:00"]),  # the old dev value still reads
    ],
)
def test_several_specs_are_published_in_their_order(raw, specs):
    assert opening_hours_specs(raw) == specs


def test_a_range_may_wrap_the_week():
    [rule] = parse_opening_hours("Sa-Mo 10:00-14:00")
    assert rule.weekdays == (5, 6, 0)


@pytest.mark.parametrize(
    "raw, specs, weekdays",
    [
        # schema.org's own example: the comma also separates the DAYS of one spec, so these are one spec, not two.
        ("Tu,Th 16:00-20:00", ["Tu,Th 16:00-20:00"], [(1, 3)]),
        ("Mo,We,Fr 09:00-17:00", ["Mo,We,Fr 09:00-17:00"], [(0, 2, 4)]),
        ("Tu, Th 16:00-20:00", ["Tu,Th 16:00-20:00"], [(1, 3)]),  # spaces folded
        ("Mo-We,Fr 09:00-18:00,Sa 10:00-14:00", ["Mo-We,Fr 09:00-18:00", "Sa 10:00-14:00"], [(0, 1, 2, 4), (5,)]),
        ("Mo-Fr 09:00-18:00,Tu,Th 19:00-21:00", ["Mo-Fr 09:00-18:00", "Tu,Th 19:00-21:00"], [(0, 1, 2, 3, 4), (1, 3)]),
        ("Mo-Fr,We 09:00-18:00", ["Mo-Fr,We 09:00-18:00"], [(0, 1, 2, 3, 4)]),  # a day named twice counts once
    ],
)
def test_a_day_list_is_one_spec_with_all_its_days(logger, raw, specs, weekdays):
    rules = parse_opening_hours(raw)
    assert opening_hours_specs(raw) == specs
    assert [rule.weekdays for rule in rules] == weekdays
    assert logger.warning.call_args_list == []


@pytest.mark.parametrize(
    "raw, warned",
    [
        ("Tu,Xx 16:00-20:00", "Tu,Xx 16:00-20:00"),  # a bad day in the list: the whole spec, not "Tu" alone
        ("Tx,Th 16:00-20:00", "Tx,Th 16:00-20:00"),  # a typo before the list's last day: never "Th 16:00-20:00"
        ("Mo,We,Fr 9-17", "Mo,We,Fr 9-17"),
    ],
)
def test_an_unreadable_day_list_is_left_out_whole_never_shortened_to_its_last_day(app, logger, raw, warned):
    assert opening_hours_specs(raw) == []
    assert logger.warning.call_args_list == [call(WARNING, warned)]
    with app.test_request_context():
        assert office_hours_line(raw, "en") == ""


def test_days_with_no_time_at_the_end_are_left_out_and_the_rest_still_publish(logger):
    assert opening_hours_specs("Mo-Fr 09:00-18:00,Sa,Su") == ["Mo-Fr 09:00-18:00"]
    assert logger.warning.call_args_list == [call(WARNING, "Sa,Su")]


@pytest.mark.parametrize("raw", [None, "", "   ", ",", " ; , "])
def test_an_empty_value_publishes_nothing_and_prints_no_line(app, raw):
    with app.test_request_context():
        assert parse_opening_hours(raw) == ()
        assert opening_hours_specs(raw) == []
        assert opening_hours_text(raw, "ru") == ""
        assert office_hours_line(raw, "ru") == ""


@pytest.mark.parametrize(
    "raw",
    [
        "Mon-Sat 9AM-6PM",  # English words, not schema.org codes
        "Mo-Sa 9:00-18:00",  # one-digit hour
        "Mo-Sa 09:00",  # no closing time
        "Mo-Sa",  # no times at all
        "Mo-Sa 09:00-24:00",  # 24:00 is not a clock time
        "Mo-Sa 09:60-18:00",
        "mo-sa 09:00-18:00",  # codes are case-sensitive in schema.org
        "Xx 09:00-18:00",
        "Mo-Sa 09:00–18:00",  # an en dash typed instead of a hyphen
        "Пн-Сб 09:00-18:00",
    ],
)
def test_a_malformed_value_publishes_nothing_and_prints_no_line(app, logger, raw):
    assert opening_hours_specs(raw) == []
    assert logger.warning.call_args_list == [call(WARNING, raw)]
    with app.test_request_context():
        assert office_hours_line(raw, "en") == ""


def test_a_malformed_spec_is_left_out_and_the_others_still_publish(logger):
    assert opening_hours_specs("Mo-Fr 09:00-18:00,Sat 10-16,Su 10:00-14:00") == [
        "Mo-Fr 09:00-18:00",
        "Su 10:00-14:00",
    ]
    assert logger.warning.call_args_list == [call(WARNING, "Sat 10-16")]


def test_a_bad_value_is_logged_once_not_on_every_page(logger):
    for _ in range(3):
        opening_hours_specs("Mo-Sa nine to six")
    assert logger.warning.call_args_list == [call(WARNING, "Mo-Sa nine to six")]


# --- The localized text: what the footer and /contact print ----------------------------------------------------------


@pytest.mark.parametrize(
    "language, text, line",
    [
        ("en", "Mon–Sat 09:00–18:00", "Office hours: Mon–Sat 09:00–18:00"),
        ("ru", "пн–сб 09:00–18:00", "Часы работы офиса: пн–сб 09:00–18:00"),
        ("uz", "Du–Sh 09:00–18:00", "Ofis ish vaqti: Du–Sh 09:00–18:00"),
    ],
)
def test_the_owners_hours_read_in_each_language(app, db, labels, language, text, line):
    with app.test_request_context():
        assert opening_hours_text("Mo-Sa 09:00-18:00", language) == text
        assert office_hours_line("Mo-Sa 09:00-18:00", language) == line


@pytest.mark.parametrize(
    "language, text",
    [
        ("en", "Mon–Fri 09:00–18:00; Sat 10:00–16:00"),
        ("ru", "пн–пт 09:00–18:00; сб 10:00–16:00"),
        ("uz", "Du–Ju 09:00–18:00; Sh 10:00–16:00"),
    ],
)
def test_several_specs_read_in_each_language(app, db, labels, language, text):
    with app.test_request_context():
        assert opening_hours_text("Mo-Fr 09:00-18:00,Sa 10:00-16:00", language) == text


@pytest.mark.parametrize(
    "language, text",
    [
        ("en", "Tue, Thu 16:00–20:00; Sat 10:00–14:00"),
        ("ru", "вт, чт 16:00–20:00; сб 10:00–14:00"),
        ("uz", "Se, Pa 16:00–20:00; Sh 10:00–14:00"),
    ],
)
def test_a_day_list_reads_with_every_day_in_each_language(app, db, labels, language, text):
    with app.test_request_context():
        assert opening_hours_text("Tu,Th 16:00-20:00,Sa 10:00-14:00", language) == text


@pytest.mark.parametrize(
    "raw, language, text",
    [
        ("Mo-We,Fr 09:00-18:00", "en", "Mon–Wed, Fri 09:00–18:00"),
        ("Sa-Su 10:00-14:00", "en", "Sat, Sun 10:00–14:00"),  # two days are named, not spanned
        ("Su 10:00-14:00", "ru", "вс 10:00–14:00"),
        ("Mo-Su 08:30-20:00", "uz", "Du–Ya 08:30–20:00"),
    ],
)
def test_short_spans_and_single_days_read_like_every_other_span_on_the_site(app, db, labels, raw, language, text):
    with app.test_request_context():
        assert opening_hours_text(raw, language) == text


def test_before_the_shell_seed_runs_the_hours_print_without_the_key_name(app, db):
    """A deploy that forgets the shell seed has no `web.shell.footer.office_hours` row: the line is the hours alone."""
    seed_copy(db, ("web.display_labels",))
    with app.test_request_context():
        assert office_hours_line("Mo-Sa 09:00-18:00", "en") == "Mon–Sat 09:00–18:00"
