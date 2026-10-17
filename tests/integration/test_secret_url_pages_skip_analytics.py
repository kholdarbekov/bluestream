"""Pages whose URL carries a live secret never load page-view analytics and are never indexed.

The password-reset email links to /reset-password/<token> and the verification email to
/verify-email?token=<token> (business_app/tasks/notification_tasks.py). GA4 reports the page's full
URL (page_location) and Yandex Metrica reports it too and records the page with Webvisor, so with
either snippet on the page the reset token, which sets a new password for the account, reaches
Google and Yandex. Such pages render with SECRET_URL_PAGE (business_app/frontend/routes.py) and
base.html drops both snippets. The reminder unsubscribe pages, the third case, are pinned in
test_reorder_reminder_unsubscribe_pages.py.

Each page is opened at the exact URL its email sends (no `?lang=`), with both analytics IDs
configured.
"""

import pytest
from bs4 import BeautifulSoup

pytestmark = pytest.mark.integration

TOKEN = "Zk3pQ9vX2mL7rT4wY8nB1cD6"

SECRET_URLS = [
    pytest.param(f"/reset-password/{TOKEN}", id="reset-password email link"),
    pytest.param(f"/reset-password?token={TOKEN}", id="reset-password query form the page also reads"),
    pytest.param(f"/verify-email?token={TOKEN}", id="verify-email email link"),
]


def test_an_ordinary_page_really_renders_both_analytics_snippets(client, db, analytics_configured):
    """The control: with these IDs configured, a page without a secret carries both snippets, so the
    tests below fail if the page loses its exemption rather than pass because analytics is off."""
    html = client.get("/forgot-password?lang=en").get_data(as_text=True)

    assert "googletagmanager.com/gtag/js?id=G-TESTTOKEN1" in html
    assert "mc.yandex.ru/metrika/tag.js" in html and "ym(98765432" in html


@pytest.mark.parametrize("url", SECRET_URLS)
def test_a_page_with_a_secret_in_its_url_loads_no_analytics_and_is_not_indexed(client, db, analytics_configured, url):
    response = client.get(url, follow_redirects=True)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    for marker in analytics_configured:
        assert marker not in html, marker
    robots = BeautifulSoup(html, "html.parser").select('meta[name="robots"]')
    assert len(robots) == 1 and robots[0]["content"] == "noindex,nofollow"
