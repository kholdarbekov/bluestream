"""
Comprehensive logging configuration and monitoring utilities for BlueStream Platform
"""

import logging
import logging.handlers
import json
import time
import traceback
import threading
from datetime import datetime, UTC
from typing import Dict, Any
from functools import wraps
from contextlib import contextmanager
from pathlib import Path
import sys

from flask import g, request, has_request_context


class StructuredFormatter(logging.Formatter):
    """JSON formatter for structured logging"""

    def format(self, record):
        """Format log record as structured JSON"""
        log_entry = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Add request context if available
        if has_request_context():
            try:
                log_entry.update(
                    {
                        "request_id": getattr(g, "request_id", None),
                        "user_id": getattr(g, "current_user_id", None),
                        "ip_address": request.remote_addr,
                        "method": request.method,
                        "url": request.url,
                        "user_agent": request.headers.get("User-Agent"),
                    }
                )
            except RuntimeError:
                # Outside request context
                pass

        # Add exception info if present
        if record.exc_info:
            log_entry["exception"] = {
                "type": record.exc_info[0].__name__,
                "message": str(record.exc_info[1]),
                "traceback": traceback.format_exception(*record.exc_info),
            }

        # Add extra fields
        for key, value in record.__dict__.items():
            if key not in (
                "name",
                "msg",
                "args",
                "levelname",
                "levelno",
                "pathname",
                "filename",
                "module",
                "lineno",
                "funcName",
                "created",
                "msecs",
                "relativeCreated",
                "thread",
                "threadName",
                "processName",
                "process",
                "getMessage",
                "exc_info",
                "exc_text",
                "stack_info",
                "taskName",
            ):
                log_entry[key] = value

        return json.dumps(log_entry, default=str)


class SecuritySensitiveFilter(logging.Filter):
    """Filter to remove sensitive information from logs"""

    SENSITIVE_KEYS = {
        "password",
        "token",
        "secret",
        "key",
        "authorization",
        "cookie",
        "session",
        "csrf",
        "api_key",
        "access_token",
        "refresh_token",
        "card_number",
        "cvv",
        "ssn",
        "passport",
    }

    def filter(self, record):
        """Filter sensitive information from log records"""
        if hasattr(record, "args") and record.args:
            # Handle string formatting args
            if isinstance(record.args, (tuple, list)):
                record.args = tuple(self._sanitize_value(arg) for arg in record.args)
            elif isinstance(record.args, dict):
                record.args = {k: self._sanitize_value(v) for k, v in record.args.items()}

        # Sanitize message
        record.msg = self._sanitize_message(str(record.msg))

        return True

    def _sanitize_value(self, value):
        """Sanitize a single value"""
        if isinstance(value, dict):
            return {
                k: (
                    "***REDACTED***"
                    if any(sens in k.lower() for sens in self.SENSITIVE_KEYS)
                    else self._sanitize_value(v)
                )
                for k, v in value.items()
            }
        elif isinstance(value, str):
            return self._sanitize_message(value)
        return value

    def _sanitize_message(self, message):
        """Sanitize log message content"""
        message_lower = message.lower()
        for sensitive_key in self.SENSITIVE_KEYS:
            if sensitive_key in message_lower:
                # Replace patterns like "password=value" with "password=***REDACTED***"
                import re

                pattern = rf'({sensitive_key}["\']?\s*[:=]\s*["\']?)([^"\s,}}]+)(["\']?)'
                message = re.sub(pattern, r"\1***REDACTED***\3", message, flags=re.IGNORECASE)
        return message


class PerformanceLogger:
    """Logger for performance metrics and monitoring"""

    def __init__(self):
        self.logger = logging.getLogger("performance")
        self._metrics = {}
        self._lock = threading.Lock()

    @contextmanager
    def measure_time(self, operation: str, **context):
        """Context manager to measure operation time"""
        start_time = time.time()
        try:
            yield
        finally:
            duration = time.time() - start_time
            self.log_performance(operation, duration, **context)

    def log_performance(self, operation: str, duration: float, **context):
        """Log performance metrics"""
        self.logger.info(
            f"Performance: {operation}",
            extra={
                "operation": operation,
                "duration_ms": round(duration * 1000, 2),
                "duration_seconds": round(duration, 4),
                **context,
            },
        )

        # Store metrics for monitoring
        with self._lock:
            if operation not in self._metrics:
                self._metrics[operation] = {
                    "count": 0,
                    "total_time": 0,
                    "min_time": float("inf"),
                    "max_time": 0,
                    "avg_time": 0,
                }

            metrics = self._metrics[operation]
            metrics["count"] += 1
            metrics["total_time"] += duration
            metrics["min_time"] = min(metrics["min_time"], duration)
            metrics["max_time"] = max(metrics["max_time"], duration)
            metrics["avg_time"] = metrics["total_time"] / metrics["count"]

    def get_metrics(self) -> Dict[str, Any]:
        """Get current performance metrics"""
        with self._lock:
            return dict(self._metrics)

    def reset_metrics(self):
        """Reset performance metrics"""
        with self._lock:
            self._metrics.clear()


