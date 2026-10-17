"""Reorder-reminder configuration: the tunables as one validated value (spec §11).

`shared/business_config.py` reads the environment and never raises, because both bots import it.
This module applies the RANGE rules, once, for every reader: the daily run, the send task, the
beat entry and the admin summary all build their `ReminderConfig` here, so an out-of-range value
reads the same everywhere. A bad value is logged and replaced by its default (an unknown mode
reads as `off`). Nothing raises: a typo in one knob must not stop the run, the worker or the beat
schedule (spec §11, review R18).

The send window, 09:00 to 18:00 local (PP-265, spec §17), is a rule rather than a tunable, so it
lives here beside the two clock helpers every sender asks.
"""

import datetime
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Mapping, Optional

from flask import current_app

from business_app.utils.delivery_window import local_now
from business_app.utils.timezone_utils import ensure_utc
from shared.business_config import REORDER_REMINDER_DEFAULTS

logger = logging.getLogger(__name__)

MODE_OFF, MODE_DRY_RUN, MODE_ON = "off", "dry_run", "on"
MODES = (MODE_OFF, MODE_DRY_RUN, MODE_ON)

# At most two reminders per lapse: the copy (R1, then a silent R2) and the *Later* button (R1 only)
# are written for no more (spec §11).
MAX_REMINDERS_CEILING = 2

# PP-265: no advertising message before 09:00 or from 18:00 local. The send task refuses outside
# it, and Celery expires an unsent task at its end.
SEND_WINDOW_START = datetime.time(9, 0)
SEND_WINDOW_END = datetime.time(18, 0)

# The daily run's own window: inside the send window, and early enough that the send task and its
# Telegram retries still finish before 18:00.
RUN_TIME_EARLIEST = datetime.time(9, 0)
RUN_TIME_LATEST = datetime.time(15, 59)


@dataclass(frozen=True)
class ReminderConfig:
    mode: str
    fallback_days: int
    ratio: Decimal
    min_deliveries: int
    max_per_episode: int
    gap_days: int
    snooze_days: int
    escalate_after_days: int
    fresh_days: int
    live_order_grace_days: int
    local_time: datetime.time
    admin_ui_base_url: str

    @classmethod
    def from_mapping(cls, cfg: Mapping[str, Any]) -> "ReminderConfig":
        """Build the config from Flask config, or any mapping with the same keys.

        Never raises. A missing key reads as its default; an invalid value is logged and replaced
        by its default; an invalid mode reads as `off`; the reminder cap is clamped to 1..2.
        """
        return cls(
            mode=_mode(cfg),
            fallback_days=_number(cfg, "REORDER_REMINDER_FALLBACK_DAYS", _as_int, lambda value: value >= 1),
            ratio=_number(cfg, "REORDER_REMINDER_RATIO", _as_decimal, lambda value: value > 1),
            min_deliveries=_number(cfg, "REORDER_REMINDER_MIN_DELIVERIES", _as_int, lambda value: value >= 2),
            max_per_episode=_max_per_episode(cfg),
            gap_days=_number(cfg, "REORDER_REMINDER_GAP_DAYS", _as_int, lambda value: value >= 1),
            snooze_days=_number(cfg, "REORDER_REMINDER_SNOOZE_DAYS", _as_int, lambda value: value >= 1),
            escalate_after_days=_number(cfg, "REORDER_REMINDER_ESCALATE_AFTER_DAYS", _as_int, lambda value: value >= 0),
            fresh_days=_number(cfg, "REORDER_REMINDER_FRESH_DAYS", _as_int, lambda value: value >= 0),
            live_order_grace_days=_number(
                cfg, "REORDER_REMINDER_LIVE_ORDER_GRACE_DAYS", _as_int, lambda value: value >= 1
            ),
            local_time=_run_time(cfg),
            admin_ui_base_url=_base_url(cfg),
        )

    @property
    def row_set_is_dry_run(self) -> bool:
        """Which row set is active (spec §9.1, §13): the dry-run rows in `dry_run`, the live rows in
        `on` and `off`. The ONE spelling of the rule (contract amendment 7): the engine, the
        actions, the worklist and the digest ask this property, never `mode == MODE_DRY_RUN`."""
        return self.mode == MODE_DRY_RUN


