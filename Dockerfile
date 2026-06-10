# Multi-stage Dockerfile for Water Business Platform

# Base Python image
FROM python:3.13-slim AS base

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FLASK_ENV=production \
    DEBUG=False

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    libpq-dev \
    curl \
    libmagic1 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Set work directory
WORKDIR /app

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared code
COPY shared/ ./shared/

# Business App Stage
FROM base AS business_app

# Copy business app code
COPY business_app/ ./business_app/

# Create uploads directory
RUN mkdir -p /app/uploads

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:80/health || exit 1

COPY scripts/start-gunicorn.sh scripts/migrate-db.sh scripts/gunicorn.conf.py /
RUN chmod u+x /start-gunicorn.sh /migrate-db.sh

# Start command
CMD ["/start-gunicorn.sh"]
# CMD ["python", "-Xfrozen_modules=off", "-m", "debugpy", "--listen", "0.0.0.0:5678", "--wait-for-client", "-m", "flask", "--app", "business_app.wsgi:app", "run", "--host", "0.0.0.0", "--port", "80"]

# Telegram Bot Stage
FROM base AS telegram_bot

# Copy telegram bot code
COPY telegram_bot/ ./telegram_bot/

# Set work directory to telegram_bot for module imports
WORKDIR /app/telegram_bot

# Add /app to Python path so shared module can be imported
ENV PYTHONPATH="/app:${PYTHONPATH}"

# Start command
CMD ["python", "bot.py"]
# CMD ["python", "-Xfrozen_modules=off", "-m", "debugpy", "--listen", "0.0.0.0:5679", "--wait-for-client", "bot.py"]

# Staff Bot Stage
FROM base AS staff_bot

# Copy staff bot code
COPY staff_bot/ ./staff_bot/

# Set work directory to staff_bot for module imports
WORKDIR /app/staff_bot

# Add /app to Python path so shared module can be imported
ENV PYTHONPATH="/app:${PYTHONPATH}"

# Start command
CMD ["python", "bot.py"]

# Celery Worker Stage
FROM base AS celery_worker

# pg_dump for backup.database (backup_tasks.py); client major must be >= the
# postgres:17 server, but bookworm stock postgresql-client is v15 — use PGDG.
RUN install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
        https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(. /etc/os-release && echo "$VERSION_CODENAME")-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client-17 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Copy business app code (needed for tasks)
COPY business_app/ ./business_app/

# Create uploads directory
RUN mkdir -p /app/uploads

# Start command will be overridden in docker-compose
CMD ["celery", "-A", "business_app.tasks.celery_app", "worker", "--loglevel=info"]

# Celery Beat Stage
FROM base AS celery_beat

# Copy business app code (needed for tasks)
COPY business_app/ ./business_app/

# Start command will be overridden in docker-compose
CMD ["celery", "-A", "business_app.tasks.celery_app", "beat", "--loglevel=info"]

# Development Stage (for local development)
FROM base AS development

# Override production environment variables for development
ENV FLASK_ENV=development \
    DEBUG=True

# Install development dependencies
RUN pip install --no-cache-dir pytest pytest-cov black flake8 mypy

# Copy all code
COPY . .

# Start command for development
CMD ["python", "-m", "business_app.app"]
