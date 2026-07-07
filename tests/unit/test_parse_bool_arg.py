import pytest
from business_app.utils.request_helpers import parse_bool_arg


@pytest.mark.parametrize("raw,expected", [
    ("false", False), ("0", False), ("no", False), ("off", False),
    ("true", True), ("1", True), ("yes", True), ("on", True),
    ("True", True), ("FALSE", False),
])
def test_parse_bool_arg_values(app, raw, expected):
    with app.test_request_context(f"/?flag={raw}"):
        assert parse_bool_arg("flag") is expected


def test_parse_bool_arg_absent_returns_default(app):
    with app.test_request_context("/"):
        assert parse_bool_arg("flag") is None
        assert parse_bool_arg("flag", default=False) is False
