"""The delivery-update email prints the arrival line and never asks the customer to phone.

Honest-ETA Phase 1, Task 6. The template data carries `eta_line` (`NotificationService._delivery_eta_line`): "" when
nothing is promised, one sentence, or that sentence, a newline and the attribution line. The row prints it with the
newline as a real line break, and the copy stays escaped. The formula ETA (`estimated_delivery`), which the order path
still stamps into the data, is never printed. The customer's question goes to the bot chat, as the rescheduled notice
sends it, never to the phone. The shared footer keeps its own `tel:` link, which is out of scope here.
"""

import pytest

from business_app.services.email_template_service import EmailTemplateService

LANGUAGES = ["en", "ru", "uz"]
DATA = {
    "order_number": "ORD-2026-0042",
    "user_name": "Test User",
    "delivery_status": "Out for delivery",
    "tracking_code": "TRK-000777",
}
ETA_LINE = "Arriving within about 29 min (by 15:40)\nLive traffic: Google Maps"
# The formula ETA the old row printed as a raw instant.
FORMULA_ETA = "2026-11-03T15:40:00+05:00"
# The label the old rows printed the formula ETA under, in each language.
OLD_ETA_LABELS = ["Estimated Delivery", "Ожидаемая доставка", "Taxminiy yetkazib berish"]
BOT_URL = "https://t.me/aqua_delivery_update_test_bot"
# The sentence before the bot link: the rescheduled notice's wording, word for word.
BOT_SENTENCES = {
    "en": "Questions? Message us in our Telegram bot:",
    "ru": "Есть вопросы? Напишите нам в нашем Telegram-боте:",
    "uz": "Savollaringiz bo'lsa, Telegram botimiz orqali bizga yozing:",
}


@pytest.fixture
def service():
    # Fresh instance (not the singleton) so cached Jinja env/dirs do not leak.
    return EmailTemplateService()


def _body(html):
    """The base layout's body block, without its footer: the shared footer keeps the company's own `tel:` link."""
    start = html.index('class="email-body"')
    return html[start : html.index('class="email-footer"', start)]


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_line_is_printed_when_present(service, language):
    html = service.render_template("delivery_update", language, {**DATA, "eta_line": ETA_LINE})

    assert html is not None
    # One real line break between the two lines. A plain `replace` under autoescape would print `&lt;br&gt;`.
    assert "Arriving within about 29 min (by 15:40)<br>Live traffic: Google Maps" in html
    assert "&lt;br&gt;" not in html

    # The copy itself stays escaped: markup in the line is shown as text, not run as HTML.
    html = service.render_template(
        "delivery_update", language, {**DATA, "eta_line": "<b>Bold</b>\nLive traffic: Google Maps"}
    )

    assert "&lt;b&gt;Bold&lt;/b&gt;<br>Live traffic: Google Maps" in html
    assert "<b>Bold</b>" not in html


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize(
    "eta_data", [{"eta_line": ""}, {"eta_line": None}, {}], ids=["empty-line", "none-line", "absent-line"]
)
def test_no_time_row_without_a_line(service, language, eta_data):
    # The order path still stamps the formula ETA into the data; with no line it must print nowhere.
    html = service.render_template("delivery_update", language, {**DATA, "estimated_delivery": FORMULA_ETA, **eta_data})

    assert html is not None
    for label in OLD_ETA_LABELS:
        assert label not in html
    assert FORMULA_ETA not in html
    assert "None" not in html
    # No empty row either: the line's row is the templates' only colspan="2" cell.
    assert 'colspan="2"' not in html


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_body_never_asks_to_phone(app, service, language):
    html = service.render_template("delivery_update", language, DATA)

    assert html is not None
    assert "tel:" not in _body(html)
    phone = app.config["COMPANY_PHONE"]
    assert phone  # a phone the render could print, so its absence below means something
    assert phone not in _body(html)


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_bot_link_is_offered(app, monkeypatch, service, language):
    monkeypatch.setitem(app.config, "COMPANY_TELEGRAM_BOT_URL", BOT_URL)

    html = service.render_template("delivery_update", language, DATA)

    assert html is not None
    assert f'{BOT_SENTENCES[language]} <a href="{BOT_URL}">{BOT_URL}</a>' in _body(html)
