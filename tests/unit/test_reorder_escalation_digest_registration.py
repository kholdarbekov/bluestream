"""The escalation digest is a registered email type (spec §9.2, plan Task 22a).

Registered like the reminder type: enum, both default-channel tables, the strict-placeholder set,
and NOT a preference group (a group toggle would look like an opt-out and decide nothing). Its
templates override the footer: no `tel:` link and no dead "update your preferences" line.

Two layers. The renders drive `EmailTemplateService` with the data Task 22b will hand it
(`escalated_count`, `top`, `flag_labels`, `worklist_url`), the flag labels read from the SEEDED
rows. The sends drive the REAL `NotificationService.send_notification` -> `_send_email_notification`
-> file template -> subject -> placeholder guard -> Brevo path, with only `requests.post` faked
(tests/notification_send_fakes.py::live_send_path).
"""

import pytest
from bs4 import BeautifulSoup

from business_app.models.notification import Notification
from business_app.models.reorder_reminder import ESCALATION_FLAGS
from business_app.services.email_template_service import EmailTemplateService
from business_app.services.notification_service import _PLACEHOLDER_STRICT_TYPES, NotificationService
from business_app.utils.constants import NotificationChannel, NotificationType
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS
from tests.notification_send_fakes import BREVO_URL, live_send_path

# The copy guard's own "asks the customer to call" pattern, so the digest is held to the same one.
from tests.unit.test_reorder_reminder_copy_is_seeded import _PHONE_PROMPT

LANGUAGES = ("uz", "ru", "en")
COMPANY = "Aqua Element"
BOT_URL = "https://t.me/aqua_digest_test_bot"
WORKLIST_URL = "https://admin.aqua-element.uz/lapsed-customers"
MARKERS = {"uz": "Yo'qolgan mijozlar", "ru": "Пропавшие клиенты", "en": "Lapsed customers"}
# Subject for `escalated_count = 7` and the company above, word for word (spec §9.2).
SUBJECTS = {
    "uz": "Yo'qolgan mijozlar: 7 ta yangi - Aqua Element",
    "ru": "Пропавшие клиенты (новых: 7) - Aqua Element",
    "en": "Lapsed customers: 7 new to follow up - Aqua Element",
}
# The footer sentence the BASE layout prints, which the digest replaces.
BASE_FOOTER_LINE = {
    "uz": "Agar siz bu xabarni olishni xohlamasangiz, sozlamalarni o'zgartiring.",
    "ru": "Если вы не хотите получать такие письма, измените настройки.",
    "en": "If you no longer wish to receive these emails, please update your preferences.",
}


def _flag_labels():
    """`{language: {flag: label}}` for EVERY escalation flag, from the seeded rows (the one copy
    the admin UI page and the digest share)."""
    return {
        language: {flag: BACKEND_TRANSLATIONS[f"ui.lapsed.flag.{flag}"][language] for flag in ESCALATION_FLAGS}
        for language in LANGUAGES
    }


def _data(**overrides):
    data = {
        "escalated_count": 7,
        "top": [
            {"name": "Alisher Mijoz", "days_overdue": 44, "flag": "long_lapsed"},
            {"name": "Mirzo Market", "days_overdue": 3, "flag": "stuck_order"},
        ],
        "flag_labels": _flag_labels(),
        "worklist_url": WORKLIST_URL,
    }
    data.update(overrides)
    return data


def _rows(html):
    """The table body as the reader sees it: `[[customer, days, reason], ...]`, entities decoded."""
    soup = BeautifulSoup(html, "html.parser")
    return [
        [cell.get_text(strip=True) for cell in row.find_all("td")]
        for row in soup.select("table.details-table tr")
        if row.find("td")
    ]


def _text(html):
    """The visible text only: no `<style>`, so a CSS word can never match a copy guard."""
    soup = BeautifulSoup(html, "html.parser")
    for style in soup.find_all("style"):
        style.decompose()
    return soup.get_text(" ", strip=True)


@pytest.fixture
def company(app, monkeypatch):
    monkeypatch.setitem(app.config, "COMPANY_NAME", COMPANY)


@pytest.fixture
def live_send(app, monkeypatch):
    """The production send path with only `requests.post` faked. Yields the fake."""
    with live_send_path(app, monkeypatch, bot_url=BOT_URL) as post:
        yield post


def _send(user, **data_overrides):
    return NotificationService().send_notification(
        user.id, NotificationType.REORDER_ESCALATION_DIGEST, template_data=_data(**data_overrides)
    )


