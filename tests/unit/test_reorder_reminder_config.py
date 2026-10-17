"""Reorder-reminder tunables: one literal each in shared/business_config.py, validated leniently (spec §11).

Pins the four separable facts tests/unit/test_sales_config_mirror.py pins for the sales knobs:
the literal exists with the spec default and type, app.config mirrors it, config/base.py derives
it by name, and a malformed environment value costs a logged error, never an import failure in a
bot. Then the range rules of `ReminderConfig.from_mapping`, which every reader goes through.
"""

import importlib
import logging
from datetime import datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from business_app.services.reorder_reminders.config import (
    MODE_DRY_RUN,
    MODE_OFF,
    MODE_ON,
    ReminderConfig,
    current_config,
    in_send_window,
    parse_local_time,
    send_window_end,
)
from shared import business_config

pytestmark = pytest.mark.unit

CONFIG_LOGGER = "business_app.services.reorder_reminders.config"
TASHKENT = ZoneInfo("Asia/Tashkent")

# name -> (spec default, exact python type). DATA transcribed from spec §11.
REMINDER_CONSTANTS = {
    "REORDER_REMINDERS_MODE": ("off", str),
    "REORDER_REMINDER_FALLBACK_DAYS": (30, int),
    "REORDER_REMINDER_RATIO": (1.5, float),
    "REORDER_REMINDER_MIN_DELIVERIES": (3, int),
    "REORDER_REMINDER_MAX_PER_EPISODE": (2, int),
    "REORDER_REMINDER_GAP_DAYS": (7, int),
    "REORDER_REMINDER_SNOOZE_DAYS": (14, int),
    "REORDER_REMINDER_ESCALATE_AFTER_DAYS": (7, int),
    "REORDER_REMINDER_FRESH_DAYS": (14, int),
    "REORDER_REMINDER_LIVE_ORDER_GRACE_DAYS": (7, int),
    "REORDER_REMINDER_LOCAL_TIME": ("10:00", str),
    "ADMIN_UI_BASE_URL": ("https://admin.aqua-element.uz", str),
}
NAMES = sorted(REMINDER_CONSTANTS)
BASE_CONFIG_PATH = Path(__file__).resolve().parents[2] / "business_app" / "config" / "base.py"


@pytest.fixture
def restore_business_config():
    """`importlib.reload` rebinds the live module every bot imports by name; put it back."""
    originals = {name: getattr(business_config, name) for name in NAMES if hasattr(business_config, name)}
    yield
    importlib.reload(business_config)
    for name, value in originals.items():
        setattr(business_config, name, value)


@pytest.fixture(autouse=True)
def capture_config_logger(caplog):
    """The `app` fixture sets `propagate = False` on the `business_app` logger (logging_config.py), so
    caplog's root-attached handler never sees this module's records. Attach its handler to the module
    logger directly, as tests/unit/test_notification_service_fixes.py does."""
    logger = logging.getLogger(CONFIG_LOGGER)
    logger.addHandler(caplog.handler)
    yield
    logger.removeHandler(caplog.handler)


def _cfg(**overrides):
    mapping = dict(business_config.REORDER_REMINDER_DEFAULTS)
    mapping.update(overrides)
    return ReminderConfig.from_mapping(mapping)


@pytest.mark.parametrize("name", NAMES)
def test_business_config_defines_each_reminder_tunable_with_its_spec_default(
    monkeypatch, restore_business_config, name
):
    default, expected_type = REMINDER_CONSTANTS[name]
    monkeypatch.delenv(name, raising=False)

    reloaded = importlib.reload(business_config)

    assert getattr(reloaded, name) == default
    assert type(getattr(reloaded, name)) is expected_type
    assert reloaded.REORDER_REMINDER_DEFAULTS[name] == default


@pytest.mark.parametrize("name", NAMES)
def test_flask_config_mirrors_each_reminder_tunable(app, name):
    assert app.config[name] == getattr(business_config, name)


def test_base_config_derives_each_reminder_tunable_by_name():
    source = BASE_CONFIG_PATH.read_text(encoding="utf-8")

    missing = [name for name in NAMES if f"{name} = business_config.{name}" not in source]

    assert missing == []


