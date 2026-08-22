"""A bot placeholder must be one that ``Translation.get`` can actually fill.

``Translation.get`` is the ONLY door through which a seeded value is rendered,
and since wave 2 it is also the only place a value may be interpolated —
``render_translation`` degrades a template the caller did not fill to the
humanised key rather than handing it back for a later ``.format()``.

That makes every parameter name ``get()`` can bind BY KEYWORD a reserved word
inside the copy. ``language`` was one of them, and a value containing
``{language}`` was therefore unfillable by anybody::

    i18n.get('telegram.language.now_using', 'ru', language='Русский')
    TypeError: Translation.get() got multiple values for argument 'language'

The alternatives — omit it (broken copy -> "Now using") or format the result
afterwards (the same, plus it is banned by ``test_i18n_get_format_idiom_guard``)
— both silently drop the value, which is how the language-switch confirmation
became unfixable in its original spelling and the seeded placeholder had to be
respelled ``{language_name}``.

WHAT CHANGED, AND WHY THIS FILE STILL EARNS ITS PLACE
-----------------------------------------------------
``key`` and ``language`` are now POSITIONAL-ONLY on both bots
(``def get(self, key, language=None, /, *args, **kwargs)``), so those two names
are free for the copy again and that particular collision cannot recur. The
reserved set is derived from the live signature, so it is EMPTY today — and a
lint over an empty set catches nothing.

So the file now pins two things instead of one:

* the reason the set is empty — that ``get()`` binds nothing by keyword — so
  the emptiness is a structural property somebody has to deliberately undo,
  not an accident nobody would notice;
* that the derivation still bites when a keyword-bindable parameter DOES exist,
  replayed against the real historical row, so the catalogue lint below is
  armed the moment someone adds one.

Plus the behavioural half the original lint could only approximate: the once
unfillable spelling really does render, through the real ``get()``, on both
bots.
"""

from __future__ import annotations

import inspect
import re
from string import Formatter

import pytest

from i18n import Translation
from staff_bot.i18n import Translation as StaffTranslation

from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

# Everything served through `Translation.get`. `ui.*` keys in the same catalogue
# are rendered by i18next in the admin UI and are not subject to this rule.
CUSTOMER_BOT_PREFIX = "telegram."

# The two bots keep separate `Translation` classes reading separate catalogues,
# but the rule is a property of the shared calling convention, so both are held
# to it here.
BOTH_BOTS = {"customer bot": Translation, "staff bot": StaffTranslation}


def _reserved_placeholder_names(func) -> set[str]:
    """Parameter names ``func`` binds ITSELF when a caller passes them by keyword.

    Positional-only parameters are deliberately excluded: their names are not
    keywords at the call site at all, so ``**kwargs`` may carry them and the
    copy may use them as placeholders. That exclusion IS the fix — if it ever
    stops being true for `key`/`language`, the emptiness test below fails and
    the catalogue lint re-arms.
    """
    signature = inspect.signature(func)
    return {
        name
        for name, parameter in signature.parameters.items()
        if name != "self"
        and parameter.kind in (parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY)
    }


RESERVED = _reserved_placeholder_names(Translation.get)


@pytest.mark.unit
@pytest.mark.parametrize("bot", sorted(BOTH_BOTS))
def test_get_reserves_no_word_the_copy_might_want(bot):
    """The set is empty BECAUSE `key` and `language` are positional-only.

    Asserting emptiness alone would pass just as happily if the method were
    renamed away or lost its parameters, so the structural reason is pinned
    directly: the two names the copy has already wanted are behind the ``/``.
    """
    get = BOTH_BOTS[bot].get
    parameters = inspect.signature(get).parameters

    for name in ("key", "language"):
        assert parameters[name].kind is inspect.Parameter.POSITIONAL_ONLY, (
            f"{bot}: `{name}` is bindable by keyword again, so a seeded "
            f"'{{{name}}}' placeholder is once more impossible to fill — put it "
            "back behind the `/` in Translation.get"
        )

    assert _reserved_placeholder_names(get) == set(), (
        f"{bot}: Translation.get binds a name by keyword; seeded copy may no "
        "longer use it as a placeholder, and the catalogue lint below now has "
        "to be taken seriously again"
    )