def test_the_type_is_registered_like_the_reminder_type(db, sample_user):
    digest = NotificationType.REORDER_ESCALATION_DIGEST
    service = NotificationService()

    assert digest.value == "reorder_escalation_digest"
    assert digest.value in _PLACEHOLDER_STRICT_TYPES
    assert service._default_channels_for_type(digest.value) == [NotificationChannel.EMAIL]
    assert service._get_user_preferred_channels(sample_user.id, digest) == [NotificationChannel.EMAIL]
    # No group: a group toggle would be an opt-out that decides nothing (spec §7.4/§9.2).
    assert all(digest.value not in types for types in NotificationService.NOTIFICATION_TYPE_GROUPS.values())


def test_the_subject_names_the_count_and_the_company_in_every_language(app, company):
    subjects = EmailTemplateService.EMAIL_SUBJECTS["reorder_escalation_digest"]

    assert set(subjects) == {"uz", "ru", "en"}
    service = EmailTemplateService()
    for language in LANGUAGES:
        assert service.get_subject("reorder_escalation_digest", language, {"escalated_count": 7}) == SUBJECTS[language]
        assert not _PHONE_PROMPT.search(subjects[language])


@pytest.mark.parametrize("count", [1, 2, 5, 11, 21, 22])
def test_the_subject_reads_right_for_one_escalation_and_for_many(app, company, count):
    """One escalation is the commonest morning, so the subject must read right at every count.
    Russian puts the number in a label ("новых: N") because a word AFTER it would have to agree
    with it ("1 новый", "2 новых", "5 новых"); uz has no agreement after "ta", and the English
    "N new to follow up" carries no noun to inflect."""
    service = EmailTemplateService()

    assert {
        language: service.get_subject("reorder_escalation_digest", language, {"escalated_count": count})
        for language in LANGUAGES
    } == {
        "uz": f"Yo'qolgan mijozlar: {count} ta yangi - Aqua Element",
        "ru": f"Пропавшие клиенты (новых: {count}) - Aqua Element",
        "en": f"Lapsed customers: {count} new to follow up - Aqua Element",
    }


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_digest_renders_in_each_language_with_the_overridden_footer(app, company, language):
    with app.app_context():
        rendered = EmailTemplateService().render_notification_email("reorder_escalation_digest", language, _data())

    assert rendered is not None, f"reorder_escalation_digest/{language} template missing"
    subject, html = rendered["subject"], rendered["content"]
    assert subject == SUBJECTS[language]
    assert MARKERS[language] in html
    assert f'<html lang="{language}">' in html
    assert html.count(WORKLIST_URL) == 1
    # The rows, as the reader sees them: customer, days overdue, the reason in THIS language.
    labels = _flag_labels()[language]
    assert _rows(html) == [
        ["Alisher Mijoz", "44", labels["long_lapsed"]],
        ["Mirzo Market", "3", labels["stuck_order"]],
    ]
    assert "<strong>7</strong>" in html
    # The footer is the digest's own: the base layout's phone link and its dead preferences line
    # are gone (nobody can opt out of this type), and no copy asks anyone to call.
    assert "tel:" not in html
    assert BASE_FOOTER_LINE[language] not in html
    assert not _PHONE_PROMPT.search(_text(html)), _PHONE_PROMPT.search(_text(html))
    assert NotificationService._unrendered_placeholders(subject + html) == []
    assert "None" not in html


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("flag", ESCALATION_FLAGS)
def test_every_escalation_flag_prints_its_label_in_every_language(app, company, language, flag):
    """The digest names the reason in words, never the raw code, for every flag the engine can
    write (`ESCALATION_FLAGS`, the tuple the DB CHECK is built from)."""
    top = [{"name": "Alisher Mijoz", "days_overdue": 12, "flag": flag}]
    with app.app_context():
        rendered = EmailTemplateService().render_notification_email(
            "reorder_escalation_digest", language, _data(top=top)
        )

    expected = BACKEND_TRANSLATIONS[f"ui.lapsed.flag.{flag}"][language]
    assert _rows(rendered["content"]) == [["Alisher Mijoz", "12", expected]]
    assert expected != flag


