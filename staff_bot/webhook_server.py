"""
Internal Webhook Server for Staff Bot
Receives webhooks from backend for staff notifications (new orders, assignments, etc.)
"""
import asyncio
import hmac
import hashlib
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional, Tuple
from aiohttp import web
import redis.asyncio as redis
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from staff_bot.config import config
from staff_bot.database import db_manager
from staff_bot.i18n import i18n
from staff_bot.keyboards.delivery import DeliveryKeyboards
from staff_bot.keyboards.operator import OperatorKeyboards
from staff_bot.keyboards.sales import LABEL_MAX, reject_reason_key
from staff_bot.utils import flow_state
from staff_bot.utils.formatters import (
    TELEGRAM_TEXT_LIMIT,
    escape_html,
    format_delivery_window_line,
    format_fail_reason,
    format_local_date,
    format_pay_amount,
    format_pay_balance_note,
    format_pay_carry_in,
    format_pay_month,
    format_pay_tiers,
    signed_currency,
    signed_deduction,
)
from shared.redis_failure import report_redis_failure
from shared.staff_constants import (
    BOTTLE_EVENT_JOIN_APPROVED,
    BOTTLE_EVENT_JOIN_REQUESTED,
    BOTTLE_EVENT_TRANSFER_RECEIVED,
    BOTTLE_EVENTS,
    SALES_EVENT_PAY_PENALTY_CONFIRMED,
    SALES_EVENT_PAY_STATEMENT_APPROVED,
    SALES_EVENTS,
)
from shared.redis_keyspace import RedisKeyspace

logger = logging.getLogger(__name__)

try:  # telegram is always present in the bot runtime; guard keeps imports safe.
    from telegram.error import Forbidden as _TelegramForbidden
except Exception:  # pragma: no cover - defensive
    _TelegramForbidden = ()

# Substrings of benign "this recipient can't receive messages" Telegram errors.
# These are operational facts (the user blocked the bot / deactivated their
# account), not server faults — they belong at WARNING, not ERROR. A single
# blocked driver otherwise emits an ERROR on every broadcast.
_UNREACHABLE_RECIPIENT_MARKERS = (
    "bot was blocked by the user",
    "user is deactivated",
    "chat not found",
    "bots can't send messages to bots",
    "have no rights to send",
    "peer_id_invalid",
)


def _is_recipient_unreachable(exc: Exception) -> bool:
    """True when ``exc`` means a specific recipient simply can't be messaged.

    Such failures are expected at the edges (drivers block the bot, delete their
    account, etc.) and must not be logged at ERROR or they drown the error feed.
    """
    if _TelegramForbidden and isinstance(exc, _TelegramForbidden):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _UNREACHABLE_RECIPIENT_MARKERS)


def _log_notify_failure(description: str, exc: Exception) -> None:
    """Log a per-recipient notification failure at the right severity.

    WARNING for benign unreachable recipients; ERROR (the genuine-fault level)
    otherwise.
    """
    if _is_recipient_unreachable(exc):
        logger.warning("%s (recipient unreachable): %s", description, exc)
    else:
        logger.error("%s: %s", description, exc)


# The morning digest's budget is Telegram's own ceiling, under a name of its own so a test can
# narrow it for the digest alone.
DIGEST_TEXT_LIMIT = TELEGRAM_TEXT_LIMIT


# The morning digest (D21/R6) is the one sales event that is a LIST. The
# backend publishes the day already decided -- which outlets are due, how late
# each is, which five unvisited ones made the cut, whether a visit is still
# open -- and everything below renders it. Nothing here queries, sorts, counts
# or compares a date.
def _digest_name(name) -> str:
    """The name as this digest may print it, in the TEXT as well as on the button.

    `LABEL_MAX` is the one ceiling: `outlets.name` is 200 characters, all of
    which an agent can type at the new-outlet step and all of which the backend
    publishes, and the backend's cap (Task 3) bounds ROWS, not characters. 26
    rows of 200 compose ~5.6k characters -- past Telegram's 4096, a
    `sendMessage` that raises, three retries that raise identically, and an
    agent with no digest at all. Cut BEFORE escaping, so the cap counts the
    characters the agent typed and can never halve an HTML entity.
    """
    return str(name or '')[:LABEL_MAX]


def _digest_button(name, outlet_id) -> InlineKeyboardButton:
    """One row, one way into its outlet card.

    Labelled with the OUTLET, not "Open outlet" four times over: the label is
    the only thing that says which row the button belongs to.
    """
    # Hoisted before the f-string like every other outlet button: the routing
    # scraper cannot read a callback literal containing an inner quote.
    outlet_id = int(outlet_id)
    label = _digest_name(name) or str(outlet_id)
    return InlineKeyboardButton(label, callback_data=f"staff_sales_outlet_{outlet_id}")


def _digest_row_suffix(row: dict, language: str, mode: Optional[str]) -> str:
    """The lateness or age tail a row earns, or ''.

    Three different facts, three renderings. `overdue_days` is lateness and
    reuses the DUE LIST's own suffix, so "how late" keeps one expression in
    this bot; `days` on an unvisited row is only an age; and `days is None`
    is NEVER VISITED, which is a word rather than a number. That last case is
    the one a falsy check silently eats: `days` of None and `days` of 0 both
    read as "no suffix", so a shop nobody has ever walked into would render
    identically to one visited this morning, under a heading that says
    otherwise. An overdue ZERO still earns nothing -- a call that became due
    this morning is not late, the same reason `SalesKeyboards.outlet_list`
    refuses to print a zero-day tail.
    """
    if mode == 'overdue' and row.get('overdue_days'):
        label = i18n.get('staff.sales.list.overdue_suffix', language, days=row['overdue_days'])
        return f" · {escape_html(label)}"
    if mode == 'age':
        if row.get('days'):
            label = i18n.get('staff.sales.notify.digest_days', language, days=row['days'])
            return f" · {escape_html(label)}"
        if row.get('days') is None:
            label = i18n.get('staff.sales.notify.digest_never', language)
            return f" · {escape_html(label)}"
    return ''


def _render_morning_digest(payload: dict, language: str, recipient=None) -> Tuple[str, list]:
    """The agent's day: the text, and one button per row in reading order."""
    # The header row carries its own <b> (it lives in STAFF_TRANSLATIONS with
    # its five siblings), so it is the one line not escaped here; `{date}` is
    # a formatted day, not a name.
    head_lines = [i18n.get(
        'staff.sales.notify.morning_digest', language,
        date=format_local_date(payload.get('date')),
    )]
    head_buttons = []

    open_visit = payload.get('open_visit') or {}
    if open_visit.get('outlet_id'):
        label = i18n.get('staff.sales.notify.digest_open_visit', language)
        name = escape_html(_digest_name(open_visit.get('outlet_name')))
        head_lines.append(f"⏯ {escape_html(label)} — {name}")
        head_buttons.append(_digest_button(open_visit.get('outlet_name'), open_visit['outlet_id']))

    sections = (
        ('due_today', '📅', i18n.get('staff.sales.notify.digest_due_today', language), None),
        ('overdue', '⏰', i18n.get('staff.sales.notify.digest_overdue', language), 'overdue'),
        ('unvisited', '🕘', i18n.get('staff.sales.notify.digest_unvisited', language), 'age'),
    )
    blocks = {}
    for section, emoji, heading, mode in sections:
        rows = [
            row for row in (payload.get(section) or [])
            if isinstance(row, dict) and row.get('outlet_id')
        ]
        if not rows:
            # An empty section is an ANSWER, not a gap: R6 sends no digest at
            # all to an agent with nothing to report, so a heading with
            # nothing under it could only ever mislead.
            continue
        block_lines = [f"{emoji} <b>{escape_html(heading)}</b>"]
        block_buttons = []
        for row in rows:
            name = escape_html(_digest_name(row.get('outlet_name')))
            block_lines.append(f"  • {name}{_digest_row_suffix(row, language, mode)}")
            block_buttons.append(_digest_button(row.get('outlet_name'), row['outlet_id']))
        blocks[section] = (block_lines, block_buttons, len(rows))

    # What the BACKEND cut (Task 3 caps each due section at ten, because an
    # uncapped digest is a message and a keyboard past Telegram's limits, a
    # `send_message` that raises, and three retries that deliver nothing at
    # all). Printed, never computed: the bot does not know what it was not
    # sent, and must not imply the short list is the whole list.
    more_count = int(payload.get('more_count') or 0)

    def _compose(shed):
        lines = list(head_lines)
        buttons = list(head_buttons)
        counted = more_count
        for section, _emoji, _heading, _mode in sections:
            block = blocks.get(section)
            if block is None:
                continue
            if section in shed:
                # Shed rows are COUNTED, never silently dropped -- into the same
                # line the backend's own cut is printed on.
                counted += block[2]
                continue
            lines.extend(block[0])
            buttons.extend(block[1])
        if counted:
            lines.append(escape_html(
                i18n.get('staff.sales.notify.digest_more', language, count=counted)
            ))
        return '\n'.join(lines), buttons

    text, buttons = _compose(())
    # Belt and braces. Capped names plus the backend's row caps fit with room to
    # spare, but a payload shape nobody has thought of yet must not cost an agent
    # the whole morning: Telegram refuses a message past DIGEST_TEXT_LIMIT and
    # every retry fails identically. The lowest-priority sections go first --
    # what is LATE is what the agent has to act on today.
    shed = []
    for section in ('unvisited', 'due_today'):
        if len(text) <= DIGEST_TEXT_LIMIT or section not in blocks:
            continue
        shed.append(section)
        logger.warning(
            "Morning digest for %s is %s characters; dropping the %s section",
            recipient, len(text), section,
        )
        text, buttons = _compose(tuple(shed))
    if len(text) > DIGEST_TEXT_LIMIT:
        # Shedding could not get under it: only `unvisited` and `due_today` are
        # droppable (what is LATE is today's work), so an overdue-only payload past
        # the limit still reaches `send_message` and raises. NOT truncated -- a cut
        # through an HTML tag turns a long message into a parse-mode 400, which is
        # the same lost morning by another route. Unreachable under today's caps
        # (26 rows of at most LABEL_MAX characters); the line exists so a future cap
        # change is visible before an agent loses a digest to it.
        logger.warning(
            "Morning digest for %s is %s characters after shedding, past the %s limit",
            recipient, len(text), DIGEST_TEXT_LIMIT,
        )
    return text, buttons