def parse_local_time(raw: Optional[str], default: str = "10:00") -> datetime.time:
    """`"HH:MM"` (local) as a time. A malformed value is logged and `default` is used; never raises."""
    parsed = _parse_hhmm(raw)
    if parsed is not None:
        return parsed
    logger.error("reorder_reminders: %r is not an HH:MM time; using %s", raw, default)
    return _parse_hhmm(default)


def current_config() -> ReminderConfig:
    """The running app's config. Read it once per run or per send, never per row."""
    return ReminderConfig.from_mapping(current_app.config)


def in_send_window(now: datetime.datetime) -> bool:
    """Is `now` inside 09:00–18:00 local (start inclusive, end exclusive)?"""
    return SEND_WINDOW_START <= _local(now).time() < SEND_WINDOW_END


def send_window_end(now: datetime.datetime) -> datetime.datetime:
    """18:00 local on the local day of `now`, as aware UTC: a published send's Celery `expires`."""
    local = _local(now)
    end = datetime.datetime.combine(local.date(), SEND_WINDOW_END, tzinfo=local.tzinfo)
    return end.astimezone(datetime.timezone.utc)


def _local(now: datetime.datetime) -> datetime.datetime:
    return ensure_utc(now).astimezone(local_now().tzinfo)


def _raw(cfg: Mapping[str, Any], key: str) -> Any:
    return cfg.get(key, REORDER_REMINDER_DEFAULTS[key])


def _invalid(key: str, raw: Any, used: Any) -> Any:
    logger.error("reorder_reminders: %s=%r is invalid; using %r", key, raw, used)
    return used


def _number(cfg: Mapping[str, Any], key: str, convert: Callable[[Any], Any], valid: Callable[[Any], bool]) -> Any:
    raw = _raw(cfg, key)
    try:
        value = convert(raw)
    except (TypeError, ValueError, ArithmeticError):
        value = None
    if value is not None and valid(value):
        return value
    return _invalid(key, raw, convert(REORDER_REMINDER_DEFAULTS[key]))


def _as_int(raw: Any) -> int:
    if isinstance(raw, bool):
        raise TypeError("a boolean is not a day count")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        return int(raw.strip())
    raise TypeError(f"{type(raw).__name__} is not a day count")


def _as_decimal(raw: Any) -> Decimal:
    if isinstance(raw, bool):
        raise TypeError("a boolean is not a ratio")
    value = Decimal(str(raw).strip())
    if not value.is_finite():
        raise ValueError("the ratio must be finite")
    return value


def _mode(cfg: Mapping[str, Any]) -> str:
    key = "REORDER_REMINDERS_MODE"
    raw = _raw(cfg, key)
    mode = raw.strip().lower() if isinstance(raw, str) else None
    if mode in MODES:
        return mode
    return _invalid(key, raw, MODE_OFF)


def _max_per_episode(cfg: Mapping[str, Any]) -> int:
    key = "REORDER_REMINDER_MAX_PER_EPISODE"
    value = _number(cfg, key, _as_int, lambda _value: True)
    clamped = min(max(value, 1), MAX_REMINDERS_CEILING)
    if clamped != value:
        _invalid(key, value, clamped)
    return clamped


def _run_time(cfg: Mapping[str, Any]) -> datetime.time:
    key = "REORDER_REMINDER_LOCAL_TIME"
    default = REORDER_REMINDER_DEFAULTS[key]
    raw = _raw(cfg, key)
    run_time = parse_local_time(raw, default)
    if RUN_TIME_EARLIEST <= run_time <= RUN_TIME_LATEST:
        return run_time
    return _invalid(key, raw, parse_local_time(default))


def _base_url(cfg: Mapping[str, Any]) -> str:
    key = "ADMIN_UI_BASE_URL"
    raw = _raw(cfg, key)
    if isinstance(raw, str) and raw.strip().lower().startswith(("https://", "http://")):
        return raw.strip().rstrip("/")
    return _invalid(key, raw, REORDER_REMINDER_DEFAULTS[key])


def _parse_hhmm(raw: Any) -> Optional[datetime.time]:
    if not isinstance(raw, str):
        return None
    parts = raw.strip().split(":")
    if len(parts) != 2 or not (1 <= len(parts[0]) <= 2 and len(parts[1]) == 2):
        return None
    if not all(part.isdigit() for part in parts):
        return None
    hour, minute = int(parts[0]), int(parts[1])
    if hour > 23 or minute > 59:
        return None
    return datetime.time(hour, minute)
