"""Submit every public URL to IndexNow (Bing, Yandex). Run on the server after the deploy.

Needs INDEXNOW_ENABLED=true in .env (recreate the container after editing it). A dry run needs no switch: it
prints the host and the URLs and sends nothing. A script piped over stdin takes no arguments, so the dry run is
an environment variable. The stdout carries other lines first (the app-creation banner and JSON log lines), so
both commands are piped through grep:

    docker compose exec -T business_app python - < scripts/indexnow_submit.py \
        | grep -E '^(IndexNow|HTTP |not sent|https?://)'
    docker compose exec -T -e INDEXNOW_DRY_RUN=1 business_app python - < scripts/indexnow_submit.py \
        | grep -E '^(IndexNow|HTTP |not sent|https?://)'

Outcomes (exit code):
    HTTP 200 / HTTP 202   accepted (202: the key is still being validated)       0
    HTTP 403 / 422 / ...  refused (403: key not reachable, 422: URL not on host)  1
    not sent: no key file or a network error                                      1
    IndexNow is switched off (the container was restarted, not recreated,
    after the .env edit)                                                          2
"""

import os
import sys

from flask import current_app

from business_app.services.public_urls_service import indexnow_urls
from business_app.utils.indexnow import ACCEPTED_STATUSES, indexnow_host, submit_urls


def submit_all(*, dry_run: bool) -> int:
    """Print what is sent and send it; the exit code. Runs inside an app context."""
    urls = indexnow_urls()
    print(f"IndexNow: {len(urls)} URLs for host {indexnow_host()}")
    if dry_run:
        print("\n".join(urls))
        return 0
    if not current_app.config.get("INDEXNOW_ENABLED"):
        print("IndexNow is switched off. Set INDEXNOW_ENABLED=true in .env and recreate the container.")
        return 2
    status = submit_urls(urls)
    if status is None:
        print("not sent: no key file or a network error")
        return 1
    print(f"HTTP {status}")
    return 0 if status in ACCEPTED_STATUSES else 1


if __name__ == "__main__":
    from business_app import create_app

    app = create_app()
    with app.app_context():
        sys.exit(submit_all(dry_run=os.environ.get("INDEXNOW_DRY_RUN") == "1"))
