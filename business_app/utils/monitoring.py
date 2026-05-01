"""
Application monitoring and health check utilities for BlueStream Platform
"""

import time
import psutil
import threading
from datetime import datetime, UTC, timedelta
from typing import Dict, Any, Optional, List, Callable
from collections import defaultdict, deque
from dataclasses import dataclass, asdict
from functools import wraps

from flask import g, request, jsonify

from business_app.utils.logging_config import performance_logger


@dataclass
class HealthStatus:
    """Health status information"""

    service: str
    status: str  # healthy, degraded, unhealthy
    message: str
    details: Dict[str, Any]
    timestamp: datetime
    response_time_ms: Optional[float] = None


@dataclass
class MetricSnapshot:
    """Metric snapshot for monitoring"""

    name: str
    value: float
    unit: str
    timestamp: datetime
    tags: Dict[str, str]


class SystemMetrics:
    """System resource monitoring"""

    def __init__(self):
        self._lock = threading.Lock()
        self._metrics_history = defaultdict(lambda: deque(maxlen=100))

    def get_cpu_usage(self) -> float:
        """Get current CPU usage percentage"""
        return psutil.cpu_percent(interval=1)

    def get_memory_usage(self) -> Dict[str, Any]:
        """Get memory usage information"""
        memory = psutil.virtual_memory()
        return {
            "total_mb": round(memory.total / (1024 * 1024), 2),
            "available_mb": round(memory.available / (1024 * 1024), 2),
            "used_mb": round(memory.used / (1024 * 1024), 2),
            "percentage": memory.percent,
            "free_mb": round(memory.free / (1024 * 1024), 2),
        }

    def get_disk_usage(self, path: str = "/") -> Dict[str, Any]:
        """Get disk usage information"""
        disk = psutil.disk_usage(path)
        return {
            "total_gb": round(disk.total / (1024**3), 2),
            "used_gb": round(disk.used / (1024**3), 2),
            "free_gb": round(disk.free / (1024**3), 2),
            "percentage": round((disk.used / disk.total) * 100, 2),
        }

    def get_network_stats(self) -> Dict[str, Any]:
        """Get network statistics"""
        net_io = psutil.net_io_counters()
        return {
            "bytes_sent": net_io.bytes_sent,
            "bytes_received": net_io.bytes_recv,
            "packets_sent": net_io.packets_sent,
            "packets_received": net_io.packets_recv,
            "errors_in": net_io.errin,
            "errors_out": net_io.errout,
            "drops_in": net_io.dropin,
            "drops_out": net_io.dropout,
        }

    def get_process_info(self) -> Dict[str, Any]:
        """Get current process information"""
        process = psutil.Process()
        with process.oneshot():
            return {
                "pid": process.pid,
                "cpu_percent": process.cpu_percent(),
                "memory_percent": process.memory_percent(),
                "memory_info_mb": {
                    "rss": round(process.memory_info().rss / (1024 * 1024), 2),
                    "vms": round(process.memory_info().vms / (1024 * 1024), 2),
                },
                "num_threads": process.num_threads(),
                "connections": len(process.connections()),
                "create_time": datetime.fromtimestamp(process.create_time(), UTC).isoformat(),
                "status": process.status(),
            }

    def collect_metrics(self) -> Dict[str, MetricSnapshot]:
        """Collect all system metrics"""
        timestamp = datetime.now(UTC)
        metrics = {}

        # CPU metrics
        cpu_usage = self.get_cpu_usage()
        metrics["cpu_usage"] = MetricSnapshot(
            name="cpu_usage", value=cpu_usage, unit="percent", timestamp=timestamp, tags={"type": "system"}
        )

        # Memory metrics
        memory = self.get_memory_usage()
        metrics["memory_usage"] = MetricSnapshot(
            name="memory_usage",
            value=memory["percentage"],
            unit="percent",
            timestamp=timestamp,
            tags={"type": "system"},
        )

        # Disk metrics
        disk = self.get_disk_usage()
        metrics["disk_usage"] = MetricSnapshot(
            name="disk_usage", value=disk["percentage"], unit="percent", timestamp=timestamp, tags={"type": "system"}
        )

        # Process metrics
        process_info = self.get_process_info()
        metrics["process_memory"] = MetricSnapshot(
            name="process_memory",
            value=process_info["memory_percent"],
            unit="percent",
            timestamp=timestamp,
            tags={"type": "process"},
        )

        metrics["process_threads"] = MetricSnapshot(
            name="process_threads",
            value=process_info["num_threads"],
            unit="count",
            timestamp=timestamp,
            tags={"type": "process"},
        )

        # Store metrics history
        with self._lock:
            for metric in metrics.values():
                self._metrics_history[metric.name].append(metric)

        return metrics

    def get_metrics_history(self, metric_name: str, minutes: int = 60) -> List[MetricSnapshot]:
        """Get metrics history for a specific metric"""
        with self._lock:
            history = list(self._metrics_history[metric_name])
            cutoff_time = datetime.now(UTC) - timedelta(minutes=minutes)
            return [m for m in history if m.timestamp >= cutoff_time]