def _root_name(field_name: str) -> str:
    """``{order.total}`` / ``{items[0]}`` -> ``order`` / ``items``."""
    return re.split(r"[.\[]", field_name, maxsplit=1)[0]


def _offending_rows(reserved: set[str], catalogue=None):
    """Rows in ``catalogue`` whose placeholders name something ``reserved``."""
    catalogue = BACKEND_TRANSLATIONS if catalogue is None else catalogue
    offenders = []
    for key, row in catalogue.items():
        if not isinstance(key, str) or not key.startswith(CUSTOMER_BOT_PREFIX):
            continue
        if not isinstance(row, dict):
            continue
        for language, value in row.items():
            if not isinstance(value, str):
                continue
            try:
                fields = [n for _, n, _, _ in Formatter().parse(value) if n]
            except ValueError:
                continue  # malformed copy is a different lint's problem
            clashing = sorted({_root_name(f) for f in fields} & reserved)
            if clashing:
                offenders.append((key, language, clashing, value))
    return offenders


@pytest.mark.unit
def test_no_customer_bot_copy_uses_a_placeholder_get_cannot_fill():
    offenders = _offending_rows(RESERVED)
    assert not offenders, (
        "these seeded values name a placeholder after a Translation.get() "
        "parameter that binds by keyword, so nothing can ever fill them — "
        "rename the placeholder here AND at the call site, or move the "
        "parameter behind the `/`:\n  "
        + "\n  ".join(f"{key} [{lang}] {clash}: {value}" for key, lang, clash, value in offenders)
    )


# The row exactly as it shipped, before the placeholder was respelled. The
# live catalogue no longer contains it — that is the whole point of the fix —
# so the positive control below replays it rather than searching for it.
HISTORICAL_CATALOGUE = {
    "telegram.language.now_using": {
        "en": "You're now using {language}",
        "ru": "Теперь вы используете язык {language}",
    },
    "telegram.orders.status": {"en": "Order {order_id} is {status}"},
}


@pytest.mark.unit
def test_the_derivation_still_bites_when_a_parameter_binds_by_keyword():
    """Positive control, replayed against the row that actually shipped.

    The catalogue lint above is vacuous while nothing is reserved, so this
    proves it is vacuous for the RIGHT reason — the derivation and the scan
    both still work — rather than because they quietly stopped detecting
    anything. Give them the pre-fix signature and the pre-fix copy and they
    must find it, and must still leave the innocent row alone.
    """

    def get_with_keyword_language(self, key, language=None, *args, **kwargs):
        """The pre-fix signature, kept here as a specimen only."""

    reserved = _reserved_placeholder_names(get_with_keyword_language)
    assert reserved == {"key", "language"}

    offenders = _offending_rows(reserved, HISTORICAL_CATALOGUE)

    assert sorted((key, lang, tuple(clash)) for key, lang, clash, _ in offenders) == [
        ("telegram.language.now_using", "en", ("language",)),
        ("telegram.language.now_using", "ru", ("language",)),
    ], "the scan no longer finds the row that actually shipped — it has gone blind"


@pytest.mark.unit
@pytest.mark.parametrize("bot", sorted(BOTH_BOTS))
def test_the_spelling_that_was_unfillable_now_renders_on_both_bots(bot):
    """The behavioural half: `{language}` goes in and comes out filled.

    This is the call the wave-2 fix could not make. It is asserted through the
    REAL ``get`` — with the catalogue stubbed, not the method — because the
    collision was in the method's own parameter binding and nothing short of
    calling it can see that.
    """
    translations = BOTH_BOTS[bot]()
    key = "probe.language.now_using"
    translations.translations = {"ru": {key: "Теперь вы используете язык {language}"}}

    assert translations.get(key, "ru", language="Русский") == (
        "Теперь вы используете язык Русский"
    )
    # `key` is the other freed name, and it is freed the same way.
    translations.translations = {"ru": {key: "Ключ: {key}"}}
    assert translations.get(key, "ru", key="ORD-7") == "Ключ: ORD-7"
