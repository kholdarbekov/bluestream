"""
Centralized service factory for consistent service initialization
"""

import logging
from typing import Dict, Any, Type, TypeVar
from flask import g, current_app
from functools import wraps
import threading

logger = logging.getLogger(__name__)

# Type variable for service classes
T = TypeVar("T")


class ServiceFactory:
    """Factory for creating and managing service instances"""

    _instances: Dict[str, Any] = {}
    _lock = threading.Lock()

    @classmethod
    def get_service(cls, service_class: Type[T], service_name: str = None) -> T:
        """
        Get or create a service instance with proper caching

        Args:
            service_class: The service class to instantiate
            service_name: Optional custom name for the service (defaults to class name)

        Returns:
            Service instance
        """
        if service_name is None:
            service_name = service_class.__name__.lower()

        # Check if service is already cached in request context
        if hasattr(g, service_name):
            return getattr(g, service_name)

        try:
            # Create new service instance
            service_instance = service_class()

            # Cache in request context
            setattr(g, service_name, service_instance)

            logger.debug(f"Created new {service_class.__name__} instance")
            return service_instance

        except Exception as e:
            logger.error(f"Failed to create {service_class.__name__}: {e}")
            raise

    @classmethod
    def get_singleton_service(cls, service_class: Type[T], service_name: str = None) -> T:
        """
        Get or create a singleton service instance (app-level caching)

        Args:
            service_class: The service class to instantiate
            service_name: Optional custom name for the service

        Returns:
            Singleton service instance
        """
        if service_name is None:
            service_name = service_class.__name__.lower()

        with cls._lock:
            if service_name not in cls._instances:
                try:
                    cls._instances[service_name] = service_class()
                    logger.debug(f"Created singleton {service_class.__name__} instance")
                except Exception as e:
                    logger.error(f"Failed to create singleton {service_class.__name__}: {e}")
                    raise

            return cls._instances[service_name]

    @classmethod
    def clear_request_services(cls):
        """Clear all request-scoped services"""
        service_attrs = [attr for attr in dir(g) if attr.endswith("_service") and not attr.startswith("_")]
        for attr in service_attrs:
            if hasattr(g, attr):
                try:
                    delattr(g, attr)
                except AttributeError:
                    # Service was already removed or never existed
                    pass

    @classmethod
    def clear_singleton_services(cls):
        """Clear all singleton services (for testing)"""
        with cls._lock:
            cls._instances.clear()


# Service getter functions for common services
def get_auth_service():
    """Get AuthService instance"""
    from business_app.services.auth_service import AuthService

    return ServiceFactory.get_service(AuthService, "auth_service")


def get_order_service():
    """Get OrderService instance"""
    from business_app.services.order_service import OrderService

    return ServiceFactory.get_service(OrderService, "order_service")


def get_payment_service():
    """Get PaymentService instance"""
    from business_app.services.payment_service import PaymentService

    return ServiceFactory.get_service(PaymentService, "payment_service")


def get_card_token_service():
    """Get CardTokenService instance (reuses PaymentService's instance to share state)."""
    return get_payment_service().card_token_service


def get_cash_collection_service():
    """Get CashCollectionService instance"""
    from business_app.services.cash_collection_service import CashCollectionService

    return ServiceFactory.get_service(CashCollectionService, "cash_collection_service")


def get_driver_reconciliation_service():
    """Get DriverReconciliationService instance"""
    from business_app.services.driver_reconciliation_service import DriverReconciliationService

    return ServiceFactory.get_service(DriverReconciliationService, "driver_reconciliation_service")


def get_delivery_service():
    """Get DeliveryService instance"""
    from business_app.services.delivery_service import DeliveryService

    return ServiceFactory.get_service(DeliveryService, "delivery_service")


def get_notification_service():
    """Get NotificationService instance"""
    from business_app.services.notification_service import NotificationService

    return ServiceFactory.get_service(NotificationService, "notification_service")


