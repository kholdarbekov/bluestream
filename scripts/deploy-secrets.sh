#!/bin/bash

# Blue Stream Water Platform - Docker Secrets Deployment Script
# This script helps deploy the application with proper secrets management

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
SECRETS_DIR="${SECRETS_DIR:-./secrets}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.secrets.yml}"
ENV_FILE="${ENV_FILE:-production.env}"

# Logging functions
log() {
    echo -e "${BLUE}[$(date +'%Y-%m-%d %H:%M:%S')] $1${NC}"
}

error() {
    echo -e "${RED}[ERROR] $1${NC}" >&2
}

warn() {
    echo -e "${YELLOW}[WARNING] $1${NC}"
}

success() {
    echo -e "${GREEN}[SUCCESS] $1${NC}"
}

# Help function
show_help() {
    cat << EOF
Blue Stream Docker Secrets Deployment Script

Usage: $0 <command> [options]

Commands:
    init                    Initialize deployment environment
    setup-secrets          Generate and setup secrets
    deploy                 Deploy the application with secrets
    update-secrets         Update existing secrets
    rollback               Rollback to previous deployment
    status                 Check deployment status
    cleanup                Clean up deployment

Options:
    -h, --help             Show this help message
    -v, --verbose          Verbose output
    -e, --env <file>       Environment file to use (default: production.env)
    -f, --force            Force operation without confirmation
    
Examples:
    $0 init                         # Initialize deployment environment
    $0 setup-secrets               # Generate and setup all secrets
    $0 deploy                      # Deploy the application
    $0 update-secrets              # Update secrets and restart services
    
Environment Setup:
    1. Copy .env.example to production.env
    2. Edit production.env with your configuration
    3. Run '$0 init' to setup the environment
    4. Run '$0 setup-secrets' to generate secrets
    5. Run '$0 deploy' to deploy the application

Required Environment Variables (in $ENV_FILE):
    - POSTGRES_DB          - PostgreSQL database name
    - POSTGRES_USER        - PostgreSQL username
    - PAYME_MERCHANT_ID    - PayMe merchant ID
    - CLICK_MERCHANT_ID    - Click merchant ID
    - CLICK_SERVICE_ID     - Click service ID
    - TWILIO_ACCOUNT_SID   - Twilio account SID (optional)
    - TWILIO_PHONE_NUMBER  - Twilio phone number (optional)
    - SENDGRID_FROM_EMAIL  - SendGrid sender email (optional)

Secrets that will be generated:
    - postgres_password    - PostgreSQL password
    - secret_key          - Flask application secret key
    - telegram_bot_token  - Telegram bot token (must be provided)
    - staff_bot_token     - Staff Telegram bot token (must be provided)
    - payme_secret_key    - PayMe secret key (optional)
    - click_secret_key    - Click secret key (optional)
    - sendgrid_api_key    - SendGrid API key (optional)
    - twilio_auth_token   - Twilio auth token (optional)
    - google_maps_api_key - Google Maps API key (optional)
    - yandex_maps_api_key - Yandex Maps API key (optional)
    - aws_secret_access_key - AWS secret access key (optional)
    - stripe_secret_key   - Stripe secret key (optional)
    - encryption_key      - Application encryption key
    - redis_password      - Redis password

EOF
}

