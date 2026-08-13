"""Route card for the staff bot (route-UX plan 2026-08-11, Phase 3).

ONE long-lived message per driver per shift, edited in place. This module
holds the pure view builders (this task) and the render/update mechanics
(Task 5). It reads ONLY backend-published fields — `route_summary`,
`is_next`, `route_position`, the webhook's materiality booleans — and never
re-derives a routing decision (CLAUDE.md SSOT).
"""
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut

from shared.constants import DISPLAY_TIMEZONE
from staff_bot.api_client import api_client
from staff_bot.i18n import i18n
from staff_bot.utils import route_card_state
from staff_bot.utils.formatters import (
    escape_html,
    format_active_delivery_summary,
    format_local_time,
)
from staff_bot.utils.render_signature import compute_render_signature

logger = logging.getLogger(__name__)

_KEYCAPS = {1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣", 5: "5️⃣",
            6: "6️⃣", 7: "7️⃣", 8: "8️⃣", 9: "9️⃣", 10: "🔟"}
NAV_URL_MAX_STOPS = 5


def _stop_number_label(n: int) -> str:
    return _KEYCAPS.get(n, str(n))


def _delivery_id(item: Dict[str, Any]) -> Optional[int]:
    return item.get("delivery_id") or item.get("id")


def _header_line(payload: Dict[str, Any], language: str) -> str:
    """`🚚 Stop N of M · finish ~HH:MM · updated HH:MM` — every number comes
    from the backend's route_summary; a missing summary (older backend)
    degrades to the plain active-count line instead of crashing."""
    summary = payload.get("route_summary") or {}
    items = payload.get("items") or []
    parts = []
    total = summary.get("stops_total_today")
    done = summary.get("stops_completed_today")
    if total and done is not None:
        parts.append(
            f"<b>{i18n.get('staff.route.card_header', language, current=int(done) + 1, total=int(total))}</b>"
        )
    else:
        parts.append(
            f"<b>{i18n.get('staff.delivery.active_count', language, count=len(items))}</b>"
        )
    finish_eta = summary.get("finish_eta")
    if finish_eta:
        try:
            finish_local = format_local_time(datetime.fromisoformat(finish_eta))
            parts.append(i18n.get('staff.route.finish_by', language, time=finish_local))
        except (TypeError, ValueError):
            pass
    parts.append(i18n.get('staff.route.updated_at', language, time=format_local_time()))
    return "🚚 " + " · ".join(parts)


def build_multi_stop_nav_url(items: List[Dict[str, Any]], limit: int = NAV_URL_MAX_STOPS) -> Optional[str]:
    """Multi-stop Yandex Maps route from the driver's current position.

    Leading `~` = "from my location". Inline buttons accept only
    http/https/tg:// — a yandexnavi:// deep link is NOT allowed here."""
    coords = []
    for item in items:
        lat = item.get("destination_latitude")
        lng = item.get("destination_longitude")
        if lat is None or lng is None:
            continue
        coords.append(f"{lat},{lng}")
        if len(coords) >= limit:
            break
    if not coords:
        return None
    return "https://yandex.ru/maps/?rtext=~" + "~".join(coords) + "&rtt=auto"


