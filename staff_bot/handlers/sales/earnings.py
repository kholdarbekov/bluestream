"""My earnings: the sales agent's pay estimate, its credited lines, the pipeline and penalties,
and the approved statements.

Cloned from `stats.py` (compensation spec §7.2), and a renderer in the same sense. Every
figure, month, status and label decision on these screens is a field that
`GET /staff/sales/me/earnings` (S1), `GET /staff/sales/me/earnings/lines` (S2),
`GET /staff/sales/me/statements` (S3) or `GET /staff/sales/me/statements/<month>` (S4)
published: the gate and its reason, the carry-in label, whether a balance is being netted,
which statement is the latest, which months have numbers at all. The one thing done to a figure
here is printing a penalty, which the backend publishes as a positive amount, with the minus it is.
"""
import logging
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from shared.staff_constants import (
    SALES_PAY_DIFFERENCE_CAUSES,
    SALES_PAY_LEDGER_KINDS,
    SALES_PAY_PENALTY_STATUSES,
    SALES_PAY_PIPELINE_WAITING,
    SALES_PAY_REVERSAL_CAUSES,
)
from staff_bot.api_client import api_client
from staff_bot.handlers.base import BaseHandler
from staff_bot.i18n import i18n
from staff_bot.keyboards.sales import SalesKeyboards
from staff_bot.permissions import require_auth, require_sales_agent
from staff_bot.utils.formatters import (
    TELEGRAM_TEXT_LIMIT,
    escape_html,
    format_currency,
    format_local_date,
    format_number,
    format_pay_amount,
    format_pay_balance_note,
    format_pay_carry_in,
    format_pay_month,
    format_pay_product_name,
    format_pay_status,
    format_pay_tiers,
    signed_currency,
    signed_deduction,
)

logger = logging.getLogger(__name__)

LINES_PREFIX = 'staff_sales_earn_l_'
# The Statement (S4): edited in place from the summary and Past statements, or sent as a new
# message from the approval push (`_sn_`, M9).
STATEMENT_PREFIX = 'staff_sales_earn_s_'
STATEMENT_PUSH_PREFIX = 'staff_sales_earn_sn_'
# §7.2: the pipeline and the penalty list show at most 20 items. The backend caps them too;
# this is the bot's own guard against a payload that did not.
LIST_CAP = 20
# §7.2: at most three adjustment reasons, each cut to 80 characters (review gaming F6).
ADJUSTMENTS_SHOWN = 3
REASON_MAX = 80
LINE_CAUSES = SALES_PAY_REVERSAL_CAUSES + SALES_PAY_DIFFERENCE_CAUSES


def _month_of(key: str) -> Optional[str]:
    """A button's month key ("202610") as "2026-10", or None for one no keyboard draws."""
    if len(key) != 6 or not key.isdigit() or not 1 <= int(key[4:]) <= 12:
        return None
    return f"{key[:4]}-{key[4:]}"


def parse_lines_callback(data: Optional[str]) -> Optional[Tuple[str, int]]:
    r"""`staff_sales_earn_l_<yyyymm>_<page>` as ("YYYY-MM", page), or None.

    The registered pattern `^staff_sales_earn_l_\d+_\d+$` guarantees digits, not a RANGE: a
    month 13 or a page 0 is a button this bot never drew. The SUFFIX after the fixed prefix is
    parsed, never the last underscore segment (`stats.py`'s rule).
    """
    key, _, page = (data or '').split(LINES_PREFIX, 1)[-1].partition('_')
    month = _month_of(key)
    if month is None or not page.isdigit() or int(page) < 1:
        return None
    return month, int(page)


def parse_statement_callback(data: Optional[str], prefix: str) -> Optional[str]:
    r"""`<prefix><yyyymm>` as "YYYY-MM", or None: `^staff_sales_earn_s_\d+$` and
    `^staff_sales_earn_sn_\d+$` guarantee digits, not a month (`parse_lines_callback`'s rule)."""
    return _month_of((data or '').split(prefix, 1)[-1])


def _multiplier(value, language: str) -> str:
    """The gate factor exactly as published: 0.8 reads "0.8", 1 reads "1.0", 0.75 "0.75"."""
    if value is None:
        return i18n.get('staff.sales.stats.na', language)
    text = format(Decimal(str(value)).normalize(), 'f')
    return text if '.' in text else f"{text}.0"


