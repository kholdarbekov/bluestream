import re
from typing import Mapping, Optional

from telegram.ext import filters

from i18n import i18n


# ---------------------------------------------------------------------------
# "Is this text a reply-keyboard tap?" -- ONE rule, one place
# ---------------------------------------------------------------------------
# The address flow arms REPLY keyboards (`ProfileKeyboards.location_request`
# with `extra_rows=`), so Enter-manually / Re-enter / Cancel arrive as ordinary
# text. Unlike the staff bot, the KEYBOARD adds no decoration of its own here:
# the row is rendered verbatim from `i18n.get`, and any emoji is part of the
# seeded copy ("❌ Bekor qilish"). The decoration therefore sits on EITHER side
# of the comparison -- a keyboard still on the customer's phone was rendered
# from the row as it read then, which may carry an emoji the row has since lost
# (or vice versa) -- so the strip is symmetric.
#
# It is an EMOJI strip, and this regex is the only place that says so. What it
# replaces was `(?:\S+\s+)?`, "any single leading token", which is the shape
# wave 3 deleted from the staff bot: there it let a five-character first word
# ("Sardor Profil") satisfy the FILTER while the matcher resolved nothing, and
# the conversation was torn down with no output at all. Here the same shape
# meant "word Bekor qilish" cancelled the address flow. Ranges rather than a
# library: `re` has no \p{Emoji}, and "strip any leading non-alphanumeric run"
# would quietly re-admit "+998...".
_EMOJI_PREFIX_RE = re.compile(
    r"^(?:"
    r"[\U0001F000-\U0001FAFF]"       # pictographs, transport, flags, extended-A
    r"|[\u2190-\u21FF]"              # arrows
    r"|[\u2300-\u23FF]"              # misc technical (watch, hourglass)
    r"|[\u2460-\u27BF]"              # enclosed alphanumerics .. dingbats (gear, check)
    r"|[\u2B00-\u2BFF]"              # misc symbols and arrows (left arrow, star)
    r"|[\u3030\u303D\u3297\u3299]"   # wavy dash, part alternation mark, congrat/secret
    r"|[\uFE00-\uFE0F\u200D\u20E3]"  # variation selectors, ZWJ, combining keycap
    r")+"
)


def bare_label(text: Optional[str]) -> str:
    """`text` stripped of surrounding space and of a leading emoji decoration."""
    return _EMOJI_PREFIX_RE.sub("", (text or "").strip()).strip()


def is_tap_on_label(text: Optional[str], label: Optional[str]) -> bool:
    """Is `text` a tap on a reply-keyboard button reading `label`?

    Whole-string, never a substring: the pattern this replaces once matched any
    message CONTAINING the copy, so "I don't want to cancel" cancelled the
    address flow -- the bot agreeing with the customer by ending their work.
    Text that merely contains a label is not a tap and is left to the state's
    own handler, which is what a customer typing a sentence is owed.

    Equal after the emoji strip, so the decoration may differ between the
    keyboard on the phone and the row as it reads now -- but only the
    decoration. A pure-emoji label strips to nothing and is compared raw, so
    two different emoji buttons can never collapse into each other.
    """
    text = (text or "").strip()
    label = (label or "").strip()
    if not text or not label:
        return False
    if text == label:
        return True
    stripped_text, stripped_label = bare_label(text), bare_label(label)
    return bool(stripped_text) and stripped_text == stripped_label


def resolve_tapped_label(text: Optional[str], translation_keys) -> Optional[str]:
    """Which of `translation_keys` is this text a tap on? The key, or None.

    THE decider, and the only copy of the rule. `MenuTapFilter` and the flow
    exit's `FlowExitHandler.check_update` (`flow_exit.py`) are the only callers,
    and both call this and nothing else, so "the filter claimed it" and "the
    matcher resolved it" cannot become different answers.

    Resolved WHEN THE TAP ARRIVES: `i18n.get` is called here, exactly as the
    keyboard builder calls it when it renders. Nothing is memoised across
    updates, and that is the feature -- an admin retitling a label in the admin
    UI (or any `i18n.reload_translations()`) changes what the button says AND
    what answers it in the same instant, with no restart, and the retired copy
    stops matching at the same instant so it cannot linger as a hotword that
    hijacks typed text. A matcher frozen at handler-build time left the button
    the customer could SEE dead, and an unmatched tap in ADDRESS_LOCATION
    escapes to the group-0 catch-all, where Cancel becomes a support ticket
    with no reply.

    Swept across EVERY supported language, not just the customer's: a reply
    keyboard is client-side and survives a language switch until Telegram
    redraws it, so a tap can arrive in the language the customer just left.
    Key order breaks a tie if two keys ever render the same copy.

    Cost per text update: one `i18n.get` -- an in-memory dict lookup plus
    `render_translation` on a placeholder-free label -- per key per language,
    abandoned on the first hit. The flow exit asks this for the seven
    `keyboards.MAIN_MENU_KEYS` on each non-command text. `MenuTapFilter` asks
    for its address-flow rows inside that flow. That is at most 7 keys x 3
    languages = 21 in-memory `i18n.get` lookups per update, stopping at the
    first hit, so microseconds. An unseeded key still logs one missing-key
    warning per language per update.
    """
    for translation_key in translation_keys:
        for lang_code in i18n.supported_languages:
            if is_tap_on_label(text, i18n.get(translation_key, lang_code)):
                return translation_key
    return None


def resolve_installed_label(text: Optional[str], installed: Mapping[str, str]) -> Optional[str]:
    """Which of `installed` (translation key -> the label sent) is this text a tap on?

    Reads nothing from `i18n`: `installed` holds the labels that were actually
    sent, so a keyboard still on the customer's phone keeps resolving after an
    admin retitles the button. The first key in `installed`'s iteration order
    that matches wins, and otherwise the result is None.
    """
    for translation_key, label in installed.items():
        if is_tap_on_label(text, label):
            return translation_key
    return None


class MenuTapFilter(filters.MessageFilter):
    """``True`` for text that IS a tap on one of `translation_keys`.

    A `filters.MessageFilter` rather than a `filters.Regex` because a regex can
    only be built from copy that is already known, i.e. at handler-build time;
    this asks `resolve_tapped_label` per update instead. Registered in place
    of the build-time label regexes that guarded the customer's way OUT of the
    address flow.
    """

    __slots__ = ("_translation_keys",)

    def __init__(self, *translation_keys: str):
        super().__init__(name="telegram_menu_tap:%s" % ",".join(translation_keys))
        self._translation_keys = translation_keys

    def filter(self, message) -> bool:
        return resolve_tapped_label(message.text, self._translation_keys) is not None