def build_next_view(payload: Dict[str, Any], language: str) -> Tuple[str, InlineKeyboardMarkup]:
    items = payload.get("items") or []
    summary = payload.get("route_summary") or {}
    head = items[0]
    head_id = _delivery_id(head)
    is_committed = (
        summary.get("committed_delivery_id") is not None
        and summary.get("committed_delivery_id") == head_id
    )

    lines = [_header_line(payload, language), ""]
    if is_committed:
        lines.append(f"▶️ <b>{i18n.get('staff.route.current_stop', language)}</b>")
    else:
        lines.append(f"📍 <b>{i18n.get('staff.route.suggested_next', language)}</b>")

    # ETA line gated purely on the backend's own `eta_suppressed` flag
    # (Plan 2 SSOT) — never on `location_status`. The backend already knows
    # whether the driver's position was fresh enough to trust when it
    # computed (or withheld) these numbers; re-deriving that here from
    # `location_status` would be a second place deciding the same question,
    # and the two can legitimately disagree (e.g. a still-valid cached ETA
    # from a moment ago vs. a location read that just went stale). Read
    # defensively (`.get`) so an older backend that never publishes
    # `eta_suppressed` degrades to "not suppressed" rather than crashing.
    if not head.get("eta_suppressed"):
        eta = head.get("eta_minutes_from_current_location")
        km = head.get("distance_km_to_next")
        eta_parts = []
        if eta is not None:
            eta_parts.append(f"⏱ {i18n.get('staff.delivery.eta_minutes', language, minutes=int(eta))}")
        if km is not None:
            eta_parts.append(f"📏 {i18n.get('staff.delivery.distance_km', language, km=km)}")
        if eta_parts:
            lines.append(" · ".join(eta_parts))

    lines.append(format_active_delivery_summary(head, language, include_money=True))
    text = "\n".join(lines)

    if is_committed:
        primary = InlineKeyboardButton(
            f"📋 {i18n.get('staff.route.open_stop', language)}",
            callback_data=f"staff_view_active_{head_id}",
        )
    else:
        primary = InlineKeyboardButton(
            f"▶️ {i18n.get('staff.route.start_this_stop', language)}",
            callback_data=f"staff_view_active_{head_id}",
        )
    row1 = [primary]
    nav_url = build_multi_stop_nav_url(items)
    if nav_url:
        row1.append(InlineKeyboardButton(
            f"🗺 {i18n.get('staff.route.navigate_all', language)}", url=nav_url
        ))
    row2 = [
        InlineKeyboardButton(
            f"📋 {i18n.get('staff.route.all_stops_button', language, count=len(items))}",
            callback_data="staff_route_view_all",
        ),
        InlineKeyboardButton(
            f"📍 {i18n.get('staff.delivery.share_location_button', language)}",
            callback_data="staff_share_location_prompt",
        ),
    ]
    return text, InlineKeyboardMarkup([row1, row2])


def build_all_view(payload: Dict[str, Any], language: str) -> Tuple[str, InlineKeyboardMarkup]:
    items = payload.get("items") or []
    summary = payload.get("route_summary") or {}
    committed_id = summary.get("committed_delivery_id")

    lines = [
        f"🚚 <b>{i18n.get('staff.route.all_stops_header', language, count=len(items))}</b>"
        f" · {i18n.get('staff.route.updated_at', language, time=format_local_time())}",
        "",
    ]
    for pos, item in enumerate(items, start=1):
        marker = "▶️ " if _delivery_id(item) == committed_id and committed_id is not None else "  "
        number = escape_html(item.get("order_number") or "")
        place = escape_html(item.get("district") or item.get("address") or "")
        lines.append(f"{marker}{pos}. #{number}  {place}")
    text = "\n".join(lines)

    keyboard: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for pos, item in enumerate(items, start=1):
        row.append(InlineKeyboardButton(
            _stop_number_label(pos), callback_data=f"staff_view_active_{_delivery_id(item)}"
        ))
        if len(row) == 5:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton(
        f"⬅️ {i18n.get('staff.back', language)}", callback_data="staff_route_view_next"
    )])
    return text, InlineKeyboardMarkup(keyboard)


def build_empty_view(payload: Dict[str, Any], language: str) -> Tuple[str, InlineKeyboardMarkup]:
    text = (
        f"🚚 {i18n.get('staff.route.all_done', language)}\n"
        f"{i18n.get('staff.route.updated_at', language, time=format_local_time())}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"📍 {i18n.get('staff.delivery.share_location_button', language)}",
            callback_data="staff_share_location_prompt",
        )],
        [InlineKeyboardButton(
            f"⬅️ {i18n.get('staff.back', language)}", callback_data="staff_back_to_main"
        )],
    ])
    return text, keyboard


# --- card mechanics (Task 5) -------------------------------------------------

# "More than ~5 messages since posting" (spec §6.3). Private-chat message_ids
# are per-chat monotonic, so (tap_message_id - card_message_id) approximates
# how many messages landed since the card was posted. This heuristic is a
# guess, not derived from anything Telegram publishes.
def _repost_gap_messages() -> int:
    """Parse ROUTE_CARD_REPOST_GAP_MESSAGES defensively -- a malformed env
    value must degrade to the default, not take the whole bot down at
    import time (fix round 1, M5)."""
    raw = os.environ.get("ROUTE_CARD_REPOST_GAP_MESSAGES", "6")
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid ROUTE_CARD_REPOST_GAP_MESSAGES=%r; using default 6", raw)
        return 6


ROUTE_CARD_REPOST_GAP_MESSAGES = _repost_gap_messages()


def local_date_str() -> str:
    """Today's date in the display timezone -- the card's SHIFT scope."""
    return datetime.now(ZoneInfo(DISPLAY_TIMEZONE)).strftime("%Y-%m-%d")