def _gate_line(gate: Dict, language: str) -> str:
    """"Plan vs fact: 75.5% · 160 of 212 visits → ×0.8", plus why ×1.0 when the minimum was
    not reached. The label is My stats' own row, because the two screens print the same %
    (C4; S1's `compliance_pct` is the call `/me/stats?period=month` makes)."""
    pct = gate.get('compliance_pct')
    shown = i18n.get('staff.sales.stats.na', language) if pct is None else f"{float(pct):.1f}%"
    visits = i18n.get('staff.sales.earnings.visits_of', language,
                      done=gate.get('visits_counted') or 0, due=gate.get('visits_due') or 0)
    line = (f"{i18n.get('staff.sales.stats.metric_plan_vs_fact_pct', language)}: {shown} · {visits} "
            f"→ ×{_multiplier(gate.get('multiplier'), language)}")
    if gate.get('rule') != 'below_min_due':
        return line
    # Review money M3: on an estimate the minimum may still be reached, so the provisional
    # wording promises nothing yet.
    if gate.get('provisional'):
        why = i18n.get('staff.sales.earnings.gate_not_applied_yet', language, min_due=gate.get('min_due'))
    else:
        why = i18n.get('staff.sales.earnings.gate_not_applied', language, min_due=gate.get('min_due'))
    return f"{line} ({why})"


def _adjustment_lines(items: List[Dict], language: str) -> List[str]:
    """At most three reasons, as the admin typed them: cut BEFORE escaping, so the cut counts
    the characters typed and never halves an HTML entity."""
    shown = items[:ADJUSTMENTS_SHOWN]
    lines = [
        f"   · {signed_currency(item.get('amount'), language)} — "
        f"{escape_html(str(item.get('reason') or '')[:REASON_MAX])}"
        for item in shown
    ]
    if len(items) > len(shown):
        lines.append(f"   · {i18n.get('staff.sales.earnings.more', language, count=len(items) - len(shown))}")
    return lines


def _product_blocks(commission: Dict, language: str, *, tiers: bool) -> List[str]:
    """One block per product of the month (§7.2 "Tier rows"): its product line, then its tier
    rows and the 🎯 next tier, or the product line alone once the card has to shrink."""
    blocks = []
    for product in commission.get('products') or []:
        rows = format_pay_tiers(product, language, with_next=True)
        blocks.append('\n'.join(rows if tiers else rows[:1]))
    return blocks


def _late_rows(late: Dict, language: str) -> List[str]:
    """Under "Corrections from earlier months", one row per product a late group moved:
    "   · 10.2026 · 19L: 700 → 400 units" (§7.2). The group's gated figure is the row above.

    A group the backend publishes `counted: false` re-reads the trial month, which is never
    paid (I-16): its units move and its money adds 0, so its rows carry the trial month's own
    marker, as a not-counted line does on the credited-orders list."""
    rows = []
    for group in late.get('groups') or []:
        month = format_pay_month(group.get('earned_month'))
        shadow = (f" · {i18n.get('staff.sales.earnings.not_counted_shadow', language)}"
                  if group.get('counted') is False else '')
        for product in group.get('products') or []:
            units = i18n.get('staff.sales.earnings.units', language, count=format_number(product.get('units')))
            rows.append(f"   · {month} · {format_pay_product_name(product.get('product_name'), language)}: "
                        f"{format_number(product.get('units_before'))} → {units}{shadow}")
    return rows


def _rows_shown(rows: List[str], cut: int, indent: str, language: str) -> List[str]:
    """`rows` less the last `cut`, the cut COUNTED in one "+N more" row, never dropped silently."""
    if not cut:
        return rows
    more = i18n.get('staff.sales.earnings.more', language, count=cut)
    return rows[:len(rows) - cut] + [f"{indent}{more}"]


