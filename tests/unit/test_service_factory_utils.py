"""Unit tests for service factory and service health utilities."""

import pytest
from flask import Flask, g

from business_app.utils.service_factory import (
    ServiceFactory,
    ServiceHealthChecker,
    init_service_factory,
    inject_services,
)


class DummyService:
    instances = 0

    def __init__(self):
        DummyService.instances += 1


class BrokenService:
    def __init__(self):
        raise RuntimeError("failed to initialize")


class HealthService:
    def __init__(self, redis_client=None):
        self.redis_client = redis_client

    def health_check(self):
        return {"custom": "ok"}


class _RedisOk:
    def ping(self):
        return True


class _RedisBad:
    def ping(self):
        raise RuntimeError("redis down")


@pytest.fixture
def flask_app():
    app = Flask(__name__)
    app.config["TESTING"] = True
    return app


@pytest.mark.unit
class TestServiceFactoryCore:
    def test_get_service_caches_in_request_context(self, flask_app):
        with flask_app.app_context():
            ServiceFactory.clear_singleton_services()
            first = ServiceFactory.get_service(DummyService, "dummy_service")
            second = ServiceFactory.get_service(DummyService, "dummy_service")

            assert first is second
            assert hasattr(g, "dummy_service")

    def test_get_service_creation_error_propagates(self, flask_app):
        with flask_app.app_context():
            with pytest.raises(RuntimeError):
                ServiceFactory.get_service(BrokenService, "broken_service")

    def test_singleton_lifecycle(self):
        ServiceFactory.clear_singleton_services()
        a = ServiceFactory.get_singleton_service(DummyService, "singleton_dummy")
        b = ServiceFactory.get_singleton_service(DummyService, "singleton_dummy")
        assert a is b

        ServiceFactory.clear_singleton_services()
        c = ServiceFactory.get_singleton_service(DummyService, "singleton_dummy")
        assert c is not a

    def test_clear_request_services(self, flask_app):
        with flask_app.app_context():
            g.auth_service = object()
            g.order_service = object()
            g.not_service_attr = object()

            ServiceFactory.clear_request_services()

            assert not hasattr(g, "auth_service")
            assert not hasattr(g, "order_service")
            assert hasattr(g, "not_service_attr")


@pytest.mark.unit
class TestInjectServicesAndHealth:
    def test_inject_services_decorator(self, monkeypatch):
        monkeypatch.setattr("business_app.utils.service_factory.get_auth_service", lambda: "AUTH")
        monkeypatch.setattr("business_app.utils.service_factory.get_cart_service", lambda: "CART")

        @inject_services("auth_service", "cart_service")
        def _handler(value, auth_service=None, cart_service=None):
            return value, auth_service, cart_service

        value, auth_service, cart_service = _handler(10)
        assert value == 10
        assert auth_service == "AUTH"
        assert cart_service == "CART"

        _, auth_override, _ = _handler(20, auth_service="OVERRIDE")
        assert auth_override == "OVERRIDE"

    def test_service_health_checker(self, flask_app):
        with flask_app.app_context():
            healthy = ServiceHealthChecker.check_service_health(HealthService(redis_client=_RedisOk()))
            assert healthy["checks"]["custom"] == "ok"

            unhealthy = ServiceHealthChecker.check_service_health(HealthService(redis_client=_RedisBad()))
            assert unhealthy["healthy"] is False
            assert any("Redis connection failed" in err for err in unhealthy["errors"])

            class PlainComponent:
                pass

            plain_result = ServiceHealthChecker.check_service_health(PlainComponent())
            assert plain_result["healthy"] is True

    def test_check_all_services_health_and_init_cleanup(self, flask_app):
        with flask_app.app_context():
            ServiceFactory.clear_singleton_services()
            g.sample_service = HealthService(redis_client=_RedisOk())
            ServiceFactory._instances["singleton_service"] = HealthService(redis_client=_RedisOk())

            all_health = ServiceHealthChecker.check_all_services_health()
            assert "sample_service" in all_health
            assert "singleton_service" in all_health

            init_service_factory(flask_app)
            assert any(func.__name__ == "cleanup_services" for func in flask_app.teardown_appcontext_funcs)

            ServiceFactory.clear_singleton_services()