def is_current_borrow(state: Optional[dict], today: str) -> bool:
    """Is this card borrowed by a detail view *for the current shift*?

    SSOT for a predicate two paths need: `update_card_for_driver`'s cheap
    pre-check (which skips a token fetch and an HTTP round trip on the
    common path) and `render_route_card`'s authoritative in-lock recheck.
    Written out twice it drifted silently -- each site had a test, neither
    asserted the two agreed, so an edit to one left the suite green.

    The `card_date` half is not decoration: without it a borrow that
    survives midnight blocks the shift rollover and the driver keeps
    yesterday's card all day (final-review I3). A borrow only wins while
    it belongs to today.
    """
    return bool(
        state
        and state.get("view") == route_card_state.VIEW_BORROWED
        and state.get("card_date") == today
    )


async def _get_user_language(telegram_id: int) -> str:
    """Seam for tests; the webhook path has no context.user_data."""
    return await i18n.get_user_language(int(telegram_id))


def _build(payload: Dict[str, Any], language: str, view: str) -> Tuple[str, InlineKeyboardMarkup]:
    if not (payload.get("items") or []):
        return build_empty_view(payload, language)
    if view == route_card_state.VIEW_ALL:
        return build_all_view(payload, language)
    return build_next_view(payload, language)


async def _create_card(bot, chat_id, text, keyboard, old_state, pin=True):
    """Send a fresh card (silent), pin it (silent), delete the old one.

    Delete-then-send-then-pin is 1 send + 1 best-effort pin, matching the
    ~1 msg/sec per-chat budget; the delete is fire-and-forget cleanup of a
    message Telegram no longer needs to know about, not a rate-limited op
    in the same sense.

    `bot.send_message` is deliberately NOT wrapped here -- the caller
    (`render_route_card`) wraps the whole call so a send failure (e.g. the
    driver blocked the bot) is handled in exactly one place rather than
    swallowed at two layers (fix round 1, I4).

    `pin=False` (review round 1, I2): when `route_card_state` has no Redis
    to persist to, we cannot durably remember "this driver already has a
    pinned card" across a bot restart -- a restart mid-outage would pin yet
    another one, on top of whichever earlier ones were never unpinned. A
    driver's chat accumulating stray pins is worse than one unpinned
    message sitting in history, so the caller skips pinning entirely for as
    long as `route_card_state.is_enabled()` is False.
    """
    if old_state and old_state.get("message_id"):
        try:
            await bot.delete_message(
                chat_id=old_state.get("chat_id", chat_id),
                message_id=old_state["message_id"],
            )
        except Exception as exc:  # noqa: BLE001 -- deleted/too old is expected
            logger.debug("route card old-message delete skipped: %s", exc)
    sent = await bot.send_message(
        chat_id=chat_id, text=text, reply_markup=keyboard,
        parse_mode="HTML", disable_notification=True,
    )
    if pin:
        try:
            await bot.pin_chat_message(
                chat_id=chat_id, message_id=sent.message_id, disable_notification=True
            )
        except Exception as exc:  # noqa: BLE001 -- pinning is best-effort; a
            # failed pin (missing admin rights) must never abort card creation.
            logger.debug("route card pin skipped: %s", exc)
    return sent