def _estimate_lines(estimate: Dict, language: str, *, tiers: bool = True, cut: int = 0,
                    balance_note: bool = True) -> List[str]:
    """The open month's formula, row by row, in the order the statement adds it up.

    The Commission row carries each product and its tier rows; the corrections row carries each
    product a late group moved. `tiers=False` keeps the product lines alone, and `cut` drops
    that many per-product rows from the end, late ones first, into "+N more" rows: how
    `format_earnings` fits a busy month into one message. A formula row is never dropped.
    `balance_note=False` leaves off the sentence under the total, for a frozen statement whose
    head decides it (`_statement_balance_note`).
    """
    base = estimate.get('base') or {}
    commission = estimate.get('commission') or {}
    late = estimate.get('late') or {}
    bonuses = estimate.get('new_outlets') or {}
    adjustments = estimate.get('adjustments') or {}
    penalties = estimate.get('penalties') or {}
    days = i18n.get('staff.sales.earnings.days_worked', language,
                    worked=base.get('worked_days') or 0, working=base.get('working_days') or 0)
    orders = i18n.get('staff.sales.earnings.orders_count', language, count=commission.get('orders') or 0)
    products = _product_blocks(commission, language, tiers=tiers)
    late_rows = _late_rows(late, language) if late.get('lines') else []
    late_cut = min(cut, len(late_rows))
    product_cut = min(cut - late_cut, len(products))
    lines = [
        f"{i18n.get('staff.sales.earnings.base', language)}: {format_pay_amount(base.get('amount'), language)} "
        f"({format_currency(base.get('monthly'), language=language)} · {days})",
        f"{i18n.get('staff.sales.earnings.commission', language)}: "
        f"{format_pay_amount(commission.get('gross'), language)} · {orders}",
        *_rows_shown(products, product_cut, '   ', language),
        _gate_line(estimate.get('gate') or {}, language),
        f"{i18n.get('staff.sales.earnings.commission_after_gate', language)}: "
        f"{format_pay_amount(commission.get('after_gate'), language)}",
    ]
    # The optional rows are drawn only when the answer has something in them, as on the §7.2
    # screens: corrections when a late line was counted, a bonus, adjustment or penalty row
    # when there is one.
    if late.get('lines'):
        lines.append(f"{i18n.get('staff.sales.earnings.late', language)}: "
                     f"{signed_currency(late.get('after_gate'), language)}")
        lines.extend(_rows_shown(late_rows, late_cut, '   · ', language))
    if bonuses.get('count'):
        lines.append(f"{i18n.get('staff.sales.earnings.new_outlets', language)}: "
                     f"{signed_currency(bonuses.get('amount'), language)} · {bonuses['count']}")
    items = adjustments.get('items') or []
    if items:
        lines.append(f"{i18n.get('staff.sales.earnings.adjustments', language)}: "
                     f"{signed_currency(adjustments.get('amount'), language)}")
        lines.extend(_adjustment_lines(items, language))
    if penalties.get('count'):
        lines.append(f"{i18n.get('staff.sales.earnings.penalties', language)}: "
                     f"{signed_deduction(penalties.get('amount'), language)} · {penalties['count']}")
    lines.append(f"{i18n.get('staff.sales.earnings.variable', language)}: "
                 f"{format_pay_amount(estimate.get('variable'), language)}")
    carry_in = format_pay_carry_in(estimate.get('carry_in'), language)
    if carry_in:
        lines.append(carry_in)
    lines.append(f"<b>{i18n.get('staff.sales.earnings.total', language)}: "
                 f"{format_pay_amount(estimate.get('total'), language)}</b>")
    if balance_note:
        note = format_pay_balance_note(estimate.get('carry_out'), estimate.get('owed'), language)
        if note:
            lines.append(note)
    return lines


def _statement_status(statement: Dict, language: str) -> str:
    """"Approved", or "Paid · paid on 05.09.2026": a statement's status, as the last-statement
    line and the Statement screen's title both word it."""
    status = format_pay_status(statement.get('status'), language)
    paid_on = format_local_date(statement.get('paid_on'))
    if not paid_on:
        return status
    return f"{status} · {i18n.get('staff.sales.earnings.paid_on', language, date=paid_on)}"


def _owed_netted_note(statement: Dict, language: str) -> Optional[str]:
    """"Owed: X. Being deducted in MM.YYYY." when an open estimate is deducting this statement's
    owed balance (`owed_netted_in`, D.6), else None. S1's last statement and S4's head publish
    the same pair for the same row."""
    owed = statement.get('owed')
    netted_in = format_pay_month(statement.get('owed_netted_in'))
    if not netted_in or owed is None or float(owed) <= 0:
        return None
    return i18n.get('staff.sales.earnings.owed_netted_note', language,
                    amount=format_currency(owed, language=language), month=netted_in)


