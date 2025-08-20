#!/bin/bash

# Docker resource monitoring script for BlueStream Water Platform
# Monitors CPU, memory, disk, and network usage of Docker containers

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
INTERVAL=${1:-5}  # Monitoring interval in seconds
OUTPUT_FILE="/tmp/docker-resources.log"

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

info() {
    echo -e "${BLUE}[$(date +'%Y-%m-%d %H:%M:%S')] INFO:${NC} $1"
}

# Function to format bytes
format_bytes() {
    local bytes=$1
    local kb=$((bytes / 1024))
    local mb=$((kb / 1024))
    local gb=$((mb / 1024))
    
    if [ $gb -gt 0 ]; then
        echo "${gb}GB"
    elif [ $mb -gt 0 ]; then
        echo "${mb}MB"
    elif [ $kb -gt 0 ]; then
        echo "${kb}KB"
    else
        echo "${bytes}B"
    fi
}

# Function to get container resource usage
get_container_stats() {
    docker stats --no-stream --format "table {{.Container}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.NetIO}}\t{{.BlockIO}}" | grep -E "(postgres|redis|business_app|telegram_bot|celery|admin_ui)"
}

# Function to check resource thresholds
check_thresholds() {
    local container=$1
    local cpu_percent=$2
    local mem_percent=$3
    
    # Remove % symbol for comparison
    cpu_percent=${cpu_percent%\%}
    mem_percent=${mem_percent%\%}
    
    # Convert to integer for comparison
    cpu_percent=${cpu_percent%.*}
    mem_percent=${mem_percent%.*}
    
    # CPU thresholds
    if [ "$cpu_percent" -gt 80 ]; then
        error "HIGH CPU: $container is using $cpu_percent% CPU"
        echo "$(date): HIGH CPU: $container - $cpu_percent%" >> "$OUTPUT_FILE"
    elif [ "$cpu_percent" -gt 60 ]; then
        warn "MEDIUM CPU: $container is using $cpu_percent% CPU"
    fi
    
    # Memory thresholds
    if [ "$mem_percent" -gt 85 ]; then
        error "HIGH MEMORY: $container is using $mem_percent% memory"
        echo "$(date): HIGH MEMORY: $container - $mem_percent%" >> "$OUTPUT_FILE"
    elif [ "$mem_percent" -gt 70 ]; then
        warn "MEDIUM MEMORY: $container is using $mem_percent% memory"
    fi
}

# Function to get container health status
get_container_health() {
    local container=$1
    local health=$(docker inspect --format='{{.State.Health.Status}}' "$container" 2>/dev/null || echo "no-healthcheck")
    
    case $health in
        "healthy")
            echo -e "${GREEN}✓${NC}"
            ;;
        "unhealthy")
            echo -e "${RED}✗${NC}"
            ;;
        "starting")
            echo -e "${YELLOW}⚠${NC}"
            ;;
        *)
            echo -e "${BLUE}-${NC}"
            ;;
    esac
}

# Function to get Docker disk usage
get_docker_disk_usage() {
    docker system df --format "table {{.Type}}\t{{.Total}}\t{{.Active}}\t{{.Size}}\t{{.Reclaimable}}"
}

# Function to show container logs with errors
show_recent_errors() {
    local container=$1
    local lines=${2:-10}
    
    echo -e "\n${BLUE}Recent errors from $container:${NC}"
    docker logs --tail $lines "$container" 2>&1 | grep -i -E "(error|exception|failed|critical)" | tail -5 || echo "No recent errors found"
}