# ---- the two pay pushes (compensation spec §7.5) ------------------------------------------------
#
# The backend publishes every figure (C24: whole UZS, "YYYY-MM" months, "YYYY-MM-DD" dates). These
# render them through the formatters' pay block, the one home the My earnings screens share, so a
# figure cannot read one way in the push and another on the screen its button opens. Nothing here
# adds, compares or decides. A missing field skips its line and never raises: the vocabulary test
# drives both with the generic payload every other sales event carries. Glyphs are code-side
# (§8.5); the button callbacks are literals so the routing guard can check them.

def _render_pay_penalty_confirmed(payload: dict, language: str) -> Tuple[str, InlineKeyboardButton]:
    """⚠️ A confirmed penalty: its type, day, amount, the month it counts in, and why.

    The button opens the penalties screen, registered in group 0 so it works from a cold chat.
    """
    lines = [f"⚠️ {i18n.get('staff.sales.notify.pay_penalty_confirmed', language)}"]
    type_name = (payload.get('type_names') or {}).get(language)
    if type_name:
        lines.append(f"{i18n.get('staff.sales.notify.penalty_type', language)}: {escape_html(type_name)}")
    day = format_local_date(payload.get('incident_date'))
    if day:
        lines.append(f"{i18n.get('staff.sales.notify.penalty_date', language)}: {day}")
    if payload.get('amount') is not None:
        lines.append(
            f"{i18n.get('staff.sales.notify.penalty_amount', language)}: "
            f"{signed_deduction(payload['amount'], language)}"
        )
    month = format_pay_month(payload.get('month'))
    if month:
        counts_in = i18n.get('staff.sales.notify.counts_in', language, month=month)
        if payload.get('is_late'):
            # Its incident month had already closed, so it counts in a later open one (§4.5).
            counts_in = f"{counts_in} ({i18n.get('staff.sales.notify.late', language)})"
        lines.append(counts_in)
    if payload.get('reason'):
        lines.append(f"{i18n.get('staff.sales.earnings.reason', language)}: {escape_html(payload['reason'])}")
    button = InlineKeyboardButton(
        # `_xn`, not the screen's `_x`: the push stays in the chat and the screen opens under it (M9).
        f"💰 {i18n.get('staff.sales.notify.open_earnings', language)}", callback_data='staff_sales_earn_xn',
    )
    return '\n'.join(lines), button


def _render_pay_statement_approved(payload: dict, language: str) -> Tuple[str, InlineKeyboardButton]:
    """✅ An approved statement: the summary's frozen figures, the commission per product and
    tier (the summary's own rows, without a next tier), the paid total in bold.

    A trial (shadow) month says it is not paid; its headline is a LITERAL key, so /health
    requires it, and the push ends with the trial note. The optional rows follow the summary's
    rule: corrections, new outlets, adjustments and penalties only when non-zero. A carried-in
    row goes before the total, labelled by its published `source`; a carried shortfall or an
    owed balance goes after it (I-28: on this push `owed` is the agent's new outstanding
    balance). The button opens the announced month's Statement.

    Telegram refuses a message over `TELEGRAM_TEXT_LIMIT`, and every retry the same way, which
    would lose the month's news for good. A push past it prints each product's line alone, the
    summary's first "Length" step (§7.2): only the tier rows go, never a figure of the formula.
    """
    text = _pay_statement_text(payload, language, tiers=True)
    if len(text) > TELEGRAM_TEXT_LIMIT:
        text = _pay_statement_text(payload, language, tiers=False)
    month_key = str(payload.get('month') or '').replace('-', '')
    button = InlineKeyboardButton(
        # `_sn_`, not the screen's own `_s_`: tapping it must not overwrite this approved month's
        # breakdown, so that month's Statement opens as a new message under it (M9).
        f"📄 {i18n.get('staff.sales.notify.open_statement', language)}",
        callback_data=f"staff_sales_earn_sn_{month_key}",
    )
    return text, button


def _pay_statement_text(payload: dict, language: str, *, tiers: bool) -> str:
    """The approval push's text, each product with its tier rows or, without `tiers`, its
    product line alone (`format_pay_tiers`' first row)."""
    month = format_pay_month(payload.get('month'))
    if payload.get('is_shadow'):
        headline = i18n.get('staff.sales.notify.pay_statement_approved_shadow', language, month=month)
    else:
        headline = i18n.get('staff.sales.notify.pay_statement_approved', language, month=month)
    lines = [f"✅ {headline}"]
    if payload.get('base') is not None:
        lines.append(
            f"{i18n.get('staff.sales.earnings.base', language)}: "
            f"{format_pay_amount(payload['base'], language)}"
        )
    commission = payload.get('commission') or {}
    if commission.get('gross') is not None:
        lines.append(
            f"{i18n.get('staff.sales.earnings.commission', language)}: "
            f"{format_pay_amount(commission['gross'], language)}"
        )
        for product in commission.get('products') or []:
            # The summary's own tier rows (§7.5). A frozen statement has no next tier.
            rows = format_pay_tiers(product, language, with_next=False)
            lines.extend(rows if tiers else rows[:1])
    if payload.get('commission_after_gate') is not None:
        lines.append(
            f"{i18n.get('staff.sales.earnings.commission_after_gate', language)}: "
            f"{format_pay_amount(payload['commission_after_gate'], language)}"
        )
    if payload.get('late'):
        lines.append(
            f"{i18n.get('staff.sales.earnings.late', language)}: "
            f"{signed_currency(payload['late'], language)}"
        )
    if payload.get('new_outlets'):
        lines.append(
            f"{i18n.get('staff.sales.earnings.new_outlets', language)}: "
            f"{signed_currency(payload['new_outlets'], language)}"
        )
    if payload.get('adjustments'):
        lines.append(
            f"{i18n.get('staff.sales.earnings.adjustments', language)}: "
            f"{signed_currency(payload['adjustments'], language)}"
        )
    if payload.get('penalties'):
        lines.append(
            f"{i18n.get('staff.sales.earnings.penalties', language)}: "
            f"{signed_deduction(payload['penalties'], language)}"
        )
    carry_in = format_pay_carry_in(payload.get('carry_in'), language)
    if carry_in:
        lines.append(carry_in)
    if payload.get('total') is not None:
        lines.append(
            f"<b>{i18n.get('staff.sales.earnings.total', language)}: "
            f"{format_pay_amount(payload['total'], language)}</b>"
        )
    note = format_pay_balance_note(payload.get('carry_out'), payload.get('owed'), language)
    if note:
        lines.append(note)
    if payload.get('is_shadow'):
        # The trial month's own note, as My earnings prints it (I-16): the headline says "not
        # paid", this says how pay is made instead.
        lines.append(i18n.get('staff.sales.earnings.shadow_note', language))
    return '\n'.join(lines)


