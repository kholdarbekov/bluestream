"""
Gunicorn configuration file for BlueStream application.

This configuration handles the --preload flag safely by disposing
database connections after fork to prevent connection sharing issues.
"""
import os
import shutil


# INF-003: prometheus_client multiprocess mode.
# Must be set before any worker imports business_app (which imports the metrics
# module). The directory is wiped on master boot so stale per-PID files from a
# previous container life don't linger after PID recycling.
_PROMETHEUS_DIR = os.environ.setdefault("PROMETHEUS_MULTIPROC_DIR", "/tmp/prom-multiproc")
if os.path.isdir(_PROMETHEUS_DIR):
    shutil.rmtree(_PROMETHEUS_DIR, ignore_errors=True)
os.makedirs(_PROMETHEUS_DIR, exist_ok=True)

# Server socket
bind = "0.0.0.0:80"

# Worker processes
workers = int(os.environ.get("GUNICORN_WORKERS", 2))
threads = int(os.environ.get("GUNICORN_THREADS", 3))
worker_class = "gthread"
worker_tmp_dir = "/dev/shm"
worker_connections = int(os.environ.get("GUNICORN_WORKER_CONNECTIONS", 1000))

# Worker lifecycle
max_requests = int(os.environ.get("GUNICORN_MAX_REQUESTS", 1000))
max_requests_jitter = int(os.environ.get("GUNICORN_MAX_REQUESTS_JITTER", 100))
timeout = int(os.environ.get("GUNICORN_TIMEOUT", 30))
keepalive = int(os.environ.get("GUNICORN_KEEPALIVE", 5))

# Preload app for faster worker startup and memory sharing
preload_app = True

# Logging
loglevel = "info"
errorlog = "-"
capture_output = True
enable_stdio_inheritance = True


def post_fork(server, worker):
    """
    Called just after a worker has been forked.

    This is critical when using --preload: the database engine and its
    connection pool are created in the master process before forking.
    After fork, each worker inherits references to those connections,
    but PostgreSQL connections are NOT fork-safe.

    Solution: Dispose the inherited connection pool so each worker
    creates fresh connections on first use.
    """
    server.log.info(f"Worker {worker.pid} spawned, disposing inherited DB connections")

    try:
        from business_app.wsgi import app
        from business_app import db

        # Need app context to access db.engine
        with app.app_context():
            # Dispose the connection pool - this closes all inherited connections
            # and marks the pool to create new connections on next use
            db.engine.dispose()

        server.log.info(f"Worker {worker.pid}: DB connection pool disposed successfully")
    except Exception as e:
        server.log.error(f"Worker {worker.pid}: Failed to dispose DB connections: {e}")


def pre_fork(server, worker):
    """Called just before a worker is forked."""
    pass


def post_worker_init(worker):
    """Called just after a worker has initialized the application."""
    pass


def worker_exit(server, worker):
    """Called just after a worker has been exited."""
    server.log.info(f"Worker {worker.pid} exiting")

    # INF-003: prometheus_client multiproc mode requires us to clean up the
    # dying worker's per-PID files so /metrics scrapes don't include its
    # counters indefinitely. See prometheus_client.multiprocess.mark_process_dead.
    try:
        from prometheus_client import multiprocess
        multiprocess.mark_process_dead(worker.pid)
    except Exception as exc:
        server.log.warning(f"Worker {worker.pid}: mark_process_dead failed: {exc}")