class ApplicationMetrics:
    """Application-specific metrics tracking"""

    def __init__(self):
        self._lock = threading.Lock()
        self._counters = defaultdict(int)
        self._timers = defaultdict(list)
        self._gauges = defaultdict(float)
        self._last_reset = datetime.now(UTC)

    def increment_counter(self, name: str, value: int = 1, tags: Dict[str, str] = None):
        """Increment a counter metric"""
        with self._lock:
            key = self._make_key(name, tags)
            self._counters[key] += value

    def set_gauge(self, name: str, value: float, tags: Dict[str, str] = None):
        """Set a gauge metric value"""
        with self._lock:
            key = self._make_key(name, tags)
            self._gauges[key] = value

    def record_timer(self, name: str, duration: float, tags: Dict[str, str] = None):
        """Record a timer metric"""
        with self._lock:
            key = self._make_key(name, tags)
            self._timers[key].append(duration)
            # Keep only last 1000 values
            if len(self._timers[key]) > 1000:
                self._timers[key] = self._timers[key][-1000:]

    def _make_key(self, name: str, tags: Dict[str, str] = None) -> str:
        """Create a key from name and tags"""
        if not tags:
            return name
        tag_str = ",".join(f"{k}={v}" for k, v in sorted(tags.items()))
        return f"{name}[{tag_str}]"

    def get_metrics_summary(self) -> Dict[str, Any]:
        """Get summary of all metrics"""
        with self._lock:
            summary = {
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
                "timers": {},
                "collection_time": datetime.now(UTC).isoformat(),
                "uptime_seconds": (datetime.now(UTC) - self._last_reset).total_seconds(),
            }

            # Calculate timer statistics
            for key, values in self._timers.items():
                if values:
                    summary["timers"][key] = {
                        "count": len(values),
                        "min": min(values),
                        "max": max(values),
                        "avg": sum(values) / len(values),
                        "sum": sum(values),
                    }

            return summary

    def reset_metrics(self):
        """Reset all metrics"""
        with self._lock:
            self._counters.clear()
            self._timers.clear()
            self._gauges.clear()
            self._last_reset = datetime.now(UTC)


class HealthChecker:
    """Health check system for services"""

    def __init__(self):
        self._checks: Dict[str, Callable[[], HealthStatus]] = {}
        self._cache: Dict[str, HealthStatus] = {}
        self._cache_ttl = 30  # seconds
        self._lock = threading.Lock()

    def register_check(self, name: str, check_func: Callable[[], HealthStatus]):
        """Register a health check function"""
        with self._lock:
            self._checks[name] = check_func

    def run_check(self, name: str, use_cache: bool = True) -> HealthStatus:
        """Run a specific health check"""
        with self._lock:
            # Check cache first
            if use_cache and name in self._cache:
                cached = self._cache[name]
                age = (datetime.now(UTC) - cached.timestamp).total_seconds()
                if age < self._cache_ttl:
                    return cached

            # Run the check
            if name not in self._checks:
                return HealthStatus(
                    service=name,
                    status="unhealthy",
                    message="Health check not registered",
                    details={},
                    timestamp=datetime.now(UTC),
                )

            try:
                start_time = time.time()
                result = self._checks[name]()
                response_time = (time.time() - start_time) * 1000
                result.response_time_ms = response_time

                # Cache the result
                self._cache[name] = result
                return result

            except Exception as e:
                return HealthStatus(
                    service=name,
                    status="unhealthy",
                    message=f"Health check failed: {str(e)}",
                    details={"error": str(e), "error_type": type(e).__name__},
                    timestamp=datetime.now(UTC),
                )

    def run_all_checks(self, use_cache: bool = True) -> Dict[str, HealthStatus]:
        """Run all registered health checks"""
        results = {}
        for name in self._checks:
            results[name] = self.run_check(name, use_cache)
        return results

    def get_overall_status(self) -> str:
        """Get overall system health status"""
        results = self.run_all_checks()
        if not results:
            return "unknown"

        statuses = [r.status for r in results.values()]
        if all(s == "healthy" for s in statuses):
            return "healthy"
        elif any(s == "unhealthy" for s in statuses):
            return "unhealthy"
        else:
            return "degraded"