def _render_sales_event(
    event: str, payload: dict, language: str, recipient,
) -> Tuple[str, Optional[InlineKeyboardMarkup], bool]:
    """One sales event as `(text, keyboard, silent)`.

    Three events make a sound: the morning digest (it starts the agent's day, and a silent 08:30
    message is a digest nobody reads) and the two pay pushes (pay news arrives while the phone is
    in a pocket, §7.5). Every other sales event arrives while the phone is already in hand and
    stays silent. C14's `agent_order_approved` / `agent_order_rejected` take the generic branch
    with no code of their own (§7.6): their rows carry their own leading glyph (§8.5's second
    exception).
    """
    if event == 'morning_digest':
        text, buttons = _render_morning_digest(payload, language, recipient)
        keyboard = InlineKeyboardMarkup([[button] for button in buttons]) if buttons else None
        return text, keyboard, False
    if event == SALES_EVENT_PAY_PENALTY_CONFIRMED:
        text, button = _render_pay_penalty_confirmed(payload, language)
        return text, InlineKeyboardMarkup([[button]]), False
    if event == SALES_EVENT_PAY_STATEMENT_APPROVED:
        text, button = _render_pay_statement_approved(payload, language)
        return text, InlineKeyboardMarkup([[button]]), False
    # The backend stores `Outlet.rejected_reason` as the plain ENGLISH
    # text the operator's button carried (SSOT: `REJECT_REASONS`), so
    # injecting it verbatim put an English phrase inside an otherwise
    # localized sentence on the agent's phone. Map it back to its key
    # and render the label in the AGENT's language. `reject_reason_key`
    # is the one place that reversal lives; a miss is expected (the
    # admin UI rejects with free text) and the raw string is echoed.
    raw_reason = str(payload.get('reason') or '')
    reason_key = reject_reason_key(raw_reason)
    reason = (
        i18n.get(f'staff.sales.approvals.reason.{reason_key}', language)
        if reason_key else raw_reason
    )
    text = i18n.get(
        f'staff.sales.notify.{event}', language,
        outlet_name=escape_html(str(payload.get('outlet_name') or '')),
        reason=escape_html(reason),
        order_number=escape_html(str(payload.get('order_number') or '')),
    )
    if event == 'activation_requested':
        button = InlineKeyboardButton(
            i18n.get('staff.sales.notify.open_requests', language), callback_data='staff_sales_approvals'
        )
    else:
        button = InlineKeyboardButton(
            i18n.get('staff.sales.notify.open_outlet', language),
            callback_data=f"staff_sales_outlet_{payload.get('outlet_id')}",
        )
    return text, InlineKeyboardMarkup([[button]]), True


def _render_bottle_event(event: str, payload: dict, language: str) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    """One bottle event as `(text, keyboard)`. Every one sounds: each asks the
    driver to act, or answers a question they asked a moment ago."""
    unknown = i18n.get('staff.common.unknown_driver', language)
    if event == BOTTLE_EVENT_TRANSFER_RECEIVED:
        text = i18n.get(
            'staff.bottles.notify.transfer_received', language,
            sender=escape_html(payload.get('sender_name') or unknown),
            qty=payload.get('declared_quantity', 0),
        )
        # The inbox's own keyboard: its confirm button is a global handler and
        # its "different count" button a conversation entry point, so both work
        # from this message exactly as from "📥 Incoming transfers".
        return text, DeliveryKeyboards.pending_transfer_list(language, [payload])
    if event == BOTTLE_EVENT_JOIN_REQUESTED:
        suffix = f"{payload.get('session_id')}_{payload.get('requester_id')}"
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"✅ {i18n.get('staff.bottles.join_request_approve', language)}",
                callback_data=f"bottles_jr_ok_{suffix}",
            ),
            InlineKeyboardButton(
                f"❌ {i18n.get('staff.bottles.join_request_decline', language)}",
                callback_data=f"bottles_jr_no_{suffix}",
            ),
        ]])
        text = i18n.get(
            'staff.bottles.notify.join_requested', language,
            name=escape_html(payload.get('requester_name') or unknown),
        )
        return text, keyboard
    owner = escape_html(payload.get('owner_name') or unknown)
    if event == BOTTLE_EVENT_JOIN_APPROVED:
        text = (
            f"✅ <b>{i18n.get('staff.bottles.joined_session', language, name=owner)}</b>\n\n"
            f"{i18n.get('staff.bottles.joined_session_info', language)}"
        )
        return text, None
    return i18n.get('staff.bottles.notify.join_declined', language, name=owner), None


def _render_delivery_failed_alert(
    data: dict, delivery_id: int, language: str
) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    """The failure alert (spec F7): its text, and the operator keyboard or None.

    Every value is the backend's; the bot decides nothing about the order. No
    date travels with it: the operator keyboard's date step fetches the
    delivery's own bounds when tapped, so a card read the next morning still
    offers the right days. An admin or manager who is not an operator would be
    refused every one of those buttons (`require_operator`), so they get no
    keyboard, and instead a line saying where the re-date is done.
    """
    number = escape_html(data.get('order_number') or i18n.get('staff.common.not_available', language))
    lines = [f"⚠️ <b>{i18n.get('staff.notification.delivery_failed', language, number=number)}</b>"]
    customer = ' · '.join(
        escape_html(value) for value in (data.get('customer_name'), data.get('customer_phone')) if value
    )
    if customer:
        lines.append(f"👤 {customer}")
    if data.get('address'):
        lines.append(f"📍 {escape_html(data['address'])}")
    reason = data.get('reason')
    if reason:
        label = format_fail_reason(reason, language)
        lines.append(f"❗ {i18n.get('staff.notification.delivery_failed_reason', language, reason=label)}")
    if data.get('driver_name'):
        driver = escape_html(data['driver_name'])
        lines.append(f"🚚 {i18n.get('staff.notification.delivery_failed_driver', language, name=driver)}")
    if data.get('attempts'):
        lines.append(
            f"🔁 {i18n.get('staff.notification.delivery_failed_attempt', language, count=data['attempts'])}"
        )
    if data.get('is_operator'):
        return '\n'.join(lines), OperatorKeyboards.redispatch_card(delivery_id, 1, language)
    lines += ['', f"👉 {i18n.get('staff.notification.delivery_failed_admin_hint', language)}"]
    return '\n'.join(lines), None


class _TokenBucket:
    """Per-endpoint token bucket rate limiter — dep-free, single-process.

    The webhook endpoints are only meant to receive traffic from our own
    backend, but the URL is reachable from inside the cluster's network and
    a leaked secret would let an attacker flood any single endpoint. This
    bucket lets the legitimate backend traffic through (well under any sane
    rate) while clamping the worst case to `rate_per_sec` sustained with a
    `burst` headroom for the natural batching the backend already does.
    """

    __slots__ = ("rate", "capacity", "_tokens", "_last", "_lock")

    def __init__(self, rate_per_sec: float, burst: int):
        self.rate = float(rate_per_sec)
        self.capacity = float(burst)
        self._tokens = float(burst)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def try_acquire(self) -> bool:
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False


async def _parse_json_body(request) -> Tuple[Optional[dict], Optional[web.Response]]:
    """Parse the request body as JSON.

    Returns (data, None) on success, or (None, error_response) on failure.
    The previous code let `await request.json()` propagate to the catch-all
    `except Exception` block which always returned 500 — masking client
    errors as server errors and making it harder to spot a misbehaving
    caller in the logs. 400 is the right status for malformed input.
    """
    try:
        return await request.json(), None
    except Exception as e:
        logger.warning(f"Webhook JSON parse failed for {request.path}: {e}")
        return None, web.json_response(
            {'success': False, 'message': 'Invalid JSON body'}, status=400
        )


