"""My stats: the sales agent's own KPI card, one window at a time."""
import logging
from typing import Dict

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from staff_bot.api_client import api_client
from staff_bot.handlers.base import BaseHandler
from staff_bot.i18n import i18n
from staff_bot.keyboards.sales import STATS_PERIODS, SalesKeyboards
from staff_bot.permissions import require_auth, require_sales_agent
from staff_bot.utils.formatters import format_currency, format_local_date

logger = logging.getLogger(__name__)

# Where the card remembers what it last drew, and on WHICH message: a
# `(message_id, period)` pair in `user_data`. The pair, not the period alone,
# because an agent can scroll up and tap the switch on an OLDER card that is
# showing a different window -- a bare period would refuse to redraw that one.
RENDERED_KEY = 'sales_stats_rendered'


def _figure(metric_key: str, value, language: str) -> str:
    """One metric, rendered the way the backend meant it.

    NULL is an ANSWER here, not an omission: the service returns None for a
    ratio whose denominator was zero and for a mean with nothing to average,
    and the agent has to be able to tell "no visit had a measurable length"
    from "the average is 0.0". So the dash is printed and the ROW still
    prints -- unlike the outlet card, which drops a line the backend answered
    NULL for. That card is a description of one shop; this is a fixed report
    card, and a row that vanished would read as a metric taken away.

    The three shapes are the service's own: a `*_pct` key is a percentage to
    one decimal, `revenue_delivered_paid` is money and goes through the
    currency SSOT, and everything else is a count -- or, for the two the
    service answers as a rounded float, a one-decimal figure. Nothing is
    recomputed here; the rounding already happened server-side.
    """
    if value is None:
        return i18n.get('staff.sales.stats.na', language)
    if metric_key == 'revenue_delivered_paid':
        return format_currency(value, language=language)
    if metric_key.endswith('_pct'):
        return f"{float(value):.1f}%"
    if isinstance(value, float):
        return f"{value:.1f}"
    return f"{int(value):,}"


def format_agent_stats(stats: Dict, language: str) -> str:
    """The KPI card: the window it covers, then the twenty figures in four groups.

    Every label is a LITERAL key rather than one built from the metric name,
    so the extractor that feeds `/health` can see all twenty. A family built
    by f-string would be invisible to it, and an unseeded label would reach an
    agent as a humanised English key tail while the container stayed green --
    the leak class this bot keeps rediscovering.

    The header prints the dates the ANSWER carries. A bot that worked out
    where its own week started would disagree with the admin UI's table the
    first time an agent compared them.
    """
    metrics = stats.get('metrics') or {}
    period = stats.get('period') or STATS_PERIODS[0]

    def row(metric_key: str, label: str) -> str:
        return f"{label}: {_figure(metric_key, metrics.get(metric_key), language)}"

    lines = [
        f"📊 <b>{i18n.get('staff.sales.stats.title', language)}</b> · "
        f"{i18n.get(f'staff.sales.stats.period_{period}', language)}",
        f"{format_local_date(stats.get('start_date'))} — {format_local_date(stats.get('end_date'))}",
        "",
        f"<b>{i18n.get('staff.sales.stats.section_visits', language)}</b>",
        row('planned_visits', i18n.get('staff.sales.stats.metric_planned_visits', language)),
        row('completed_visits', i18n.get('staff.sales.stats.metric_completed_visits', language)),
        row('plan_vs_fact_pct', i18n.get('staff.sales.stats.metric_plan_vs_fact_pct', language)),
        row('unplanned_visits', i18n.get('staff.sales.stats.metric_unplanned_visits', language)),
        row('visits_per_day', i18n.get('staff.sales.stats.metric_visits_per_day', language)),
        row('strike_rate_pct', i18n.get('staff.sales.stats.metric_strike_rate_pct', language)),
        "",
        f"<b>{i18n.get('staff.sales.stats.section_outlets', language)}</b>",
        row('assigned_outlets', i18n.get('staff.sales.stats.metric_assigned_outlets', language)),
        row('active_outlets', i18n.get('staff.sales.stats.metric_active_outlets', language)),
        row('active_share_pct', i18n.get('staff.sales.stats.metric_active_share_pct', language)),
        row('new_outlets_registered',
            i18n.get('staff.sales.stats.metric_new_outlets_registered', language)),
        row('new_outlets_activated',
            i18n.get('staff.sales.stats.metric_new_outlets_activated', language)),
        "",
        f"<b>{i18n.get('staff.sales.stats.section_orders', language)}</b>",
        row('orders_placed', i18n.get('staff.sales.stats.metric_orders_placed', language)),
        row('orders_delivered_paid',
            i18n.get('staff.sales.stats.metric_orders_delivered_paid', language)),
        row('bottles_delivered_paid',
            i18n.get('staff.sales.stats.metric_bottles_delivered_paid', language)),
        row('revenue_delivered_paid',
            i18n.get('staff.sales.stats.metric_revenue_delivered_paid', language)),
        row('agent_orders_cancelled',
            i18n.get('staff.sales.stats.metric_agent_orders_cancelled', language)),
        row('suggested_vs_accepted_pct',
            i18n.get('staff.sales.stats.metric_suggested_vs_accepted_pct', language)),
        "",
        f"<b>{i18n.get('staff.sales.stats.section_discipline', language)}</b>",
        row('out_of_range_checkins',
            i18n.get('staff.sales.stats.metric_out_of_range_checkins', language)),
        row('skipped_checkins', i18n.get('staff.sales.stats.metric_skipped_checkins', language)),
        row('avg_visit_minutes', i18n.get('staff.sales.stats.metric_avg_visit_minutes', language)),
    ]
    return "\n".join(lines)