def _last_statement_lines(last: Optional[Dict], language: str) -> List[str]:
    """The latest approved statement and, under it, the agent's outstanding balance (I-28)."""
    if not last:
        return []
    lines = [f"✅ {i18n.get('staff.sales.earnings.last_statement', language)}: "
             f"{format_pay_month(last.get('month'))} · {format_pay_amount(last.get('total'), language)} · "
             f"{_statement_status(last, language)}"]
    if last.get('is_shadow'):
        # A trial month is approved and never paid (I-16), and its push said so: without the
        # note its line reads like a paid statement. Only the epoch can be a trial month, so
        # the open month above never carries the same note.
        lines.append(i18n.get('staff.sales.earnings.shadow_note', language))
    owed = last.get('owed')
    netted = _owed_netted_note(last, language)
    if netted:
        # An open estimate on this screen is already deducting this balance (D.6): the
        # words must say where, or the agent reads one debt twice.
        lines.append(netted)
    elif owed is not None and float(owed) > 0:
        lines.append(i18n.get('staff.sales.earnings.owed_note', language,
                              amount=format_currency(owed, language=language)))
    return lines


def _footer_lines(data: Dict, language: str, *, with_estimate: bool) -> List[str]:
    """Pipeline, older open months, months under review, the last statement. The first two
    need an estimate above them; without terms (§7.2) only the last two are shown."""
    lines = []
    if with_estimate:
        pipeline = data.get('pipeline') or {}
        if pipeline.get('count'):
            summary = i18n.get('staff.sales.earnings.pipeline_summary', language, count=pipeline['count'])
            lines.append(f"⏳ {summary} ≈ {format_currency(pipeline.get('estimated_total'), language=language)}")
        for older in (data.get('open_months') or [])[1:]:
            line = f"🧾 {format_pay_month(older.get('month'))} · {format_pay_status(older.get('status'), language)}"
            estimate = older.get('estimate') if older.get('configured') else None
            if estimate is not None:
                line = f"{line} · ≈ {format_pay_amount(estimate.get('total'), language)}"
                if older.get('is_shadow'):
                    # The trial month is never paid (I-16): its estimate must not read as pay due.
                    line = f"{line} · {i18n.get('staff.sales.earnings.not_counted_shadow', language)}"
            lines.append(line)
    for month in data.get('in_review') or []:
        # I-14: a closed month is named by its status only, never with a figure.
        lines.append(f"🔎 {format_pay_month(month.get('month'))} · {format_pay_status(month.get('status'), language)}")
    lines.extend(_last_statement_lines(data.get('last_statement'), language))
    return lines


def _summary_text(data: Dict, language: str, *, tiers: bool = True, cut: int = 0) -> str:
    """The summary card at one level of detail (`tiers` and `cut` as in `_estimate_lines`)."""
    title = i18n.get('staff.sales.earnings.title', language)
    if data.get('state') == 'not_started':
        return f"💰 <b>{title}</b>\n{i18n.get('staff.sales.earnings.not_started', language)}"
    months = data.get('open_months') or []
    current = months[0] if months else {}
    month = format_pay_month(current.get('month'))
    lines = [f"💰 <b>{title} — {month}</b>" if month else f"💰 <b>{title}</b>"]
    if current.get('is_shadow'):
        lines.append(i18n.get('staff.sales.earnings.shadow_note', language))
    estimate = current.get('estimate') if current.get('configured') else None
    if estimate is None:
        if current:
            lines.append(i18n.get('staff.sales.earnings.not_configured', language))
    else:
        as_of = format_local_date(data.get('as_of'), '%d.%m.%Y %H:%M')
        lines.append(f"<i>{i18n.get('staff.sales.earnings.estimate_note', language, time=as_of)}</i>")
        lines.append('')
        lines.extend(_estimate_lines(estimate, language, tiers=tiers, cut=cut))
    footer = _footer_lines(data, language, with_estimate=estimate is not None)
    if footer:
        lines.append('')
        lines.extend(footer)
    return '\n'.join(lines)


def _fitted(text_at) -> str:
    """The card `text_at(tiers=…, cut=…)` draws, at the most detail one message holds.

    Telegram refuses a message over `TELEGRAM_TEXT_LIMIT`, and the tier rows are most of a busy
    month's card. Over the limit they collapse to their product lines first. Only then are the
    per-product rows cut from the end, the late corrections' before the month's own, each cut
    counted in a "+N more" row (§7.2 "Length"). Which rows fit is the one thing decided here.
    """
    text = text_at(tiers=True, cut=0)
    if len(text) <= TELEGRAM_TEXT_LIMIT:
        return text
    cut, text = 0, text_at(tiers=False, cut=0)
    while len(text) > TELEGRAM_TEXT_LIMIT:
        cut += 1
        shorter = text_at(tiers=False, cut=cut)
        if shorter == text:
            break  # every per-product row is cut: what is left is the formula itself
        text = shorter
    return text


