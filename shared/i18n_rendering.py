"""One expression of "how a translation VALUE becomes the string a user reads".

Both bots keep their own ``Translation`` class — they read different rows
(``category='telegram'`` vs ``category='staff_bot'``), track missing keys
differently and log differently — but the RENDERING rule below has to be the
same on both, because it is the rule that decides what a human ends up reading
when a translation row is wrong. It used to be copy-pasted into
``telegram_bot/i18n.py`` and ``staff_bot/i18n.py``, which is how the same defect
came to exist twice:

* the interpolation guard caught ``KeyError``/``ValueError`` but NOT
  ``IndexError``, so a value carrying a POSITIONAL placeholder (``{}`` /
  ``{0}``) formatted with keyword arguments raised straight out of ``get()``
  and took the calling flow down with it (the customer bot's address flow ended
  in total silence for every customer in that language);
* a value whose placeholder the call site never fills — ``get()`` only calls
  ``.format()`` when the caller passes args/kwargs — was delivered to the
  customer with the braces intact ("Salom, {first_name}!").

Translation values are free-text fields editable from the admin UI, so neither
of those is exotic: both are one careless paste away, at any time, without a
deploy. The rule here therefore treats a template it cannot resolve as BROKEN
COPY and degrades to the humanised key, which reads like a plain label rather
than like a bug. Nothing here raises.
"""

from __future__ import annotations

import logging
from string import Formatter
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

_FORMATTER = Formatter()


def humanise_key(key: str) -> str:
    """The placeholder text shown for a key with no usable copy.

    ``telegram.orders.cod_restricted_place`` -> ``Cod restricted place``.

    Carries no ``{...}`` of its own, so it is safe to return from every branch
    below without re-checking it.
    """
    last_part = key.rsplit('.', 1)[-1] if '.' in key else key
    return last_part.replace('_', ' ').capitalize()


def has_unresolved_placeholder(template: str) -> bool:
    """True when ``template`` still carries a replacement field, or is malformed.

    Uses ``string.Formatter().parse`` rather than a brace search so doubled
    braces (``{{literal}}``) — which are NOT replacement fields — do not count.
    A template ``str.format`` itself would refuse (an unbalanced brace) is
    reported as unresolved too: it can never render, so emitting it raw only
    ever shows the customer the breakage.
    """
    try:
        return any(field_name is not None for _, field_name, _, _ in _FORMATTER.parse(template))
    except (AttributeError, TypeError, ValueError):
        return True


def render_translation(
    key: str,
    template: str,
    args: Sequence[Any] = (),
    kwargs: Mapping[str, Any] | None = None,
) -> str:
    """The string a user reads for ``key``. Never raises, never emits ``{...}``.

    * caller passed args/kwargs -> interpolate; a template whose placeholders do
      not match what the caller passed (renamed, positional, malformed) is
      broken copy -> humanised key.
    * caller passed nothing -> the template must already be complete; one that
      still carries a replacement field is broken copy -> humanised key.

    The second branch is why a call site must pass its values to ``get()``
    rather than calling ``.format()`` on the result: ``get()`` no longer hands
    back a template to fill in later.
    """
    kwargs = kwargs or {}

    if args or kwargs:
        try:
            return template.format(*args, **kwargs)
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "Translation '%s' does not match the values its call site passes "
                "(%s); showing the humanised key instead of the raw template",
                key, exc,
            )
            return humanise_key(key)

    if has_unresolved_placeholder(template):
        logger.warning(
            "Translation '%s' carries a placeholder its call site never fills; "
            "showing the humanised key instead of the raw template",
            key,
        )
        return humanise_key(key)

    return template
