"""D24 guard: every literal translation key a cabinet template renders is owned by ONE seed.

A key the cabinet renders but no seed writes shows the visitor raw English (or a raw
``landing.*`` path). A bare English key that another seed also writes is worse: re-seeding
it would move that other surface's row into a different category.
"""

import re
from pathlib import Path

import pytest

from scripts.seed_backend_translations import _DOTTED_KEY, BACKEND_TRANSLATIONS
from scripts.seed_web_ui_account_translations import TRANSLATIONS as ACCOUNT_TRANSLATIONS
from scripts.seed_web_ui_auth_translations import TRANSLATIONS as AUTH_TRANSLATIONS
from scripts.seed_web_ui_cabinet_translations import TRANSLATIONS as CABINET_TRANSLATIONS
from scripts.seed_web_ui_checkout_translations import TRANSLATIONS as CHECKOUT_TRANSLATIONS
from scripts.seed_web_ui_shop_translations import TRANSLATIONS as SHOP_TRANSLATIONS

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "business_app" / "templates"
FRONTEND = TEMPLATES / "frontend"
SCRIPTS = ROOT / "scripts"
OWNING_SEED = SCRIPTS / "seed_backend_translations.py"
# The website redesign's cabinet copy (2026-11-14) lives in its group seed, under its own `web.cabinet.` namespace.
CABINET_PREFIX = "web.cabinet."
CABINET_ROWS = {
    key: {language: rows[key] for language, rows in CABINET_TRANSLATIONS.items() if key in rows}
    for key in set().union(*CABINET_TRANSLATIONS.values())
}
# The account group's pages (Aqua Club, profile, security) keep theirs under `web.account.` in their group seed.
ACCOUNT_PREFIX = "web.account."
ACCOUNT_ROWS = {
    key: {language: rows[key] for language, rows in ACCOUNT_TRANSLATIONS.items() if key in rows}
    for key in set().union(*ACCOUNT_TRANSLATIONS.values())
}
# The checkout group's pages (My addresses and its address wizard) keep theirs under `web.checkout.`.
CHECKOUT_PREFIX = "web.checkout."
CHECKOUT_ROWS = {
    key: {language: rows[key] for language, rows in CHECKOUT_TRANSLATIONS.items() if key in rows}
    for key in set().union(*CHECKOUT_TRANSLATIONS.values())
}

# A word the cabinet's order pages share with the cart and checkout (the courtesy line's "{qty} x {price} (your price,
# kept for you)") is ONE `web.shop.` row in the shop seed: the same sentence under a second key would drift.
SHOP_PREFIX = "web.shop."
SHOP_ROWS = {
    key: {language: rows[key] for language, rows in SHOP_TRANSLATIONS.items() if key in rows}
    for key in set().union(*SHOP_TRANSLATIONS.values())
}

# The auth group's building blocks (macros/auth.html: password fields, the checklist, the one-time code) and their
# shared copy are `web.auth.` keys in the auth seed; the cabinet's verify-phone page and the account security page
# use them.
AUTH_PREFIX = "web.auth."
AUTH_ROWS = {
    key: {language: rows[key] for language, rows in AUTH_TRANSLATIONS.items() if key in rows}
    for key in set().union(*AUTH_TRANSLATIONS.values())
}

_KEY_RE = re.compile(r"""(['"])((?:\\.|(?!\1).)+?)\1\s*\|\s*(?:t|translate)\b""")
_EXTENDS_RE = re.compile(r"""\{%-?\s*extends\s+['"]frontend/cabinet_base\.html['"]""")
_LINK_RE = re.compile(r"""\{%-?\s*(?:include|from)\s+['"]([^'"]+)['"]""")

KNOWN_CABINET_TEMPLATES = {
    "cabinet_base.html",
    "profile_settings.html",
    "addresses.html",
    "account_security.html",
    "orders.html",
    "loyalty.html",
    "my_account.html",
    "my_subscriptions.html",
    "partials/order_detail_modal.html",
    "partials/address_wizard.html",
    "macros/page_data.html",
    "macros/order_status.html",
}