def format_earnings(data: Dict, language: str) -> str:
    """The summary: the newest open month in full (S1 lists them newest first), then one line
    for each other month the answer names, fitted to one message (`_fitted`)."""
    return _fitted(lambda **detail: _summary_text(data, language, **detail))


def _statement_balance_note(data: Dict, language: str) -> Optional[str]:
    """The ONE balance sentence an S4 statement carries, or None (controller ruling, Task 1).

    A balance an open estimate is deducting says where (`owed_netted_in`). Otherwise only the
    agent's latest statement (`is_latest`, the row S1 calls the last statement) says what comes
    next, with the sentence the summary prints under a total. An older statement's shortfall or
    owed balance has since been carried or netted, so a forward-looking sentence would be false.
    Its figure rows (`_estimate_lines`) have no carry-out or owed row either: that history shows
    on the next statement, as its carry-in row.
    """
    netted = _owed_netted_note(data, language)
    if netted:
        return netted
    if not data.get('is_latest'):
        return None
    statement = data.get('statement') or {}
    return format_pay_balance_note(statement.get('carry_out'), statement.get('owed'), language)


def _statement_text(data: Dict, language: str, *, tiers: bool = True, cut: int = 0) -> str:
    """The Statement screen at one level of detail (`tiers` and `cut` as in `_estimate_lines`):
    the title with the month's status, the trial note where the summary prints it, then the
    summary's own rows for the frozen month. No "Estimate as of" line: these figures are final."""
    title = i18n.get('staff.sales.earnings.statement_title', language, month=format_pay_month(data.get('month')))
    status = _statement_status(data, language)
    lines = [f"📄 <b>{title}</b> · {status}" if status else f"📄 <b>{title}</b>"]
    if data.get('is_shadow'):
        lines.append(i18n.get('staff.sales.earnings.shadow_note', language))
    lines.append('')
    lines.extend(_estimate_lines(data.get('statement') or {}, language, tiers=tiers, cut=cut, balance_note=False))
    note = _statement_balance_note(data, language)
    if note:
        lines.append(note)
    return '\n'.join(lines)


def format_statement(data: Dict, language: str) -> str:
    """S4: one approved or paid month, row for row as the summary printed it while it was open,
    fitted to one message the summary's way (`_fitted`)."""
    return _fitted(lambda **detail: _statement_text(data, language, **detail))


def format_past_statements(items: List[Dict], language: str) -> str:
    """Past statements (S3): the months are the keyboard's buttons, so the text is the title, or
    the sentence saying there is none yet."""
    title = f"📚 <b>{i18n.get('staff.sales.earnings.past_statements', language)}</b>"
    if items:
        return title
    return '\n'.join((title, '', i18n.get('staff.sales.earnings.past_empty', language)))


def _fit(head: List[str], blocks: List[str], language: str, *, unshown: int = 0) -> str:
    """The head and as many whole blocks as fit one Telegram message, cut from the END.

    What was cut is COUNTED into the "+N more" row, together with `unshown` (what the list
    cap already left out), never silently dropped.
    """
    shown = list(blocks)
    while True:
        cut = unshown + len(blocks) - len(shown)
        tail = [i18n.get('staff.sales.earnings.more', language, count=cut)] if cut else []
        text = '\n'.join(head + shown + tail)
        if len(text) <= TELEGRAM_TEXT_LIMIT or not shown:
            return text
        shown.pop()


