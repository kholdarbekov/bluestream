"""The office's hours come from ONE value (owner, 2026-11-14: "Mon–Sat 9–18 is the office working hour").

Before: the footer and /contact printed a database-only row, `landing.footer.business_hours` ("Пн - Сб: 9:00 - 18:00."),
under "Часы работы", while the Store JSON-LD published `COMPANY_OPENING_HOURS` ("Mo-Sa 09:00-21:00", "Su 10:00-19:00"
on dev): two sources, two answers. Now `COMPANY_OPENING_HOURS` feeds both. The footer and /contact print it localized
and labelled as the OFFICE's hours, above the delivery line, which keeps its own wording; an unset or unreadable value
prints no office line and publishes no openingHours.

Each page is fetched as a signed-out visitor fetches it, in uz, ru and en, with a value no environment file holds, so
only the configured value can pass. The retired row is put in the database, as production has it, to prove nothing
reads it any more.
"""

import pytest

from business_app.models.translation import Translation
from business_app.utils.opening_hours import LINE_KEY
from scripts.seed_web_ui_shell_translations import TRANSLATIONS as SHELL
from tests.integration.public_page_builders import LANGS, flat, get_page, jsonld, seed_copy, shipped

pytestmark = pytest.mark.integration

OPENING_HOURS = "Mo-Fr 08:30-17:30, Sa 10:00-14:00"
PUBLISHED = ["Mo-Fr 08:30-17:30", "Sa 10:00-14:00"]
HOURS_TEXT = {  # the value as visitors read it, day names from web.display_labels.weekday_short.*
    "en": "Mon–Fri 08:30–17:30; Sat 10:00–14:00",
    "ru": "пн–пт 08:30–17:30; сб 10:00–14:00",
    "uz": "Du–Ju 08:30–17:30; Sh 10:00–14:00",
}
# Orders are taken around the clock in the bot and on the site (owner, 2026-11-14); delivery stays "every day".
ORDERS_KEY = "web.shell.footer.orders_hours"
# What production's database holds for the retired row; no page may print it.
RETIRED = {"en": "Mon - Sat: 9AM - 6PM.", "ru": "Пн - Сб: 9:00 - 18:00.", "uz": "Dush - Shan: 9:00 - 18:00."}


def _office_line(lang: str) -> str:
    return flat(SHELL[lang][LINE_KEY].format(hours=HOURS_TEXT[lang]))


def _lines(lang: str) -> list:
    """Office hours, then orders around the clock, then the delivery line, as the footer and /contact print them."""
    return [_office_line(lang), flat(SHELL[lang][ORDERS_KEY]), flat(shipped("landing.footer.delivery_hours", lang))]


@pytest.fixture
def hours_copy(app, db, monkeypatch):
    """The seeds' rows, the retired hours row production still has, and a street address so the Store JSON-LD shows."""
    monkeypatch.setitem(app.config, "COMPANY_OPENING_HOURS", OPENING_HOURS)
    monkeypatch.setitem(app.config, "COMPANY_STREET_ADDRESS", "Office hours test street 1")
    seed_copy(db)  # web.display_labels.* (day names) and landing.* (the delivery line)
    present = set(db.session.query(Translation.key, Translation.language).all())
    rows = [(key, language, SHELL[language][key], "web") for key in (LINE_KEY, ORDERS_KEY) for language in LANGS]
    rows += [("landing.footer.business_hours", language, value, "landing") for language, value in RETIRED.items()]
    for key, language, value, category in rows:
        if (key, language) not in present:
            db.session.add(Translation(key=key, language=language, value=value, category=category, is_active=True))
    db.session.commit()


def _footer_hours(soup) -> list:
    return [flat(li.get_text(" ")) for li in soup.select("footer .ae-footer__hours li")]


def _store(soup) -> dict:
    [store] = jsonld(soup, "Store")
    return store