def test_an_empty_top_and_missing_labels_still_render_without_none(app, company):
    with app.app_context():
        no_top = EmailTemplateService().render_notification_email("reorder_escalation_digest", "en", _data(top=[]))
        no_labels = EmailTemplateService().render_notification_email(
            "reorder_escalation_digest", "en", _data(flag_labels=None)
        )

    assert _rows(no_top["content"]) == []
    assert WORKLIST_URL in no_top["content"]
    # No label for a flag falls back to the flag's own code, never a blank or "None".
    assert _rows(no_labels["content"])[0][2] == "long_lapsed"
    assert "None" not in no_top["content"] + no_labels["content"]


def test_every_flag_label_is_seeded_trilingual():
    for flag in ESCALATION_FLAGS:
        key = f"ui.lapsed.flag.{flag}"
        assert key in BACKEND_TRANSLATIONS, key
        assert all(BACKEND_TRANSLATIONS[key].get(language) for language in LANGUAGES), key
        for language in LANGUAGES:
            label = BACKEND_TRANSLATIONS[key][language]
            assert NotificationService._unrendered_placeholders(label) == [], (key, language)
            assert not _PHONE_PROMPT.search(label), (key, language)


@pytest.mark.parametrize(
    "stored, language",
    [
        ("uz", "uz"),
        ("ru", "ru"),
        ("en", "en"),
        ("ru-RU", "ru"),
        ("uz_UZ", "uz"),
        ("EN", "en"),
        (None, "en"),
        ("", "en"),
        ("de", "en"),
    ],
)
def test_a_recipient_gets_the_digest_in_their_own_language(db, live_send, admin_user, stored, language):
    """D15 through the real send path. The recipient's stored `preferred_language` is read as it
    is stored; the ONE rule (`EmailTemplateService.resolve_language`) turns it into uz/ru/en, so
    the subject, the body and the reason labels agree, with no raw key or `{placeholder}`."""
    admin_user.preferred_language = stored
    db.session.commit()

    results = _send(admin_user)

    assert results["email"]["success"] is True
    live_send.assert_called_once()
    assert live_send.call_args.args == (BREVO_URL,)
    sent = live_send.call_args.kwargs["json"]
    assert sent["to"] == [{"email": "admin@example.com", "name": "Admin User"}]
    assert sent["subject"] == SUBJECTS[language]
    labels = _flag_labels()[language]
    assert _rows(sent["htmlContent"]) == [
        ["Alisher Mijoz", "44", labels["long_lapsed"]],
        ["Mirzo Market", "3", labels["stuck_order"]],
    ]
    assert MARKERS[language] in sent["htmlContent"]
    assert "headers" not in sent  # no unsubscribe header: nobody can opt out of this type
    (row,) = Notification.query.filter_by(notification_type="reorder_escalation_digest").all()
    assert row.user_id == admin_user.id


def test_a_leftover_placeholder_skips_the_send(db, live_send, admin_user):
    """The type is placeholder-strict: a digest whose count never arrived (the subject keeps its
    literal `{escalated_count}`) is dropped, never mailed to the staff."""
    data = _data()
    del data["escalated_count"]

    results = NotificationService().send_notification(
        admin_user.id, NotificationType.REORDER_ESCALATION_DIGEST, template_data=data
    )

    assert results["email"]["success"] is False
    assert results["email"]["skipped"] is True
    assert results["email"]["reason"] == "unrendered_placeholders"
    # `get_subject` returns the whole raw template when a variable is missing, so BOTH of its
    # placeholders are still literal.
    assert results["email"]["placeholders"] == ["escalated_count", "company_name"]
    live_send.assert_not_called()


def test_customer_data_is_escaped(db, live_send, admin_user):
    name = '<script>alert("x")</script> & <b>Co</b>'

    _send(admin_user, top=[{"name": name, "days_overdue": 5, "flag": "no_response"}])

    html = live_send.call_args.kwargs["json"]["htmlContent"]
    assert "<script>" not in html and "<b>Co</b>" not in html
    assert "&lt;script&gt;" in html
    # The reader still sees the customer's name exactly as it was typed.
    assert _rows(html)[0][0] == name


def test_the_sent_email_has_no_phone_and_one_link(db, live_send, admin_user, monkeypatch, app):
    monkeypatch.setitem(app.config, "COMPANY_PHONE", "+998712000000")

    _send(admin_user)

    html = live_send.call_args.kwargs["json"]["htmlContent"]
    assert "tel:" not in html
    assert "+998712000000" not in html
    assert html.count(WORKLIST_URL) == 1
    assert [a["href"] for a in BeautifulSoup(html, "html.parser").find_all("a")] == [WORKLIST_URL]