async def render_route_card(
    bot,
    *,
    telegram_id: int,
    chat_id: int,
    language: str,
    payload: Dict[str, Any],
    view: Optional[str] = None,
    reference_message_id: Optional[int] = None,
    respect_borrowed: bool = False,
    session_hint: Optional[Dict[str, Any]] = None,
) -> bool:
    """THE single read-modify-write path for the route card. Both the PTB
    handlers (driver taps a button) and the aiohttp webhook server (backend
    pushes an update) funnel through here in the same process, so the whole
    body is serialized per driver by `route_card_state.get_lock` -- this IS
    the render-lock/concurrency answer, the successor of the old per-user
    `context.user_data` render lock (active_delivery.py:32-39).

    session_hint (review round 1, I2): an OPTIONAL, caller-owned mutable
    dict -- in practice `context.user_data`-scoped, so it lives only as
    long as this bot process does. It is a last-resort stand-in for Redis
    state, consulted ONLY when `route_card_state.load` returns None, and
    kept warm (mutated in place, same shape as real state) on every
    success regardless of whether Redis was actually used this call. Redis
    remains the sole source of truth per Task 3's decision -- this dict is
    never read when real state exists and never itself persisted. Its only
    job is to stop a Redis outage from turning "one card per driver" into
    "one new pinned card per tap": without it, `state` is unconditionally
    None on every call while Redis is down, so every tap would hit the
    create branch. With it, the FIRST call in a fresh process still
    creates (unpinned -- see `_create_card`), but every call after that
    edits the same message in place, same as healthy Redis would. The
    bound this leaves is "one send per bot-process restart during an
    outage", not "one send per tap" -- restarts lose the hint like they
    lose everything else in `user_data`, which is an accepted, documented
    trade-off, not a bug.

    respect_borrowed: when True (the webhook path), a card currently
    BORROWED (showing a stop detail / at-door flow) is left untouched --
    editing it would yank the driver's screen out from under them. This is
    re-tested HERE, on state freshly loaded *inside* the per-driver lock, so
    a driver's tap racing a webhook push always wins the race (fix round 1,
    I1: a borrowed check made before acquiring the lock is a TOCTOU race --
    a caller's own pre-check, if any, is only a best-effort short-circuit,
    never the authority). The driver's own tap always passes
    respect_borrowed=False -- a driver is always allowed to update their
    own screen, and doing so naturally clears any stale borrowed flag
    because this function always writes a real view on success.

    The borrowed short-circuit ONLY holds for the SAME shift the borrow was
    set in (FINAL review, I3): it additionally requires
    `state["card_date"] == today`, so a card borrowed at the end of one
    shift cannot block the rollover-create on a later day -- the rollover
    branch below always wins once the date has moved on. This matters now
    that C1 (this same review) makes `mark_borrowed`'s write actually
    survive a concurrent render instead of being clobbered; without this
    date guard a reliable borrow would strand the card indefinitely instead
    of just racily.

    Returns True iff the card now shows the requested render (including a
    true no-op -- it already did). Returns False when the render did not
    happen: found borrowed, or a Telegram-side failure this function
    swallows rather than raising (fix round 1, I2/I4) -- the driver blocked
    the bot, a transient network error, an unrecognized edit failure, etc.
    Never raises for a Telegram-side failure; a bug in our own code (e.g. a
    TypeError building the text) still propagates.

    At most ONE send or ONE edit per call, except the one documented
    recovery: an edit that fails because the message is genuinely gone
    falls through to a fresh create (1 edit + 1 send) -- the brief's own
    prescribed recovery. `disable_notification` is always True on every
    send here: creation and repost are the only sends this function issues,
    and neither should ping the driver -- editMessageText (the "otherwise"
    branch) never notifies at all, which is the entire point of a single
    long-lived card.
    """
    async with route_card_state.get_lock(telegram_id):
        state = await route_card_state.load(telegram_id)

        if state is None and session_hint and session_hint.get("message_id"):
            # Redis has nothing for this driver right now -- never
            # configured, still erroring, or genuinely this driver's first
            # render ever. Borrow the caller's session-scoped hint as a
            # stand-in; see the session_hint paragraph above.
            state = dict(session_hint)

        def _keep_hint_warm(s: Dict[str, Any]) -> None:
            if session_hint is not None:
                session_hint.clear()
                session_hint.update(s)

        today = local_date_str()

        # FINAL review, I3: a borrow only blocks a render for the SAME
        # shift it was set in. Computed `today` up front (it used to be
        # computed further down, after this check) so the borrowed
        # short-circuit can also see `card_date`. Without the
        # `card_date == today` guard, a card borrowed at end of shift (any
        # at-door flow the driver walked away from without tapping ⬅️ Back)
        # would short-circuit here forever -- every webhook push the
        # following day returns False right here, before ever reaching the
        # rollover branch below, and the pinned card is stuck showing
        # yesterday's cash prompt until the 48h TTL evicts it. Letting
        # rollover win is correct regardless: a card whose `card_date` is
        # not today is by definition not showing anything the driver is
        # CURRENTLY mid-flow in, so there is nothing left to protect.
        # This only bites now that C1 (the same review) makes a borrow
        # actually survive a concurrent render instead of being clobbered
        # -- fixing C1 without this turns the race into a stuck card.
        if respect_borrowed and is_current_borrow(state, today):
            return False

        if view is None:
            stored = (state or {}).get("view")
            view = stored if stored in (route_card_state.VIEW_NEXT, route_card_state.VIEW_ALL) \
                else route_card_state.VIEW_NEXT

        text, keyboard = _build(payload, language, view)
        sig = compute_render_signature(text, keyboard)

        # 1. No card for today: no state, no message_id, or a stale shift.
        need_create = (
            state is None
            or not state.get("message_id")
            or state.get("card_date") != today
        )
        # 2. Card exists but is buried under other chat traffic. Only a tap
        # carries a reference_message_id -- webhook edits pass None and can
        # never repost (an edit adds no message, so it cannot bury anything,
        # and this keeps the webhook path inside the per-chat send budget).
        need_repost = (
            not need_create
            and reference_message_id is not None
            and (reference_message_id - int(state["message_id"])) > ROUTE_CARD_REPOST_GAP_MESSAGES
        )

        if not need_create and not need_repost:
            # 3. Otherwise: edit in place. Skip entirely on a true no-op --
            # Telegram rejects an identical edit_message_text outright.
            if state.get("content_sig") == sig and state.get("view") == view:
                _keep_hint_warm(state)
                return True
            try:
                await bot.edit_message_text(
                    chat_id=state.get("chat_id", chat_id),
                    message_id=state["message_id"],
                    text=text, reply_markup=keyboard, parse_mode="HTML",
                )
                state.update({"view": view, "content_sig": sig})
                await route_card_state.save(telegram_id, state)
                _keep_hint_warm(state)
                return True
            except BadRequest as exc:
                reason = str(exc).lower()
                if "not modified" in reason:
                    # Telegram already shows this exact content -- not a
                    # failure (e.g. a prior `save` failed and left a stale
                    # content_sig, so this edit was a genuine no-op).
                    # Persist the signature so we stop retrying and return.
                    state.update({"view": view, "content_sig": sig})
                    await route_card_state.save(telegram_id, state)
                    _keep_hint_warm(state)
                    return True
                message_gone = (
                    "message to edit not found" in reason
                    or "message can't be edited" in reason
                    or "message_id_invalid" in reason
                    or "message identifier is not specified" in reason
                )
                if not message_gone:
                    # Unrecognized BadRequest -- do NOT delete a working
                    # pinned card over an error we don't understand. Leave
                    # it alone this round; the next render tries again.
                    logger.warning(
                        "route card edit failed with unrecognized BadRequest for %s: %s",
                        telegram_id, exc,
                    )
                    return False
                logger.debug("route card message gone (%s); sending fresh", exc)
                # fall through to _create_card below -- this IS the case
                # the delete+resend fallback exists for.
            except RetryAfter as exc:
                # Flood control -- reposting now would make it worse.
                logger.debug("route card edit rate-limited for %s: %s", telegram_id, exc)
                return False
            except (NetworkError, TimedOut) as exc:
                # Transient -- leave the card alone this round rather than
                # deleting a perfectly good pinned message over a blip.
                logger.debug("route card edit hit a transient error for %s: %s", telegram_id, exc)
                return False
            except Forbidden as exc:
                # Driver blocked the bot / kicked it -- a repost can't
                # succeed either. Nothing to do but wait for them to come
                # back; the next render attempt will hit the same wall.
                logger.info("route card edit forbidden for %s (blocked?): %s", telegram_id, exc)
                return False
            except TelegramError as exc:  # noqa: BLE001 -- catch-all for any
                # other Telegram-side failure not enumerated above; never
                # let it turn into a delete+resend storm.
                logger.warning("route card edit failed unexpectedly for %s: %s", telegram_id, exc)
                return False

        try:
            sent = await _create_card(
                bot, chat_id, text, keyboard, state,
                pin=route_card_state.is_enabled(),
            )
        except TelegramError as exc:
            # Driver blocked the bot, chat gone, rate-limited, etc. Don't
            # save state -- the next event retries a fresh create instead
            # of editing against a message that was never sent.
            logger.warning("route card create failed for %s: %s", telegram_id, exc)
            return False

        new_state = {
            "chat_id": sent.chat_id,
            "message_id": sent.message_id,
            "card_date": today,
            "view": view,
            "content_sig": sig,
            # Alert throttle survives reposts/rollover (Task 9).
            "last_alert_at": (state or {}).get("last_alert_at"),
            "last_alert_message_id": (state or {}).get("last_alert_message_id"),
        }
        await route_card_state.save(telegram_id, new_state)
        _keep_hint_warm(new_state)
        return True