@pytest.mark.parametrize(
    "name, raw",
    [
        ("REORDER_REMINDER_GAP_DAYS", "seven"),
        ("REORDER_REMINDER_RATIO", "1,5"),
        ("REORDER_REMINDER_MAX_PER_EPISODE", "2.0"),
    ],
)
def test_a_malformed_reminder_number_is_logged_and_never_breaks_the_import(
    monkeypatch, restore_business_config, caplog, name, raw
):
    """Both bots import this module: `_int` would raise here and the customer bot would not start."""
    monkeypatch.setenv(name, raw)

    with caplog.at_level(logging.ERROR, logger="shared.business_config"):
        reloaded = importlib.reload(business_config)

    assert getattr(reloaded, name) == REMINDER_CONSTANTS[name][0]
    assert any(name in record.getMessage() and repr(raw) in record.getMessage() for record in caplog.records)


def test_a_ratio_override_reads_as_a_float(monkeypatch, restore_business_config):
    monkeypatch.setenv("REORDER_REMINDER_RATIO", "1.75")

    reloaded = importlib.reload(business_config)

    assert reloaded.REORDER_REMINDER_RATIO == 1.75
    assert type(reloaded.REORDER_REMINDER_RATIO) is float


def test_the_defaults_build_the_spec_config():
    assert _cfg() == ReminderConfig(
        mode=MODE_OFF,
        fallback_days=30,
        ratio=Decimal("1.5"),
        min_deliveries=3,
        max_per_episode=2,
        gap_days=7,
        snooze_days=14,
        escalate_after_days=7,
        fresh_days=14,
        live_order_grace_days=7,
        local_time=time(10, 0),
        admin_ui_base_url="https://admin.aqua-element.uz",
    )


@pytest.mark.parametrize(
    "raw, mode",
    [("off", MODE_OFF), ("dry_run", MODE_DRY_RUN), ("on", MODE_ON), (" ON ", MODE_ON), ("Dry_Run", MODE_DRY_RUN)],
)
def test_the_mode_is_normalised(raw, mode):
    assert _cfg(REORDER_REMINDERS_MODE=raw).mode == mode


@pytest.mark.parametrize("raw, dry", [("off", False), ("dry_run", True), ("on", False), (" Dry_Run ", True), ("yes", False)])
def test_the_active_row_set_is_the_dry_run_one_only_in_dry_run_mode(raw, dry):
    """Contract amendment 7: the one spelling of "which row set is active" (spec §9.1: dry-run rows
    in `dry_run`, live rows in `on` and `off`). An unknown mode reads as off, so as the live set."""
    assert _cfg(REORDER_REMINDERS_MODE=raw).row_set_is_dry_run is dry


@pytest.mark.parametrize("raw", ["yes", "", None, "enabled", 1])
def test_an_unknown_mode_reads_as_off_and_is_logged(caplog, raw):
    with caplog.at_level(logging.ERROR, logger=CONFIG_LOGGER):
        assert _cfg(REORDER_REMINDERS_MODE=raw).mode == MODE_OFF
    assert "REORDER_REMINDERS_MODE" in caplog.text


@pytest.mark.parametrize("raw, expected, logged", [(1, 1, False), (2, 2, False), (3, 2, True), (99, 2, True), (0, 1, True), (-4, 1, True)])
def test_the_reminder_cap_is_clamped_to_one_or_two(caplog, raw, expected, logged):
    with caplog.at_level(logging.ERROR, logger=CONFIG_LOGGER):
        assert _cfg(REORDER_REMINDER_MAX_PER_EPISODE=raw).max_per_episode == expected
    assert ("REORDER_REMINDER_MAX_PER_EPISODE" in caplog.text) is logged


