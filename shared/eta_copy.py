"""The customer's arrival lines: ONE assembly of the honest-ETA copy keys.

Honest-ETA spec 2026-11-03 §4.8. `DeliveryEtaService.customer_eta` (business_app/services/delivery_eta_service.py)
decides what is promised and publishes it as finished fields: the stage, the minutes left, the "by" time on the
Tashkent clock, and where the minutes came from. This module decides, once, what is SAID for them: which sentence, with
which values, and when the attribution follows. `promised_time_lines` is the promised time: the backend's in-transit
message (`NotificationService._delivery_eta_line`, whose `eta_line` the delivery-update email prints too)
renders through it. `track_lines` is the customer bot's Track screen: the promised time, or "On the way" when nothing
is promised, or "Arrived at your address". Each caller passes a `render(key, /, **values)` callable bound to one
language and gets finished lines back. The lines carry no emoji: a client that wants a marker puts it before the first.

`render` is the caller's ordinary renderer: the bot's `Translation.renderer(language)`, or the backend's
`business_app.utils.translations.renderer(language, copy_name=...)` over `get_translation`. It never raises. Copy it
cannot render (a row missing in every language, or a template its values cannot fill) comes back as the humanised key
(`shared.i18n_rendering.humanise_key`, as `render_translation` degrades it), or as "". This module decides, once, that
either makes the line unsayable. The promised time's lines are all or nothing: when any one of them is unsayable, or
the minutes come with no "by" time to print, none of them is said. So a humanised key, half a line or a "None" never
shows, and a time from Google never shows without its attribution. The Track screen then says the single line "On the
way" instead, and nothing only when even that line is unsayable. The in-transit Telegram message keeps the rule
through its driver-info sanitiser too (`NotificationService._telegram_arrival_lines`).

Every key is spelled out literally here and ONLY here: tests/unit/test_eta_copy_is_seeded.py pins the set against the
seed and refuses the literal anywhere else in the product code.
"""

from __future__ import annotations

from typing import Callable, List, Optional

from shared.constants import ETA_ATTRIBUTION_GOOGLE, ETA_STAGE_ARRIVED, ETA_STAGE_ON_THE_WAY
from shared.i18n_rendering import humanise_key

Render = Callable[..., str]


def _say(render: Render, key: str, /, **values) -> str:
    """`key` rendered, or "" when `render` could not say it: it answered "", or the humanised key it puts in place of
    missing or broken copy."""
    text = render(key, **values)
    return "" if text == humanise_key(key) else text


def promised_time_lines(
    render: Render, *, minutes_left: Optional[int], by_time: Optional[str], attribution: Optional[str]
) -> List[str]:
    """What the customer is told about the promised time, line by line.

    Nothing when nothing is promised (`minutes_left` is None). With a minute or more left: "Arriving within about N
    min (by HH:MM)", `by_time` being the HH:MM that comes with `minutes_left`. Once the "by" time has passed (0 left):
    "Arriving any minute". Then "Live traffic: Google Maps" when Google's traffic gave the minutes. `[]` when `render`
    cannot say any one of these lines, and when minutes are left but `by_time` is missing or empty: the sentence would
    print "None" for it.
    """
    if minutes_left is None:
        return []
    if minutes_left >= 1:
        if not by_time:
            return []
        lines = [_say(render, "telegram.orders.eta_on_the_way", minutes=minutes_left, time=by_time)]
    else:
        lines = [_say(render, "telegram.orders.eta_arriving_now")]
    if attribution == ETA_ATTRIBUTION_GOOGLE:
        lines.append(_say(render, "telegram.orders.eta_google_attribution"))
    return lines if all(lines) else []


def _single_line(render: Render, key: str) -> List[str]:
    """`key` as the one line of a screen, or no line when `render` cannot say it."""
    text = _say(render, key)
    return [text] if text else []


def track_lines(
    render: Render,
    *,
    stage: Optional[str],
    minutes_left: Optional[int],
    by_time: Optional[str],
    attribution: Optional[str],
) -> List[str]:
    """What the customer bot's Track screen says about the arrival, line by line, from the fields Track publishes:
    `stage`, `minutes_left`, `by_time` and `attribution` (the `eta_by` instant is not needed).

    On the way (`ETA_STAGE_ON_THE_WAY`): the promised time (`promised_time_lines`) when there is one, else the single
    line "On the way". No minutes were promised (the driver's position was too old at the tap, Google was down), or the
    promise cannot be said (its copy, or the time it is due by, is missing), but the driver is out all the same.
    Arrived (`ETA_STAGE_ARRIVED`): the single line "Arrived at your address", whatever minutes a body still carries.
    Any other stage, or none: no line. `[]` also when `render` cannot say the one line a stage needs.
    """
    if stage == ETA_STAGE_ON_THE_WAY:
        promised = promised_time_lines(render, minutes_left=minutes_left, by_time=by_time, attribution=attribution)
        return promised or _single_line(render, "telegram.orders.eta_on_the_way_no_time")
    if stage == ETA_STAGE_ARRIVED:
        return _single_line(render, "telegram.orders.eta_arrived")
    return []