async def update_card_for_driver(
    bot_app,
    telegram_id: int,
    *,
    language: Optional[str] = None,
    reference_message_id: Optional[int] = None,
) -> bool:
    """Webhook-side silent card refresh. Returns False when skipped, or when
    the render did not happen (see `render_route_card`'s return contract).

    Skips before any network work when there is no token manager wired up,
    no valid token (logged-out driver), the active-deliveries API call
    fails, or -- as a CHEAP pre-check only -- the card looked borrowed at
    load time. That pre-check is a best-effort short-circuit to avoid a
    wasted backend round-trip for the common case (driver mid-interaction);
    it is NOT the authoritative borrowed check.
    `render_route_card(respect_borrowed=True)` re-tests the flag on state
    loaded inside its own per-driver lock, which is what actually prevents
    a driver's tap from racing this call (fix round 1, I1) -- a tap that
    borrows the card between this pre-check and the lock acquisition still
    wins.

    FINAL review, I3: this pre-check now also requires `card_date == today`
    before treating the card as borrowed, mirroring
    `render_route_card`'s own (now date-aware) guard. Without this, fixing
    ONLY `render_route_card` would have been cosmetic for the real bug: this
    is the ONE function every production webhook push actually calls, and
    it used to return False here -- on `view` alone -- before ever reaching
    `render_route_card`. A card borrowed at the end of one shift would still
    have silently blocked every following day's webhook pushes forever
    (until a driver's own tap, which alone reaches `render_route_card`
    directly with `respect_borrowed=False`), even after the deeper fix.
    Two places were deciding the same "is this borrow still current"
    question with two different answers -- this makes the cheap check a
    true (if approximate) mirror of the authoritative one instead of a
    stricter, silently-conflicting duplicate.

    Reading the token manager off `bot_app.bot_data` (rather than importing
    a global) keeps this callable from both `Application.bot_data` and the
    lightweight test double used here.

    Creates the card on first contact -- that is what makes the card
    'appear immediately with the accepted stops' (spec §9a): accept ->
    optimize(trigger=accept) -> silent webhook -> this function.

    reference_message_id (Task 8 fix round 1, M2): pure passthrough to
    `render_route_card`'s repost-when-buried heuristic. Defaults to None,
    so every existing caller is unaffected -- every webhook caller,
    including Task 9's `send_head_change_alert`, passes None here (Task 9
    fix round 2, item 2: threading the alert's OWN message id through was
    tried and reverted -- Telegram message ids are monotonic, so a freshly
    sent alert always has a higher id than an existing card, which
    re-enabled a repost on every buried card on top of the alert send: 2
    sends + delete + pin per event instead of 1. The alert's own "Open
    route card" button is what handles a buried card on this path, not a
    repost). Kept as a real parameter (not deleted) because the driver-tap
    path (`active_delivery.py`'s `show_active_deliveries` -> `render_route_card`
    directly, not through this function) still has a genuine reference
    message and legitimately uses the repost heuristic.
    """
    state = await route_card_state.load(telegram_id)
    if is_current_borrow(state, local_date_str()):
        return False
    token_manager = (getattr(bot_app, "bot_data", None) or {}).get("token_manager")
    if token_manager is None:
        return False
    token = await token_manager.get_valid_token(int(telegram_id), api_client)
    if not token:
        return False
    async with api_client as client:
        response = await client.get_active_deliveries(token)
    if not getattr(response, "success", False):
        logger.warning("route card webhook refresh: API failed for %s", telegram_id)
        return False
    payload = response.data if isinstance(response.data, dict) else {"items": response.data or []}
    if language is None:
        language = await _get_user_language(telegram_id)
    return await render_route_card(
        bot_app.bot, telegram_id=int(telegram_id), chat_id=int(telegram_id),  # private chat: chat_id == user id
        language=language, payload=payload,
        view=None, reference_message_id=reference_message_id,
        respect_borrowed=True,
    )