# Main monitoring function
monitor_resources() {
    clear
    
    echo "=================================================="
    echo "Docker Resource Monitor - BlueStream Platform"
    echo "Monitoring interval: ${INTERVAL}s"
    echo "Log file: $OUTPUT_FILE"
    echo "=================================================="
    
    while true; do
        echo -e "\n${BLUE}=== $(date) ===${NC}"
        
        # Container statistics
        echo -e "\n${GREEN}Container Resource Usage:${NC}"
        printf "%-20s %-10s %-15s %-10s %-20s %-20s %-8s\n" "CONTAINER" "CPU %" "MEMORY" "MEM %" "NET I/O" "BLOCK I/O" "HEALTH"
        printf "%-20s %-10s %-15s %-10s %-20s %-20s %-8s\n" "--------" "-----" "------" "-----" "-------" "---------" "------"
        
        # Get container stats and process each line
        get_container_stats | tail -n +2 | while IFS=$'\t' read -r container cpu_percent mem_usage mem_percent net_io block_io; do
            health=$(get_container_health "$container")
            printf "%-20s %-10s %-15s %-10s %-20s %-20s %-8s\n" "$container" "$cpu_percent" "$mem_usage" "$mem_percent" "$net_io" "$block_io" "$health"
            
            # Check thresholds
            check_thresholds "$container" "$cpu_percent" "$mem_percent"
        done
        
        # Docker disk usage
        echo -e "\n${GREEN}Docker Disk Usage:${NC}"
        get_docker_disk_usage
        
        # System resources
        echo -e "\n${GREEN}Host System Resources:${NC}"
        echo "CPU Load: $(uptime | awk -F'load average:' '{print $2}')"
        echo "Memory: $(free -h | awk 'NR==2{printf "Used: %s/%s (%.1f%%)", $3,$2,$3*100/$2}')"
        echo "Disk: $(df -h / | awk 'NR==2{printf "Used: %s/%s (%s)", $3,$2,$5}')"
        
        # Check for unhealthy containers
        echo -e "\n${GREEN}Container Health Status:${NC}"
        for container in postgres redis business_app telegram_bot celery_worker celery_beat admin_ui; do
            if docker ps --filter "name=$container" --format "{{.Names}}" | grep -q "$container"; then
                health=$(get_container_health "$container")
                status=$(docker inspect --format='{{.State.Status}}' "$container" 2>/dev/null || echo "not-found")
                echo "  $container: $status $health"
                
                # Show errors for unhealthy containers
                if [ "$(docker inspect --format='{{.State.Health.Status}}' "$container" 2>/dev/null)" = "unhealthy" ]; then
                    show_recent_errors "$container" 5
                fi
            else
                echo "  $container: not running ✗"
            fi
        done
        
        # Docker compose services status
        echo -e "\n${GREEN}Docker Compose Services:${NC}"
        if command -v docker-compose >/dev/null 2>&1; then
            docker-compose ps --format table
        else
            echo "docker-compose not available"
        fi
        
        echo -e "\n${BLUE}Waiting ${INTERVAL} seconds... (Press Ctrl+C to stop)${NC}"
        sleep $INTERVAL
        clear
    done
}

# Function to generate resource report
generate_report() {
    local output_file="${1:-docker-resource-report.txt}"
    
    echo "Generating resource usage report..."
    
    {
        echo "Docker Resource Usage Report"
        echo "Generated: $(date)"
        echo "========================================"
        echo ""
        
        echo "Container Resource Usage:"
        get_container_stats
        echo ""
        
        echo "Docker Disk Usage:"
        get_docker_disk_usage
        echo ""
        
        echo "Container Health Status:"
        for container in postgres redis business_app telegram_bot celery_worker celery_beat admin_ui; do
            if docker ps --filter "name=$container" --format "{{.Names}}" | grep -q "$container"; then
                health=$(get_container_health "$container")
                status=$(docker inspect --format='{{.State.Status}}' "$container" 2>/dev/null || echo "not-found")
                restart_count=$(docker inspect --format='{{.RestartCount}}' "$container" 2>/dev/null || echo "0")
                echo "  $container: $status $health (restarts: $restart_count)"
            else
                echo "  $container: not running"
            fi
        done
        echo ""
        
        echo "System Resources:"
        echo "CPU Load: $(uptime | awk -F'load average:' '{print $2}')"
        echo "Memory: $(free -h | awk 'NR==2{printf "Used: %s/%s (%.1f%%)", $3,$2,$3*100/$2}')"
        echo "Disk: $(df -h / | awk 'NR==2{printf "Used: %s/%s (%s)", $3,$2,$5}')"
        echo ""
        
        if [ -f "$OUTPUT_FILE" ]; then
            echo "Recent Alerts:"
            tail -20 "$OUTPUT_FILE"
        fi
        
    } > "$output_file"
    
    log "Report generated: $output_file"
}

# Function to cleanup Docker resources
cleanup_docker() {
    echo "Cleaning up Docker resources..."
    
    # Remove stopped containers
    docker container prune -f
    
    # Remove unused images
    docker image prune -f
    
    # Remove unused volumes
    docker volume prune -f
    
    # Remove unused networks
    docker network prune -f
    
    # Show space freed
    echo "Docker cleanup completed!"
    get_docker_disk_usage
}

# Parse command line arguments
case "${1:-monitor}" in
    "monitor")
        monitor_resources
        ;;
    "report")
        generate_report "$2"
        ;;
    "cleanup")
        cleanup_docker
        ;;
    "help"|"-h"|"--help")
        echo "Usage: $0 [COMMAND] [OPTIONS]"
        echo ""
        echo "Commands:"
        echo "  monitor [interval]     Monitor resources continuously (default: 5s)"
        echo "  report [filename]      Generate resource usage report"
        echo "  cleanup               Clean up unused Docker resources"
        echo "  help                  Show this help message"
        echo ""
        echo "Examples:"
        echo "  $0                    # Start monitoring with 5s interval"
        echo "  $0 monitor 10         # Monitor with 10s interval"
        echo "  $0 report usage.txt   # Generate report to usage.txt"
        echo "  $0 cleanup            # Clean up Docker resources"
        exit 0
        ;;
    *)
        if [[ "$1" =~ ^[0-9]+$ ]]; then
            INTERVAL="$1"
            monitor_resources
        else
            error "Unknown command: $1"
            echo "Use '$0 help' for usage information"
            exit 1
        fi
        ;;
esac