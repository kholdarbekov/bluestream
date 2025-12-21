#! /usr/bin/env sh

set -e

APP_MODULE='business_app.wsgi:app'

# Export environment variables for gunicorn.conf.py
export GUNICORN_WORKERS=${GUNICORN_WORKERS:-2}
export GUNICORN_THREADS=${GUNICORN_THREADS:-3}
export GUNICORN_MAX_REQUESTS=${GUNICORN_MAX_REQUESTS:-1000}
export GUNICORN_MAX_REQUESTS_JITTER=${GUNICORN_MAX_REQUESTS_JITTER:-100}
export GUNICORN_WORKER_CONNECTIONS=${GUNICORN_WORKER_CONNECTIONS:-1000}
export GUNICORN_TIMEOUT=${GUNICORN_TIMEOUT:-30}
export GUNICORN_KEEPALIVE=${GUNICORN_KEEPALIVE:-5}

echo "***** GUNICORN Configuration *****"
echo "Workers: $GUNICORN_WORKERS"
echo "Threads: $GUNICORN_THREADS"
echo "Max requests: $GUNICORN_MAX_REQUESTS"
echo "Max requests jitter: $GUNICORN_MAX_REQUESTS_JITTER"
echo "Worker connections: $GUNICORN_WORKER_CONNECTIONS"
echo "Timeout: $GUNICORN_TIMEOUT"
echo "Keepalive: $GUNICORN_KEEPALIVE"
echo "APP_MODULE: $APP_MODULE"
echo "Config: gunicorn.conf.py (with post_fork DB connection disposal)"

# Create logs directory if it doesn't exist
mkdir -p /app/logs

# Run gunicorn with Python config file
# The config file handles --preload safely by disposing DB connections after fork
exec gunicorn \
  --config=/gunicorn.conf.py \
  "$APP_MODULE"