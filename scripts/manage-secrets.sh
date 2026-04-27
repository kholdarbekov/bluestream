#!/bin/bash

# Docker Secrets Management Script for Blue Stream Water Business Platform
# This script helps create, update, and manage Docker secrets

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
SECRETS_DIR="${SECRETS_DIR:-./secrets}"
# shellcheck disable=SC2034  # exposed for callers that source this script
COMPOSE_FILE="docker-compose.secrets.yml"
# shellcheck disable=SC2034
ENV_FILE=".env"

# Logging function
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
Docker Secrets Management Script

Usage: $0 <command> [options]

Commands:
    init                    Initialize secrets directory and generate secrets
    create <secret_name>    Create a new secret
    update <secret_name>    Update an existing secret
    remove <secret_name>    Remove a secret
    list                    List all secrets
    validate               Validate all required secrets exist
    deploy                 Deploy secrets to Docker Swarm
    cleanup                Remove all secrets from Docker Swarm
    backup                 Backup secrets to encrypted archive
    restore <backup_file>  Restore secrets from backup
    generate               Generate random values for all secrets

Options:
    -h, --help             Show this help message
    -v, --verbose          Verbose output
    -f, --force            Force operation without confirmation

Examples:
    $0 init                         # Initialize secrets
    $0 create postgres_password     # Create a new secret
    $0 update secret_key           # Update an existing secret
    $0 deploy                      # Deploy all secrets to Docker Swarm
    $0 backup                      # Backup all secrets

Required secrets:
    - postgres_password
    - secret_key
    - telegram_bot_token
    - staff_bot_token

Optional secrets:
    - payme_secret_key
    - click_secret_key
    - sendgrid_api_key
    - twilio_auth_token
    - google_maps_api_key
    - yandex_maps_api_key
    - aws_secret_access_key
    - stripe_secret_key
    - encryption_key
    - redis_password

EOF
}

# Generate random secret
generate_secret() {
    local length=${1:-32}
    openssl rand -base64 $length | tr -d "=+/" | cut -c1-$length
}

# Generate random hex secret
generate_hex_secret() {
    local length=${1:-64}
    openssl rand -hex $((length/2))
}

