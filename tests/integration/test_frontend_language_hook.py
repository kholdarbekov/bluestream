"""A `?lang=uz` link sticks (D25, spec 2026-10-27 §11).

Uzbek is the site default, so the language hook drops `?lang=uz` from a frontend URL with a 301. It used
to drop it without remembering it, so a visitor whose session said Russian (or whose browser prefers it)
was bounced to `?lang=ru` on the next request. The 301 now saves the link's language to the session first.
"""

import pytest

from business_app import _remember_frontend_language

pytestmark = pytest.mark.integration


def _html_lang(response):
    return response.get_data(as_text=True).split('<html lang="', 1)[1].split('"', 1)[0]


def test_a_lang_uz_link_sticks_over_a_russian_session(app, db):
    c = app.test_client()
    c.get("/set-language/ru")

    r = c.get("/about?lang=uz")
    assert r.status_code == 301 and r.headers["Location"].endswith("/about")

    follow = c.get("/about")
    assert follow.status_code == 200
    assert '<html lang="uz"' in follow.get_data(as_text=True)
    with c.session_transaction() as s:
        assert s["language"] == "uz"


def test_a_lang_uz_link_beats_a_russian_browser(app, db):
    c = app.test_client()
    r = c.get("/about?lang=uz", headers={"Accept-Language": "ru"})
    assert r.status_code == 301

    follow = c.get("/about", headers={"Accept-Language": "ru"})
    assert follow.status_code == 200, "no 302 to ?lang=ru"
    assert _html_lang(follow) == "uz"


def test_lang_uz_changes_the_session_only_not_the_stored_language(app, db, sample_user):
    from flask_jwt_extended import create_access_token

    sample_user.preferred_language = "ru"
    db.session.commit()
    with app.app_context():
        token = create_access_token(identity=str(sample_user.id))
    c = app.test_client()
    c.set_cookie("access_token_cookie", token)

    assert c.get("/about?lang=uz", headers={"Authorization": f"Bearer {token}"}).status_code == 301
    db.session.refresh(sample_user)
    assert sample_user.preferred_language == "ru"
    with c.session_transaction() as s:
        assert s["language"] == "uz"


def test_the_canonical_and_hreflang_stay_bare_for_uz(app, db):
    c = app.test_client()
    c.get("/about?lang=uz")
    html = c.get("/about").get_data(as_text=True)
    assert 'rel="canonical" href="' in html
    canonical = html.split('rel="canonical" href="', 1)[1].split('"', 1)[0]
    assert "lang=" not in canonical
    assert canonical.endswith("/about")


def test_the_helper_writes_the_session_once(app):
    with app.test_request_context("/about"):
        from flask import session

        _remember_frontend_language("ru")
        assert session["language"] == "ru" and session.permanent is True