class SecurityLogger:
    """Logger for security events and threats"""

    def __init__(self):
        self.logger = logging.getLogger("security")

    def log_auth_attempt(
        self, username: str, success: bool, ip_address: str, user_agent: str = None, additional_info: Dict = None
    ):
        """Log authentication attempts"""
        self.logger.info(
            f"Auth attempt: {'SUCCESS' if success else 'FAILED'} for {username}",
            extra={
                "event_type": "auth_attempt",
                "username": username,
                "success": success,
                "ip_address": ip_address,
                "user_agent": user_agent,
                "additional_info": additional_info or {},
            },
        )

    def log_permission_denied(
        self, user_id: int, resource: str, action: str, ip_address: str, additional_info: Dict = None
    ):
        """Log permission denied events"""
        self.logger.warning(
            f"Permission denied: User {user_id} attempted {action} on {resource}",
            extra={
                "event_type": "permission_denied",
                "user_id": user_id,
                "resource": resource,
                "action": action,
                "ip_address": ip_address,
                "additional_info": additional_info or {},
            },
        )

    def log_suspicious_activity(
        self,
        description: str,
        user_id: int = None,
        ip_address: str = None,
        severity: str = "medium",
        additional_info: Dict = None,
    ):
        """Log suspicious activities"""
        self.logger.warning(
            f"Suspicious activity: {description}",
            extra={
                "event_type": "suspicious_activity",
                "description": description,
                "user_id": user_id,
                "ip_address": ip_address,
                "severity": severity,
                "additional_info": additional_info or {},
            },
        )

    def log_data_access(
        self, user_id: int, resource: str, action: str, record_count: int = None, additional_info: Dict = None
    ):
        """Log data access events"""
        self.logger.info(
            f"Data access: User {user_id} {action} {resource}",
            extra={
                "event_type": "data_access",
                "user_id": user_id,
                "resource": resource,
                "action": action,
                "record_count": record_count,
                "additional_info": additional_info or {},
            },
        )


class BusinessLogger:
    """Logger for business events and transactions"""

    def __init__(self):
        self.logger = logging.getLogger("business")

    def log_order_event(
        self, order_id: int, event: str, user_id: int = None, amount: float = None, additional_info: Dict = None
    ):
        """Log order-related events"""
        try:
            self.logger.info(
                f"Order {event}: Order {order_id}",
                extra={
                    "event_type": "order_event",
                    "order_id": order_id,
                    "event": event,
                    "user_id": user_id,
                    "amount": float(amount),
                    "additional_info": additional_info or {},
                },
            )
        except Exception as e:
            self.logger.error(f"EXCEPTION in log_order_event: {e}")

    def log_payment_event(
        self,
        payment_id: str,
        event: str,
        amount: float,
        method: str,
        user_id: int = None,
        order_id: int = None,
        additional_info: Dict = None,
    ):
        """Log payment-related events"""
        self.logger.info(
            f"Payment {event}: {payment_id} - {amount}",
            extra={
                "event_type": "payment_event",
                "payment_id": payment_id,
                "event": event,
                "amount": amount,
                "method": method,
                "user_id": user_id,
                "order_id": order_id,
                "additional_info": additional_info or {},
            },
        )

    def log_delivery_event(
        self, delivery_id: int, event: str, order_id: int = None, driver_id: int = None, additional_info: Dict = None
    ):
        """Log delivery-related events"""
        self.logger.info(
            f"Delivery {event}: Delivery {delivery_id}",
            extra={
                "event_type": "delivery_event",
                "delivery_id": delivery_id,
                "event": event,
                "order_id": order_id,
                "driver_id": driver_id,
                "additional_info": additional_info or {},
            },
        )

    def log_inventory_event(
        self,
        product_id: int,
        event: str,
        quantity: int = None,
        user_id: int = None,
        order_id: int = None,
        additional_info: Dict = None,
    ):
        """Log inventory-related events"""
        self.logger.info(
            f"Inventory {event}: Product {product_id}",
            extra={
                "event_type": "inventory_event",
                "product_id": product_id,
                "event": event,
                "quantity": quantity,
                "user_id": user_id,
                "order_id": order_id,
                "additional_info": additional_info or {},
            },
        )


