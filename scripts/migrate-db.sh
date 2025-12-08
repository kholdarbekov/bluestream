#!/bin/bash

###############################################################################
# Database Migration Script
# Applies Flask-Migrate migrations in production Docker environment
#
# Usage:
#   ./scripts/migrate-db.sh                 # Apply all pending migrations
#   ./scripts/migrate-db.sh --dry-run       # Show what would be applied
#   ./scripts/migrate-db.sh --rollback      # Rollback last migration
#   ./scripts/migrate-db.sh --status        # Show current migration status
#   ./scripts/migrate-db.sh --history       # Show migration history
###############################################################################

set -e  # Exit on any error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
CONTAINER_NAME="business_app"
MIGRATIONS_DIR="/app/business_app/migrations"
BACKUP_DIR="./backups"

# Helper functions
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if container is running
check_container() {
    if ! docker compose ps | grep -q "$CONTAINER_NAME.*Up"; then
        log_error "Container '$CONTAINER_NAME' is not running!"
        log_info "Start services with: docker compose up -d"
        exit 1
    fi
    log_success "Container '$CONTAINER_NAME' is running"
}

# Check database connection
check_database() {
    log_info "Checking database connection..."
    if docker compose exec -T postgres pg_isready -U postgres > /dev/null 2>&1; then
        log_success "Database is accessible"
    else
        log_error "Cannot connect to database!"
        exit 1
    fi
}

# Create database backup
create_backup() {
    log_info "Creating database backup..."

    # Create backup directory if it doesn't exist
    mkdir -p "$BACKUP_DIR"

    # Generate backup filename with timestamp
    TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
    BACKUP_FILE="$BACKUP_DIR/bluestream_db_${TIMESTAMP}.sql"

    # Create backup
    docker compose exec -T postgres pg_dump -U postgres bluestream_db > "$BACKUP_FILE"

    if [ -f "$BACKUP_FILE" ]; then
        log_success "Backup created: $BACKUP_FILE"

        # Compress backup
        gzip "$BACKUP_FILE"
        log_success "Backup compressed: ${BACKUP_FILE}.gz"

        # Keep only last 10 backups
        ls -t "$BACKUP_DIR"/bluestream_db_*.sql.gz 2>/dev/null | tail -n +11 | xargs -r rm
        log_info "Old backups cleaned up (keeping last 10)"
    else
        log_error "Backup failed!"
        exit 1
    fi
}

# Show migration status
show_status() {
    log_info "Current migration status:"
    echo ""
    docker compose exec $CONTAINER_NAME bash -c "cd /app && FLASK_APP=business_app python -m flask db current -d $MIGRATIONS_DIR"
    echo ""

    log_info "Pending migrations:"
    echo ""
    docker compose exec $CONTAINER_NAME bash -c "cd /app && FLASK_APP=business_app python -m flask db heads -d $MIGRATIONS_DIR"
}

# Show migration history
show_history() {
    log_info "Migration history:"
    echo ""
    docker compose exec $CONTAINER_NAME bash -c "cd /app && FLASK_APP=business_app python -m flask db history -d $MIGRATIONS_DIR"
}

# Apply migrations (dry run)
dry_run() {
    log_info "Showing pending migrations (DRY RUN - no changes will be made):"
    echo ""
    docker compose exec $CONTAINER_NAME bash -c "cd /app && FLASK_APP=business_app python -m flask db upgrade -d $MIGRATIONS_DIR --sql" | head -50
    echo ""
    log_warning "This was a DRY RUN. No changes were applied."
    log_info "Run without --dry-run to apply migrations."
}

# Apply migrations
apply_migrations() {
    log_info "Applying database migrations..."
    echo ""

    # Run migrations
    if docker compose exec $CONTAINER_NAME bash -c "cd /app && FLASK_APP=business_app python -m flask db upgrade -d $MIGRATIONS_DIR"; then
        log_success "Migrations applied successfully!"
        echo ""

        # Show current status
        show_status
    else
        log_error "Migration failed!"
        log_warning "You may need to restore from backup: $BACKUP_FILE.gz"
        exit 1
    fi
}

# Rollback last migration
rollback() {
    log_warning "Rolling back last migration..."
    echo ""

    read -p "Are you sure you want to rollback the last migration? (yes/no): " -r
    echo
    if [[ ! $REPLY =~ ^[Yy][Ee][Ss]$ ]]; then
        log_info "Rollback cancelled"
        exit 0
    fi

    # Create backup before rollback
    create_backup

    # Rollback
    if docker compose exec $CONTAINER_NAME bash -c "cd /app && FLASK_APP=business_app python -m flask db downgrade -d $MIGRATIONS_DIR"; then
        log_success "Rollback completed!"
        echo ""
        show_status
    else
        log_error "Rollback failed!"
        exit 1
    fi
}

# Main script
main() {
    log_info "=== Database Migration Script ==="
    echo ""

    # Check prerequisites
    check_container
    check_database

    # Parse command
    case "${1:-}" in
        --dry-run)
            dry_run
            ;;
        --rollback)
            rollback
            ;;
        --status)
            show_status
            ;;
        --history)
            show_history
            ;;
        --help|-h)
            cat << EOF
Database Migration Script

Usage:
  $0                 Apply all pending migrations (with backup)
  $0 --dry-run       Show what would be applied without making changes
  $0 --rollback      Rollback the last migration
  $0 --status        Show current migration status
  $0 --history       Show migration history
  $0 --help          Show this help message

Examples:
  # Check status before applying
  $0 --status

  # See what will be applied
  $0 --dry-run

  # Apply migrations (recommended in production)
  $0

  # Rollback if something goes wrong
  $0 --rollback

Notes:
  - Automatic backup is created before applying migrations
  - Backups are stored in ./backups/ directory
  - Last 10 backups are kept automatically
  - Always test migrations in staging environment first

EOF
            ;;
        "")
            # Default: apply migrations with backup
            log_warning "This will apply pending migrations to the database"
            read -p "Continue? (yes/no): " -r
            echo
            if [[ $REPLY =~ ^[Yy][Ee][Ss]$ ]]; then
                create_backup
                apply_migrations
            else
                log_info "Migration cancelled"
                exit 0
            fi
            ;;
        *)
            log_error "Unknown option: $1"
            log_info "Use --help for usage information"
            exit 1
            ;;
    esac
}

# Run main function
main "$@"
