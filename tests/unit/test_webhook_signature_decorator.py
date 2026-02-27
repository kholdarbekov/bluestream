"""Behavior tests for shared webhook signature decorator."""

import hashlib
import hmac
import json

from flask import Flask, jsonify

from business_app.utils.decorators import verify_webhook_signature


def _signed_headers(payload: dict, secret: str) -> dict:
    body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {"X-Bot-Webhook-Signature": signature}


def _make_app(secret="test-secret") -> Flask:
    app = Flask(__name__)
    app.config["BOT_WEBHOOK_SECRET"] = secret

    @app.route("/hook", methods=["POST"])
    @verify_webhook_signature()
    def hook():
        return jsonify({"success": True}), 200

    return app


def test_verify_webhook_signature_rejects_missing_signature():
    app = _make_app()
    client = app.test_client()

    response = client.post("/hook", json={"action": "reload"})

    assert response.status_code == 401
    assert response.get_json()["message"] == "Missing webhook signature"


def test_verify_webhook_signature_rejects_invalid_signature():
    app = _make_app()
    client = app.test_client()

    response = client.post(
        "/hook",
        json={"action": "reload"},
        headers={"X-Bot-Webhook-Signature": "invalid"},
    )

    assert response.status_code == 401
    assert response.get_json()["message"] == "Invalid signature"


def test_verify_webhook_signature_rejects_missing_secret_config():
    app = Flask(__name__)

    @app.route("/hook", methods=["POST"])
    @verify_webhook_signature()
    def hook_without_secret():
        return jsonify({"success": True}), 200

    client = app.test_client()

    response = client.post(
        "/hook",
        json={"action": "reload"},
        headers={"X-Bot-Webhook-Signature": "abc"},
    )

    assert response.status_code == 500
    assert response.get_json()["message"] == "Webhook not properly configured"


def test_verify_webhook_signature_allows_valid_signature():
    payload = {"action": "reload"}
    secret = "test-secret"
    app = _make_app(secret=secret)
    client = app.test_client()

    response = client.post(
        "/hook",
        data=json.dumps(payload),
        content_type="application/json",
        headers=_signed_headers(payload, secret),
    )

    assert response.status_code == 200
    assert response.get_json()["success"] is True
