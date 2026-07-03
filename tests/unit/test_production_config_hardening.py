"""Unit tests for ARCH-010 — production refuses to boot on missing/weak secrets."""

from __future__ import annotations

import os
import secrets

import pytest

from business_app.config.production import ProductionConfig


# A few randomly generated strong secrets reused across tests.
_STRONG_A = secrets.token_hex(32)
_STRONG_B = secrets.token_hex(32)


@pytest.fixture
def clean_env(monkeypatch):
    """Start each test with the ARCH-010 variables wiped."""
    for var in (
        'SECRET_KEY', 'JWT_SECRET_KEY',
        'DATABASE_URL', 'REDIS_URL',
        'SENTRY_DSN', 'BREVO_API_KEY',
    ):
        monkeypatch.delenv(var, raising=False)
    yield monkeypatch


def _seed_happy_path(monkeypatch):
    monkeypatch.setenv('SECRET_KEY', _STRONG_A)
    monkeypatch.setenv('JWT_SECRET_KEY', _STRONG_B)
    monkeypatch.setenv('DATABASE_URL', 'postgresql://user:pw@db.internal/bluestream')
    monkeypatch.setenv('REDIS_URL', 'redis://:pw@redis.internal:6379/0')
    monkeypatch.setenv('SENTRY_DSN', 'https://abc@sentry.io/123')
    monkeypatch.setenv('BREVO_API_KEY', 'xkeysib-xxxxxxxx')


def test_required_env_vars_happy_path(clean_env):
    _seed_happy_path(clean_env)
    # Must not raise.
    ProductionConfig.validate_required_env_vars()


def test_required_env_vars_missing_database_url(clean_env):
    _seed_happy_path(clean_env)
    clean_env.delenv('DATABASE_URL')
    with pytest.raises(ValueError, match="DATABASE_URL"):
        ProductionConfig.validate_required_env_vars()


def test_required_env_vars_missing_jwt_secret(clean_env):
    _seed_happy_path(clean_env)
    clean_env.delenv('JWT_SECRET_KEY')
    with pytest.raises(ValueError, match="JWT_SECRET_KEY"):
        ProductionConfig.validate_required_env_vars()


def test_sqla_uri_requires_database_url(clean_env):
    _seed_happy_path(clean_env)
    clean_env.delenv('DATABASE_URL')
    cfg = ProductionConfig()
    with pytest.raises(ValueError, match="DATABASE_URL"):
        _ = cfg.SQLALCHEMY_DATABASE_URI


def test_jwt_secret_has_no_fallback_to_secret_key(clean_env):
    _seed_happy_path(clean_env)
    clean_env.delenv('JWT_SECRET_KEY')
    cfg = ProductionConfig()
    with pytest.raises(ValueError, match="JWT_SECRET_KEY"):
        _ = cfg.JWT_SECRET_KEY


def test_production_secrets_reject_short_secret_key(clean_env):
    _seed_happy_path(clean_env)
    clean_env.setenv('SECRET_KEY', 'too-short')
    with pytest.raises(ValueError, match="SECRET_KEY must be at least 32"):
        ProductionConfig.validate_production_secrets()


def test_production_secrets_reject_weak_placeholder(clean_env):
    _seed_happy_path(clean_env)
    clean_env.setenv('SECRET_KEY', 'dev-secret-key-change-in-production' + 'x' * 10)
    with pytest.raises(ValueError, match="known-weak placeholder"):
        ProductionConfig.validate_production_secrets()


def test_production_secrets_reject_jwt_equal_to_secret(clean_env):
    _seed_happy_path(clean_env)
    shared = _STRONG_A
    clean_env.setenv('SECRET_KEY', shared)
    clean_env.setenv('JWT_SECRET_KEY', shared)
    with pytest.raises(ValueError, match="must differ from SECRET_KEY"):
        ProductionConfig.validate_production_secrets()


def test_production_secrets_accept_strong_distinct_pair(clean_env):
    _seed_happy_path(clean_env)
    # Happy path: no exception.
    ProductionConfig.validate_production_secrets()