# Health check implementations
def check_database_health() -> HealthStatus:
    """Check database connectivity and performance"""
    from business_app import db

    try:
        start_time = time.time()
        result = db.session.execute(db.text("SELECT 1")).scalar()
        query_time = (time.time() - start_time) * 1000

        if result == 1:
            status = "healthy" if query_time < 100 else "degraded"
            message = f"Database responsive in {query_time:.2f}ms"
        else:
            status = "unhealthy"
            message = "Database query returned unexpected result"

        return HealthStatus(
            service="database",
            status=status,
            message=message,
            details={"query_time_ms": query_time, "result": result},
            timestamp=datetime.now(UTC),
        )

    except Exception as e:
        return HealthStatus(
            service="database",
            status="unhealthy",
            message=f"Database connection failed: {str(e)}",
            details={"error": str(e), "error_type": type(e).__name__},
            timestamp=datetime.now(UTC),
        )


def check_redis_health() -> HealthStatus:
    """Check Redis connectivity and performance"""
    try:
        from business_app import redis_client

        start_time = time.time()
        redis_client.ping()
        ping_time = (time.time() - start_time) * 1000

        # Get Redis info
        info = redis_client.info()

        status = "healthy" if ping_time < 50 else "degraded"
        message = f"Redis responsive in {ping_time:.2f}ms"

        return HealthStatus(
            service="redis",
            status=status,
            message=message,
            details={
                "ping_time_ms": ping_time,
                "version": info.get("redis_version"),
                "connected_clients": info.get("connected_clients"),
                "used_memory": info.get("used_memory_human"),
                "uptime_seconds": info.get("uptime_in_seconds"),
            },
            timestamp=datetime.now(UTC),
        )

    except Exception as e:
        return HealthStatus(
            service="redis",
            status="unhealthy",
            message=f"Redis connection failed: {str(e)}",
            details={"error": str(e), "error_type": type(e).__name__},
            timestamp=datetime.now(UTC),
        )


def check_disk_space_health() -> HealthStatus:
    """Check disk space availability"""
    try:
        disk_usage = psutil.disk_usage("/")
        percentage = (disk_usage.used / disk_usage.total) * 100

        if percentage < 80:
            status = "healthy"
            message = f"Disk usage at {percentage:.1f}%"
        elif percentage < 90:
            status = "degraded"
            message = f"Disk usage high at {percentage:.1f}%"
        else:
            status = "unhealthy"
            message = f"Disk usage critical at {percentage:.1f}%"

        return HealthStatus(
            service="disk_space",
            status=status,
            message=message,
            details={
                "usage_percentage": percentage,
                "total_gb": round(disk_usage.total / (1024**3), 2),
                "used_gb": round(disk_usage.used / (1024**3), 2),
                "free_gb": round(disk_usage.free / (1024**3), 2),
            },
            timestamp=datetime.now(UTC),
        )

    except Exception as e:
        return HealthStatus(
            service="disk_space",
            status="unhealthy",
            message=f"Disk check failed: {str(e)}",
            details={"error": str(e), "error_type": type(e).__name__},
            timestamp=datetime.now(UTC),
        )


def check_memory_health() -> HealthStatus:
    """Check memory usage"""
    try:
        memory = psutil.virtual_memory()
        percentage = memory.percent

        if percentage < 80:
            status = "healthy"
            message = f"Memory usage at {percentage:.1f}%"
        elif percentage < 90:
            status = "degraded"
            message = f"Memory usage high at {percentage:.1f}%"
        else:
            status = "unhealthy"
            message = f"Memory usage critical at {percentage:.1f}%"

        return HealthStatus(
            service="memory",
            status=status,
            message=message,
            details={
                "usage_percentage": percentage,
                "total_mb": round(memory.total / (1024**2), 2),
                "available_mb": round(memory.available / (1024**2), 2),
                "used_mb": round(memory.used / (1024**2), 2),
            },
            timestamp=datetime.now(UTC),
        )

    except Exception as e:
        return HealthStatus(
            service="memory",
            status="unhealthy",
            message=f"Memory check failed: {str(e)}",
            details={"error": str(e), "error_type": type(e).__name__},
            timestamp=datetime.now(UTC),
        )