@pytest.mark.parametrize("lang", LANGS)
def test_the_footer_prints_the_configured_office_hours_above_the_delivery_line(app, db, hours_copy, lang):
    soup = get_page(app, "/", lang)
    box = soup.select_one("footer .ae-footer__hours")
    assert _footer_hours(soup) == _lines(lang)
    assert box.select_one("li[data-office-hours]") is not None
    assert box.select_one("li[data-orders-hours]") is not None
    assert _store(soup)["openingHours"] == PUBLISHED  # the same value, in schema.org text
    html = str(soup)
    assert not [text for text in RETIRED.values() if text in html]
    # Round the clock is said of ordering only, never of delivery.
    assert "24/7" not in flat(soup.select_one("footer").get_text(" "))
    delivery = flat(shipped("landing.footer.delivery_hours", lang)).lower()
    assert not [word for word in ("24/7", "around the clock", "круглосуточ", "kecha-kunduz") if word in delivery]


@pytest.mark.parametrize("lang", LANGS)
def test_contact_prints_the_footers_own_lines(app, db, hours_copy, lang):
    soup = get_page(app, "/contact", lang)
    lines = [flat(li.get_text(" ")) for li in soup.select("main [data-contact-hours] li")]
    assert lines == _footer_hours(soup) == _lines(lang)
    assert not [text for text in RETIRED.values() if text in str(soup)]


def test_the_owners_value_reads_as_the_office_hours_in_russian(app, db, hours_copy, monkeypatch):
    monkeypatch.setitem(app.config, "COMPANY_OPENING_HOURS", "Mo-Sa 09:00-18:00")
    soup = get_page(app, "/", "ru")
    assert _footer_hours(soup)[0] == "Часы работы офиса: пн–сб 09:00–18:00"
    assert _store(soup)["openingHours"] == ["Mo-Sa 09:00-18:00"]


@pytest.mark.parametrize(
    "path, lang, line",
    [
        ("/", "en", "Office hours: Tue, Thu 16:00–20:00; Sat 10:00–14:00"),
        ("/", "ru", "Часы работы офиса: вт, чт 16:00–20:00; сб 10:00–14:00"),
        ("/contact", "uz", "Ofis ish vaqti: Se, Pa 16:00–20:00; Sh 10:00–14:00"),
    ],
)
def test_a_day_list_keeps_every_day_in_the_footer_and_the_json_ld(app, db, hours_copy, monkeypatch, path, lang, line):
    """schema.org's "Tu,Th 16:00-20:00" is one spec: the comma there separates days, not specs. Splitting on it
    printed and published "Thu 16:00–20:00" alone, a valid-looking schedule with Tuesday missing."""
    monkeypatch.setitem(app.config, "COMPANY_OPENING_HOURS", "Tu,Th 16:00-20:00, Sa 10:00-14:00")
    soup = get_page(app, path, lang)
    assert _footer_hours(soup)[0] == line
    assert _store(soup)["openingHours"] == ["Tu,Th 16:00-20:00", "Sa 10:00-14:00"]
    if path == "/contact":
        assert flat(soup.select_one("main [data-contact-hours] li").get_text(" ")) == line


@pytest.mark.parametrize("value", ["", "Mon-Sat 9-18", "Mo-Sa", "Tx,Th 16:00-20:00"])
@pytest.mark.parametrize("path", ["/", "/contact"])
def test_an_unset_or_unreadable_value_prints_no_office_line_and_publishes_no_hours(
    app, db, hours_copy, monkeypatch, value, path
):
    monkeypatch.setitem(app.config, "COMPANY_OPENING_HOURS", value)
    soup = get_page(app, path, "en")
    remaining = _lines("en")[1:]  # orders and delivery still print; only the office line goes
    assert _footer_hours(soup) == remaining
    assert soup.select("[data-office-hours]") == []
    assert "openingHours" not in _store(soup)
    if path == "/contact":
        assert [flat(li.get_text(" ")) for li in soup.select("main [data-contact-hours] li")] == remaining