@pytest.mark.parametrize(
    "key, raw",
    [
        ("REORDER_REMINDER_FALLBACK_DAYS", 0),
        ("REORDER_REMINDER_RATIO", 1.0),
        ("REORDER_REMINDER_RATIO", "abc"),
        ("REORDER_REMINDER_MIN_DELIVERIES", 1),
        ("REORDER_REMINDER_GAP_DAYS", 0),
        ("REORDER_REMINDER_GAP_DAYS", "7d"),
        ("REORDER_REMINDER_GAP_DAYS", True),
        ("REORDER_REMINDER_SNOOZE_DAYS", 0),
        ("REORDER_REMINDER_ESCALATE_AFTER_DAYS", -1),
        ("REORDER_REMINDER_FRESH_DAYS", -1),
        ("REORDER_REMINDER_LIVE_ORDER_GRACE_DAYS", 0),
        ("ADMIN_UI_BASE_URL", "ftp://admin.example.uz"),
        ("ADMIN_UI_BASE_URL", ""),
    ],
)
def test_an_out_of_range_tunable_falls_back_to_its_default_and_is_logged(caplog, key, raw):
    with caplog.at_level(logging.ERROR, logger=CONFIG_LOGGER):
        assert _cfg(**{key: raw}) == _cfg()
    assert key in caplog.text


@pytest.mark.parametrize(
    "key, raw, attribute, value",
    [
        ("REORDER_REMINDER_ESCALATE_AFTER_DAYS", 0, "escalate_after_days", 0),
        ("REORDER_REMINDER_FRESH_DAYS", "0", "fresh_days", 0),
        ("REORDER_REMINDER_RATIO", "1.25", "ratio", Decimal("1.25")),
        ("REORDER_REMINDER_MIN_DELIVERIES", 2, "min_deliveries", 2),
        ("ADMIN_UI_BASE_URL", " https://admin.example.uz/ ", "admin_ui_base_url", "https://admin.example.uz"),
    ],
)
def test_a_value_on_the_edge_of_its_range_is_kept(key, raw, attribute, value):
    assert getattr(_cfg(**{key: raw}), attribute) == value


@pytest.mark.parametrize("raw, expected", [("09:00", time(9, 0)), ("15:59", time(15, 59)), ("10:30", time(10, 30)), (" 11:05 ", time(11, 5))])
def test_the_run_time_inside_09_00_to_15_59_is_kept(raw, expected):
    assert _cfg(REORDER_REMINDER_LOCAL_TIME=raw).local_time == expected


@pytest.mark.parametrize("raw", ["16:00", "08:59", "18:00", "10", "10:00:00", "ten", "", None, "24:00", "10:60"])
def test_a_run_time_outside_the_window_or_malformed_reads_as_ten(caplog, raw):
    with caplog.at_level(logging.ERROR, logger=CONFIG_LOGGER):
        assert _cfg(REORDER_REMINDER_LOCAL_TIME=raw).local_time == time(10, 0)
    assert caplog.records


@pytest.mark.parametrize(
    "raw, default, expected",
    [("07:00", "10:00", time(7, 0)), ("8:30", "10:00", time(8, 30)), ("25:00", "10:00", time(10, 0)), (None, "09:15", time(9, 15)), ("bad", "09:15", time(9, 15))],
)
def test_parse_local_time_never_raises(raw, default, expected):
    assert parse_local_time(raw, default) == expected


def test_current_config_reads_the_running_app(app, monkeypatch):
    monkeypatch.setitem(app.config, "REORDER_REMINDERS_MODE", "dry_run")
    monkeypatch.setitem(app.config, "REORDER_REMINDER_GAP_DAYS", 5)

    cfg = current_config()

    assert (cfg.mode, cfg.gap_days) == (MODE_DRY_RUN, 5)


@pytest.mark.parametrize(
    "local, inside",
    [((8, 59), False), ((9, 0), True), ((15, 30), True), ((17, 59), True), ((18, 0), False), ((22, 0), False)],
)
def test_the_send_window_is_09_00_to_18_00_local(local, inside):
    now = datetime(2026, 3, 13, *local, tzinfo=TASHKENT).astimezone(timezone.utc)

    assert in_send_window(now) is inside


def test_a_send_expires_at_18_00_local_on_the_day_it_was_published():
    published = datetime(2026, 3, 13, 10, 0, tzinfo=TASHKENT)

    assert send_window_end(published) == datetime(2026, 3, 13, 18, 0, tzinfo=TASHKENT)
    assert send_window_end(published).tzinfo == timezone.utc
