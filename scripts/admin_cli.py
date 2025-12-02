#!/usr/bin/env python3
"""
Admin Management CLI for Blue Stream Water Business Platform

This script provides command-line tools for managing admin users.

Usage:
    python scripts/admin_cli.py create-admin --email admin@example.com --password SecurePass123
    python scripts/admin_cli.py list-users --role admin
    python scripts/admin_cli.py change-role --email user@example.com --role manager
    python scripts/admin_cli.py reset-password --email user@example.com
    python scripts/admin_cli.py deactivate-user --email user@example.com
"""

import sys
import os
import argparse
import getpass
from pathlib import Path

# Add the parent directory to the Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import logging
from flask import Flask
from business_app import create_app
from business_app.services.auth_service import AuthService
from business_app.models.user import User
from business_app.utils.exceptions import ConflictError, ValidationError
from business_app.utils.constants import UserRole, UserStatus
from business_app import db

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def create_admin_user(app: Flask, phone: str, email: str, password: str, first_name: str, last_name: str):
    """Create a new admin user"""
    
    with app.app_context():
        try:
            auth_service = AuthService()
            
            user = auth_service.create_admin_user(
                phone=phone,
                email=email,
                password=password,
                first_name=first_name,
                last_name=last_name
            )
            
            print(f"✓ Successfully created admin user: {user.email}")
            print(f"  User ID: {user.id}")
            print(f"  Name: {user.first_name} {user.last_name}")
            print(f"  Role: {user.role}")
            print(f"  Status: {user.status}")
            
            return user
            
        except ConflictError as e:
            print(f"❌ Error: {e}")
            return None
        except ValidationError as e:
            print(f"❌ Validation Error: {e}")
            return None
        except Exception as e:
            print(f"❌ Unexpected error: {e}")
            return None


def list_users(app: Flask, role: str = None, status: str = None, limit: int = 50):
    """List users with optional filtering"""
    
    with app.app_context():
        try:
            query = User.query
            
            if role:
                query = query.filter(User.role == role)
            
            if status:
                query = query.filter(User.status == status)
            
            users = query.limit(limit).all()
            
            if not users:
                print("No users found matching the criteria.")
                return
            
            print(f"\nFound {len(users)} user(s):")
            print("=" * 80)
            print(f"{'ID':<5} {'Email':<30} {'Name':<25} {'Role':<12} {'Status':<12}")
            print("=" * 80)
            
            for user in users:
                name = f"{user.first_name} {user.last_name}"
                print(f"{user.id:<5} {user.email:<30} {name:<25} {user.role:<12} {user.status:<12}")
            
            print("=" * 80)
            
        except Exception as e:
            print(f"❌ Error listing users: {e}")


def change_user_role(app: Flask, email: str, new_role: str):
    """Change a user's role"""
    
    with app.app_context():
        try:
            # Validate role
            valid_roles = [role.value for role in UserRole]
            if new_role not in valid_roles:
                print(f"❌ Invalid role. Valid roles: {', '.join(valid_roles)}")
                return False
            
            user = User.query.filter_by(email=email).first()
            if not user:
                print(f"❌ User not found: {email}")
                return False
            
            old_role = user.role
            user.role = new_role
            db.session.commit()
            
            print(f"✓ Successfully changed role for {email}")
            print(f"  Old role: {old_role}")
            print(f"  New role: {new_role}")
            
            return True
            
        except Exception as e:
            db.session.rollback()
            print(f"❌ Error changing role: {e}")
            return False


def reset_user_password(app: Flask, email: str, new_password: str = None):
    """Reset a user's password"""
    
    with app.app_context():
        try:
            user = User.query.filter_by(email=email).first()
            if not user:
                print(f"❌ User not found: {email}")
                return False
            
            if not new_password:
                new_password = getpass.getpass("Enter new password: ")
                confirm_password = getpass.getpass("Confirm new password: ")
                
                if new_password != confirm_password:
                    print("❌ Passwords do not match")
                    return False
            
            auth_service = AuthService()
            success = auth_service.change_password(
                user.id,
                user.password_hash,  # This won't work, need to modify for admin reset
                new_password
            )
            
            if success:
                print(f"✓ Successfully reset password for {email}")
                return True
            else:
                print(f"❌ Failed to reset password for {email}")
                return False
            
        except Exception as e:
            print(f"❌ Error resetting password: {e}")
            return False


