#! /usr/bin/env sh

set -e

APP_MODULE='app.wsgi:app'

cpu_count=$(grep -c ^processor /proc/cpuinfo)
#workers=$((1 * $cpu_count))
workers=1
# IMPORTANT: Do not increase threads, it must be 1. Otherwise race conditions are happening.
threads=4

# NOTE: gunicorn workers in GPU is taking longer, with 3 workers it takes ~7 min to startup the app

echo "***** GUNICORN workers" $workers ", threads" $threads ", APP_MODULE: " $APP_MODULE

#echo "APPLYING MIGRATIONS"

#alembic upgrade head

#echo "MIGRATIONS APPLIED"

gunicorn \
  --bind=0.0.0.0:80 \
  --workers=$workers \
  --threads=$threads \
  --worker-class=gthread \
  --worker-tmp-dir=/dev/shm \
  --timeout=10 \
  "$APP_MODULE"

