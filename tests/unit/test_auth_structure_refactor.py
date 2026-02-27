"""Static regression checks for auth API/service structure boundaries."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
AUTH_API_FILE = ROOT / "business_app" / "api" / "auth.py"


def _extract_function_block(text: str, function_name: str) -> str:
    start = text.index(f"def {function_name}(")
    next_route = text.find("\n\n@auth_bp.route", start)
    if next_route == -1:
        next_route = len(text)
    return text[start:next_route]


def test_auth_api_profile_address_and_merge_routes_delegate_to_auth_service():
    text = AUTH_API_FILE.read_text(encoding="utf-8")

    assert "get_auth_service().get_user_profile_data(" in text
    assert "get_auth_service().get_user_addresses(" in text
    assert "get_auth_service().add_user_address(" in text
    assert "get_auth_service().update_user_address(" in text
    assert "get_auth_service().delete_user_address(" in text
    assert "get_auth_service().set_default_user_address(" in text
    assert "get_auth_service().update_user_profile_data(" in text
    assert "get_auth_service().link_telegram_account(" in text
    assert "get_auth_service().link_web_account(" in text
    assert "get_auth_service().check_phone_availability_for_telegram(" in text
    assert "get_auth_service().send_phone_link_otp(" in text
    assert "get_auth_service().verify_phone_link_and_merge_accounts(" in text


def test_auth_api_targeted_blocks_no_longer_do_direct_query_or_commit():
    text = AUTH_API_FILE.read_text(encoding="utf-8")
    functions = [
        "get_profile",
        "get_user_addresses",
        "add_user_address",
        "update_user_address",
        "delete_user_address",
        "set_default_address",
        "update_profile",
        "link_telegram",
        "link_web_account",
        "check_phone_availability",
        "link_phone_send_otp",
        "link_phone_verify",
    ]

    for function_name in functions:
        block = _extract_function_block(text, function_name)
        assert ".query" not in block, function_name
        assert "db.session" not in block, function_name
        assert "from business_app.models" not in block, function_name
