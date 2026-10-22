"""The customer copy of a reorder reminder: ONE assembly of the reorder-reminder keys.

Spec docs/superpowers/specs/2026-10-09-lapsed-customer-reorder-reminders-design.md §7.5.

The Telegram message (telegram_bot/webhook_server.py), its buttons (telegram_bot/keyboards.py),
the Later/Stop confirmations (telegram_bot/handlers/reorder_reminders.py), the Profile ->
Notifications line (telegram_bot/handlers/profile.py) and the reminder email
(business_app/services/reorder_reminders/messaging.py) all read the same DB rows. Without
this module each of them would decide which key carries which placeholder, how the greeting
is chosen, how many order lines `{items}` holds and which plural row a number reads: five
places deciding one thing. They pass a `render(key, **values)` callable and get finished
strings back.

`render` is the caller's own lookup + `shared.i18n_rendering.render_translation`, bound to one
language:

* in the customer bot, `Translation.renderer(language)` (telegram_bot/i18n.py), which routes
  through the bot's `Translation.get`;
* in the backend (the email), `_renderer(language)` in
  business_app/services/reorder_reminders/messaging.py, over `get_translation`.

Neither raises nor emits a `{placeholder}`, so nothing here does either. Values arrive ready to
print: the bot HTML-escapes names first (it sends with parse_mode='HTML'), the email lets
Jinja's autoescape do it.

Every key is spelled out literally here and ONLY here:
tests/unit/test_reorder_reminder_copy_is_seeded.py pins the set against the seed and refuses
the literal anywhere else in the product code.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from shared.i18n_rendering import SUPPORTED_LANGUAGES, base_language, plural_key

Render = Callable[..., str]

# Spec §7.3: "Up to 2 lines {name, qty} from that order". The backend payload and the email
# take their slice from here too.
MAX_ITEMS = 2

# The languages every reminder string is seeded in: the ones `i18n_rendering.reminder_language`
# can answer. A payload item carries its product name in each of them (master amendment 13:
# {"qty": int, "names": {"uz", "ru", "en"}}); the backend reads this tuple to fill `names`, and
# `item_name` prints the one the copy is rendered in.
LANGUAGES = SUPPORTED_LANGUAGES

# When an item has no name in the language the copy is rendered in, `item_name` tries these in
# order: en is the canonical column, then ru, then uz.
ITEM_NAME_FALLBACK = ("en", "ru", "uz")

# Plural base: rows `<DAYS_KEY>.one|few|many|other`, picked by `plural_key` (CLDR integers).
DAYS_KEY = "telegram.reorder_reminder.days"


def days_phrase(render: Render, language: str, n: int) -> str:
    """`{n}` days in the reader's grammar: 1 день, 2 дня, 5 дней, 21 день."""
    return render(plural_key(DAYS_KEY, language, n), n=n)


def greeting(render: Render, name: Optional[str]) -> str:
    """`Hi {name}!`, or the nameless greeting: never `Hi None!`."""
    if name:
        return render("telegram.reorder_reminder.greeting", name=name)
    return render("telegram.reorder_reminder.greeting_noname")


def _name_in(names: Mapping[str, Any], code: str) -> str:
    """The non-blank name `names` holds for `code`, or ""."""
    return (names.get(code) or "").strip()


def item_name(item: Mapping[str, Any], language: str) -> str:
    """The item's product name in `language`, the language the surrounding copy is rendered in
    (master amendment 13), so a line and its sentence are never in two languages.

    `language` is normalised the way the plural rule normalises it ("ru-RU" reads as "ru"). A
    name missing in that language falls back to en, then ru, then uz (`ITEM_NAME_FALLBACK`). An
    item with no name in any of them returns "", and `printable` refuses that line.
    """
    names = item.get("names") or {}
    for code in dict.fromkeys((base_language(language), *ITEM_NAME_FALLBACK)):
        name = _name_in(names, code)
        if name:
            return name
    return ""


def printable(item: Mapping[str, Any]) -> bool:
    """Can this order line be printed? The ONE rule, asked by `items_text` here and by the backend
    payload (`business_app/services/reorder_reminders/messaging.py`), which drops a refused line
    before it takes its MAX_ITEMS, so such a line never costs the customer one of theirs.

    Both must hold: `qty` is an int above 0 (a bool is not a quantity), and the item has a
    non-blank name in at least one of LANGUAGES. `item_name` falls back across every one of them,
    so a printable line always has a name to print, in whatever language the copy is rendered in.
    """
    qty = item.get("qty")
    if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
        return False
    names = item.get("names") or {}
    return any(_name_in(names, code) for code in LANGUAGES)