# Request monitoring decorator
def monitor_request_performance(func):
    """Decorator to monitor API request performance"""

    @wraps(func)
    def wrapper(*args, **kwargs):
        start_time = time.time()

        try:
            result = func(*args, **kwargs)
            duration = time.time() - start_time

            # Record metrics
            app_metrics.record_timer(
                "request_duration",
                duration,
                {"endpoint": request.endpoint or "unknown", "method": request.method, "status": "success"},
            )

            app_metrics.increment_counter(
                "requests_total",
                1,
                {"endpoint": request.endpoint or "unknown", "method": request.method, "status": "success"},
            )

            # Log performance if slow
            if duration > 2.0:  # Log requests slower than 2 seconds
                performance_logger.log_performance(
                    f"{request.method} {request.endpoint}",
                    duration,
                    endpoint=request.endpoint,
                    method=request.method,
                    status_code=getattr(result, "status_code", 200) if hasattr(result, "status_code") else 200,
                )

            return result

        except Exception as e:
            duration = time.time() - start_time

            # Record error metrics
            app_metrics.record_timer(
                "request_duration",
                duration,
                {"endpoint": request.endpoint or "unknown", "method": request.method, "status": "error"},
            )

            app_metrics.increment_counter(
                "requests_total",
                1,
                {"endpoint": request.endpoint or "unknown", "method": request.method, "status": "error"},
            )

            app_metrics.increment_counter(
                "request_errors",
                1,
                {"endpoint": request.endpoint or "unknown", "method": request.method, "error_type": type(e).__name__},
            )

            raise

    return wrapper


# Global monitoring instances
system_metrics = SystemMetrics()
app_metrics = ApplicationMetrics()
health_checker = HealthChecker()

# Register default health checks
health_checker.register_check("database", check_database_health)
health_checker.register_check("redis", check_redis_health)
health_checker.register_check("disk_space", check_disk_space_health)
health_checker.register_check("memory", check_memory_health)


def setup_monitoring(app):
    """Setup monitoring system for Flask app"""

    # Request monitoring middleware
    @app.before_request
    def before_request():
        g.monitoring_start_time = time.time()
        g.request_id = request.headers.get("X-Request-ID", f"req_{int(time.time() * 1000000)}")

    @app.after_request
    def after_request(response):
        if hasattr(g, "monitoring_start_time"):
            duration = time.time() - g.monitoring_start_time

            # Record request metrics
            app_metrics.record_timer(
                "request_duration",
                duration,
                {
                    "endpoint": request.endpoint or "unknown",
                    "method": request.method,
                    "status_code": str(response.status_code),
                },
            )

            app_metrics.increment_counter(
                "requests_total",
                1,
                {
                    "endpoint": request.endpoint or "unknown",
                    "method": request.method,
                    "status_code": str(response.status_code),
                },
            )

            # Add performance headers
            response.headers["X-Response-Time"] = f"{duration:.3f}s"
            response.headers["X-Request-ID"] = getattr(g, "request_id", "unknown")

        return response

    # Health check endpoint - Note: Main health check is in app factory, this provides detailed checks
    @app.route("/health/detailed")
    def detailed_health_check():
        """Detailed health check endpoint with comprehensive status"""
        overall_status = health_checker.get_overall_status()
        checks = health_checker.run_all_checks()

        response_data = {
            "status": overall_status,
            "timestamp": datetime.now(UTC).isoformat(),
            "checks": {name: asdict(status) for name, status in checks.items()},
        }

        status_code = 200 if overall_status == "healthy" else 503
        return jsonify(response_data), status_code

    # Metrics endpoint
    @app.route("/metrics")
    def metrics_endpoint():
        """Metrics endpoint for monitoring"""
        system_data = system_metrics.collect_metrics()
        app_data = app_metrics.get_metrics_summary()
        performance_data = performance_logger.get_metrics()

        return jsonify(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "system": {name: asdict(metric) for name, metric in system_data.items()},
                "application": app_data,
                "performance": performance_data,
            }
        )

    app.logger.info("Monitoring system initialized")


# Export commonly used components
__all__ = [
    "setup_monitoring",
    "monitor_request_performance",
    "system_metrics",
    "app_metrics",
    "health_checker",
    "HealthStatus",
    "MetricSnapshot",
    "SystemMetrics",
    "ApplicationMetrics",
    "HealthChecker",
]
