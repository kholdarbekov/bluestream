"""D24 guard: every literal translation key a cabinet template renders is owned by ONE seed.

A key the cabinet renders but no seed writes shows the visitor raw English (or a raw
``landing.*`` path). A bare English key that another seed also writes is worse: re-seeding
it would move that other surface's row into a different category.
"""

import re
from pathlib import Path

import pytest

from scripts.seed_backend_translations import _DOTTED_KEY, BACKEND_TRANSLATIONS

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "business_app" / "templates"
FRONTEND = TEMPLATES / "frontend"
SCRIPTS = ROOT / "scripts"
OWNING_SEED = SCRIPTS / "seed_backend_translations.py"

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
    """loyalty.js fills `{count}` into ONE translated sentence; no prefix + number + suffix glue."""
    row = BACKEND_TRANSLATIONS["cabinet.loyalty.earn_more_to_keep"]
    script = (ROOT / "business_app" / "static" / "js" / "pages" / "loyalty.js").read_text(encoding="utf-8")
    assert "PAGE_DATA.i18n.earn_more_to_keep.replace('{count}', needed)" in script
    assert row["en"].replace("{count}", "120") == "Earn 120 more AquaCoins to keep your status"
    for language in ("uz", "ru"):
        rendered = row[language].replace("{count}", "120")
        assert row[language].count("{count}") == 1 and "120" in rendered and "{" not in rendered


def test_the_home_chip_is_not_the_navigation_word():
    assert BACKEND_TRANSLATIONS["cabinet.addresses.home"] == {"en": "Home", "uz": "Uy", "ru": "Дом"}
    assert "Home" not in BACKEND_TRANSLATIONS