def get_support_conversation_service():
    """Get SupportConversationService instance"""
    from business_app.services.support_conversation_service import SupportConversationService

    return ServiceFactory.get_service(SupportConversationService, "support_conversation_service")


def get_loyalty_service():
    """Get LoyaltyService instance"""
    from business_app.services.loyalty_service import LoyaltyService

    return ServiceFactory.get_service(LoyaltyService, "loyalty_service")


def get_analytics_service():
    """Get AnalyticsService instance"""
    from business_app.services.analytics_service import AnalyticsService

    return ServiceFactory.get_service(AnalyticsService, "analytics_service")


def get_subscription_service():
    """Get SubscriptionService instance"""
    from business_app.services.subscription_service import SubscriptionService

    return ServiceFactory.get_service(SubscriptionService, "subscription_service")


def get_file_storage_service():
    """Get FileStorageService instance"""
    from business_app.services.file_storage_service import FileStorageService

    return ServiceFactory.get_service(FileStorageService, "file_storage_service")


def get_maps_service():
    """Get MapsService instance"""
    from business_app.services.maps_service import MapsService

    return ServiceFactory.get_service(MapsService, "maps_service")


def get_inventory_service():
    """Get InventoryService instance"""
    from business_app.services.inventory_service import InventoryService

    return ServiceFactory.get_singleton_service(InventoryService, "inventory_service")


def get_token_service():
    """Get TokenService instance"""
    from business_app.services.token_service import TokenService

    return ServiceFactory.get_singleton_service(TokenService, "token_service")


def get_product_service():
    """Get ProductService instance"""
    from business_app.services.product_service import ProductService

    return ServiceFactory.get_service(ProductService, "product_service")


def get_review_service():
    """Get ReviewService instance"""
    from business_app.services.review_service import ReviewService

    return ServiceFactory.get_service(ReviewService, "review_service")


def get_cart_service():
    """Get CartService instance"""
    from business_app.services.cart_service import CartService

    return ServiceFactory.get_service(CartService, "cart_service")


def get_corporate_contract_service():
    """Get CorporateContractService instance"""
    from business_app.services.corporate_contract_service import CorporateContractService

    return ServiceFactory.get_service(CorporateContractService, "corporate_contract_service")


# Decorator for automatic service injection
def inject_services(*service_names):
    """
    Decorator to automatically inject services into function arguments

    Usage:
        @inject_services('auth_service', 'order_service')
        def my_function(user_id, auth_service, order_service):
            # Services are automatically injected
            pass
    """

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Service mapping
            service_map = {
                "auth_service": get_auth_service,
                "order_service": get_order_service,
                "payment_service": get_payment_service,
                "cash_collection_service": get_cash_collection_service,
                "driver_reconciliation_service": get_driver_reconciliation_service,
                "delivery_service": get_delivery_service,
                "notification_service": get_notification_service,
                "loyalty_service": get_loyalty_service,
                "analytics_service": get_analytics_service,
                "subscription_service": get_subscription_service,
                "file_storage_service": get_file_storage_service,
                "maps_service": get_maps_service,
                "inventory_service": get_inventory_service,
                "token_service": get_token_service,
                "product_service": get_product_service,
                "review_service": get_review_service,
                "cart_service": get_cart_service,
                "corporate_contract_service": get_corporate_contract_service,
            }

            # Inject requested services
            for service_name in service_names:
                if service_name in service_map and service_name not in kwargs:
                    kwargs[service_name] = service_map[service_name]()

            return func(*args, **kwargs)

        return wrapper

    return decorator