class DatabaseQueryLogger:
    """Logger for database query performance"""

    def __init__(self):
        self.logger = logging.getLogger("database")
        self.slow_query_threshold = 1.0  # seconds

    def log_query(self, query: str, duration: float, result_count: int = None, operation: str = None, **context):
        """Log database queries"""
        level = logging.WARNING if duration > self.slow_query_threshold else logging.DEBUG

        self.logger.log(
            level,
            f"{'SLOW ' if duration > self.slow_query_threshold else ''}Query: {operation or 'Unknown'}",
            extra={
                "query": query[:500] + "..." if len(query) > 500 else query,
                "duration_ms": round(duration * 1000, 2),
                "result_count": result_count,
                "operation": operation,
                "is_slow": duration > self.slow_query_threshold,
                **context,
            },
        )


def setup_enhanced_logging(app):
    """Setup comprehensive logging configuration"""

    # Prevent duplicate initialization
    if hasattr(app, "_logging_initialized"):
        return
    app._logging_initialized = True

    # Create logs directory
    log_dir = Path(app.config["LOG_FILE"]).parent
    log_dir.mkdir(exist_ok=True)

    # Remove default handlers
    app.logger.handlers.clear()

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.handlers.clear()

    # Create formatters
    structured_formatter = StructuredFormatter()
    human_formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Security filter
    security_filter = SecuritySensitiveFilter()

    # File handlers for different log types
    handlers = {}

    # Main application log
    main_handler = logging.handlers.RotatingFileHandler(
        app.config["LOG_FILE"], maxBytes=app.config["LOG_MAX_BYTES"], backupCount=app.config["LOG_BACKUP_COUNT"]
    )
    main_handler.setFormatter(structured_formatter)
    main_handler.addFilter(security_filter)
    main_handler.setLevel(getattr(logging, app.config["LOG_LEVEL"]))
    handlers["main"] = main_handler

    # Security log
    security_handler = logging.handlers.RotatingFileHandler(
        str(log_dir / "security.log"), maxBytes=app.config["LOG_MAX_BYTES"], backupCount=app.config["LOG_BACKUP_COUNT"]
    )
    security_handler.setFormatter(structured_formatter)
    security_handler.addFilter(security_filter)
    security_handler.setLevel(logging.INFO)
    handlers["security"] = security_handler

    # Performance log
    performance_handler = logging.handlers.RotatingFileHandler(
        str(log_dir / "performance.log"),
        maxBytes=app.config["LOG_MAX_BYTES"],
        backupCount=app.config["LOG_BACKUP_COUNT"],
    )
    performance_handler.setFormatter(structured_formatter)
    performance_handler.setLevel(logging.INFO)
    handlers["performance"] = performance_handler

    # Business log
    business_handler = logging.handlers.RotatingFileHandler(
        str(log_dir / "business.log"), maxBytes=app.config["LOG_MAX_BYTES"], backupCount=app.config["LOG_BACKUP_COUNT"]
    )
    business_handler.setFormatter(structured_formatter)
    business_handler.setLevel(logging.INFO)
    handlers["business"] = business_handler

    # Database log
    database_handler = logging.handlers.RotatingFileHandler(
        str(log_dir / "database.log"), maxBytes=app.config["LOG_MAX_BYTES"], backupCount=app.config["LOG_BACKUP_COUNT"]
    )
    database_handler.setFormatter(structured_formatter)
    database_handler.setLevel(logging.DEBUG)
    handlers["database"] = database_handler

    # Error log (errors only)
    error_handler = logging.handlers.RotatingFileHandler(
        str(log_dir / "errors.log"), maxBytes=app.config["LOG_MAX_BYTES"], backupCount=app.config["LOG_BACKUP_COUNT"]
    )
    error_handler.setFormatter(structured_formatter)
    error_handler.addFilter(security_filter)
    error_handler.setLevel(logging.ERROR)
    handlers["error"] = error_handler

    # Console handler for both development and production
    # Docker containers need stdout/stderr logging for `docker logs` to work
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.addFilter(security_filter)

    if app.debug:
        # Development: Human-readable format
        console_handler.setFormatter(human_formatter)
        console_handler.setLevel(logging.DEBUG)
    else:
        # Production: Structured JSON format for log aggregation
        console_handler.setFormatter(structured_formatter)
        console_handler.setLevel(getattr(logging, app.config["LOG_LEVEL"]))

    handlers["console"] = console_handler

    # Configure specific loggers
    loggers_config = {
        "security": ["security", "error"],
        "performance": ["performance", "error"],
        "business": ["business", "error"],
        "database": ["database", "error"],
        "flask.app": ["main", "error"],
        "werkzeug": ["main"],
        # 'sqlalchemy.engine': ['database'],  # DISABLED - SQL logging turned off for debugging
        # 'sqlalchemy.dialects': ['database'],  # DISABLED - SQL logging turned off for debugging
        "celery": ["main", "error"],
        "gunicorn": ["main", "error"],
    }

    # Add console handler to all loggers (both development and production)
    # This ensures logs appear in docker logs output
    for logger_names in loggers_config.values():
        if "console" not in logger_names:
            logger_names.append("console")

    # Setup loggers
    for logger_name, handler_names in loggers_config.items():
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.setLevel(logging.DEBUG if app.debug else getattr(logging, app.config["LOG_LEVEL"]))

        for handler_name in handler_names:
            if handler_name in handlers:
                logger.addHandler(handlers[handler_name])

        logger.propagate = False

    # Setup root logger with main handlers
    root_logger.addHandler(main_handler)
    root_logger.addHandler(error_handler)
    root_logger.addHandler(console_handler)  # Always add console handler for Docker logs
    root_logger.setLevel(logging.DEBUG if app.debug else getattr(logging, app.config["LOG_LEVEL"]))

    # Setup app logger
    app.logger.handlers.clear()
    app.logger.addHandler(main_handler)
    app.logger.addHandler(error_handler)
    app.logger.addHandler(console_handler)  # Always add console handler for Docker logs
    app.logger.setLevel(logging.DEBUG if app.debug else getattr(logging, app.config["LOG_LEVEL"]))
    app.logger.propagate = False  # Prevent duplicate logs from propagating to root logger

    # EXPLICITLY DISABLE SQLAlchemy logging for debugging
    logging.getLogger("sqlalchemy.engine").setLevel(logging.CRITICAL)
    logging.getLogger("sqlalchemy.dialects").setLevel(logging.CRITICAL)
    logging.getLogger("sqlalchemy.pool").setLevel(logging.CRITICAL)
    logging.getLogger("sqlalchemy.orm").setLevel(logging.CRITICAL)

    app.logger.info("Enhanced logging system initialized")


