#!/usr/bin/env python3
"""
Admin User Seeding Script for Blue Stream Water Business Platform

This script creates the initial admin user and other administrative accounts.
Run this script after setting up the database and before starting the application.

Usage:
    python scripts/seed_admin.py
"""

import sys
import os
import secrets
import string
import getpass
from pathlib import Path

# Add the parent directory to the Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import logging
from flask import Flask
from business_app import create_app
from business_app.services.auth_service import AuthService
from business_app.utils.exceptions import ConflictError, ValidationError
from business_app.utils.constants import UserRole, UserStatus

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def generate_secure_password(length: int = 16) -> str:
    """Generate a secure random password"""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    password = ''.join(secrets.choice(alphabet) for _ in range(length))

    # Ensure password has at least one of each character type
    if not any(c.islower() for c in password):
        password = password[:-1] + secrets.choice(string.ascii_lowercase)
    if not any(c.isupper() for c in password):
        password = password[:-1] + secrets.choice(string.ascii_uppercase)
    if not any(c.isdigit() for c in password):
        password = password[:-1] + secrets.choice(string.digits)
    if not any(c in "!@#$%^&*" for c in password):
        password = password[:-1] + secrets.choice("!@#$%^&*")

    return password


def create_admin_users(app: Flask):
    """Create initial admin users"""

    with app.app_context():
        auth_service = AuthService()

        # Generate secure passwords for admin users
        admin_password = generate_secure_password()
        manager_password = generate_secure_password()
        operator_password = generate_secure_password()

        # Admin users to create
        admin_users = [
            {
                'email': 'admin@bluestream.com',
                'password': admin_password,
                'first_name': 'System',
                'last_name': 'Administrator',
                'role': UserRole.ADMIN.value
            },
            {
                'email': 'manager@bluestream.com',
                'password': manager_password,
                'first_name': 'General',
                'last_name': 'Manager',
                'role': UserRole.MANAGER.value
            },
            {
                'email': 'operator@bluestream.com',
                'password': operator_password,
                'first_name': 'System',
                'last_name': 'Operator',
                'role': UserRole.OPERATOR.value
            }
        ]

        created_users = []

        for user_data in admin_users:
            try:
                if user_data['role'] == UserRole.ADMIN.value:
                    # Use create_admin_user for admin
                    user = auth_service.create_admin_user(
                        email=user_data['email'],
                        password=user_data['password'],
                        first_name=user_data['first_name'],
                        last_name=user_data['last_name']
                    )
                else:
                    # Use register_user for other roles
                    user, tokens = auth_service.register_user(
                        email=user_data['email'],
                        password=user_data['password'],
                        phone='+998901234567',  # Default phone
                        first_name=user_data['first_name'],
                        last_name=user_data['last_name'],
                        role=user_data['role'],
                        status=UserStatus.ACTIVE.value,
                        email_verified=True,
                        is_verified=True
                    )

                created_users.append(user)
                logger.info(f"✓ Created {user_data['role']} user: {user.email}")

            except ConflictError as e:
                logger.warning(f"⚠ User {user_data['email']} already exists: {e}")
                continue
            except ValidationError as e:
                logger.error(f"✗ Validation error for {user_data['email']}: {e}")
                continue
            except Exception as e:
                logger.error(f"✗ Failed to create {user_data['email']}: {e}")
                continue

        logger.info(f"\n{'='*50}")
        logger.info(f"Admin User Seeding Complete!")
        logger.info(f"Created {len(created_users)} new admin users")
        logger.info(f"{'='*50}")

        if created_users:
            logger.info("\nLogin Credentials:")
            logger.info("-" * 30)
            for i, user_data in enumerate(admin_users):
                if i < len(created_users):
                    logger.info(f"Email: {user_data['email']}")
                    logger.info(f"Password: {user_data['password']}")
                    logger.info(f"Role: {user_data['role']}")
                    logger.info("-" * 30)

            logger.info("\n⚠ IMPORTANT SECURITY NOTES:")
            logger.info("1. Change these default passwords immediately after first login")
            logger.info("2. Use strong, unique passwords for production")
            logger.info("3. Enable two-factor authentication if available")
            logger.info("4. Remove this script from production servers")

        # Write credentials to secure file for admin reference
        credentials_file = project_root / 'admin_credentials.txt'
        with open(credentials_file, 'w', encoding='utf-8') as f:
            f.write("BLUESTREAM ADMIN CREDENTIALS\n")
            f.write("=" * 50 + "\n\n")
            f.write("⚠️  IMPORTANT: Delete this file after recording credentials securely!\n\n")
            for user_data in admin_users:
                f.write(f"Email: {user_data['email']}\n")
                f.write(f"Password: {user_data['password']}\n")
                f.write(f"Role: {user_data['role']}\n")
                f.write("-" * 30 + "\n")
            f.write("\nSECURITY NOTES:\n")
            f.write("1. Change these passwords immediately after first login\n")
            f.write("2. Use strong, unique passwords for production\n")
            f.write("3. Enable two-factor authentication if available\n")
            f.write("4. DELETE THIS FILE after recording credentials\n")

        # Set secure file permissions (readable only by owner)
        import stat
        credentials_file.chmod(stat.S_IRUSR | stat.S_IWUSR)

        logger.info(f"\n🔐 Admin credentials saved to: {credentials_file}")
        logger.info("⚠️  DELETE this file after recording credentials securely!")

        return created_users, admin_password


def verify_admin_access(app: Flask, admin_password: str):
    """Verify that admin users can access admin endpoints"""

    with app.app_context():
        auth_service = AuthService()

        # Test admin login
        try:
            admin_user, tokens = auth_service.login_user(
                'admin@bluestream.com',
                admin_password
            )

            # Check permissions
            permissions = auth_service.get_user_permissions(admin_user.id)

            logger.info(f"\n✓ Admin user verification successful")
            logger.info(f"User ID: {admin_user.id}")
            logger.info(f"Role: {admin_user.role}")
            logger.info(f"Admin Panel Access: {permissions.get('can_view_admin_panel', False)}")
            logger.info(f"User Management: {permissions.get('can_manage_users', False)}")

            return True

        except Exception as e:
            logger.error(f"✗ Admin verification failed: {e}")
            return False


def main():
    """Main function"""

    logger.info("Starting Blue Stream Admin User Seeding...")
    logger.info("=" * 50)

    try:
        # Create Flask app
        app = create_app()

        # Create admin users
        created_users, admin_password = create_admin_users(app)

        # Verify admin access (only if users were created)
        if created_users:
            logger.info("\nVerifying admin access...")
            verify_admin_access(app, admin_password)

        logger.info("\n🎉 Admin seeding completed successfully!")

    except Exception as e:
        logger.error(f"❌ Admin seeding failed: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
