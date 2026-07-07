import hmac, hashlib, json


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_telegram_login_without_signature_is_rejected(client):
    resp = client.post("/api/v1/auth/telegram-login", json={"telegram_id": 123456789})
    assert resp.status_code == 401


def test_telegram_login_with_wrong_signature_is_rejected(app, client):
    body = json.dumps({"telegram_id": 123456789}).encode()
    sig = _sign("not-the-real-secret", body)
    resp = client.post(
        "/api/v1/auth/telegram-login",
        data=body,
        headers={"Content-Type": "application/json", "X-Bot-Webhook-Signature": sig},
    )
    assert resp.status_code == 401


def test_telegram_login_with_valid_signature_passes_signature_gate(app, client):
    secret = app.config["BOT_WEBHOOK_SECRET"]
    body = json.dumps({"telegram_id": 123456789}).encode()
    sig = _sign(secret, body)
    resp = client.post(
        "/api/v1/auth/telegram-login",
        data=body,
        headers={"Content-Type": "application/json", "X-Bot-Webhook-Signature": sig},
    )
    # Past the signature gate: never the "Invalid signature" 401.
    assert resp.status_code != 401 or resp.get_json().get("message") != "Invalid signature"


def test_staff_login_without_signature_is_rejected(client):
    resp = client.post("/api/v1/staff/auth/login", json={"telegram_id": 555})
    assert resp.status_code == 401