class SalesStatsHandler(BaseHandler):
    """The agent's own numbers. Callback-only: the profile hub draws the way
    in, and the three period buttons re-enter the same screen.

    The bot computes NOTHING. Every figure is a field the backend published
    for the window IT chose from `period`, which is why there is no arithmetic
    in this module and no second definition of a week anywhere near it.

    Not a conversation, so no flow key and no draft -- and deliberately not
    refused during an open visit: `_refused_by_open_visit` guards the entry
    points that would ARM a second flow on top of a live one, and this screen
    arms nothing. It is the outlet card's neighbour, not Nearby's.
    """

    async def _render(self, update: Update, text: str, keyboard) -> None:
        """Callback-only, exactly like the approvals queue: every screen here
        is reached by tapping a button, so a missing `callback_query` would be
        a wiring bug rather than a shape to fall back on. The acknowledgement
        goes through `_safe_callback_answer` so a tap that outlived a restart
        cannot raise BEFORE the edit, which is the part that matters.

        `Message is not modified` is Telegram AGREEING that the screen already
        shows this, so it is swallowed here rather than raised (the route card
        does the same). `change_period`'s state check catches the common case
        without spending a request, but it cannot be the only defence: the
        memory it reads lives in `context.user_data` and this bot runs no PTB
        persistence, so the dict dies with the process -- the first re-tap
        after every restart, and any tap on a second card already showing the
        same window, arrives here with byte-identical content. Anything else
        Telegram rejects is a real fault and is re-raised for the caller's
        `except` to log and report.
        """
        query = update.callback_query
        await self._safe_callback_answer(query, None, show_alert=False)
        try:
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode='HTML')
        except BadRequest as exc:
            if 'not modified' in str(exc).lower():
                return
            raise

    async def _show_card(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                         period: str, language: str) -> None:
        token = await self._get_auth_token(update, context)
        if not token:
            await self._handle_auth_error(update, language)
            return
        async with api_client as client:
            response = await client.sales_agent_stats(token, period=period)
        if not response.success:
            await self._handle_api_response_error(update, response, language)
            return
        stats = response.data or {}
        # The window the ANSWER named, never the one that was asked for: the tick
        # on the keyboard and the remembered pair both have to describe what is
        # actually on screen.
        rendered = stats.get('period') or period
        await self._render(update, format_agent_stats(stats, language),
                           SalesKeyboards.stats(language, rendered))
        query = update.callback_query
        message = query.message if query else None
        if message is not None and context.user_data is not None:
            context.user_data[RENDERED_KEY] = (message.message_id, rendered)

    @require_auth
    @require_sales_agent
    async def show_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """The profile hub's button: opens on the default window."""
        language = await self._get_language(update, context)
        try:
            await self._show_card(update, context, STATS_PERIODS[0], language)
        except Exception as e:
            logger.error(f"Error showing sales stats: {e}", exc_info=True)
            await self._handle_error(update, context)

    @require_auth
    @require_sales_agent
    async def change_period(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        r"""One of the three period buttons.

        The registered pattern is `^staff_sales_stats_\w+$` -- it cannot be
        the three-way alternation the keyboard draws, because the collision
        check cannot sample an alternation and would stop checking this button
        for theft altogether. So the VALUE is re-checked here, the way
        `SalesHubHandler.show_list` re-checks its scope: a window this screen
        never offered is ANSWERED (the button stops spinning) and never
        reaches the backend, which would refuse it and leave the agent reading
        a generic 400 about a value they could not have typed.

        Parsed BEFORE the try below on purpose: an exception raised while
        parsing happens outside it and leaves the tap spinning. Both branches
        below answer through `_safe_callback_answer`, which never raises, so
        neither can strand a spinning button outside the try.

        Re-tapping the TICKED button is a first-class gesture -- `SalesKeyboards.stats`
        deliberately leaves the current window's button on screen -- and the card
        carries no clock and no nonce, so redrawing it produces byte-identical
        text and keyboard. Telegram answers that with `400 Bad Request: Message
        is not modified`, which the `except` below turned into a stack trace and
        a failure alert for a tap that did nothing wrong. So the same window on
        the same message is ACKNOWLEDGED and nothing else: no request, no edit.
        The trade is that the ticked button no longer re-fetches the figures --
        it never refreshed them in practice either, because the identical redraw
        was rejected. A different window, or the same window on a different
        message, still falls through and redraws.
        """
        query = update.callback_query
        language = await self._get_language(update, context)
        # The SUFFIX after the fixed prefix, never the last underscore segment:
        # a period whose name ever contains an underscore would otherwise parse
        # to its own tail and route nowhere (`new_outlet.py`'s `_skipped_step`).
        period = (query.data or '').split('staff_sales_stats_', 1)[-1]
        if period not in STATS_PERIODS:
            logger.warning("Unroutable sales stats callback: %s", query.data)
            await self._safe_callback_answer(query, None, show_alert=False)
            return
        message = query.message
        rendered = (context.user_data or {}).get(RENDERED_KEY)
        if message is not None and rendered == (message.message_id, period):
            await self._safe_callback_answer(query, None, show_alert=False)
            return
        try:
            await self._show_card(update, context, period, language)
        except Exception as e:
            logger.error(f"Error changing sales stats period: {e}", exc_info=True)
            await self._handle_error(update, context)
