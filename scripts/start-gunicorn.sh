#! /usr/bin/env sh

set -e

APP_MODULE='business_app.wsgi:app'

# Get CPU count and configure workers based on environment variables or defaults
cpu_count=$(grep -c ^processor /proc/cpuinfo)
# workers=${GUNICORN_WORKERS:-$((2 * $cpu_count))}
workers=2
# threads=${GUNICORN_THREADS:-4}
threads=3
max_requests=${GUNICORN_MAX_REQUESTS:-1000}
max_requests_jitter=${GUNICORN_MAX_REQUESTS_JITTER:-100}
worker_connections=${GUNICORN_WORKER_CONNECTIONS:-1000}
timeout=${GUNICORN_TIMEOUT:-30}
keepalive=${GUNICORN_KEEPALIVE:-5}

# Limit workers to prevent resource exhaustion
if [ "$workers" -gt 8 ]; then
    workers=8
fi

echo "***** GUNICORN Configuration *****"
echo "Workers: $workers"
echo "Threads: $threads"
echo "Max requests: $max_requests"
echo "Max requests jitter: $max_requests_jitter"
echo "Worker connections: $worker_connections"
echo "Timeout: $timeout"
echo "Keepalive: $keepalive"
echo "APP_MODULE: $APP_MODULE"

# Create logs directory if it doesn't exist
mkdir -p /app/logs

# Run gunicorn with comprehensive configuration
exec gunicorn \
  --bind=0.0.0.0:80 \
  --workers=$workers \
  --threads=$threads \
  --worker-class=gthread \
  --worker-tmp-dir=/dev/shm \
  --worker-connections=$worker_connections \
  --max-requests=$max_requests \
  --max-requests-jitter=$max_requests_jitter \
  --timeout=$timeout \
  --keep-alive=$keepalive \
  --preload \
  --log-level=info \
  --access-logfile=- \
  --error-logfile=- \
  --capture-output \
  --enable-stdio-inheritance \
  "$APP_MODULE"