# Create secrets directory
init_secrets_dir() {
    log "Initializing secrets directory..."

    mkdir -p "$SECRETS_DIR"
    chmod 700 "$SECRETS_DIR"

    # Create .gitignore for secrets directory
    cat > "$SECRETS_DIR/.gitignore" << EOF
# Ignore all secret files
*
!.gitignore
!README.md
EOF

    # Create README
    cat > "$SECRETS_DIR/README.md" << EOF
# Docker Secrets Directory

This directory contains sensitive secrets for the Blue Stream Water Business Platform.

## Security Notice

- **NEVER** commit secret files to version control
- Keep this directory secure with appropriate file permissions (700)
- Use different secrets for different environments
- Rotate secrets regularly
- Use strong, randomly generated values

## Secret Files

Each file should contain only the secret value (no newlines or extra whitespace).

### Required Secrets:
- \`postgres_password\` - PostgreSQL database password
- \`secret_key\` - Flask application secret key
- \`telegram_bot_token\` - Telegram bot API token
- \`staff_bot_token\` - Staff Telegram bot API token

### Optional Secrets:
- \`payme_secret_key\` - PayMe payment gateway secret
- \`click_secret_key\` - Click payment gateway secret
- \`sendgrid_api_key\` - SendGrid email API key
- \`twilio_auth_token\` - Twilio SMS API token
- \`google_maps_api_key\` - Google Maps API key
- \`yandex_maps_api_key\` - Yandex Maps API key
- \`aws_secret_access_key\` - AWS secret access key
- \`stripe_secret_key\` - Stripe payment secret key
- \`encryption_key\` - Application encryption key
- \`redis_password\` - Redis authentication password

## Usage

Use the \`manage-secrets.sh\` script to manage these secrets safely.
EOF

    success "Secrets directory initialized at $SECRETS_DIR"
}

# Create a secret
create_secret() {
    local secret_name="$1"
    local secret_file="$SECRETS_DIR/$secret_name"

    if [[ -f "$secret_file" ]]; then
        error "Secret '$secret_name' already exists. Use 'update' to modify it."
        return 1
    fi

    log "Creating secret: $secret_name"

    # Prompt for secret value
    echo -n "Enter secret value (leave empty to generate): "
    read -r -s secret_value
    echo

    if [[ -z "$secret_value" ]]; then
        log "Generating random secret value..."
        # INF-007: more-specific patterns must come first. Previously the
        # `*_key` glob caught `encryption_key` and `secret_key` before their
        # specific branches, so encryption_key was being generated as a 32-char
        # base64 string instead of the intended 64-char hex.
        case "$secret_name" in
            encryption_key)
                secret_value=$(generate_hex_secret 64)
                ;;
            secret_key)
                secret_value=$(generate_secret 64)
                ;;
            *_password|*_token|*_key)
                secret_value=$(generate_secret 32)
                ;;
            *)
                secret_value=$(generate_secret 32)
                ;;
        esac
        log "Generated random value for $secret_name"
    fi

    # Write secret to file
    echo -n "$secret_value" > "$secret_file"
    chmod 600 "$secret_file"

    success "Created secret: $secret_name"
}

# Update a secret
update_secret() {
    local secret_name="$1"
    local secret_file="$SECRETS_DIR/$secret_name"

    if [[ ! -f "$secret_file" ]]; then
        error "Secret '$secret_name' does not exist. Use 'create' to create it."
        return 1
    fi

    log "Updating secret: $secret_name"

    # Prompt for new secret value
    echo -n "Enter new secret value: "
    read -r -s secret_value
    echo

    if [[ -z "$secret_value" ]]; then
        error "Secret value cannot be empty"
        return 1
    fi

    # Backup old secret
    cp "$secret_file" "$secret_file.backup.$(date +%s)"

    # Write new secret to file
    echo -n "$secret_value" > "$secret_file"
    chmod 600 "$secret_file"

    success "Updated secret: $secret_name"
}

# Remove a secret
remove_secret() {
    local secret_name="$1"
    local secret_file="$SECRETS_DIR/$secret_name"

    if [[ ! -f "$secret_file" ]]; then
        error "Secret '$secret_name' does not exist."
        return 1
    fi

    warn "This will permanently delete secret: $secret_name"
    echo -n "Are you sure? (y/N): "
    read -r confirmation

    if [[ "$confirmation" != "y" && "$confirmation" != "Y" ]]; then
        log "Operation cancelled"
        return 0
    fi

    # Secure delete
    shred -vfz -n 3 "$secret_file" 2>/dev/null || rm -f "$secret_file"

    success "Removed secret: $secret_name"
}

# List all secrets
list_secrets() {
    log "Listing secrets in $SECRETS_DIR:"

    if [[ ! -d "$SECRETS_DIR" ]]; then
        warn "Secrets directory does not exist. Run 'init' first."
        return 1
    fi

    local count=0
    for secret_file in "$SECRETS_DIR"/*; do
        if [[ -f "$secret_file" && "$(basename "$secret_file")" != "README.md" && "$(basename "$secret_file")" != ".gitignore" ]]; then
            local secret_name
            secret_name=$(basename "$secret_file")
            local file_size
            file_size=$(stat -c%s "$secret_file" 2>/dev/null || stat -f%z "$secret_file" 2>/dev/null || echo "unknown")
            echo "  - $secret_name ($file_size bytes)"
            ((count++))
        fi
    done

    if [[ $count -eq 0 ]]; then
        warn "No secrets found"
    else
        success "Found $count secrets"
    fi
}

# Validate secrets
validate_secrets() {
    log "Validating required secrets..."

    local required_secrets=("postgres_password" "secret_key" "telegram_bot_token" "staff_bot_token")
    local missing_secrets=()

    for secret in "${required_secrets[@]}"; do
        if [[ ! -f "$SECRETS_DIR/$secret" ]]; then
            missing_secrets+=("$secret")
        fi
    done

    if [[ ${#missing_secrets[@]} -eq 0 ]]; then
        success "All required secrets are present"
        return 0
    else
        error "Missing required secrets: ${missing_secrets[*]}"
        return 1
    fi
}

# Deploy secrets to Docker Swarm
deploy_secrets() {
    log "Deploying secrets to Docker Swarm..."

    if ! docker info | grep -q "Swarm: active"; then
        error "Docker Swarm is not active. Initialize with: docker swarm init"
        return 1
    fi

    local deployed=0
    for secret_file in "$SECRETS_DIR"/*; do
        if [[ -f "$secret_file" && "$(basename "$secret_file")" != "README.md" && "$(basename "$secret_file")" != ".gitignore" ]]; then
            local secret_name
            secret_name=$(basename "$secret_file")
            local docker_secret_name="bluestream_$secret_name"

            # Check if secret already exists
            if docker secret inspect "$docker_secret_name" >/dev/null 2>&1; then
                warn "Secret '$docker_secret_name' already exists in Docker Swarm"
                continue
            fi

            # Create secret in Docker Swarm
            if docker secret create "$docker_secret_name" "$secret_file"; then
                success "Deployed secret: $docker_secret_name"
                ((deployed++))
            else
                error "Failed to deploy secret: $docker_secret_name"
            fi
        fi
    done

    success "Deployed $deployed secrets to Docker Swarm"
}

# Cleanup secrets from Docker Swarm
cleanup_secrets() {
    log "Cleaning up secrets from Docker Swarm..."

    warn "This will remove ALL Blue Stream secrets from Docker Swarm"
    echo -n "Are you sure? (y/N): "
    read -r confirmation

    if [[ "$confirmation" != "y" && "$confirmation" != "Y" ]]; then
        log "Operation cancelled"
        return 0
    fi

    local removed=0
    for secret in $(docker secret ls --format "{{.Name}}" | grep "^bluestream_"); do
        if docker secret rm "$secret"; then
            success "Removed secret: $secret"
            ((removed++))
        else
            error "Failed to remove secret: $secret"
        fi
    done

    success "Removed $removed secrets from Docker Swarm"
}

# Generate all secrets
generate_all_secrets() {
    log "Generating all secrets..."

    # Required secrets
    local secrets=(
        "postgres_password"
        "secret_key"
        "telegram_bot_token"
        "staff_bot_token"
        "payme_secret_key"
        "click_secret_key"
        "sendgrid_api_key"
        "twilio_auth_token"
        "google_maps_api_key"
        "yandex_maps_api_key"
        "aws_secret_access_key"
        "stripe_secret_key"
        "encryption_key"
        "redis_password"
    )

    local generated=0
    for secret in "${secrets[@]}"; do
        if [[ ! -f "$SECRETS_DIR/$secret" ]]; then
            local secret_value
            case "$secret" in
                encryption_key)
                    secret_value=$(generate_hex_secret 64)
                    ;;
                secret_key)
                    secret_value=$(generate_secret 64)
                    ;;
                telegram_bot_token)
                    warn "Telegram bot token must be obtained from @BotFather. Skipping generation."
                    continue
                    ;;
                staff_bot_token)
                    warn "Staff bot token must be obtained from @BotFather. Skipping generation."
                    continue
                    ;;
                *_api_key|*_token)
                    warn "$secret should be obtained from the service provider. Generating placeholder."
                    secret_value="REPLACE_WITH_REAL_$(echo "$secret" | tr '[:lower:]' '[:upper:]')"
                    ;;
                *)
                    secret_value=$(generate_secret 32)
                    ;;
            esac

            echo -n "$secret_value" > "$SECRETS_DIR/$secret"
            chmod 600 "$SECRETS_DIR/$secret"
            success "Generated secret: $secret"
            ((generated++))
        else
            warn "Secret '$secret' already exists, skipping"
        fi
    done

    success "Generated $generated new secrets"
}

# Main script logic
main() {
    case "${1:-}" in
        init)
            init_secrets_dir
            ;;
        create)
            if [[ -z "${2:-}" ]]; then
                error "Secret name is required"
                exit 1
            fi
            init_secrets_dir >/dev/null 2>&1 || true
            create_secret "$2"
            ;;
        update)
            if [[ -z "${2:-}" ]]; then
                error "Secret name is required"
                exit 1
            fi
            update_secret "$2"
            ;;
        remove)
            if [[ -z "${2:-}" ]]; then
                error "Secret name is required"
                exit 1
            fi
            remove_secret "$2"
            ;;
        list)
            list_secrets
            ;;
        validate)
            validate_secrets
            ;;
        deploy)
            validate_secrets && deploy_secrets
            ;;
        cleanup)
            cleanup_secrets
            ;;
        generate)
            init_secrets_dir >/dev/null 2>&1 || true
            generate_all_secrets
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

# Check dependencies
check_dependencies() {
    local deps=("docker" "openssl")
    local missing=()

    for dep in "${deps[@]}"; do
        if ! command -v "$dep" >/dev/null 2>&1; then
            missing+=("$dep")
        fi
    done

    if [[ ${#missing[@]} -gt 0 ]]; then
        error "Missing dependencies: ${missing[*]}"
        exit 1
    fi
}

# Run main function
check_dependencies
main "$@"
