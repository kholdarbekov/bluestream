"""SSOT builder for driver-facing order offers (route-UX plan 2026-08-11,
Phase 3, Task 10).

Two shapes, one decision point:
  * plain pool-insertion offer — "#N fits your route, saves ~M min. Accept?"
  * diversion offer (spec §7)  — "#N is close. Go here first instead of #M?"

Before this module existed, the offer's text + keyboard were constructed
independently in THREE places: the live webhook path
(`webhook_server.pool_insertion_suggestion_handler`), the deferred-drain
path (`flow_state.clear_and_drain`), and the diversion shape Plan 1 added on
top of the first. All three now route through `build_offer` here, so the
copy and the callback data cannot drift apart (CLAUDE.md: never leave two
places deciding the same thing).

This module never re-derives a routing decision — `gain_minutes` and
`committed_order_number` are read exactly as the backend published them
(`RouteOptimizationService.compute_diversion_gain` /
`business_app.tasks.delivery_tasks`). In particular `gain_minutes` is
already signed "positive = minutes saved by going there first"; a past bug
in this exact copy inverted that sign and showed the driver "+12 min" for an
offer that saved 12 minutes. Nothing here negates it.
"""
from typing import Any, Dict, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from staff_bot.i18n import i18n


def is_diversion_offer(payload: Dict[str, Any]) -> bool:
    """A diversion needs BOTH a measured gain and the order it displaces.

    Exposed (not `_`-prefixed): callers that decide whether a LIVE send may
    ping (`disable_notification`) need this exact same predicate — reused,
    not re-implemented, so "is this a diversion" is decided in one place
    for both the copy and the notification policy.
    """
    gain = payload.get("gain_minutes")
    return gain is not None and bool(payload.get("committed_order_number"))


def build_offer(payload: Dict[str, Any], language: str) -> Tuple[str, InlineKeyboardMarkup]:
    """Render the offer's text + Accept/Decline keyboard from a backend-
    published payload. Read defensively (`.get` with defaults throughout)
    so an older business_app that hasn't shipped `gain_minutes` /
    `committed_order_number` yet still renders the plain shape correctly
    (deploy-skew tolerance)."""
    delivery_id = payload.get("delivery_id")
    order_no = payload.get("order_no", "")

    if is_diversion_offer(payload):
        gain_minutes = float(payload.get("gain_minutes") or 0)
        text = i18n.get(
            "staff.route.diversion_offer",
            language,
            order_no=order_no,
            committed_no=payload.get("committed_order_number", ""),
            minutes=int(round(gain_minutes)),
        )
        accept_label = i18n.get("staff.route.go_here_first", language)
        decline_label = i18n.get("staff.route.keep_current", language)
    else:
        text = i18n.get(
            "staff.delivery.pool_insertion_offer",
            language,
            order_no=order_no,
            minutes=int(round(float(payload.get("detour_minutes") or 0))),
        )
        accept_label = i18n.get("staff.delivery.accept", language)
        decline_label = i18n.get("staff.delivery.suggestion_declined_button", language)

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ {accept_label}", callback_data=f"staff_confirm_accept_{delivery_id}"),
        InlineKeyboardButton(f"➡️ {decline_label}", callback_data=f"staff_decline_suggestion_{delivery_id}"),
    ]])
    return text, keyboard
