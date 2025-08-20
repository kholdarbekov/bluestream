# Docker Resource Limits and Health Checks

This document outlines the comprehensive Docker resource limits and health check configurations implemented for the BlueStream Water Platform.

## Overview

The platform uses Docker Compose with carefully configured resource limits, health checks, and monitoring to ensure reliable operation and prevent resource exhaustion.

## Resource Allocation

### PostgreSQL Database
- **Memory Limit**: 1GB (2GB in production)
- **CPU Limit**: 1.0 vCPUs (2.0 in production)
- **Memory Reservation**: 512MB (1GB in production)
- **CPU Reservation**: 0.5 vCPUs (1.0 in production)
- **Health Check**: `pg_isready` every 10s
- **Special Configuration**: Enhanced with performance tuning parameters

### Redis Cache
- **Memory Limit**: 512MB (1GB in production)
- **CPU Limit**: 0.5 vCPUs (1.0 in production)
- **Memory Reservation**: 256MB (512MB in production)
- **CPU Reservation**: 0.25 vCPUs (0.5 in production)
- **Health Check**: `redis-cli ping` every 10s
- **Special Configuration**: LRU eviction policy, append-only persistence

### Business Application
- **Memory Limit**: 1GB (2GB in production)
- **CPU Limit**: 1.0 vCPUs (2.0 in production)
- **Memory Reservation**: 512MB (1GB in production)
- **CPU Reservation**: 0.5 vCPUs (1.0 in production)
- **Health Check**: HTTP health endpoint every 30s
- **Special Configuration**: Gunicorn with configurable workers

### Telegram Bot
- **Memory Limit**: 512MB (1GB in production)
- **CPU Limit**: 0.5 vCPUs (1.0 in production)
- **Memory Reservation**: 256MB (512MB in production)
- **CPU Reservation**: 0.25 vCPUs (0.5 in production)
- **Health Check**: Checks business app connectivity every 60s

### Celery Worker
- **Memory Limit**: 1GB (2GB in production)
- **CPU Limit**: 1.0 vCPUs (2.0 in production)
- **Memory Reservation**: 512MB (1GB in production)
- **CPU Reservation**: 0.5 vCPUs (1.0 in production)
- **Health Check**: `celery status` every 60s
- **Special Configuration**: Memory and task limits to prevent leaks

### Celery Beat
- **Memory Limit**: 256MB (512MB in production)
- **CPU Limit**: 0.25 vCPUs (0.5 in production)
- **Memory Reservation**: 128MB (256MB in production)
- **CPU Reservation**: 0.1 vCPUs (0.25 in production)
- **Health Check**: Process check every 60s

### Admin UI
- **Memory Limit**: 256MB (512MB in production)
- **CPU Limit**: 0.5 vCPUs (1.0 in production)
- **Memory Reservation**: 128MB (256MB in production)
- **CPU Reservation**: 0.25 vCPUs (0.5 in production)
- **Health Check**: HTTP health endpoint every 30s

## Health Check Configuration

### Health Check Parameters
- **Interval**: How often to run the health check
- **Timeout**: Maximum time to wait for health check response
- **Retries**: Number of consecutive failures before marking unhealthy
- **Start Period**: Grace period during container startup

### Service-Specific Health Checks

#### PostgreSQL
```yaml
healthcheck:
  test: ["CMD-SHELL", "pg_isready -U postgres -d bluestream_db"]
  interval: 10s
  timeout: 5s
  retries: 5
  start_period: 30s
```

#### Redis
```yaml
healthcheck:
  test: ["CMD", "redis-cli", "ping"]
  interval: 10s
  timeout: 5s
  retries: 5
  start_period: 10s
```

#### Business Application
```yaml
healthcheck:
  test: ["CMD", "curl", "-f", "http://localhost:80/health"]
  interval: 30s
  timeout: 10s
  retries: 3
  start_period: 60s
```

## Production Optimizations

### Database Tuning
The production PostgreSQL configuration includes:
- Optimized shared buffers and effective cache size
- Checkpoint and WAL configurations for performance
- Connection pooling limits
- Performance monitoring with pg_stat_statements