class ServiceHealthChecker:
    """Health checker for services"""

    @staticmethod
    def check_service_health(service_instance: Any) -> Dict[str, Any]:
        """
        Check health of a service instance

        Args:
            service_instance: The service instance to check

        Returns:
            Health status dictionary
        """
        service_name = service_instance.__class__.__name__
        health_status = {"service": service_name, "healthy": True, "checks": {}, "errors": []}

        try:
            # Check if service has a health_check method
            if hasattr(service_instance, "health_check"):
                health_result = service_instance.health_check()
                health_status["checks"].update(health_result)

            # Check Redis connectivity for services that use it
            if hasattr(service_instance, "redis_client"):
                try:
                    if service_instance.redis_client:
                        service_instance.redis_client.ping()
                        health_status["checks"]["redis"] = "healthy"
                    else:
                        health_status["checks"]["redis"] = "not_configured"
                except Exception as e:
                    health_status["checks"]["redis"] = "unhealthy"
                    health_status["errors"].append(f"Redis connection failed: {e}")
                    health_status["healthy"] = False

            # Check database connectivity for services that use it
            if hasattr(service_instance, "db") or "Service" in service_name:
                try:
                    from business_app import db

                    db.session.execute("SELECT 1")
                    health_status["checks"]["database"] = "healthy"
                except Exception as e:
                    health_status["checks"]["database"] = "unhealthy"
                    health_status["errors"].append(f"Database connection failed: {e}")
                    health_status["healthy"] = False

        except Exception as e:
            health_status["healthy"] = False
            health_status["errors"].append(f"Health check failed: {e}")

        return health_status

    @staticmethod
    def check_all_services_health() -> Dict[str, Dict[str, Any]]:
        """Check health of all initialized services"""
        health_results = {}

        # Check request-scoped services
        for attr_name in dir(g):
            if not attr_name.startswith("_") and attr_name.endswith("_service"):
                service_instance = getattr(g, attr_name)
                health_results[attr_name] = ServiceHealthChecker.check_service_health(service_instance)

        # Check singleton services
        for service_name, service_instance in ServiceFactory._instances.items():
            health_results[service_name] = ServiceHealthChecker.check_service_health(service_instance)

        return health_results


def _is_loggable_teardown_exception(exception):
    """Whether a teardown exception is worth logging as a request error.

    Celery control-flow signals (Retry / Ignore / Reject) propagate through the
    Flask app-context teardown when a task reschedules or aborts itself. They are
    control flow, not failures, so logging them as "Request ended with
    exception" is misleading noise (e.g. ``auto_assign_delivery_task``'s benign
    900s no-driver back-off). Real exceptions still log.
    """
    if exception is None:
        return False
    try:
        from celery.exceptions import Ignore, Reject, Retry
    except ImportError:  # celery unavailable in some contexts — log normally
        return True
    return not isinstance(exception, (Retry, Ignore, Reject))


# Flask integration
def init_service_factory(app):
    """Initialize service factory with Flask app"""

    @app.teardown_appcontext
    def cleanup_services(exception):
        """Clean up request-scoped services"""
        if _is_loggable_teardown_exception(exception):
            logger.warning(f"Request ended with exception: {exception}")
        ServiceFactory.clear_request_services()

    @app.route("/api/services/health")
    def services_health_check():
        """Health check endpoint for all services"""
        from flask import jsonify

        try:
            health_results = ServiceHealthChecker.check_all_services_health()
            overall_health = all(result["healthy"] for result in health_results.values())

            return jsonify(
                {
                    "overall_health": overall_health,
                    "services": health_results,
                    "timestamp": current_app.config.get("HEALTH_CHECK_TIMESTAMP", "N/A"),
                }
            ), (200 if overall_health else 503)

        except Exception as e:
            logger.error(f"Service health check failed: {e}")
            return jsonify({"overall_health": False, "error": str(e), "services": {}}), 500


def get_service_metrics():
    """Get metrics about service usage"""
    metrics = {
        "request_services": len([attr for attr in dir(g) if attr.endswith("_service")]),
        "singleton_services": len(ServiceFactory._instances),
        "available_services": [
            "auth_service",
            "order_service",
            "payment_service",
            "delivery_service",
            "notification_service",
            "loyalty_service",
            "analytics_service",
            "subscription_service",
            "file_storage_service",
            "maps_service",
            "inventory_service",
            "token_service",
            "product_service",
            "review_service",
            "cart_service",
        ],
    }

    return metrics