def _line_block(item: Dict, language: str) -> str:
    """One S2 row: "12.10 · SA_000412_26 · Oasis market" over "   19L × 12 · Commission
    +18,000 UZS · <cause> · for 09.2026".

    The order's units come first, per product as S2 publishes them: the units still counted, or
    those a reversal took out (I-41). A tier-change row (`tier_shift`) is an order that did not
    change itself, so S2 gives it no date, kind or cause and it reads "Tier change". The kind,
    the cause, the late marker and the not-counted marker are the backend's fields: the last is
    S2's `counted`, false for a trial-month line that does not change this month's pay (I2)."""
    head = ' · '.join(part for part in (
        format_local_date(item.get('date'), '%d.%m'),
        escape_html(item.get('order_number')),
        escape_html(item.get('outlet_name')),
    ) if part)
    units = ', '.join(
        f"{format_pay_product_name(entry.get('product_name'), language)} × {format_number(entry.get('units'))}"
        for entry in item.get('units') or []
    )
    if item.get('tier_shift'):
        label = i18n.get('staff.sales.earnings.tier_shift', language)
    else:
        kind = item.get('kind')
        label = i18n.get(f'staff.sales.earnings.kind.{kind}', language) if kind in SALES_PAY_LEDGER_KINDS else ''
    detail = [part for part in (units, f"{label} {signed_currency(item.get('amount'), language)}".strip()) if part]
    cause = item.get('cause')
    if cause in LINE_CAUSES:
        detail.append(i18n.get(f'staff.sales.earnings.cause.{cause}', language))
    if item.get('is_late'):
        detail.append(i18n.get('staff.sales.earnings.for_month', language,
                               month=format_pay_month(item.get('earned_month'))))
    if item.get('counted') is False:
        detail.append(i18n.get('staff.sales.earnings.not_counted_shadow', language))
    return f"{head}\n   {' · '.join(detail)}"


def _no_lines(data: Dict, month: str, language: str) -> str:
    """Why S2's page is empty, from its own `available` and `status`: a readable month with
    nothing credited in it, a closed month under review (I-14: no rows until it is approved),
    or, with no period at all, the generic sentence."""
    if data.get('available'):
        return i18n.get('staff.sales.earnings.lines_none', language, month=month)
    if data.get('status') == 'closed':
        return i18n.get('staff.sales.earnings.lines_under_review', language, month=month)
    return i18n.get('staff.sales.earnings.empty', language)


def format_earnings_lines(data: Dict, language: str, *, month: str, page: int) -> str:
    """A page of credited lines. A month S2 answers `available: false` for (closed, or no
    period) shows why it is empty and no figure (I-14). The month and page printed are the
    ANSWER's when it names them, the requested ones otherwise."""
    shown_month = format_pay_month(data.get('month') or month)
    head = [
        f"🧾 <b>{i18n.get('staff.sales.earnings.lines_title', language)} — {shown_month}</b> · "
        f"{i18n.get('staff.sales.earnings.page', language, page=data.get('page') or page)}",
        '',
    ]
    items = (data.get('items') or []) if data.get('available') else []
    if not items:
        return '\n'.join(head + [_no_lines(data, shown_month, language)])
    return _fit(head, [_line_block(item, language) for item in items], language)


def _pipeline_line(item: Dict, language: str) -> str:
    waiting = item.get('waiting_for')
    parts = [escape_html(item.get('order_number')), escape_html(item.get('outlet_name'))]
    if waiting in SALES_PAY_PIPELINE_WAITING:
        parts.append(i18n.get(f'staff.sales.earnings.waiting.{waiting}', language))
    line = ' · '.join(part for part in parts if part)
    estimate = item.get('estimated_commission')
    if estimate is None:
        # No plan resolves for the order (no terms): the backend made no estimate, and
        # `format_currency` would print one of 0.
        return line
    return f"{line} ≈ {format_currency(estimate, language=language)}"


def format_pipeline(pipeline: Optional[Dict], language: str) -> str:
    """Placed orders not counted yet, each with the wait the backend named (a held order reads
    "waiting for manager approval", C14) and its estimated commission."""
    head = [
        f"⏳ <b>{i18n.get('staff.sales.earnings.pipeline_title', language)}</b>",
        f"<i>{i18n.get('staff.sales.earnings.pipeline_note', language)}</i>",
        '',
    ]
    items = (pipeline or {}).get('items') or []
    if not items:
        return '\n'.join(head + [i18n.get('staff.sales.earnings.empty', language)])
    blocks = [_pipeline_line(item, language) for item in items[:LIST_CAP]]
    # The backend lists at most `PIPELINE_SIZE` orders and its `count` covers every waiting one,
    # so the orders it did not list are named in the "+N more" row too (T-PIPE-1).
    count = int((pipeline or {}).get('count') or 0)
    return _fit(head, blocks, language, unshown=max(count, len(items)) - len(blocks))