# Check dependencies
check_dependencies() {
    local deps=("docker" "docker-compose" "openssl")
    local missing=()
    
    for dep in "${deps[@]}"; do
        if ! command -v "$dep" >/dev/null 2>&1; then
            missing+=("$dep")
        fi
    done
    
    if [[ ${#missing[@]} -gt 0 ]]; then
        error "Missing dependencies: ${missing[*]}"
        error "Please install the missing dependencies and try again"
        exit 1
    fi
    
    # Check Docker Swarm
    if ! docker info | grep -q "Swarm: active"; then
        warn "Docker Swarm is not active"
        echo -n "Initialize Docker Swarm? (y/N): "
        read -r response
        if [[ "$response" =~ ^([yY][eE][sS]|[yY])$ ]]; then
            log "Initializing Docker Swarm..."
            docker swarm init
            success "Docker Swarm initialized"
        else
            error "Docker Swarm is required for secrets management"
            exit 1
        fi
    fi
}

# Initialize deployment environment
init_deployment() {
    log "Initializing deployment environment..."
    
    # Create required directories
    mkdir -p logs uploads secrets
    chmod 700 secrets
    
    # Create environment file if it doesn't exist
    if [[ ! -f "$ENV_FILE" ]]; then
        if [[ -f ".env.example" ]]; then
            cp .env.example "$ENV_FILE"
            success "Created $ENV_FILE from .env.example"
            warn "Please edit $ENV_FILE with your configuration before proceeding"
        else
            warn "No .env.example found. Creating basic $ENV_FILE"
            cat > "$ENV_FILE" << 'EOF'
# Blue Stream Water Platform - Production Environment Configuration

# Application Environment
ENVIRONMENT=production
FLASK_ENV=production
DEBUG=False

# Database Configuration
POSTGRES_DB=bluestream_production
POSTGRES_USER=bluestream
POSTGRES_HOST=postgres
POSTGRES_PORT=5432

# Redis Configuration
REDIS_HOST=redis
REDIS_PORT=6379
REDIS_DB=0

# Business Configuration
COMPANY_NAME=Aqua Element
COMPANY_PHONE=+998901234567
COMPANY_EMAIL=info@bluestream.uz
COMPANY_ADDRESS=Tashkent, Uzbekistan

# Payment Configuration
PAYME_MERCHANT_ID=your_payme_merchant_id
CLICK_MERCHANT_ID=your_click_merchant_id
CLICK_SERVICE_ID=your_click_service_id

# Notification Configuration (Optional)
TWILIO_ACCOUNT_SID=your_twilio_account_sid
TWILIO_PHONE_NUMBER=+1234567890
SENDGRID_FROM_EMAIL=noreply@bluestream.uz

# Storage Configuration
STORAGE_TYPE=local
UPLOAD_FOLDER=uploads/

# Feature Configuration
MAPS_PROVIDER=google
EOF
            success "Created basic $ENV_FILE"
            warn "Please edit $ENV_FILE with your configuration before proceeding"
        fi
    fi
    
    # Initialize secrets directory
    ./scripts/manage-secrets.sh init
    
    success "Deployment environment initialized"
    warn "Next steps:"
    warn "1. Edit $ENV_FILE with your configuration"
    warn "2. Run '$0 setup-secrets' to generate secrets"
    warn "3. Run '$0 deploy' to deploy the application"
}

# Setup secrets
setup_secrets() {
    log "Setting up secrets..."
    
    # Check if environment file exists
    if [[ ! -f "$ENV_FILE" ]]; then
        error "Environment file $ENV_FILE not found"
        error "Run '$0 init' first"
        exit 1
    fi
    
    # Generate all secrets
    ./scripts/manage-secrets.sh generate
    
    # Prompt for mandatory secrets that need manual input
    log "Please provide the following mandatory secrets:"
    
    # Customer Telegram Bot Token
    if [[ ! -f "$SECRETS_DIR/telegram_bot_token" ]]; then
        while true; do
            echo -n "Enter Customer Telegram Bot Token (from @BotFather): "
            read -r -s bot_token
            echo
            if [[ -n "$bot_token" ]]; then
                echo -n "$bot_token" > "$SECRETS_DIR/telegram_bot_token"
                chmod 600 "$SECRETS_DIR/telegram_bot_token"
                success "Customer telegram bot token saved"
                break
            else
                error "Telegram bot token cannot be empty"
            fi
        done
    fi

    # Staff Telegram Bot Token
    if [[ ! -f "$SECRETS_DIR/staff_bot_token" ]]; then
        while true; do
            echo -n "Enter Staff Telegram Bot Token (from @BotFather): "
            read -r -s staff_bot_token
            echo
            if [[ -n "$staff_bot_token" ]]; then
                echo -n "$staff_bot_token" > "$SECRETS_DIR/staff_bot_token"
                chmod 600 "$SECRETS_DIR/staff_bot_token"
                success "Staff telegram bot token saved"
                break
            else
                error "Staff Telegram bot token cannot be empty"
            fi
        done
    fi
    
    # Validate all required secrets
    ./scripts/manage-secrets.sh validate
    
    # Deploy secrets to Docker Swarm
    ./scripts/manage-secrets.sh deploy
    
    success "Secrets setup completed"
}

# Deploy application
deploy_application() {
    log "Deploying Blue Stream Water Platform..."
    
    # Check if secrets are setup
    if ! ./scripts/manage-secrets.sh validate >/dev/null 2>&1; then
        error "Secrets are not properly configured"
        error "Run '$0 setup-secrets' first"
        exit 1
    fi
    
    # Load environment variables
    if [[ -f "$ENV_FILE" ]]; then
        export $(grep -v '^#' "$ENV_FILE" | xargs)
    fi
    
    # Pull latest images
    log "Pulling Docker images..."
    docker-compose -f "$COMPOSE_FILE" pull
    
    # Build images
    log "Building Docker images..."
    docker-compose -f "$COMPOSE_FILE" build
    
    # Deploy services
    log "Deploying services..."
    docker-compose -f "$COMPOSE_FILE" up -d
    
    # Wait for services to be healthy
    log "Waiting for services to be healthy..."
    local max_wait=300
    local wait_time=0
    
    while [[ $wait_time -lt $max_wait ]]; do
        if docker-compose -f "$COMPOSE_FILE" ps | grep -q "unhealthy"; then
            echo -n "."
            sleep 10
            wait_time=$((wait_time + 10))
        else
            break
        fi
    done
    echo
    
    if [[ $wait_time -ge $max_wait ]]; then
        error "Services failed to become healthy within $max_wait seconds"
        docker-compose -f "$COMPOSE_FILE" ps
        exit 1
    fi
    
    success "Application deployed successfully"
    
    # Show status
    deployment_status
}

# Update secrets
update_secrets() {
    log "Updating secrets..."
    
    # Re-deploy secrets to Docker Swarm
    ./scripts/manage-secrets.sh deploy
    
    # Restart services to pick up new secrets
    log "Restarting services to pick up new secrets..."
    docker-compose -f "$COMPOSE_FILE" restart
    
    success "Secrets updated and services restarted"
}

# Show deployment status
deployment_status() {
    log "Deployment Status:"
    echo
    
    # Show running containers
    echo "Running Services:"
    docker-compose -f "$COMPOSE_FILE" ps
    echo
    
    # Show Docker secrets
    echo "Docker Secrets:"
    docker secret ls | grep bluestream || echo "No secrets found"
    echo
    
    # Show service URLs
    echo "Service URLs:"
    echo "  - Business App: http://localhost:5000"
    echo "  - Admin UI: http://localhost:3000"
    echo "  - API Documentation: http://localhost:5000/docs"
    echo
    
    # Show logs for any unhealthy services
    local unhealthy_services
    unhealthy_services=$(docker-compose -f "$COMPOSE_FILE" ps | grep -E "(unhealthy|restarting|exited)" | awk '{print $1}' || true)
    
    if [[ -n "$unhealthy_services" ]]; then
        warn "Unhealthy services detected:"
        for service in $unhealthy_services; do
            echo "  - $service"
        done
        echo
        warn "Check logs with: docker-compose -f $COMPOSE_FILE logs <service_name>"
    fi
}

# Rollback deployment
rollback_deployment() {
    warn "This will stop all services and remove containers"
    echo -n "Are you sure you want to rollback? (y/N): "
    read -r confirmation
    
    if [[ "$confirmation" != "y" && "$confirmation" != "Y" ]]; then
        log "Rollback cancelled"
        return 0
    fi
    
    log "Rolling back deployment..."
    docker-compose -f "$COMPOSE_FILE" down
    success "Deployment rolled back"
}

# Cleanup deployment
cleanup_deployment() {
    warn "This will remove all containers, volumes, and secrets"
    echo -n "Are you sure you want to cleanup everything? (y/N): "
    read -r confirmation
    
    if [[ "$confirmation" != "y" && "$confirmation" != "Y" ]]; then
        log "Cleanup cancelled"
        return 0
    fi
    
    log "Cleaning up deployment..."
    
    # Stop and remove containers
    docker-compose -f "$COMPOSE_FILE" down -v
    
    # Remove secrets
    ./scripts/manage-secrets.sh cleanup
    
    # Remove images
    docker-compose -f "$COMPOSE_FILE" down --rmi all
    
    success "Cleanup completed"
}

# Main script logic
main() {
    case "${1:-}" in
        init)
            check_dependencies
            init_deployment
            ;;
        setup-secrets)
            check_dependencies
            setup_secrets
            ;;
        deploy)
            check_dependencies
            deploy_application
            ;;
        update-secrets)
            check_dependencies
            update_secrets
            ;;
        status)
            deployment_status
            ;;
        rollback)
            rollback_deployment
            ;;
        cleanup)
            cleanup_deployment
            ;;
        -h|--help)
            show_help
            ;;
        "")
            error "No command provided"
            show_help
            exit 1
            ;;
        *)
            error "Unknown command: $1"
            show_help
            exit 1
            ;;
    esac
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -e|--env)
            ENV_FILE="$2"
            shift 2
            ;;
        -v|--verbose)
            set -x
            shift
            ;;
        -f|--force)
            # Force flag for future use
            shift
            ;;
        -h|--help)
            show_help
            exit 0
            ;;
        *)
            break
            ;;
    esac
done

# Run main function
main "$@"