# --- head-change alert (Task 9) ----------------------------------------------

def _alert_min_interval_seconds() -> int:
    """Parse ROUTE_ALERT_MIN_INTERVAL_SECONDS defensively -- same posture as
    `_repost_gap_messages`: a malformed env value degrades to the documented
    default instead of taking the whole bot down at import time."""
    raw = os.environ.get("ROUTE_ALERT_MIN_INTERVAL_SECONDS", "300")
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid ROUTE_ALERT_MIN_INTERVAL_SECONDS=%r; using default 300", raw)
        return 300


ROUTE_ALERT_MIN_INTERVAL_SECONDS = _alert_min_interval_seconds()


def _alert_is_capped(state: Optional[Dict[str, Any]]) -> bool:
    """True iff the LAST alert is still inside the throttle window, i.e. a
    new alert must be delivered silently and supersede it.

    `age < ROUTE_ALERT_MIN_INTERVAL_SECONDS` (strictly less than) is the
    boundary: an age exactly equal to the interval means the window has
    fully elapsed, so that case pings again, not stays silent. A malformed
    or missing `last_alert_at` (no prior alert, or state predating this
    field) degrades to "not capped" -- silence-by-default would mean a
    genuinely new head-change alert never pings on a driver's very first
    one, which defeats the point of a SOUNDED alert.
    """
    raw = (state or {}).get("last_alert_at")
    if not raw:
        return False
    try:
        last = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - last).total_seconds()
    return age < ROUTE_ALERT_MIN_INTERVAL_SECONDS