def _penalty_block(item: Dict, language: str) -> str:
    """The reason, never the evidence (§5.4). The type name is the agent's language, else
    English, exactly as the backend's `@translatable` rows name it."""
    names = item.get('type_names') or {}
    name = names.get(language) or names.get('en')
    first = ' · '.join(part for part in (
        format_local_date(item.get('incident_date')),
        escape_html(name),
        signed_deduction(item.get('amount'), language),
    ) if part)
    status = item.get('status')
    label = (i18n.get(f'staff.sales.earnings.penalty_status.{status}', language)
             if status in SALES_PAY_PENALTY_STATUSES else '')
    second = ' · '.join(part for part in (format_pay_month(item.get('posting_month')), label) if part)
    lines = [first, f"   {second}"]
    if item.get('reason'):
        lines.append(f"   {i18n.get('staff.sales.earnings.reason', language)}: {escape_html(item['reason'])}")
    return '\n'.join(lines)


def format_penalties(penalties: Optional[List[Dict]], language: str) -> str:
    head = [f"⚠️ <b>{i18n.get('staff.sales.earnings.penalties', language)}</b>", '']
    items = penalties or []
    if not items:
        return '\n'.join(head + [i18n.get('staff.sales.earnings.empty', language)])
    blocks = [_penalty_block(item, language) for item in items[:LIST_CAP]]
    return _fit(head, blocks, language, unshown=len(items) - len(blocks))