async def verify_webhook_signature(request):
    """Verify webhook signature for security"""
    signature = request.headers.get('X-Bot-Webhook-Signature')
    if not signature:
        return False

    # Use only the dedicated webhook secret. JWT_SECRET_KEY belongs to a
    # different trust domain (auth tokens); falling back to it here would
    # let a JWT-secret leak forge webhooks. Mismatch with backend signer
    # surfaces as a 401 with a clear log line — failing closed by design.
    webhook_secret = config.security.webhook_secret
    if not webhook_secret:
        logger.error("WEBHOOK_SECRET not configured")
        return False

    body = await request.read()
    expected_signature = hmac.new(
        webhook_secret.encode('utf-8'),
        body,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(signature, expected_signature)


async def reload_translations_handler(request):
    """Handle translation reload webhook - POST /internal/reload-translations"""
    try:
        if not await verify_webhook_signature(request):
            return web.json_response({'success': False, 'message': 'Invalid signature'}, status=401)

        # The translation-reload bucket is the tightest in the set (1/s, burst
        # of 5) — translations are seeded by an admin action, so anything
        # faster than that is either a runaway script or a flood.
        server = request.app.get('staff_webhook_server')
        if server is not None:
            limited = await server._check_rate_limit(request)
            if limited:
                return limited

        await i18n.reload_translations()
        return web.json_response({
            'success': True,
            'message': 'Staff translations reloaded',
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'translation_count': sum(len(keys) for keys in i18n.translations.values())
        })
    except Exception as e:
        logger.error(f"Error reloading translations: {e}", exc_info=True)
        return web.json_response({'success': False, 'message': 'Internal server error'}, status=500)


async def health_handler(_request):
    """Health check - GET /health"""
    checks = {}
    overall_healthy = True

    try:
        translation_count = sum(len(keys) for keys in i18n.translations.values())
        if translation_count > 0:
            missing = i18n.get_missing_translation_keys()
            if not missing:
                checks['translations'] = {'status': 'ok', 'total_count': translation_count}
            else:
                missing_summary = {
                    lang: {
                        'missing_count': len(keys),
                        'sample': keys[:5],
                    }
                    for lang, keys in missing.items()
                }
                checks['translations'] = {
                    'status': 'error',
                    'total_count': translation_count,
                    'missing_by_language': missing_summary,
                }
                overall_healthy = False
        else:
            checks['translations'] = {
                'status': 'error',
                'error': 'No translations loaded',
                'total_count': 0
            }
            overall_healthy = False
    except Exception as e:
        checks['translations'] = {'status': 'error', 'error': str(e)}
        overall_healthy = False

    try:
        if db_manager.pool:
            async with db_manager.pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            checks['database'] = {
                'status': 'ok',
                'pool_size': db_manager.pool.get_size(),
                'pool_free': db_manager.pool.get_idle_size()
            }
        else:
            checks['database'] = {'status': 'not_connected'}
            overall_healthy = False
    except Exception as e:
        checks['database'] = {'status': 'error', 'error': str(e)}
        overall_healthy = False

    status_code = 200 if overall_healthy else 503
    return web.json_response({
        'status': 'healthy' if overall_healthy else 'degraded',
        'service': 'staff_bot',
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'checks': checks
    }, status=status_code)


async def handle_route_updated_payload(server, data: dict) -> bool:
    """Apply one parsed `/internal/route-updated` payload.

    Returns True iff this call caused a new observable action -- a card
    was actually refreshed (create or edit), or a sounded push was
    attempted (Telegram-side failure there is swallowed below, matching
    existing best-effort semantics -- "attempted" still counts). Returns
    False when the call was a genuine no-op: the event was already
    deduped, or `update_card_for_driver` itself determined nothing should
    change (no cached token, borrowed card, API failure). The caller
    (`route_updated_handler`) does not currently branch on this, but a
    meaningful value beats an unconditional True that can never be wrong
    (fix round 1, M4) -- callers and tests can tell "something happened"
    from "this call was inert".

    Callers must have already validated `data['telegram_id']` is present
    and int-coercible -- see `route_updated_handler`. This function assumes
    it.

    THE GATE IS PLAN 1'S: `data["sound"]` is the backend's already-computed
    verdict on whether this update is worth interrupting the driver for.
    This function reads that field and MUST NOT re-derive it from
    `head_changed` / `set_changed` / `sequence_changed` / `driver_initiated`
    -- re-deciding materiality here would put the same rule in two places
    (CLAUDE.md SSOT). See `docs/*route-ux*` Plan 1 for the gate itself.

    sound=False (the common case, and the whole point of the user's original
    complaint -- "your address is updated" on every arrival is noise): NO
    chat message, ever. The driver's route card quietly becomes correct via
    `route_card.update_card_for_driver` (Task 5), which edits the existing
    card in place -- `editMessageText` produces no notification -- or, on
    first contact for that driver, sends + pins exactly one silent card
    (`disable_notification=True` on both calls; see `route_card._create_card`).
    Checked BEFORE dedup so a silent event never consumes the dedup slot of
    a later sounded one -- the bug shape `test_silent_then_sounded_is_not_deduped_away`
    guards: a constant-key dedup fallback swallowing a genuine sounded push.
    Trade-off accepted for that ordering (fix round 1, M3): a silent event
    is never deduped at all, so a backend retry of the SAME silent event
    costs one extra `GET /delivery/active` round trip (idempotent -- the
    card content-signature check skips the actual Telegram edit when
    nothing changed). Cheaper than the alternative: `_is_duplicate_event`'s
    fallback key is a *constant* per driver, so checking it before the
    sound gate would let one silent push swallow every later push --
    including genuine sounded ones -- for the full 24h dedup TTL during a
    Redis outage.

    sound=True or missing (an older backend that predates the `sound` field
    keeps today's behaviour, fail-open toward sounding): Task 9's restyled,
    capped `route_card.send_head_change_alert` -- the ONLY sounded message
    this plan sends. It refreshes the card alongside the alert (the alert is
    a pointer, not a data carrier -- Plan 3's Behaviour note), throttled by
    `ROUTE_ALERT_MIN_INTERVAL_SECONDS`: outside the window it pings, inside
    it a newest-supersedes silent alert replaces the previous one. Dedup is
    still checked first here (unlike the silent branch above) -- a sounded
    push is exactly the kind of driver-visible event a backend retry must
    not double-send.
    """
    from staff_bot.handlers.delivery import route_card

    telegram_id = int(data['telegram_id'])
    driver_id = data.get('driver_id')
    event_id = data.get('event_id')

    if not data.get('sound', True):
        return await route_card.update_card_for_driver(server.bot_app, telegram_id)

    if await server._is_duplicate_event(event_id, f"route_updated:{telegram_id}:{driver_id}"):
        return False

    await route_card.send_head_change_alert(server.bot_app, telegram_id=telegram_id)
    return True


class StaffWebhookServer:
    """Internal webhook server for staff bot notifications"""

    def __init__(self, host='0.0.0.0', port=8081):
        self.host = host
        self.port = port
        self.app = None
        self.runner = None
        self.site = None
        self.bot_app = None
        self._processed_events: dict = {}
        self._dedup_ttl = int(os.environ.get('STAFF_WEBHOOK_DEDUP_TTL_SECONDS', '86400'))
        self._redis: Optional[redis.Redis] = None
        self._redis_connected = False
        # Per-endpoint rate limits. Sized to comfortably absorb the backend's
        # legitimate traffic — broadcast `new-order` happens once per pool
        # insertion (single-digit / minute peak), the others are per-driver
        # actions (low double-digit / minute peak). The limits below are 50×
        # the realistic peaks, so legitimate callers never see 429s; the
        # bucket exists purely to clamp a rogue / leaked-key flood.
        self._rate_limiters = {
            '/internal/new-order': _TokenBucket(rate_per_sec=20, burst=60),
            '/internal/order-assigned': _TokenBucket(rate_per_sec=20, burst=60),
            '/internal/order-reassigned': _TokenBucket(rate_per_sec=20, burst=60),
            '/internal/order-cancelled': _TokenBucket(rate_per_sec=20, burst=60),
            '/internal/order-unassigned': _TokenBucket(rate_per_sec=20, burst=60),
            '/internal/delivery-failed': _TokenBucket(rate_per_sec=20, burst=60),
            '/internal/sales-event': _TokenBucket(rate_per_sec=20, burst=60),
            '/internal/bottle-event': _TokenBucket(rate_per_sec=20, burst=60),
            '/internal/route-updated': _TokenBucket(rate_per_sec=50, burst=120),
            '/internal/pool-insertion-suggestion': _TokenBucket(rate_per_sec=50, burst=120),
            '/internal/reload-translations': _TokenBucket(rate_per_sec=1, burst=5),
        }

    async def _check_rate_limit(self, request) -> Optional[web.Response]:
        """Return a 429 response if the per-endpoint token bucket is empty,
        otherwise None. Called at the top of each handler after the signature
        check so we don't account unauthorized traffic against the legitimate
        rate budget."""
        limiter = self._rate_limiters.get(request.path)
        if limiter is None:
            return None
        if await limiter.try_acquire():
            return None
        logger.warning(f"Webhook rate-limited: {request.path}")
        return web.json_response(
            {'success': False, 'message': 'Rate limit exceeded'}, status=429
        )

    def set_application(self, application):
        """Set Telegram Application instance"""
        self.bot_app = application

    async def setup(self):
        """Setup webhook server routes"""
        self.app = web.Application(client_max_size=1024 * 1024)
        # Stash a back-reference so the module-level handlers
        # (reload_translations_handler, health_handler) can reach the
        # rate-limit table without a global.
        self.app['staff_webhook_server'] = self

        self.app.router.add_post('/internal/new-order', self.new_order_handler)
        self.app.router.add_post('/internal/order-assigned', self.order_assigned_handler)
        self.app.router.add_post('/internal/order-reassigned', self.order_reassigned_handler)
        self.app.router.add_post('/internal/order-cancelled', self.order_cancelled_handler)
        self.app.router.add_post('/internal/order-unassigned', self.order_unassigned_handler)
        self.app.router.add_post('/internal/delivery-failed', self.delivery_failed_handler)
        self.app.router.add_post('/internal/sales-event', self.sales_event_handler)
        self.app.router.add_post('/internal/bottle-event', self.bottle_event_handler)
        self.app.router.add_post('/internal/reload-translations', reload_translations_handler)
        self.app.router.add_post('/internal/route-updated', self.route_updated_handler)
        self.app.router.add_post('/internal/pool-insertion-suggestion', self.pool_insertion_suggestion_handler)
        self.app.router.add_get('/health', health_handler)
        self.app.router.add_get('/internal/stats', self.stats_handler)

        await self._init_redis()

        logger.info(f"Staff webhook server configured on {self.host}:{self.port}")

    async def _init_redis(self):
        """Initialize Redis connection for webhook idempotency keys."""
        try:
            self._redis = redis.from_url(
                config.redis.url,
                encoding='utf-8',
                decode_responses=True,
            )
            await self._redis.ping()
            self._redis_connected = True
            logger.info("Webhook server Redis dedup enabled")
        except Exception as e:
            self._redis_connected = False
            self._redis = None
            # RED-005: TIER_RELIABILITY — cross-replica dedup requires Redis.
            # In-memory fallback only works for single-replica deployments, so
            # ops must know the moment this degrades.
            report_redis_failure(
                "staff_bot.webhook_server.init_redis", str(e), tier="reliability"
            )

    def _deduplicate(self, event_key: str) -> bool:
        """Return True if this event was already processed recently."""
        now = datetime.now(timezone.utc)
        self._processed_events = {
            k: v for k, v in self._processed_events.items()
            if (now - v).total_seconds() < self._dedup_ttl
        }
        if event_key in self._processed_events:
            return True
        self._processed_events[event_key] = now
        return False

    async def _is_duplicate_event(self, event_id: str, fallback_key: str) -> bool:
        """
        Return True if event was already processed.
        Uses Redis when event_id is provided; otherwise falls back to in-memory dedup.
        """
        if event_id and self._redis_connected and self._redis:
            key = RedisKeyspace.staff_bot_webhook_event(event_id)
            try:
                created = await self._redis.set(
                    key,
                    datetime.now(timezone.utc).isoformat(),
                    ex=self._dedup_ttl,
                    nx=True,
                )
                return not bool(created)
            except Exception as e:
                # RED-005: TIER_RELIABILITY — in-memory fallback loses dedup
                # across replicas. Alert so ops treats a sustained failure as
                # a priority fix, not a silent degradation.
                report_redis_failure(
                    "staff_bot.webhook_server.is_duplicate_event", str(e), tier="reliability"
                )

        return self._deduplicate(fallback_key)

    async def _release_event(self, event_id: Optional[str], fallback_key: str) -> None:
        """Give back the slot `_is_duplicate_event(event_id, fallback_key)` claimed.

        For a handler whose send failed AFTER the claim: while the slot is held,
        the producer's retry of the same event is "Already processed" and the
        message is never sent. Both stores are cleared, because either may hold
        the claim: Redis, or the in-memory fallback when Redis was down or failed
        during the claim. Best-effort: if the Redis delete fails too, it is
        reported, and the retry is refused as a duplicate, exactly as it was
        before this helper existed.
        """
        self._processed_events.pop(fallback_key, None)
        if event_id and self._redis_connected and self._redis:
            try:
                await self._redis.delete(RedisKeyspace.staff_bot_webhook_event(event_id))
            except Exception as e:
                report_redis_failure(
                    "staff_bot.webhook_server.release_event", str(e), tier="reliability"
                )

    async def stats_handler(self, _request):
        """Internal stats endpoint - GET /internal/stats"""
        now = datetime.now(timezone.utc)
        # Cleanup stale in-memory keys before reporting.
        self._deduplicate("__stats_cleanup__")
        self._processed_events.pop("__stats_cleanup__", None)

        return web.json_response({
            'success': True,
            'service': 'staff_bot',
            'timestamp': now.isoformat(),
            'dedup': {
                'ttl_seconds': self._dedup_ttl,
                'redis_enabled': self._redis_connected,
                'in_memory_keys': len(self._processed_events),
            }
        })

    async def new_order_handler(self, request):
        """Broadcast a freshly-created pool order to every eligible driver
        with an inline Accept/Decline UX. First driver to Accept wins
        (server-side row lock returns 409 to the rest, which the bot
        gracefully renders as 'already taken').

        POST /internal/new-order
        """
        try:
            if not await verify_webhook_signature(request):
                return web.json_response({'success': False, 'message': 'Invalid signature'}, status=401)

            limited = await self._check_rate_limit(request)
            if limited:
                return limited

            if not self.bot_app:
                return web.json_response({'success': False, 'message': 'Bot not initialized'}, status=503)

            data, parse_error = await _parse_json_body(request)
            if parse_error:
                return parse_error
            order_id = data.get('order_id')
            event_id = data.get('event_id')

            if await self._is_duplicate_event(event_id, f"new_order:{order_id}"):
                return web.json_response({'success': True, 'message': 'Already processed'})

            telegram_ids = data.get('delivery_person_telegram_ids', [])
            order_info = data.get('order_info', {})
            delivery_id = order_info.get('delivery_id')

            # Without a delivery_id we can't render Accept buttons that wire
            # into the standard accept flow — log loudly so the missing
            # field gets fixed at the source rather than silently skipped.
            if not delivery_id:
                logger.error(
                    "new_order broadcast missing delivery_id (order=%s) — Accept/Decline UX disabled",
                    order_id,
                )

            from telegram import InlineKeyboardButton, InlineKeyboardMarkup

            sent_count = 0
            failed_ids = []
            for tid in telegram_ids:
                try:
                    language = await i18n.get_user_language(int(tid))
                    message = self._format_new_order_message(order_info, language)
                    keyboard = None
                    if delivery_id:
                        # Accept reuses the existing confirm-accept callback so
                        # the broadcast and pool browse share one downstream
                        # flow (auth, row lock, location prompt, re-opt).
                        keyboard = InlineKeyboardMarkup([
                            [
                                InlineKeyboardButton(
                                    f"✅ {i18n.get('staff.delivery.accept', language)}",
                                    callback_data=f"staff_confirm_accept_{int(delivery_id)}",
                                ),
                                InlineKeyboardButton(
                                    f"❌ {i18n.get('staff.cancel', language)}",
                                    callback_data=f"staff_decline_suggestion_{int(delivery_id)}",
                                ),
                            ]
                        ])
                    await self.bot_app.bot.send_message(
                        chat_id=tid, text=message, parse_mode='HTML', reply_markup=keyboard,
                    )
                    sent_count += 1
                except Exception as e:
                    failed_ids.append(int(tid) if isinstance(tid, (int, str)) else tid)
                    _log_notify_failure(f"Failed to notify delivery person {tid}", e)

            # F-3: report partial success. The previous return shape was always
            # `success: True`, even when 0/N sends landed — the backend had no
            # signal to retry the failed recipients. Now success means "every
            # send succeeded"; partial_success surfaces the mixed case so the
            # backend can re-emit for the failed_ids without re-broadcasting
            # to the ones that already received the message.
            total = len(telegram_ids)
            full_success = sent_count == total
            return web.json_response({
                'success': full_success,
                'partial_success': not full_success and sent_count > 0,
                'sent_count': sent_count,
                'failed_count': total - sent_count,
                'failed_telegram_ids': failed_ids,
                'message': f'Notified {sent_count}/{total} delivery persons',
            })
        except Exception as e:
            logger.error(f"Error handling new order notification: {e}", exc_info=True)
            return web.json_response({'success': False, 'message': 'Internal server error'}, status=500)

    async def order_assigned_handler(self, request):
        """
        Notify delivery person that an order was assigned to them by admin.
        POST /internal/order-assigned
        """
        try:
            if not await verify_webhook_signature(request):
                return web.json_response({'success': False, 'message': 'Invalid signature'}, status=401)
            limited = await self._check_rate_limit(request)
            if limited:
                return limited
            if not self.bot_app:
                return web.json_response({'success': False, 'message': 'Bot not initialized'}, status=503)

            data, parse_error = await _parse_json_body(request)
            if parse_error:
                return parse_error
            telegram_id = data.get('telegram_id')
            order_info = data.get('order_info', {})
            event_id = data.get('event_id')

            if await self._is_duplicate_event(event_id, f"order_assigned:{telegram_id}:{order_info.get('order_number', '')}"):
                return web.json_response({'success': True, 'message': 'Already processed'})

            if not telegram_id:
                return web.json_response({'success': False, 'message': 'Missing telegram_id'}, status=400)

            language = await i18n.get_user_language(int(telegram_id))
            message = i18n.get('staff.notification.order_assigned', language,
                               number=order_info.get('order_number', ''))
            await self.bot_app.bot.send_message(chat_id=telegram_id, text=message)

            return web.json_response({'success': True, 'message': 'Notification sent'})
        except Exception as e:
            logger.error(f"Error handling order assigned notification: {e}", exc_info=True)
            return web.json_response({'success': False, 'message': 'Internal server error'}, status=500)

    async def sales_event_handler(self, request):
        """Push one sales-module event to a staff chat.

        Outlet events (approved / rejected / activation requested) go to the
        agent or the approvers; the two agent-order events are the store's
        answer to an order the agent placed on their behalf.

        The two pay pushes (compensation spec §7.5) carry C24's payloads and
        render through their own branches.

        POST /internal/sales-event
          {telegram_id, event, event_id,
           payload{outlet_id, outlet_name, reason, order_id, order_number}
                  | pay_penalty_confirmed_payload | pay_statement_approved_payload}
        """
        try:
            if not await verify_webhook_signature(request):
                return web.json_response({'success': False, 'message': 'Invalid signature'}, status=401)
            limited = await self._check_rate_limit(request)
            if limited:
                return limited
            if not self.bot_app:
                return web.json_response({'success': False, 'message': 'Bot not initialized'}, status=503)

            data, parse_error = await _parse_json_body(request)
            if parse_error:
                return parse_error
            telegram_id = data.get('telegram_id')
            event = data.get('event')
            payload = data.get('payload') or {}
            if not telegram_id or event not in SALES_EVENTS:
                return web.json_response(
                    {'success': False, 'message': 'Missing telegram_id or unknown event'}, status=400
                )
            # The fallback dedup key (used when the producer sent no event_id)
            # keys on the ORDER for the agent-order events: two orders for one
            # outlet on one day are two events, and keying on the outlet alone
            # would swallow the second as a duplicate of the first. Outlet
            # events carry no order_id, so they fall back to outlet_id exactly
            # as before.
            # The two pay pushes key on their own row (compensation spec §7.5):
            # neither carries an order, an outlet or a `date`, so without these
            # two penalties confirmed on one day collapse into one when Redis is
            # down.
            # The digest carries none of those, so it keys on its DAY. With a
            # constant key and a 24-hour dedup TTL, a producer that sent no
            # `event_id` would have tomorrow's digest swallowed as a duplicate of
            # today's.
            entity_id = (
                payload.get('order_id') or payload.get('outlet_id') or payload.get('penalty_id')
                or payload.get('statement_id') or payload.get('date')
            )
            event_id = data.get('event_id')
            fallback_key = f"sales:{event}:{telegram_id}:{entity_id}"
            if await self._is_duplicate_event(event_id, fallback_key):
                return web.json_response({'success': True, 'message': 'Already processed'})

            language = await i18n.get_user_language(int(telegram_id))
            text, keyboard, silent = _render_sales_event(event, payload, language, telegram_id)
            try:
                await self.bot_app.bot.send_message(
                    chat_id=telegram_id, text=text, parse_mode='HTML',
                    reply_markup=keyboard, disable_notification=silent,
                )
            except Exception as e:
                # Compensation spec §7.5 point 3 (S-18), for EVERY sales event: the
                # slot was claimed above, and `push_sales_event` retries only on a
                # non-200. Kept, the retry is "Already processed" and the message is
                # lost while reported sent. The `delivery_failed_handler` pattern:
                # nobody can reach a recipient who blocked the bot, so that is a 200
                # that keeps the slot; anything else frees it and answers 502, and
                # the retry delivers.
                _log_notify_failure(f"Failed to send sales event {event} to {telegram_id}", e)
                if _is_recipient_unreachable(e):
                    return web.json_response({'success': False, 'message': 'Recipient unreachable'})
                await self._release_event(event_id, fallback_key)
                return web.json_response({'success': False, 'message': 'Send failed'}, status=502)
            return web.json_response({'success': True, 'message': 'Notification sent'})
        except Exception as e:
            logger.error(f"Error handling sales event: {e}", exc_info=True)
            return web.json_response({'success': False, 'message': 'Internal server error'}, status=500)

    async def bottle_event_handler(self, request):
        """Push one bottle event to one driver's chat.

        POST /internal/bottle-event
          {telegram_id, event, event_id, payload}
        """
        try:
            if not await verify_webhook_signature(request):
                return web.json_response({'success': False, 'message': 'Invalid signature'}, status=401)
            limited = await self._check_rate_limit(request)
            if limited:
                return limited
            if not self.bot_app:
                return web.json_response({'success': False, 'message': 'Bot not initialized'}, status=503)

            data, parse_error = await _parse_json_body(request)
            if parse_error:
                return parse_error
            telegram_id = data.get('telegram_id')
            event = data.get('event')
            payload = data.get('payload') or {}
            if not telegram_id or event not in BOTTLE_EVENTS:
                return web.json_response(
                    {'success': False, 'message': 'Missing telegram_id or unknown event'}, status=400
                )
            event_id = data.get('event_id')
            entity = ":".join(
                str(payload[name]) for name in ('id', 'session_id', 'requester_id') if payload.get(name) is not None
            )
            # Without Redis the in-memory fallback must still tell a genuine
            # re-request (new event id, same payload) from a retry (same id).
            fallback_key = f"bottle:{event_id}" if event_id else f"bottle:{event}:{telegram_id}:{entity}"
            if await self._is_duplicate_event(event_id, fallback_key):
                return web.json_response({'success': True, 'message': 'Already processed'})

            language = await i18n.get_user_language(int(telegram_id))
            text, keyboard = _render_bottle_event(event, payload, language)
            try:
                await self.bot_app.bot.send_message(
                    chat_id=telegram_id, text=text, parse_mode='HTML', reply_markup=keyboard,
                )
            except Exception as e:
                # `sales_event_handler`'s contract: a driver who blocked the bot
                # keeps the slot (nobody can reach them); anything else frees it
                # and answers 502 so `push_bottle_event` retries and delivers.
                _log_notify_failure(f"Failed to send bottle event {event} to {telegram_id}", e)
                if _is_recipient_unreachable(e):
                    return web.json_response({'success': False, 'message': 'Recipient unreachable'})
                await self._release_event(event_id, fallback_key)
                return web.json_response({'success': False, 'message': 'Send failed'}, status=502)
            return web.json_response({'success': True, 'message': 'Notification sent'})
        except Exception as e:
            logger.error(f"Error handling bottle event: {e}", exc_info=True)
            return web.json_response({'success': False, 'message': 'Internal server error'}, status=500)

    async def order_reassigned_handler(self, request):
        """
        Notify both old and new delivery persons about reassignment.
        POST /internal/order-reassigned
        """
        try:
            if not await verify_webhook_signature(request):
                return web.json_response({'success': False, 'message': 'Invalid signature'}, status=401)
            limited = await self._check_rate_limit(request)
            if limited:
                return limited
            if not self.bot_app:
                return web.json_response({'success': False, 'message': 'Bot not initialized'}, status=503)

            data, parse_error = await _parse_json_body(request)
            if parse_error:
                return parse_error
            old_telegram_id = data.get('old_telegram_id')
            new_telegram_id = data.get('new_telegram_id')
            order_info = data.get('order_info', {})
            event_id = data.get('event_id')

            if await self._is_duplicate_event(
                event_id,
                f"order_reassigned:{old_telegram_id}:{new_telegram_id}:{order_info.get('order_number', '')}"
            ):
                return web.json_response({'success': True, 'message': 'Already processed'})

            # F-3: track each leg of the reassignment so the caller can tell
            # if neither, one, or both of the drivers were notified. Useful
            # because reassignment is the rare case where a half-success
            # (only the new driver got the ping) is still meaningful — they
            # at least know the order is theirs — but the backend may want to
            # retry for the old driver to clear their stale "you're assigned"
            # state.
            failed = []
            sent = 0

            if old_telegram_id:
                try:
                    lang = await i18n.get_user_language(int(old_telegram_id))
                    msg = i18n.get('staff.notification.order_reassigned_from', lang,
                                   number=order_info.get('order_number', ''))
                    await self.bot_app.bot.send_message(chat_id=old_telegram_id, text=msg)
                    sent += 1
                except Exception as e:
                    failed.append({'telegram_id': old_telegram_id, 'role': 'old'})
                    _log_notify_failure(f"Failed to notify old delivery person {old_telegram_id}", e)

            if new_telegram_id:
                try:
                    lang = await i18n.get_user_language(int(new_telegram_id))
                    msg = i18n.get('staff.notification.order_assigned', lang,
                                   number=order_info.get('order_number', ''))
                    await self.bot_app.bot.send_message(chat_id=new_telegram_id, text=msg)
                    sent += 1
                except Exception as e:
                    failed.append({'telegram_id': new_telegram_id, 'role': 'new'})
                    _log_notify_failure(f"Failed to notify new delivery person {new_telegram_id}", e)

            return web.json_response({
                'success': not failed,
                'partial_success': bool(failed) and sent > 0,
                'sent_count': sent,
                'failed_count': len(failed),
                'failed_recipients': failed,
                'message': 'Reassignment notifications processed',
            })
        except Exception as e:
            logger.error(f"Error handling order reassigned notification: {e}", exc_info=True)
            return web.json_response({'success': False, 'message': 'Internal server error'}, status=500)

    async def order_cancelled_handler(self, request):
        """
        Notify assigned delivery person that order was cancelled.
        POST /internal/order-cancelled
        """
        try:
            if not await verify_webhook_signature(request):
                return web.json_response({'success': False, 'message': 'Invalid signature'}, status=401)
            limited = await self._check_rate_limit(request)
            if limited:
                return limited
            if not self.bot_app:
                return web.json_response({'success': False, 'message': 'Bot not initialized'}, status=503)

            data, parse_error = await _parse_json_body(request)
            if parse_error:
                return parse_error
            telegram_id = data.get('telegram_id')
            order_info = data.get('order_info', {})
            event_id = data.get('event_id')

            if await self._is_duplicate_event(event_id, f"order_cancelled:{telegram_id}:{order_info.get('order_number', '')}"):
                return web.json_response({'success': True, 'message': 'Already processed'})

            if not telegram_id:
                return web.json_response({'success': True, 'message': 'No delivery person to notify'})

            language = await i18n.get_user_language(int(telegram_id))
            message = i18n.get('staff.notification.order_cancelled', language,
                               number=order_info.get('order_number', ''))
            await self.bot_app.bot.send_message(chat_id=telegram_id, text=message)

            return web.json_response({'success': True, 'message': 'Cancellation notification sent'})
        except Exception as e:
            logger.error(f"Error handling order cancelled notification: {e}", exc_info=True)
            return web.json_response({'success': False, 'message': 'Internal server error'}, status=500)

    async def order_unassigned_handler(self, request):
        """
        Tell a driver that dispatch removed an order from their route.
        POST /internal/order-unassigned

        Deliberately separate from /internal/order-cancelled: the order is not
        cancelled. Either it is back in the pool for another driver, or, when
        `order_info` carries `rescheduled_to` (an admin reschedule, spec §4.2),
        it moved to that day. Each gets its own copy; the cancellation copy
        would be a lie to the driver.
        """
        try:
            if not await verify_webhook_signature(request):
                return web.json_response({'success': False, 'message': 'Invalid signature'}, status=401)
            limited = await self._check_rate_limit(request)
            if limited:
                return limited
            if not self.bot_app:
                return web.json_response({'success': False, 'message': 'Bot not initialized'}, status=503)

            data, parse_error = await _parse_json_body(request)
            if parse_error:
                return parse_error
            telegram_id = data.get('telegram_id')
            order_info = data.get('order_info', {})
            event_id = data.get('event_id')

            if await self._is_duplicate_event(
                event_id, f"order_unassigned:{telegram_id}:{order_info.get('order_number', '')}"
            ):
                return web.json_response({'success': True, 'message': 'Already processed'})

            if not telegram_id:
                return web.json_response({'success': True, 'message': 'No delivery person to notify'})

            language = await i18n.get_user_language(int(telegram_id))
            number = order_info.get('order_number', '')
            # `format_local_date` is the staff bot's date SSOT and answers '' for a
            # missing or unreadable value, so the whole date sentence is gated on it:
            # never "rescheduled to  by dispatch".
            rescheduled_to = format_local_date(order_info.get('rescheduled_to'))
            if rescheduled_to:
                message = i18n.get('staff.notification.order_rescheduled', language,
                                   number=number, date=rescheduled_to)
            else:
                message = i18n.get('staff.notification.order_unassigned', language, number=number)
            await self.bot_app.bot.send_message(chat_id=telegram_id, text=message)

            return web.json_response({'success': True, 'message': 'Unassignment notification sent'})
        except Exception as e:
            logger.error(f"Error handling order unassigned notification: {e}", exc_info=True)
            return web.json_response({'success': False, 'message': 'Internal server error'}, status=500)

    async def delivery_failed_handler(self, request):
        """Alert one operator, admin or manager that a delivery failed (spec F7).

        POST /internal/delivery-failed
          {event_id, telegram_id, delivery_id, order_id, order_number, reason,
           driver_name, attempts, customer_name, customer_phone, address,
           is_operator, language}

        The guards are `order_unassigned_handler`'s; the send is not. The dedup
        slot is claimed BEFORE the send, and `push_delivery_failed` retries only
        on a non-200. So a failed send that kept its slot would be answered
        "Already processed" on every retry: the alert lost, and reported
        delivered. A recipient who can never be reached (blocked the bot, never
        started it) is therefore a 200 that keeps the slot, because no retry can
        change that. Any other send error gives the slot back and answers 502, so
        the retry really re-sends. A timeout on a message Telegram did deliver can
        then arrive twice. One duplicate alert is the accepted price of never
        losing one.
        """
        try:
            if not await verify_webhook_signature(request):
                return web.json_response({'success': False, 'message': 'Invalid signature'}, status=401)
            limited = await self._check_rate_limit(request)
            if limited:
                return limited
            if not self.bot_app:
                return web.json_response({'success': False, 'message': 'Bot not initialized'}, status=503)

            data, parse_error = await _parse_json_body(request)
            if parse_error:
                return parse_error
            try:
                telegram_id = int(data.get('telegram_id'))
                delivery_id = int(data.get('delivery_id'))
            except (TypeError, ValueError):
                return web.json_response(
                    {'success': False, 'message': 'Missing or malformed telegram_id or delivery_id'},
                    status=400,
                )

            # The recipient's language travels in the payload. The card is rendered
            # before the slot is claimed, so the send is the only step that can
            # fail while the slot is held.
            language = i18n.normalize_language(data.get('language'))
            text, keyboard = _render_delivery_failed_alert(data, delivery_id, language)

            event_id = data.get('event_id')
            # Used only when Redis is down. The attempt count is in it so that the
            # SAME delivery failing again after a re-date is a new alert, not a
            # replay of the first one inside the 24h dedup TTL.
            fallback_key = f"delivery_failed:{telegram_id}:{delivery_id}:{data.get('attempts')}"
            if await self._is_duplicate_event(event_id, fallback_key):
                return web.json_response({'success': True, 'message': 'Already processed'})

            try:
                # F7: it plays a sound. Nobody is watching this chat when a
                # delivery fails somewhere else.
                await self.bot_app.bot.send_message(
                    chat_id=telegram_id, text=text, parse_mode='HTML',
                    reply_markup=keyboard, disable_notification=False,
                )
            except Exception as e:
                _log_notify_failure(
                    f"Failed to send the delivery-failed alert for delivery {delivery_id} to {telegram_id}", e
                )
                if _is_recipient_unreachable(e):
                    return web.json_response({'success': False, 'message': 'Recipient unreachable'})
                await self._release_event(event_id, fallback_key)
                return web.json_response({'success': False, 'message': 'Send failed'}, status=502)

            return web.json_response({'success': True, 'message': 'Notification sent'})
        except Exception as e:
            logger.error(f"Error handling delivery failed notification: {e}", exc_info=True)
            return web.json_response({'success': False, 'message': 'Internal server error'}, status=500)

    async def route_updated_handler(self, request):
        """Notify driver that their optimized route changed.
        POST /internal/route-updated

        Best-effort. Signature/rate-limit/existence guards live here; the
        parsed payload is delegated to `handle_route_updated_payload` (a
        free function) so the sound-gate / dedup / card-refresh branching is
        unit-testable without an aiohttp request. See that function's
        docstring for the sounded vs. silent split.

        Every failure from THIS POINT ON (payload handling, or an
        unrecognized exception) still returns 200 (fix round 1, M5,
        confirmed deliberate): this is a fire-and-forget backend->bot ping
        with no caller that acts on failure, so a 5xx here would only
        trigger the backend's webhook retry policy -- re-attempting an
        already-lost cause and, worse, risking a second driver-visible send
        on retry for the sounded branch. The malformed-input checks below
        are the one exception: those ARE surfaced as 4xx, because a
        malformed payload is a backend bug worth making loud, not a
        recipient-side condition retries would fix.
        """
        try:
            if not await verify_webhook_signature(request):
                return web.json_response({'success': False, 'message': 'Invalid signature'}, status=401)
            limited = await self._check_rate_limit(request)
            if limited:
                return limited
            if not self.bot_app:
                return web.json_response({'success': False, 'message': 'Bot not initialized'}, status=503)

            data, parse_error = await _parse_json_body(request)
            if parse_error:
                return parse_error

            raw_telegram_id = data.get('telegram_id')
            if not raw_telegram_id:
                return web.json_response({'success': False, 'message': 'Missing telegram_id'}, status=400)
            try:
                int(raw_telegram_id)
            except (TypeError, ValueError):
                # fix round 1, M1: this used to reach `int(data['telegram_id'])`
                # deep inside `handle_route_updated_payload`, where the
                # best-effort try/except below silently turned a malformed
                # payload into a 200 -- hiding a genuine backend bug. Caught
                # here, before that swallow, as an explicit 4xx instead.
                return web.json_response(
                    {'success': False, 'message': 'Malformed telegram_id'}, status=400
                )

            try:
                await handle_route_updated_payload(self, data)
            except Exception as e:  # noqa: BLE001 -- best-effort ping; a
                # failure applying the update must not turn into a 500 the
                # backend would retry (and potentially double-notify on retry).
                # exc_info=True (fix round 2, item 5): this branch now covers
                # the sounded alert too, a much larger blast radius than the
                # silent card-only refresh it used to guard alone -- a bare
                # message with no traceback made a real regression here
                # invisible in the logs while still returning 200.
                logger.warning(
                    f"route_updated handling failed for {data.get('telegram_id')}: {e}",
                    exc_info=True,
                )

            return web.json_response({'success': True, 'message': 'Route-updated handled'})
        except Exception as e:
            logger.error(f"Error in route_updated_handler: {e}", exc_info=True)
            return web.json_response({'success': False, 'message': 'Internal server error'}, status=500)

    async def pool_insertion_suggestion_handler(self, request):
        """Push an Accept/Decline suggestion for a freshly-pooled order that
        fits the driver's current route.
        POST /internal/pool-insertion-suggestion
        """
        try:
            if not await verify_webhook_signature(request):
                return web.json_response({'success': False, 'message': 'Invalid signature'}, status=401)
            limited = await self._check_rate_limit(request)
            if limited:
                return limited
            if not self.bot_app:
                return web.json_response({'success': False, 'message': 'Bot not initialized'}, status=503)

            data, parse_error = await _parse_json_body(request)
            if parse_error:
                return parse_error
            telegram_id = data.get('telegram_id')
            delivery_id = data.get('delivery_id')
            order_no = data.get('order_no', '')
            detour_km = data.get('detour_km', 0)
            detour_min = data.get('detour_minutes', 0)
            # Plan 1's diversion-offer fields (§7). Read defensively — an
            # older backend that hasn't shipped them yet simply omits them,
            # and `offers.build_offer` already treats that as the plain
            # shape (deploy-skew tolerance).
            gain_minutes = data.get('gain_minutes')
            committed_order_number = data.get('committed_order_number')
            event_id = data.get('event_id')

            if not telegram_id or not delivery_id:
                return web.json_response({'success': False, 'message': 'Missing telegram_id or delivery_id'}, status=400)

            if await self._is_duplicate_event(event_id, f"pool_insert:{telegram_id}:{delivery_id}"):
                return web.json_response({'success': True, 'message': 'Already processed'})

            # C-2: check whether the driver is mid-flow (cash collection,
            # COD collect, bottle collect, reconciliation, tryout pickup).
            # The text router would interpret an unrelated typed reply as
            # input for the active flow, and an Accept tap would orphan
            # the flow's pending_*_flow flag. Queue the suggestion instead;
            # the flow's clear/finalize path drains the queue and dispatches
            # the deferred message at the user's next idle moment.
            active_flow = await flow_state.get_active_flow(int(telegram_id))
            payload = {
                'delivery_id': int(delivery_id),
                'order_no': order_no,
                'detour_km': detour_km,
                'detour_minutes': detour_min,
                'gain_minutes': gain_minutes,
                'committed_order_number': committed_order_number,
            }
            if active_flow:
                queued = await flow_state.queue_pool_suggestion(int(telegram_id), payload)
                logger.info(
                    f"pool_insertion deferred for {telegram_id} (active_flow={active_flow}, "
                    f"queued={queued})"
                )
                return web.json_response({
                    'success': True,
                    'deferred': True,
                    'queued': queued,
                    'active_flow': active_flow,
                    'message': 'Driver is mid-flow; suggestion queued',
                })

            language = await i18n.get_user_language(int(telegram_id))

            # SSOT: staff_bot/utils/offers.py is the ONE place that decides
            # the offer's text + keyboard (plain pool-insertion vs. diversion),
            # shared with the deferred-drain path in flow_state.clear_and_drain.
            from staff_bot.utils import offers

            text, keyboard = offers.build_offer(payload, language)
            # Rules that must hold (Task 10 brief): disable_notification=True
            # on every non-urgent send — only an uncapped (sent live, not
            # deferred) diversion offer is time-critical enough to ping.
            try:
                await self.bot_app.bot.send_message(
                    chat_id=int(telegram_id),
                    text=text,
                    reply_markup=keyboard,
                    disable_notification=not offers.is_diversion_offer(payload),
                )
            except Exception as e:
                logger.warning(f"pool_insertion send failed for {telegram_id}: {e}")

            return web.json_response({'success': True, 'message': 'Suggestion sent'})
        except Exception as e:
            logger.error(f"Error in pool_insertion_suggestion_handler: {e}", exc_info=True)
            return web.json_response({'success': False, 'message': 'Internal server error'}, status=500)

    def _format_new_order_message(self, order_info: dict, language: str) -> str:
        """Format new order notification message.

        Fields carrying customer-provided free text (name, address, product
        names) are HTML-escaped because the message is sent with
        parse_mode='HTML'.
        """
        number = escape_html(order_info.get('order_number') or i18n.get('staff.common.not_available', language))
        customer_name = escape_html(order_info.get('customer_name', ''))
        address = escape_html(order_info.get('address') or order_info.get('district', ''))
        window_line = format_delivery_window_line(order_info, language)
        amount = order_info.get('total_amount', 0)
        payment = order_info.get('payment_method', '')
        payment_label = i18n.get(f'staff.delivery.payment.{payment}', language) if payment else ''
        amount_text = format(amount, ',.0f')
        if payment_label:
            amount_text = f"{amount_text} {i18n.get('staff.currency.uzs', language)} ({payment_label})"
        else:
            amount_text = f"{amount_text} {i18n.get('staff.currency.uzs', language)}"

        lines = [
            f"🆕 {i18n.get('staff.notification.new_order', language)}",
            "",
            f"📦 #{number}",
        ]
        if customer_name:
            lines.append(f"👤 {customer_name}")
        if address:
            lines.append(f"📍 {address}")
        if window_line:
            lines.append(window_line)
        lines.append(f"💰 {amount_text}")

        items = order_info.get('items') or []
        if items:
            for item in items:
                name = escape_html(item.get('product_name') or i18n.get('staff.common.not_available', language))
                quantity = item.get('quantity', 0)
                lines.append(f"📝 {name} × {quantity}")
        else:
            # Fall back to the bare count if the payload predates item details.
            item_count = order_info.get('item_count', 0)
            lines.append(f"📝 {item_count} {i18n.get('staff.items', language)}")

        return '\n'.join(lines)

    async def start(self):
        """Start the webhook server"""
        try:
            if not self.app:
                await self.setup()
            self.runner = web.AppRunner(self.app)
            await self.runner.setup()
            self.site = web.TCPSite(self.runner, self.host, self.port)
            await self.site.start()
            logger.info(f"Staff webhook server started on http://{self.host}:{self.port}")
        except Exception as e:
            logger.error(f"Failed to start staff webhook server: {e}", exc_info=True)
            raise

    async def stop(self):
        """Stop the webhook server"""
        try:
            if self.site:
                await self.site.stop()
            if self.runner:
                await self.runner.cleanup()
            if self._redis:
                await self._redis.close()
                self._redis = None
                self._redis_connected = False
            logger.info("Staff webhook server stopped")
        except Exception as e:
            logger.error(f"Error stopping staff webhook server: {e}", exc_info=True)


# Global webhook server instance
webhook_server = StaffWebhookServer(
    host='0.0.0.0',
    port=int(os.environ.get('STAFF_WEBHOOK_SERVER_PORT', '8081'))
)