def deactivate_user(app: Flask, email: str):
    """Deactivate a user account"""
    
    with app.app_context():
        try:
            user = User.query.filter_by(email=email).first()
            if not user:
                print(f"❌ User not found: {email}")
                return False
            
            user.is_active = False
            user.status = UserStatus.INACTIVE.value
            db.session.commit()
            
            print(f"✓ Successfully deactivated user: {email}")
            
            return True
            
        except Exception as e:
            db.session.rollback()
            print(f"❌ Error deactivating user: {e}")
            return False


def activate_user(app: Flask, email: str):
    """Activate a user account"""
    
    with app.app_context():
        try:
            user = User.query.filter_by(email=email).first()
            if not user:
                print(f"❌ User not found: {email}")
                return False
            
            user.is_active = True
            user.status = UserStatus.ACTIVE.value
            db.session.commit()
            
            print(f"✓ Successfully activated user: {email}")
            
            return True
            
        except Exception as e:
            db.session.rollback()
            print(f"❌ Error activating user: {e}")
            return False


def main():
    """Main CLI function"""
    
    parser = argparse.ArgumentParser(description='Blue Stream Admin Management CLI')
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # Create admin command
    create_parser = subparsers.add_parser('create-admin', help='Create a new admin user')
    create_parser.add_argument('--phone', required=True, help='Admin phone number')
    create_parser.add_argument('--email', required=True, help='Admin email address')
    create_parser.add_argument('--password', help='Admin password (will prompt if not provided)')
    create_parser.add_argument('--first-name', default='Admin', help='First name (default: Admin)')
    create_parser.add_argument('--last-name', default='User', help='Last name (default: User)')
    
    # List users command
    list_parser = subparsers.add_parser('list-users', help='List users')
    list_parser.add_argument('--role', help='Filter by role')
    list_parser.add_argument('--status', help='Filter by status')
    list_parser.add_argument('--limit', type=int, default=50, help='Maximum number of users to show')
    
    # Change role command
    role_parser = subparsers.add_parser('change-role', help='Change user role')
    role_parser.add_argument('--email', required=True, help='User email address')
    role_parser.add_argument('--role', required=True, help='New role')
    
    # Reset password command
    reset_parser = subparsers.add_parser('reset-password', help='Reset user password')
    reset_parser.add_argument('--email', required=True, help='User email address')
    reset_parser.add_argument('--password', help='New password (will prompt if not provided)')
    
    # Deactivate user command
    deactivate_parser = subparsers.add_parser('deactivate-user', help='Deactivate user account')
    deactivate_parser.add_argument('--email', required=True, help='User email address')
    
    # Activate user command
    activate_parser = subparsers.add_parser('activate-user', help='Activate user account')
    activate_parser.add_argument('--email', required=True, help='User email address')
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    try:
        app = create_app()
        
        if args.command == 'create-admin':
            password = args.password
            if not password:
                password = getpass.getpass("Enter admin password: ")
                confirm_password = getpass.getpass("Confirm password: ")
                
                if password != confirm_password:
                    print("❌ Passwords do not match")
                    return
            
            create_admin_user(app, args.phone, args.email, password, args.first_name, args.last_name)
            
        elif args.command == 'list-users':
            list_users(app, args.role, args.status, args.limit)
            
        elif args.command == 'change-role':
            change_user_role(app, args.email, args.role)
            
        elif args.command == 'reset-password':
            reset_user_password(app, args.email, args.password)
            
        elif args.command == 'deactivate-user':
            deactivate_user(app, args.email)
            
        elif args.command == 'activate-user':
            activate_user(app, args.email)
        
    except Exception as e:
        print(f"❌ CLI Error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()