class SalesEarningsHandler(BaseHandler):
    """The agent's own pay. Callback-only: the profile hub and the two pay pushes draw the ways
    in, and every screen re-enters from a button.

    Not refused during an open visit, like My stats: it arms nothing and writes nothing. And
    no `RENDERED_KEY` short-circuit either, unlike My stats: re-tapping the summary is the
    refresh gesture (§7.2), so every tap re-reads S1.
    """

    async def _render(self, update: Update, text: str, keyboard, *, new_message: bool = False) -> None:
        """`stats.py`'s `_render`: acknowledge through `_safe_callback_answer`, then edit.
        `Message is not modified` is Telegram agreeing the screen already shows this (an
        unchanged refresh) and is swallowed; anything else is re-raised for the caller.

        `new_message` is a pay push's button (final-review M9): the push is the only place an
        approved month's breakdown or a confirmed penalty is spelled out, so the screen is SENT
        under it rather than written over it. Every button on that new screen edits in place.
        """
        query = update.callback_query
        await self._safe_callback_answer(query, None, show_alert=False)
        if new_message:
            await query.message.reply_text(text, reply_markup=keyboard, parse_mode='HTML')
            return
        try:
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
        except BadRequest as exc:
            if 'not modified' in str(exc).lower():
                return
            raise

    async def _earnings(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                        language: str) -> Optional[Dict]:
        """S1, or None when the caller has already been told why not."""
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return None
        async with api_client as client:
            response = await client.sales_my_earnings(token)
        if not response.success:
            await self._handle_api_response_error(update, response, language)
            return None
        return response.data or {}

    @require_auth
    @require_sales_agent
    async def show_summary(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """The profile hub's button, Refresh and every Back: the summary, edited in place."""
        await self._summary(update, context, new_message=False)

    @require_auth
    @require_sales_agent
    async def show_summary_from_push(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """The approval push's button before the Statement screen (`staff_sales_earn_n`, still
        in agents' chats): the summary as a new message."""
        await self._summary(update, context, new_message=True)

    async def _summary(self, update: Update, context: ContextTypes.DEFAULT_TYPE, *, new_message: bool) -> None:
        language = await self._get_language(update, context)
        try:
            data = await self._earnings(update, context, language)
            if data is None:
                return
            await self._render(update, format_earnings(data, language), SalesKeyboards.earnings(language, data),
                               new_message=new_message)
        except Exception as e:
            logger.error(f"Error showing sales earnings: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_sales_agent
    async def show_lines(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """A Credited orders or older-month button (the summary's or a Statement's), or a page
        switch.

        Parsed BEFORE the try, as in `stats.py`: a month or page this bot never drew is
        answered (the button stops spinning) and never reaches the backend.
        """
        query = update.callback_query
        language = await self._get_language(update, context)
        parsed = parse_lines_callback(query.data)
        if parsed is None:
            logger.warning("Unroutable sales earnings lines callback: %s", query.data)
            await self._safe_callback_answer(query, None, show_alert=False)
            return
        month, page = parsed
        try:
            token = await self._get_auth_token(update, context)
            if not token:
                await self._handle_auth_error(update, language)
                return
            async with api_client as client:
                response = await client.sales_my_earnings_lines(token, month=month, page=page)
            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return
            data = response.data or {}
            shown_page = data.get('page') or page
            await self._render(
                update,
                format_earnings_lines(data, language, month=month, page=page),
                SalesKeyboards.earnings_lines(language, month.replace('-', ''), shown_page,
                                              bool(data.get('has_more'))),
            )
        except Exception as e:
            logger.error(f"Error showing sales earnings lines: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_sales_agent
    async def show_statement(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """The summary's Statement button and a Past statements row: S4, edited in place."""
        await self._statement(update, context, STATEMENT_PREFIX, new_message=False)

    @require_auth
    @require_sales_agent
    async def show_statement_from_push(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """The approval push's button (`staff_sales_earn_sn_<yyyymm>`): that month's Statement
        as a new message under the push."""
        await self._statement(update, context, STATEMENT_PUSH_PREFIX, new_message=True)

    async def _statement(self, update: Update, context: ContextTypes.DEFAULT_TYPE, prefix: str, *,
                         new_message: bool) -> None:
        """S4 for the tapped month, parsed BEFORE the try as `show_lines` parses its own.

        S4 refuses a month that is not approved or paid (SALES_PAY_STATEMENT_NOT_AVAILABLE),
        which only a stale button can ask for. The refusal is told the way every refusal is,
        then the summary is drawn in the screen's place, re-read, so the agent lands on what is
        true now rather than on the button that failed.
        """
        query = update.callback_query
        language = await self._get_language(update, context)
        month = parse_statement_callback(query.data, prefix)
        if month is None:
            logger.warning("Unroutable sales statement callback: %s", query.data)
            await self._safe_callback_answer(query, None, show_alert=False)
            return
        try:
            token = await self._get_auth_token(update, context)
            if not token:
                await self._handle_auth_error(update, language)
                return
            async with api_client as client:
                response = await client.sales_my_statement(token, month)
            if not response.success:
                await self._handle_api_response_error(update, response, language)
                if getattr(response, 'error_code', None) == 'SALES_PAY_STATEMENT_NOT_AVAILABLE':
                    await self._summary(update, context, new_message=new_message)
                return
            data = response.data or {}
            await self._render(update, format_statement(data, language),
                               SalesKeyboards.earnings_statement(language, data.get('month') or month),
                               new_message=new_message)
        except Exception as e:
            logger.error(f"Error showing sales statement: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_sales_agent
    async def show_past_statements(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Past statements: S3, one button per approved or paid month, edited in place."""
        language = await self._get_language(update, context)
        try:
            token = await self._get_auth_token(update, context)
            if not token:
                await self._handle_auth_error(update, language)
                return
            async with api_client as client:
                response = await client.sales_my_statements(token)
            if not response.success:
                await self._handle_api_response_error(update, response, language)
                return
            items = (response.data or {}).get('items') or []
            await self._render(update, format_past_statements(items, language),
                               SalesKeyboards.earnings_statements(language, items))
        except Exception as e:
            logger.error(f"Error showing sales past statements: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_sales_agent
    async def show_pipeline(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Not counted yet: S1's `pipeline`, re-read (§7.2: there is no extra route)."""
        language = await self._get_language(update, context)
        try:
            data = await self._earnings(update, context, language)
            if data is None:
                return
            await self._render(update, format_pipeline(data.get('pipeline'), language),
                               SalesKeyboards.earnings_back(language))
        except Exception as e:
            logger.error(f"Error showing sales earnings pipeline: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_sales_agent
    async def show_penalties(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Penalties: S1's `penalties`, re-read, edited in place."""
        await self._penalties(update, context, new_message=False)

    @require_auth
    @require_sales_agent
    async def show_penalties_from_push(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """The penalty push's button (`staff_sales_earn_xn`): the penalties as a new message."""
        await self._penalties(update, context, new_message=True)

    async def _penalties(self, update: Update, context: ContextTypes.DEFAULT_TYPE, *, new_message: bool) -> None:
        language = await self._get_language(update, context)
        try:
            data = await self._earnings(update, context, language)
            if data is None:
                return
            await self._render(update, format_penalties(data.get('penalties'), language),
                               SalesKeyboards.earnings_back(language), new_message=new_message)
        except Exception as e:
            logger.error(f"Error showing sales earnings penalties: {e}", exc_info=True)
            await self._handle_error(update, context)