async def send_head_change_alert(bot_app, *, telegram_id: int) -> None:
    """The ONLY sounded message this plan sends -- Plan 1's backend gate
    (`sound=True`) has already decided the driver's *next* stop changed
    while they are driving and this is worth interrupting them for. Every
    other update flows through Task 8's silent `update_card_for_driver`
    (editMessageText, no notification, ever).

    Capped by ROUTE_ALERT_MIN_INTERVAL_SECONDS (default 300s, env-overridable):
    outside the window this pings (`disable_notification=False`); inside it,
    a new alert is still sent (so the driver's chat always shows a CURRENT
    pointer to what changed) but silently, and the previous alert message is
    deleted first so the chat never accumulates a stack of superseded
    "next stop changed" notices. The delete is best-effort -- Telegram's
    `deleteMessage` only works within 48h, and a message the driver already
    dismissed is simply gone -- so any failure there is swallowed and must
    never block the new alert from going out.

    The card itself is refreshed on every call via `update_card_for_driver`
    (Task 8) -- never re-implemented here (CLAUDE.md SSOT: one function
    owns "fetch active deliveries, respect borrowed, render"). A borrowed
    card (driver mid-detail-view) is protected by THAT function's own
    guarantee, not by anything here -- this alert is a distinct message, so
    it still fires regardless; only the card *edit* is skipped when
    borrowed. `reference_message_id` is always passed as `None` (fix round
    2, item 2) -- see that fix note below for why.

    `bot_app` (fix round 1, Critical/Important): takes the Application
    wrapper, matching `update_card_for_driver`'s own first parameter, NOT a
    raw Bot. An earlier revision took `bot` and passed it straight through
    to `update_card_for_driver` in the `bot_app` slot -- that function reads
    `bot_app.bot_data` for the token manager and `bot_app.bot` for its own
    Telegram calls, neither of which a raw `telegram.Bot` has, so the
    card-refresh half of this function would have silently never worked in
    production (masked in tests that mock the delegate entirely, or that
    happen to hit a short-circuit -- like a borrowed card -- before
    `bot_data` is ever touched). `bot = bot_app.bot` below recovers the raw
    Bot this function's own Telegram calls need.

    Fix round 2, item 1 (CRITICAL): the state save below used to write back
    the WHOLE blob this function read at the top, across three awaits
    (language lookup, an optional delete, the send) with no lock. A driver's
    tap landing in that window (`route_card_state.mark_borrowed`) got
    silently overwritten by that stale blob the instant this function saved
    -- reviewer-probed: `final stored view: next` after a borrow, card edited
    despite it. Fixed by taking `route_card_state.get_lock(telegram_id)` --
    the SAME per-driver lock `render_route_card` holds for its own
    read-modify-write -- and reloading fresh state *inside* the lock,
    immediately before saving, merging in only this function's own two
    fields (`last_alert_at`, `last_alert_message_id`) instead of the whole
    object. The lock is released (the `async with` block ends) before
    `update_card_for_driver` is called -- `asyncio.Lock` is NOT reentrant
    and that delegate acquires this same lock internally via
    `render_route_card`; holding it across that call would deadlock.

    Fix round 2, item 2 (Important): `reference_message_id` is always
    `None`. Threading the alert's own message id through used to re-enable
    `render_route_card`'s repost-when-buried heuristic on this path, which
    (Telegram ids being monotonic -- a freshly-sent alert always has a
    HIGHER id than an existing card) meant a buried card would get a full
    repost (send + pin) on top of the alert send: 2 sends + delete + pin per
    event. The alert already carries an "Open route card" button, so a
    buried card is handled by that pointer, not by a repost -- restores the
    single-send-per-event invariant `render_route_card`'s own "webhook edits
    pass None and can never repost" comment documents.

    Fix round 2, item 4 (Important): the language lookup is now guarded. An
    unguarded `_get_user_language` failure (a DB blip) used to raise BEFORE
    any Telegram call, silently dropping the entire sounded event -- no
    alert, no card refresh, contradicting this docstring's old "never
    raises" claim. Falls back to `i18n.fallback_language` (a static config
    value, no DB access) so a DB blip degrades to a language-fallback rather
    than losing the one message class this plan allows to ping. The trailing
    `update_card_for_driver` call keeps ITS OWN existing raise-on-I/O-failure
    contract (token manager / API fetch) -- identical to the silent branch
    elsewhere in this codebase, and already tolerated by the webhook
    handler's outer try/except -- but by the time that call runs, the alert
    itself has already been attempted, so that class of failure never costs
    the sounded message.

    Fix round 3 (FINAL review, I2): the cap decision (`_alert_is_capped`)
    used to be computed from `state` loaded at the very top of this
    function, OUTSIDE any lock, with the lock only wrapping the later
    merge-and-save. Two sounded events handled concurrently for the same
    driver (distinct `event_id`s, so the webhook's dedup at
    `webhook_server.py:297` does not collapse them -- e.g. a dispatcher's
    multi-save, or accept + auto-assign racing) could both read the same
    `last_alert_at`, both compute `capped=False`, and both ping. The
    decision, the supersede-delete, the send, and the save are now ONE
    critical section under `get_lock(telegram_id)`: a second concurrent
    caller blocks until the first has both decided AND recorded its own
    alert, then re-reads state fresh and correctly sees itself as capped.
    This is not a new pattern -- `render_route_card`'s own docstring already
    holds this same per-driver lock across its Telegram edit call ("the
    whole body is serialized per driver... this IS the
    render-lock/concurrency answer"); this fix just applies that same
    posture to the alert's decide-then-send. As before, the lock is
    released before `update_card_for_driver` is called below -- holding it
    across that call would deadlock, since that delegate reacquires this
    same lock internally via `render_route_card`.
    """
    bot = bot_app.bot
    try:
        language = await _get_user_language(telegram_id)
    except Exception as exc:  # noqa: BLE001 -- a DB blip on language lookup
        # must not swallow the one message class this plan allows to ping.
        logger.warning("head-change alert: language lookup failed for %s: %s", telegram_id, exc)
        language = i18n.fallback_language

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            f"🚚 {i18n.get('staff.route.open_route_card', language)}",
            callback_data="staff_route_view_next",
        )
    ]])

    # Fix round 3, I2: decide + supersede-delete + send + save as ONE
    # critical section -- see the docstring note above. Everything that
    # reads or writes the alert-throttle state now happens inside this
    # single lock acquisition.
    sent = None
    async with route_card_state.get_lock(telegram_id):
        state = await route_card_state.load(telegram_id) or {}
        chat_id = state.get("chat_id") or telegram_id
        capped = _alert_is_capped(state)
        previous_alert_message_id = state.get("last_alert_message_id")
        if capped and previous_alert_message_id:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=previous_alert_message_id)
            except Exception as exc:  # noqa: BLE001 -- already gone / past the 48h
                # window / any other delete failure must never block the new
                # alert below.
                logger.debug("superseded head-change alert delete skipped: %s", exc)

        try:
            sent = await bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ {i18n.get('staff.route.head_changed_alert', language)}",
                reply_markup=keyboard,
                disable_notification=capped,
            )
        except Exception as exc:  # noqa: BLE001 -- a single recipient's send
            # failure (blocked bot, etc.) must not crash the caller.
            logger.warning("head-change alert send failed for %s: %s", telegram_id, exc)

        if sent is not None:
            # Merge-only-our-fields, reloading fresh immediately before
            # saving (fix round 2, item 1) -- still correct and cheap even
            # though nothing can mutate `state` out from under us while we
            # hold this lock; kept for defense in depth against a future
            # writer added inside this same critical section.
            fresh_state = await route_card_state.load(telegram_id) or {}
            fresh_state["last_alert_at"] = datetime.now(timezone.utc).isoformat()
            fresh_state["last_alert_message_id"] = sent.message_id
            await route_card_state.save(telegram_id, fresh_state)

    await update_card_for_driver(
        bot_app, telegram_id,
        language=language,
        reference_message_id=None,  # fix round 2, item 2 -- see docstring
    )