def cabinet_templates() -> set[str]:
    """cabinet_base, every frontend page extending it, and their transitive include/import targets."""
    found = {"cabinet_base.html"}
    for path in FRONTEND.glob("*.html"):
        if _EXTENDS_RE.search(path.read_text(encoding="utf-8")):
            found.add(path.name)
    queue = list(found)
    while queue:
        name = queue.pop()
        for target in _LINK_RE.findall((FRONTEND / name).read_text(encoding="utf-8")):
            if not target.startswith("frontend/"):
                continue
            rel = target[len("frontend/"):]
            if rel not in found and (FRONTEND / rel).exists():
                found.add(rel)
                queue.append(rel)
    return found


def cabinet_template_keys() -> dict[str, list[str]]:
    """key -> ["file:line", ...] for every literal ``'...'|t`` / ``|translate`` in the cabinet set."""
    keys: dict[str, list[str]] = {}
    for name in sorted(cabinet_templates()):
        text = (FRONTEND / name).read_text(encoding="utf-8")
        for match in _KEY_RE.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            keys.setdefault(match.group(2), []).append(f"{name}:{line}")
    return keys


def test_the_cabinet_template_set_is_the_cabinet():
    assert KNOWN_CABINET_TEMPLATES <= cabinet_templates()


@pytest.mark.parametrize("key", sorted(cabinet_template_keys()))
def test_every_cabinet_key_has_a_seed_row_in_all_three_languages(key):
    if key.startswith(CABINET_PREFIX):
        row = CABINET_ROWS.get(key)
    elif key.startswith(ACCOUNT_PREFIX):
        row = ACCOUNT_ROWS.get(key)
    elif key.startswith(CHECKOUT_PREFIX):
        row = CHECKOUT_ROWS.get(key)
    elif key.startswith(AUTH_PREFIX):
        row = AUTH_ROWS.get(key)
    elif key.startswith(SHOP_PREFIX):
        row = SHOP_ROWS.get(key)
    else:
        row = BACKEND_TRANSLATIONS.get(key)
    assert row is not None, f"{key!r} is rendered at {cabinet_template_keys()[key]} but no seed row exists"
    for language in ("en", "uz", "ru"):
        assert row.get(language), f"{key!r} has no {language} value"


def _bare(key: str) -> bool:
    return not _DOTTED_KEY.match(key)


def _seed_dict_key_literals() -> dict[Path, str]:
    return {
        path: path.read_text(encoding="utf-8")
        for path in SCRIPTS.glob("*.py")
        if path != OWNING_SEED
    }


@pytest.mark.parametrize("key", sorted(k for k in cabinet_template_keys() if _bare(k)))
def test_no_other_seed_writes_a_bare_cabinet_key(key):
    """A hit is a dict-key literal (``'<key>':`` / ``"<key>":``) in any other script; an f-string format spec such as ``{'Status':<12}`` is not one."""
    hits = [
        path.name
        for path, text in _seed_dict_key_literals().items()
        if re.search(r"""(['"])""" + re.escape(key) + r"""\1\s*:(?![<>^=\d])""", text)
    ]
    assert hits == [], f"{key!r} is also seeded bare by {hits}; rename the cabinet key"


def test_the_requalification_line_is_one_sentence_with_the_count_in_it():
    """/my-loyalty fills `{pts}` into ONE translated sentence; no prefix + number + suffix glue. Since the
    2026-11-14 redesign the server renders the line (loyalty.html) from the account group's key."""
    row = ACCOUNT_ROWS["web.account.loyalty.keep_level"]
    template = (FRONTEND / "loyalty.html").read_text(encoding="utf-8")
    assert "'web.account.loyalty.keep_level'|t(pts=pts(summary.points_needed_to_keep))" in template
    assert row["en"].replace("{pts}", "120") == "Earn 120 more AquaCoins to keep your level"
    for language in ("uz", "ru"):
        rendered = row[language].replace("{pts}", "120")
        assert row[language].count("{pts}") == 1 and "120" in rendered and "{" not in rendered


def test_the_home_chip_is_not_the_navigation_word():
    assert BACKEND_TRANSLATIONS["cabinet.addresses.home"] == {"en": "Home", "uz": "Uy", "ru": "Дом"}
    assert "Home" not in BACKEND_TRANSLATIONS