def log_function_call(func):
    """Decorator to log function calls with performance tracking"""

    @wraps(func)
    def wrapper(*args, **kwargs):
        logger = logging.getLogger(func.__module__)
        perf_logger = PerformanceLogger()

        # Log function entry
        logger.debug(
            f"Entering {func.__name__}",
            extra={
                "function": func.__name__,
                "module": func.__module__,
                "args_count": len(args),
                "kwargs_keys": list(kwargs.keys()),
            },
        )

        try:
            with perf_logger.measure_time(f"{func.__module__}.{func.__name__}"):
                result = func(*args, **kwargs)

            logger.debug(f"Exiting {func.__name__} successfully")
            return result

        except Exception as e:
            logger.error(
                f"Error in {func.__name__}: {e}",
                exc_info=True,
                extra={
                    "function": func.__name__,
                    "module": func.__module__,
                    "error_type": type(e).__name__,
                    "error_message": str(e),
                },
            )
            raise

    return wrapper


# Global logger instances
performance_logger = PerformanceLogger()
security_logger = SecurityLogger()
business_logger = BusinessLogger()
database_logger = DatabaseQueryLogger()


# Export commonly used loggers
__all__ = [
    "setup_enhanced_logging",
    "log_function_call",
    "performance_logger",
    "security_logger",
    "business_logger",
    "database_logger",
    "StructuredFormatter",
    "SecuritySensitiveFilter",
    "PerformanceLogger",
    "SecurityLogger",
    "BusinessLogger",
    "DatabaseQueryLogger",
]
