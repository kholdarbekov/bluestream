#!/usr/bin/env python3
"""
Database backup script to run before multilingual migration
Creates a full PostgreSQL backup with timestamp
"""
import os
import sys
import subprocess
import argparse
from datetime import datetime, UTC

def get_database_url():
    """Get database URL from environment or return default"""
    # Try different environment variable names
    db_url_vars = ['DATABASE_URL', 'SQLALCHEMY_DATABASE_URI', 'DB_URL']
    
    for var in db_url_vars:
        if os.getenv(var):
            return os.getenv(var)
    
    # Default fallback
    return "postgresql://postgres:postgres@localhost:5432/bluestream_db"

def parse_database_url(url):
    """Parse database URL into connection parameters"""
    # Simple parser for postgresql://user:password@host:port/database
    if url.startswith('postgresql://'):
        url = url.replace('postgresql://', '')
    elif url.startswith('postgres://'):
        url = url.replace('postgres://', '')
    
    # Split user:password@host:port/database
    if '@' in url:
        auth, rest = url.split('@', 1)
        if ':' in auth:
            user, password = auth.split(':', 1)
        else:
            user = auth
            password = None
    else:
        user = 'postgres'
        password = None
        rest = url
    
    # Split host:port/database
    if '/' in rest:
        host_port, database = rest.split('/', 1)
    else:
        host_port = rest
        database = 'bluestream_db'
    
    if ':' in host_port:
        host, port = host_port.split(':', 1)
    else:
        host = host_port
        port = '5432'
    
    return {
        'user': user,
        'password': password,
        'host': host,
        'port': port,
        'database': database
    }

def create_backup(db_params, backup_dir, dry_run=False):
    """Create a PostgreSQL backup using pg_dump"""
    
    # Create backup directory if it doesn't exist
    if not os.path.exists(backup_dir):
        if not dry_run:
            os.makedirs(backup_dir)
        print(f"Created backup directory: {backup_dir}")
    
    # Generate backup filename with timestamp
    timestamp = datetime.now(UTC).strftime('%Y%m%d_%H%M%S')
    backup_filename = f"bluestream_backup_{timestamp}.sql"
    backup_path = os.path.join(backup_dir, backup_filename)
    
    # Prepare pg_dump command
    cmd = [
        'pg_dump',
        '--host', db_params['host'],
        '--port', db_params['port'],
        '--username', db_params['user'],
        '--dbname', db_params['database'],
        '--verbose',
        '--clean',
        '--no-owner',
        '--no-privileges',
        '--file', backup_path
    ]
    
    # Set up environment for password
    env = os.environ.copy()
    if db_params['password']:
        env['PGPASSWORD'] = db_params['password']
    
    print(f"\n🔄 Creating backup...")
    print(f"Database: {db_params['database']}@{db_params['host']}:{db_params['port']}")
    print(f"Backup file: {backup_path}")
    print(f"Command: {' '.join(cmd[:6])} [credentials hidden] --file {backup_path}")
    
    if dry_run:
        print("🔍 DRY RUN: Backup command prepared but not executed")
        return backup_path
    
    try:
        # Run pg_dump
        result = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            check=True
        )
        
        # Check if backup file was created
        if os.path.exists(backup_path):
            file_size = os.path.getsize(backup_path)
            file_size_mb = file_size / (1024 * 1024)
            print(f"✅ Backup created successfully!")
            print(f"File size: {file_size_mb:.2f} MB")
            
            # Create a compressed version
            compressed_path = backup_path + '.gz'
            compress_cmd = ['gzip', '-c', backup_path]
            with open(compressed_path, 'wb') as f:
                subprocess.run(compress_cmd, stdout=f, check=True)
            
            compressed_size = os.path.getsize(compressed_path)
            compressed_size_mb = compressed_size / (1024 * 1024)
            print(f"✅ Compressed backup created: {compressed_path}")
            print(f"Compressed size: {compressed_size_mb:.2f} MB")
            
            return backup_path
        else:
            print("❌ Backup file not created")
            return None
            
    except subprocess.CalledProcessError as e:
        print(f"❌ Backup failed: {e}")
        if e.stderr:
            print(f"Error output: {e.stderr}")
        return None
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        return None

def verify_backup(backup_path):
    """Verify the backup file is valid"""
    if not os.path.exists(backup_path):
        print(f"❌ Backup file not found: {backup_path}")
        return False
    
    print(f"\n🔍 Verifying backup file...")
    
    try:
        with open(backup_path, 'r', encoding='utf-8') as f:
            # Read first few lines to verify it's a SQL dump
            lines = [f.readline().strip() for _ in range(10)]
            
        # Check for pg_dump header
        header_found = any('PostgreSQL database dump' in line for line in lines)
        if not header_found:
            print("❌ Backup file doesn't appear to be a valid pg_dump file")
            return False
        
        # Check for critical tables
        with open(backup_path, 'r', encoding='utf-8') as f:
            content = f.read()
            
        critical_tables = [
            'CREATE TABLE products',
            'CREATE TABLE product_categories', 
            'CREATE TABLE users',
            'CREATE TABLE orders'
        ]
        
        missing_tables = []
        for table in critical_tables:
            if table not in content:
                missing_tables.append(table)
        
        if missing_tables:
            print(f"⚠️  Warning: Some expected tables not found in backup:")
            for table in missing_tables:
                print(f"  - {table}")
        else:
            print("✅ Backup verification successful - all critical tables found")
        
        return True
        
    except Exception as e:
        print(f"❌ Error verifying backup: {e}")
        return False

def main():
    """Main backup function"""
    parser = argparse.ArgumentParser(description='Create database backup before multilingual migration')
    parser.add_argument('--backup-dir', default='./backups', help='Directory to store backups')
    parser.add_argument('--database-url', help='Database URL (defaults to environment variables)')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be done without executing')
    parser.add_argument('--verify-only', help='Verify an existing backup file')
    
    args = parser.parse_args()
    
    if args.verify_only:
        if verify_backup(args.verify_only):
            print("✅ Backup verification passed")
            return 0
        else:
            print("❌ Backup verification failed")
            return 1
    
    print("🗂️  BlueStream Database Backup")
    print("=" * 40)
    print(f"Timestamp: {datetime.now(UTC).isoformat()}")
    
    # Get database connection parameters
    db_url = args.database_url or get_database_url()
    db_params = parse_database_url(db_url)
    
    print(f"Database: {db_params['database']}")
    print(f"Host: {db_params['host']}:{db_params['port']}")
    print(f"User: {db_params['user']}")
    
    if args.dry_run:
        print("\n🔍 DRY RUN MODE - No actual backup will be created")
    
    # Create backup
    backup_path = create_backup(db_params, args.backup_dir, args.dry_run)
    
    if backup_path and not args.dry_run:
        # Verify backup
        if verify_backup(backup_path):
            print(f"\n✅ Backup completed successfully!")
            print(f"📁 Backup location: {backup_path}")
            print(f"📁 Compressed backup: {backup_path}.gz")
            print("\n⚠️  IMPORTANT: Keep this backup safe before running migration!")
            return 0
        else:
            print(f"\n❌ Backup verification failed!")
            return 1
    elif args.dry_run:
        print(f"\n🔍 DRY RUN completed successfully")
        return 0
    else:
        print(f"\n❌ Backup creation failed!")
        return 1

if __name__ == '__main__':
    sys.exit(main())