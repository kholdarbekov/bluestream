"""TimezoneMiddleware leaves a downloaded attachment as the route wrote it (review EXP-1, 2026-11-14).

The middleware presents a JSON answer's date-time fields (`created_at`, `paid_at`, ...) in the viewer's timezone for
the page that reads it. A file the browser saves (`Content-Disposition: attachment`: the cabinet's account data
download, the admin translations export, a support-inbox document) is not such an answer: rewriting it shifted only the
fields the middleware knows by name and re-dumped the whole file on one line with \\u escapes.
"""

import json

import pytest
from flask import Response, g

from business_app.middleware.timezone_middleware import TimezoneMiddleware

pytestmark = pytest.mark.unit

BODY = json.dumps({"created_at": "2026-11-02T09:30:00+00:00", "name": "Офис"}, indent=2, ensure_ascii=False)


def _after_request(app, disposition=None):
    headers = {"Content-Disposition": disposition} if disposition else {}
    response = Response(BODY, mimetype="application/json", headers=headers)
    with app.test_request_context("/api/v1/auth/export-data", method="POST"):
        g.user_timezone = None  # the configured display timezone (Asia/Tashkent)
        return TimezoneMiddleware().after_request(response)


@pytest.mark.parametrize(
    "disposition",
    [
        "attachment; filename=account-data-7.json",  # auth.export_account_data
        "attachment;filename=translations.json",  # admin translations export
        'Attachment; filename="note.json"',  # the disposition type is case-insensitive (RFC 6266)
    ],
)
def test_an_attachment_keeps_the_routes_own_bytes(app, disposition):
    response = _after_request(app, disposition)

    assert response.get_data(as_text=True) == BODY


def test_a_json_answer_is_still_presented_in_the_viewers_timezone(app):
    """Control: the same body as an ordinary JSON answer is converted, as every API page expects."""
    response = _after_request(app)

    assert json.loads(response.get_data(as_text=True))["created_at"] == "2026-11-02T14:30:00+05:00"


def test_an_inline_json_answer_is_still_presented_in_the_viewers_timezone(app):
    response = _after_request(app, 'inline; filename="x.json"')

    assert json.loads(response.get_data(as_text=True))["created_at"] == "2026-11-02T14:30:00+05:00"
