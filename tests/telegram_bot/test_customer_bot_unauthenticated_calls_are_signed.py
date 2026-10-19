"""Every customer-bot call to an identity route that carries no user token must be signed.

The backend refuses an unsigned request on these routes, so a call that forgets
`sign=True` fails in production while every mocked journey test stays green. This lint walks
`telegram_bot/api_client.py` and flags any `_make_request` to `/api/v1/auth/...` or
`/api/v1/bot/...` that passes neither `user_token=` nor `sign=True`.
"""
import ast
from pathlib import Path

API_CLIENT = Path(__file__).resolve().parents[2] / "telegram_bot" / "api_client.py"
GUARDED_PREFIXES = ("/api/v1/auth/", "/api/v1/bot/")
# The refresh token in the body is itself the credential; there is no JWT to send and the
# backend does not require a signature on this route.
CREDENTIAL_IN_BODY = {"/api/v1/auth/refresh"}


def _path_of(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value if isinstance(part, ast.Constant) else "{}" for part in node.values
        )
    return None


def _unsigned_tokenless_calls() -> list[str]:
    offenders = []
    for node in ast.walk(ast.parse(API_CLIENT.read_text())):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_make_request" and len(node.args) >= 2):
            continue
        path = _path_of(node.args[1])
        if path is None or not path.startswith(GUARDED_PREFIXES) or path in CREDENTIAL_IN_BODY:
            continue
        keywords = {kw.arg: kw.value for kw in node.keywords}
        signed = isinstance(keywords.get("sign"), ast.Constant) and keywords["sign"].value is True
        if "user_token" not in keywords and not signed:
            offenders.append(f"api_client.py:{node.lineno} {path}")
    return offenders


def test_every_tokenless_identity_call_is_signed():
    assert _unsigned_tokenless_calls() == [], (
        "these customer-bot calls send no user token and no signature: "
        f"{_unsigned_tokenless_calls()}"
    )
