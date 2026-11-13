import re
from pathlib import Path

import pytest

import business_app
from tests.unit.test_nginx_telegram_login_logging import SITES, _location

pytestmark = pytest.mark.unit

STATIC = Path(business_app.__file__).parent / "static"


def _key_files():
    return [path for path in STATIC.glob("*.txt") if re.fullmatch(r"[0-9a-f]{32}", path.stem)]


def test_one_key_file_whose_name_body_and_nginx_location_agree():
    [key_file] = _key_files()
    assert key_file.read_text(encoding="utf-8").strip() == key_file.stem
    location = _location("aqua-element.conf", "aqua-element.uz", f"/{key_file.name}")
    assert location == [f"alias /var/www/static/{key_file.name};"]  # nothing else: logged, no add_header


def test_the_site_file_names_no_other_key_shaped_file():
    [key_file] = _key_files()
    text = (SITES / "aqua-element.conf").read_text(encoding="utf-8")
    assert set(re.findall(r"/([0-9a-f]{32})\.txt", text)) == {key_file.stem}  # a rotation leaves no stale location