### Redis Optimization
- Memory eviction policy (allkeys-lru)
- Persistence configuration (AOF + RDB)
- Connection limits and timeouts
- Memory usage limits

### Application Scaling
- Gunicorn worker and thread configuration
- Resource-based worker scaling
- Request limits to prevent memory leaks
- Preloading for better performance

## Monitoring and Alerts

### Prometheus Metrics
The platform includes Prometheus monitoring for:
- Container resource usage (CPU, memory, disk)
- Application metrics (requests, errors, response times)
- Database and Redis performance metrics
- Custom business metrics

### Alert Rules
Configured alerts for:
- High CPU usage (>80%)
- High memory usage (>90%)
- Service availability issues
- High error rates (>10%)
- Disk space usage (>85%)
- Queue backup (>1000 tasks)

### Log Management
- Centralized logging with Loki
- Log rotation (10MB max, 3 files)
- Structured JSON logging in production
- Error aggregation and filtering

## Usage Instructions

### Starting Services
```bash
# Development
docker-compose up -d

# Production with resource limits
docker-compose -f docker-compose.yml -f docker-compose.production.yml up -d
```

### Health Monitoring
```bash
# Check all services
./scripts/health-check.sh

# Check specific service
./scripts/health-check.sh database

# Monitor resources continuously
./scripts/docker-resource-monitor.sh

# Generate resource report
./scripts/docker-resource-monitor.sh report
```

### Resource Cleanup
```bash
# Clean up unused Docker resources
./scripts/docker-resource-monitor.sh cleanup

# Check disk usage
docker system df
```

## Environment Variables

### Gunicorn Configuration
- `GUNICORN_WORKERS`: Number of worker processes (default: CPU count * 2)
- `GUNICORN_THREADS`: Threads per worker (default: 4)
- `GUNICORN_MAX_REQUESTS`: Requests before worker restart (default: 1000)
- `GUNICORN_MAX_REQUESTS_JITTER`: Random jitter for restarts (default: 100)
- `GUNICORN_TIMEOUT`: Request timeout (default: 30s)

### Celery Configuration
- `CELERY_WORKER_CONCURRENCY`: Worker concurrency (default: 4)
- `CELERY_WORKER_MAX_MEMORY_PER_CHILD`: Memory limit per worker (default: 200MB)
- `CELERY_WORKER_MAX_TASKS_PER_CHILD`: Tasks before worker restart (default: 1000)

### Database Configuration
- `POSTGRES_MAX_CONNECTIONS`: Maximum database connections (default: 200)
- `POSTGRES_SHARED_BUFFERS`: Shared buffer size (default: 512MB)
- `POSTGRES_EFFECTIVE_CACHE_SIZE`: Cache size hint (default: 1536MB)

## Security Considerations

### Resource Limits as Security
Resource limits help prevent:
- Resource exhaustion attacks
- Memory leaks from affecting other services
- CPU monopolization by single services
- Disk space exhaustion

### Health Check Security
- Health checks don't expose sensitive information
- Internal endpoints used for container-to-container checks
- Timeout limits prevent hanging connections

## Troubleshooting

### Common Issues

#### Container Won't Start
1. Check resource availability on host
2. Verify health check endpoints
3. Check dependency service health
4. Review container logs

#### High Resource Usage
1. Use monitoring scripts to identify bottlenecks
2. Check for memory leaks in application logs
3. Review database query performance
4. Consider scaling horizontally

#### Health Check Failures
1. Verify service is actually running
2. Check network connectivity between containers
3. Review health check timeout settings
4. Check application logs for errors

### Monitoring Commands
```bash
# View container resource usage
docker stats

# Check container logs
docker-compose logs [service_name]

# Inspect container health
docker inspect [container_name] | grep -A 20 Health

# View system resources
free -h && df -h && uptime
```

## Best Practices

1. **Resource Planning**: Allocate resources based on actual usage patterns
2. **Health Checks**: Keep health checks lightweight and fast
3. **Monitoring**: Set up alerts for resource thresholds
4. **Scaling**: Use horizontal scaling before increasing resource limits
5. **Testing**: Test resource limits under load conditions
6. **Documentation**: Keep resource configurations documented and versioned

This comprehensive resource management ensures the BlueStream platform runs reliably and efficiently in both development and production environments.