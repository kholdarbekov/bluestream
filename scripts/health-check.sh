#!/bin/bash

# Comprehensive health check script for BlueStream Water Platform
# Usage: ./health-check.sh [service]

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Default configuration
TIMEOUT=10
RETRIES=3
HEALTH_ENDPOINT="/health"
API_ENDPOINT="/api/health"

# Log function
log() {
    echo -e "${GREEN}[$(date +'%Y-%m-%d %H:%M:%S')]${NC} $1"
}

warn() {
    echo -e "${YELLOW}[$(date +'%Y-%m-%d %H:%M:%S')] WARNING:${NC} $1"
}

error() {
    echo -e "${RED}[$(date +'%Y-%m-%d %H:%M:%S')] ERROR:${NC} $1"
}

# Check if a service is responding
check_http_service() {
    local service=$1
    local port=$2
    local endpoint=$3
    local retries=${4:-$RETRIES}
    
    log "Checking $service on port $port..."
    
    for i in $(seq 1 $retries); do
        if curl -f -s --max-time $TIMEOUT "http://localhost:$port$endpoint" > /dev/null; then
            log "$service is healthy ✓"
            return 0
        else
            warn "$service check failed (attempt $i/$retries)"
            sleep 2
        fi
    done
    
    error "$service is unhealthy ✗"
    return 1
}

# Check TCP port
check_tcp_port() {
    local service=$1
    local port=$2
    
    log "Checking $service TCP port $port..."
    
    if nc -z localhost $port 2>/dev/null; then
        log "$service port $port is open ✓"
        return 0
    else
        error "$service port $port is not responding ✗"
        return 1
    fi
}

# Check database connectivity
check_database() {
    log "Checking PostgreSQL database..."
    
    if docker-compose exec -T postgres pg_isready -U ${POSTGRES_USER:-postgres} > /dev/null 2>&1; then
        log "Database is ready ✓"
        return 0
    else
        error "Database is not ready ✗"
        return 1
    fi
}

# Check Redis connectivity
check_redis() {
    log "Checking Redis..."
    
    if docker-compose exec -T redis redis-cli ping | grep -q PONG; then
        log "Redis is responding ✓"
        return 0
    else
        error "Redis is not responding ✗"
        return 1
    fi
}

# Check Celery worker
check_celery_worker() {
    log "Checking Celery worker..."
    
    if docker-compose exec -T celery_worker celery -A business_app.tasks.celery_app status > /dev/null 2>&1; then
        log "Celery worker is active ✓"
        return 0
    else
        error "Celery worker is not responding ✗"
        return 1
    fi
}

# Check disk space
check_disk_space() {
    log "Checking disk space..."
    
    local usage=$(df / | awk 'NR==2 {print $(NF-1)}' | sed 's/%//')
    
    if [ "$usage" -gt 90 ]; then
        error "Disk usage is critical: ${usage}% ✗"
        return 1
    elif [ "$usage" -gt 80 ]; then
        warn "Disk usage is high: ${usage}%"
        return 0
    else
        log "Disk usage is normal: ${usage}% ✓"
        return 0
    fi
}

# Check memory usage
check_memory() {
    log "Checking memory usage..."
    
    local usage=$(free | awk 'NR==2{printf "%.0f", $3*100/$2}')
    
    if [ "$usage" -gt 90 ]; then
        error "Memory usage is critical: ${usage}% ✗"
        return 1
    elif [ "$usage" -gt 80 ]; then
        warn "Memory usage is high: ${usage}%"
        return 0
    else
        log "Memory usage is normal: ${usage}% ✓"
        return 0
    fi
}

# Check Docker containers
check_docker_containers() {
    log "Checking Docker containers..."
    
    local containers=("postgres" "redis" "business_app" "telegram_bot" "celery_worker" "celery_beat")
    local failed=0
    
    for container in "${containers[@]}"; do
        if docker-compose ps --services --filter "status=running" | grep -q "^$container$"; then
            log "Container $container is running ✓"
        else
            error "Container $container is not running ✗"
            failed=1
        fi
    done
    
    return $failed
}

# Check application endpoints
check_application_endpoints() {
    log "Checking application endpoints..."
    
    local endpoints=(
        "/health:200"
        "/api/health:200"
        "/api/auth/status:401"
    )
    
    local failed=0
    
    for endpoint_check in "${endpoints[@]}"; do
        local endpoint=$(echo $endpoint_check | cut -d: -f1)
        local expected_code=$(echo $endpoint_check | cut -d: -f2)
        
        local actual_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time $TIMEOUT "http://localhost:5000$endpoint")
        
        if [ "$actual_code" = "$expected_code" ]; then
            log "Endpoint $endpoint returned $actual_code ✓"
        else
            error "Endpoint $endpoint returned $actual_code, expected $expected_code ✗"
            failed=1
        fi
    done
    
    return $failed
}

# Main health check function
run_health_check() {
    local service=${1:-all}
    local failed=0
    
    log "Starting health check for: $service"
    echo "=================================================="
    
    case $service in
        "database"|"postgres")
            check_database || failed=1
            ;;
        "redis")
            check_redis || failed=1
            ;;
        "business_app"|"app")
            check_http_service "Business App" 5000 "$HEALTH_ENDPOINT" || failed=1
            ;;
        "admin_ui")
            check_http_service "Admin UI" 3000 "$HEALTH_ENDPOINT" || failed=1
            ;;
        "celery")
            check_celery_worker || failed=1
            ;;
        "system")
            check_disk_space || failed=1
            check_memory || failed=1
            ;;
        "all")
            check_docker_containers || failed=1
            check_database || failed=1
            check_redis || failed=1
            check_http_service "Business App" 5000 "$HEALTH_ENDPOINT" || failed=1
            check_http_service "Admin UI" 3000 "$HEALTH_ENDPOINT" || failed=1
            check_celery_worker || failed=1
            check_application_endpoints || failed=1
            check_disk_space || failed=1
            check_memory || failed=1
            ;;
        *)
            error "Unknown service: $service"
            echo "Available services: database, redis, business_app, admin_ui, celery, system, all"
            exit 1
            ;;
    esac
    
    echo "=================================================="
    
    if [ $failed -eq 0 ]; then
        log "All health checks passed! ✓"
        exit 0
    else
        error "Some health checks failed! ✗"
        exit 1
    fi
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -t|--timeout)
            TIMEOUT="$2"
            shift 2
            ;;
        -r|--retries)
            RETRIES="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 [OPTIONS] [SERVICE]"
            echo ""
            echo "Options:"
            echo "  -t, --timeout SECONDS   Timeout for health checks (default: $TIMEOUT)"
            echo "  -r, --retries COUNT     Number of retries (default: $RETRIES)"
            echo "  -h, --help             Show this help message"
            echo ""
            echo "Services:"
            echo "  database, redis, business_app, admin_ui, celery, system, all"
            echo ""
            echo "Examples:"
            echo "  $0                      # Check all services"
            echo "  $0 database             # Check only database"
            echo "  $0 -t 30 business_app   # Check business app with 30s timeout"
            exit 0
            ;;
        *)
            SERVICE="$1"
            shift
            ;;
    esac
done

# Run the health check
run_health_check "${SERVICE:-all}"