def items_text(render: Render, language: str, items: Sequence[Mapping[str, Any]]) -> str:
    """Up to MAX_ITEMS order lines in `language`, joined with ", ", or the plain word for water.

    A line `printable` refuses (no positive whole quantity, or no name in any language) is left
    out and does not use up one of the MAX_ITEMS slots, so "1 × ", "0 × ..." and "None × ..." are
    never printed.
    """
    lines: list[str] = []
    for item in list(items or ()):
        if not printable(item):
            continue
        lines.append(render("telegram.reorder_reminder.item", qty=item["qty"], name=item_name(item, language)))
        if len(lines) == MAX_ITEMS:
            break
    if not lines:
        return render("telegram.reorder_reminder.items_fallback")
    return ", ".join(lines)


def statement(render: Render, language: str, *, step: int, name: Optional[str], days: int, gap: Optional[int]) -> str:
    """The statement both channels share. It never contains a call to action (§7.5 "Assembly")."""
    greet = greeting(render, name)
    if step >= 2:
        return render("telegram.reorder_reminder.r2", greeting=greet)
    if gap:
        return render(
            "telegram.reorder_reminder.r1_personal",
            greeting=greet,
            days_phrase=days_phrase(render, language, days),
            gap_phrase=days_phrase(render, language, gap),
        )
    return render(
        "telegram.reorder_reminder.r1_preset",
        greeting=greet,
        days_phrase=days_phrase(render, language, days),
    )


def telegram_text(
    render: Render,
    language: str,
    *,
    step: int,
    name: Optional[str],
    days: int,
    gap: Optional[int],
    items: Sequence[Mapping[str, Any]],
) -> str:
    """The Telegram message: statement + tap line."""
    body = statement(render, language, step=step, name=name, days=days, gap=gap)
    tap = render("telegram.reorder_reminder.tap_line", items=items_text(render, language, items))
    return f"{body}\n\n{tap}"


def order_label(render: Render) -> str:
    """The "🛒 Order again" label: the reminder's Order button, and the same button under the
    delivered-order summary and on the open-order notice (telegram_bot/keyboards.py)."""
    return render("telegram.reorder_reminder.btn_order")


def button_labels(render: Render, language: str, *, snooze_days: int) -> Dict[str, str]:
    """Labels for the three reminder buttons (§7.3); the keyboard decides which it shows."""
    return {
        "order": order_label(render),
        "later": render(
            "telegram.reorder_reminder.btn_later",
            snooze_phrase=days_phrase(render, language, snooze_days),
        ),
        "stop": render("telegram.reorder_reminder.btn_stop"),
    }


def later_ok(render: Render, language: str, *, snooze_days: int) -> str:
    return render(
        "telegram.reorder_reminder.later_ok",
        snooze_phrase=days_phrase(render, language, snooze_days),
    )


def stop_ok(render: Render) -> str:
    """Names the real path back from the menu labels themselves (§7.5 "Settings path")."""
    return render(
        "telegram.reorder_reminder.stop_ok",
        profile_label=render("telegram.menu.profile"),
        notifications_label=render("telegram.profile.notifications"),
    )


def expired(render: Render) -> str:
    return render("telegram.reorder_reminder.expired")


def email_parts(
    render: Render,
    language: str,
    *,
    step: int,
    name: Optional[str],
    days: int,
    gap: Optional[int],
    items: Sequence[Mapping[str, Any]],
) -> Dict[str, str]:
    """Every sentence of the reminder email (§7.4): statement + CTA line + buttons + opt-out."""
    return {
        "greeting": greeting(render, name),
        "statement": statement(render, language, step=step, name=name, days=days, gap=gap),
        "cta_line": render("telegram.reorder_reminder.email_cta_line", items=items_text(render, language, items)),
        "btn_website": render("telegram.reorder_reminder.email_btn_website"),
        "btn_telegram": render("telegram.reorder_reminder.email_btn_telegram"),
        "unsubscribe_line": render("telegram.reorder_reminder.email_unsubscribe_line"),
    }


def settings_status(render: Render, enabled: bool) -> str:
    """The Profile -> Notifications line for the EFFECTIVE value the backend publishes (§8)."""
    if enabled:
        return render("telegram.reorder_reminder.settings_on")
    return render("telegram.reorder_reminder.settings_off")


def settings_button(render: Render, enabled: bool) -> str:
    """The toggle's label names the action it takes: the opposite of the current value."""
    if enabled:
        return render("telegram.reorder_reminder.settings_button_off")
    return render("telegram.reorder_reminder.settings_button_on")
