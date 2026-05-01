"""
Cross-Platform User Synchronization Service for BlueStream
Handles user account linking and synchronization between web app and Telegram bot
"""

import logging
from typing import Dict, Any, List
from sqlalchemy import or_

from business_app import db
from business_app.models.user import User
from shared.enums import UserStatus
from business_app.services.token_service import TokenService

logger = logging.getLogger(__name__)


class CrossPlatformSyncService:
    """Service for handling cross-platform user synchronization and account linking"""

    def __init__(self):
        self.token_service = TokenService()

    def find_potential_matches(
        self, email: str = None, phone: str = None, telegram_id: str = None, exclude_user_id: int = None
    ) -> List[User]:
        """
        Find potential user account matches across platforms

        Args:
            email: Email to search for
            phone: Phone number to search for
            telegram_id: Telegram ID to search for
            exclude_user_id: User ID to exclude from results

        Returns:
            List of potentially matching User objects
        """
        if not any([email, phone, telegram_id]):
            return []

        query = User.query.filter(User.status == UserStatus.ACTIVE.value)

        if exclude_user_id:
            query = query.filter(User.id != exclude_user_id)

        conditions = []

        if email:
            # Check for exact email match or placeholder telegram emails
            conditions.append(User.email == email)
            # Don't match against placeholder telegram emails
            conditions.append(~User.email.like(f"telegram_%@bluestream.local"))  # noqa: F541

        if phone:
            conditions.append(User.phone == phone)

        if telegram_id:
            conditions.append(User.telegram_id == telegram_id)

        if conditions:
            query = query.filter(or_(*conditions))

        matches = query.all()

        logger.info(f"Found {len(matches)} potential account matches")
        return matches

    def suggest_account_linking(self, current_user: User) -> Dict[str, Any]:
        """
        Analyze current user and suggest potential account linking opportunities

        Args:
            current_user: The current user to analyze

        Returns:
            Dictionary with linking suggestions
        """
        suggestions = {
            "can_link_telegram": False,
            "can_link_web": False,
            "potential_matches": [],
            "linking_benefits": [],
        }

        # Check if user registered via web but no telegram link
        if current_user.registration_source == "web" and not current_user.telegram_id:
            suggestions["can_link_telegram"] = True
            suggestions["linking_benefits"].append("Access via Telegram bot")
            suggestions["linking_benefits"].append("Unified notifications")

        # Check if user registered via telegram but limited web access
        elif current_user.registration_source == "telegram":
            # Check if this is a placeholder email
            if (
                current_user.email
                and current_user.email.startswith("telegram_")
                and current_user.email.endswith("@bluestream.local")
            ):
                suggestions["can_link_web"] = True
                suggestions["linking_benefits"].append("Full web app access")
                suggestions["linking_benefits"].append("Advanced account management")

                # Find potential web accounts with same phone
                if current_user.phone:
                    potential_matches = self.find_potential_matches(
                        phone=current_user.phone, exclude_user_id=current_user.id
                    )
                    suggestions["potential_matches"] = [
                        {
                            "id": match.id,
                            "email": match.email,
                            "name": f"{match.first_name} {match.last_name}".strip(),
                            "registration_source": match.registration_source,
                            "created_at": match.created_at.isoformat(),
                        }
                        for match in potential_matches
                        if match.registration_source == "web"
                    ]

        return suggestions

    def auto_link_accounts(self, primary_user: User, secondary_user: User, link_type: str = "merge") -> Dict[str, Any]:
        """
        Automatically link two user accounts

        Args:
            primary_user: The user account to keep as primary
            secondary_user: The user account to merge/link
            link_type: 'merge' or 'link' (merge removes secondary, link keeps both)

        Returns:
            Dictionary with result information
        """
        try:
            # Validate that accounts can be linked
            if primary_user.id == secondary_user.id:
                raise ValueError("Cannot link user to themselves")

            # Check status - handle both enum and string values
            primary_status = primary_user.status.value if hasattr(primary_user.status, "value") else primary_user.status
            if primary_status != UserStatus.ACTIVE.value:
                raise ValueError("Primary user account is not active")

            # Perform the linking based on registration sources
            if (
                primary_user.registration_source in ("web", "admin_created")
                and secondary_user.registration_source == "telegram"
            ):
                return self._link_web_primary_telegram_secondary(primary_user, secondary_user, link_type)
            elif primary_user.registration_source == "telegram" and secondary_user.registration_source == "web":
                return self._link_telegram_primary_web_secondary(primary_user, secondary_user, link_type)
            else:
                # Both from same platform - merge based on creation date (keep older account)
                if primary_user.created_at > secondary_user.created_at:
                    primary_user, secondary_user = secondary_user, primary_user
                return self._merge_same_platform_accounts(primary_user, secondary_user)

        except Exception as e:
            logger.exception("Failed to link accounts %s and %s", primary_user.id, secondary_user.id)
            db.session.rollback()
            return {"success": False, "error": str(e)}

    def _link_web_primary_telegram_secondary(
        self, web_user: User, telegram_user: User, link_type: str
    ) -> Dict[str, Any]:
        """
        Fully merge telegram account into web account.
        Transfers all related data and deletes the telegram user entirely.
        """
        secondary_id = telegram_user.id
        primary_id = web_user.id

        logger.info(f"Starting full merge: telegram user {secondary_id} -> web user {primary_id}")

        # Save telegram data to transfer
        telegram_id = telegram_user.telegram_id
        telegram_username = telegram_user.telegram_username
        is_bot_active = telegram_user.is_bot_active
        bot_state = telegram_user.bot_state
        last_bot_interaction = telegram_user.last_bot_interaction
        tg_first_name = telegram_user.first_name
        tg_last_name = telegram_user.last_name

        # Step 1: Handle unique constraints FIRST
        self._merge_carts(primary_id, secondary_id)
        self._merge_loyalty_membership(primary_id, secondary_id)

        # Step 2: Transfer all foreign key references
        self._transfer_user_references(primary_id, secondary_id)

        # Step 3: Clear telegram_id from secondary to avoid constraint violation
        telegram_user.telegram_id = None
        db.session.flush()

        # Step 4: Transfer telegram-specific data to web user
        web_user.telegram_id = telegram_id
        web_user.telegram_username = telegram_username
        web_user.is_bot_active = is_bot_active
        web_user.bot_state = bot_state
        web_user.last_bot_interaction = last_bot_interaction

        # Update name if web user has incomplete info
        if not web_user.first_name and tg_first_name:
            web_user.first_name = tg_first_name
        if not web_user.last_name and tg_last_name:
            web_user.last_name = tg_last_name

        # Step 5: Delete the secondary user entirely
        db.session.delete(telegram_user)
        db.session.commit()

        logger.info(f"Successfully merged and deleted telegram user {secondary_id} into web user {primary_id}")

        return {
            "success": True,
            "primary_user_id": primary_id,
            "deleted_user_id": secondary_id,
            "link_type": "full_merge",
            "message": "Accounts fully merged. Telegram user has been deleted.",
        }

    def _merge_carts(self, primary_id: int, secondary_id: int):
        """Merge cart items from secondary user to primary user, then delete secondary cart."""
        from business_app.models.cart import Cart, CartItem

        secondary_cart = Cart.query.filter_by(user_id=secondary_id).first()
        if not secondary_cart:
            return  # No cart to merge

        primary_cart = Cart.query.filter_by(user_id=primary_id).first()

        if primary_cart:
            # Transfer cart items to primary cart
            CartItem.query.filter_by(cart_id=secondary_cart.id).update({"cart_id": primary_cart.id})
            # Delete secondary cart
            db.session.delete(secondary_cart)
        else:
            # Just reassign the cart to primary user
            secondary_cart.user_id = primary_id

        db.session.flush()
        logger.info(f"Merged cart from user {secondary_id} to user {primary_id}")

    def _merge_loyalty_membership(self, primary_id: int, secondary_id: int):
        """Merge loyalty points from secondary to primary."""
        from business_app.models.loyalty import LoyaltyPoints, LoyaltyTransaction

        secondary_points = LoyaltyPoints.query.filter_by(user_id=secondary_id).first()
        if not secondary_points:
            return  # No loyalty points to merge

        primary_points = LoyaltyPoints.query.filter_by(user_id=primary_id).first()

        if primary_points:
            # Add secondary's points to primary
            primary_points.current_points += secondary_points.current_points
            primary_points.lifetime_points += secondary_points.lifetime_points
            primary_points.total_orders += secondary_points.total_orders
            primary_points.total_spent += secondary_points.total_spent

            # Transfer loyalty transactions to primary
            LoyaltyTransaction.query.filter_by(user_id=secondary_id).update({"user_id": primary_id})

            # Delete secondary points record
            db.session.delete(secondary_points)
        else:
            # Just reassign the points record to primary user
            secondary_points.user_id = primary_id

        db.session.flush()
        logger.info(f"Merged loyalty points from user {secondary_id} to user {primary_id}")

    def _transfer_user_references(self, primary_id: int, secondary_id: int):
        """Transfer all foreign key references from secondary user to primary user."""
        # Import all models with user_id references
        from business_app.models.order import Order, OrderStatusHistory
        from business_app.models.payment import Payment, PaymentTransaction, CreditCard
        from business_app.models.subscription import Subscription, SubscriptionLog
        from business_app.models.notification import Notification, NotificationPreference, PushNotificationToken
        from business_app.models.review import Review
        from business_app.models.user import UserAddress, UserSession
        from business_app.models.delivery import Delivery, DeliveryStatusHistory, DeliveryPerson
        from business_app.models.loyalty import ReferralProgram
        from business_app.models.analytics import UserBehavior, UserEvent, ProductView
        from business_app.models.audit import AuditLog

        # Orders
        Order.query.filter_by(user_id=secondary_id).update({"user_id": primary_id})
        OrderStatusHistory.query.filter_by(changed_by=secondary_id).update({"changed_by": primary_id})

        # Payments
        Payment.query.filter_by(user_id=secondary_id).update({"user_id": primary_id})
        PaymentTransaction.query.filter_by(initiated_by=secondary_id).update({"initiated_by": primary_id})
        CreditCard.query.filter_by(user_id=secondary_id).update({"user_id": primary_id})

        # Subscriptions
        Subscription.query.filter_by(user_id=secondary_id).update({"user_id": primary_id})
        SubscriptionLog.query.filter_by(user_id=secondary_id).update({"user_id": primary_id})

        # Notifications
        Notification.query.filter_by(user_id=secondary_id).update({"user_id": primary_id})
        NotificationPreference.query.filter_by(user_id=secondary_id).update({"user_id": primary_id})
        PushNotificationToken.query.filter_by(user_id=secondary_id).update({"user_id": primary_id})

        # Reviews
        Review.query.filter_by(user_id=secondary_id).update({"user_id": primary_id})

        # User data
        UserAddress.query.filter_by(user_id=secondary_id).update({"user_id": primary_id})
        UserSession.query.filter_by(user_id=secondary_id).update({"user_id": primary_id})

        # Delivery
        Delivery.query.filter_by(delivery_person_id=secondary_id).update({"delivery_person_id": primary_id})
        DeliveryStatusHistory.query.filter_by(changed_by=secondary_id).update({"changed_by": primary_id})

        # Handle DeliveryPerson (unique constraint)
        secondary_profile = DeliveryPerson.query.filter_by(user_id=secondary_id).first()
        if secondary_profile:
            primary_profile = DeliveryPerson.query.filter_by(user_id=primary_id).first()
            if primary_profile:
                # Primary already has profile, delete secondary's
                db.session.delete(secondary_profile)
            else:
                # Transfer to primary
                secondary_profile.user_id = primary_id

        # Referrals
        ReferralProgram.query.filter_by(referrer_id=secondary_id).update({"referrer_id": primary_id})
        ReferralProgram.query.filter_by(referee_id=secondary_id).update({"referee_id": primary_id})

        # Analytics (nullable user_id)
        UserBehavior.query.filter_by(user_id=secondary_id).update({"user_id": primary_id})
        UserEvent.query.filter_by(user_id=secondary_id).update({"user_id": primary_id})
        ProductView.query.filter_by(user_id=secondary_id).update({"user_id": primary_id})

        # Audit logs
        AuditLog.query.filter_by(user_id=secondary_id).update({"user_id": primary_id})

        db.session.flush()
        logger.info(f"Transferred all user references from {secondary_id} to {primary_id}")

    def _link_telegram_primary_web_secondary(
        self, telegram_user: User, web_user: User, link_type: str
    ) -> Dict[str, Any]:
        """
        Fully merge web account into telegram account.
        Transfers all related data and deletes the web user entirely.
        """
        secondary_id = web_user.id
        primary_id = telegram_user.id

        logger.info(f"Starting full merge: web user {secondary_id} -> telegram user {primary_id}")

        # Save web data to transfer
        web_email = web_user.email
        web_password_hash = web_user.password_hash
        web_phone = web_user.phone
        web_first_name = web_user.first_name
        web_last_name = web_user.last_name
        web_preferred_language = web_user.preferred_language
        web_preferred_currency = web_user.preferred_currency
        web_email_notifications = web_user.email_notifications
        web_sms_notifications = web_user.sms_notifications
        web_email_verified_at = web_user.email_verified_at
        web_phone_verified_at = web_user.phone_verified_at
        web_is_verified = web_user.is_verified

        # Step 1: Handle unique constraints FIRST
        self._merge_carts(primary_id, secondary_id)
        self._merge_loyalty_membership(primary_id, secondary_id)

        # Step 2: Transfer all foreign key references
        self._transfer_user_references(primary_id, secondary_id)

        # Step 3: Clear email from secondary to avoid constraint violation (email is unique)
        web_user.email = f"deleted_{secondary_id}@bluestream.local"
        web_user.phone = None
        db.session.flush()

        # Step 4: Transfer web-specific data to telegram user
        telegram_user.email = web_email
        telegram_user.password_hash = web_password_hash
        telegram_user.phone = web_phone or telegram_user.phone

        # Preserve primary telegram profile names; only backfill missing values
        if web_first_name and (not telegram_user.first_name or telegram_user.first_name == "Telegram User"):
            telegram_user.first_name = web_first_name
        if web_last_name and not telegram_user.last_name:
            telegram_user.last_name = web_last_name

        # Transfer preferences
        telegram_user.preferred_language = web_preferred_language
        telegram_user.preferred_currency = web_preferred_currency
        telegram_user.email_notifications = web_email_notifications
        telegram_user.sms_notifications = web_sms_notifications

        # Update verification status
        telegram_user.email_verified_at = web_email_verified_at
        telegram_user.phone_verified_at = web_phone_verified_at
        telegram_user.is_verified = web_is_verified

        # Step 5: Delete the secondary user entirely
        db.session.delete(web_user)
        db.session.commit()

        logger.info(f"Successfully merged and deleted web user {secondary_id} into telegram user {primary_id}")

        return {
            "success": True,
            "primary_user_id": primary_id,
            "deleted_user_id": secondary_id,
            "link_type": "full_merge",
            "message": "Accounts fully merged. Web user has been deleted.",
        }

    def _merge_same_platform_accounts(self, primary_user: User, secondary_user: User) -> Dict[str, Any]:
        """
        Merge two accounts from the same platform (keep the older one).
        Fully deletes the secondary user after transferring all data.
        """
        primary_id = primary_user.id
        secondary_id = secondary_user.id

        logger.info(f"Starting same-platform merge: user {secondary_id} -> user {primary_id}")

        # Step 1: Handle unique constraints FIRST
        self._merge_carts(primary_id, secondary_id)
        self._merge_loyalty_membership(primary_id, secondary_id)

        # Step 2: Transfer all foreign key references
        self._transfer_user_references(primary_id, secondary_id)

        # Step 3: Transfer any missing data from secondary to primary
        if not primary_user.phone and secondary_user.phone:
            primary_user.phone = secondary_user.phone
            primary_user.phone_verified_at = secondary_user.phone_verified_at

        if not primary_user.email_verified_at and secondary_user.email_verified_at:
            primary_user.email_verified_at = secondary_user.email_verified_at
            primary_user.is_verified = True

        if not primary_user.preferred_language:
            primary_user.preferred_language = secondary_user.preferred_language

        # Transfer telegram data if primary doesn't have it
        if secondary_user.telegram_id and not primary_user.telegram_id:
            secondary_telegram_id = secondary_user.telegram_id
            secondary_user.telegram_id = None  # Clear to avoid unique constraint
            db.session.flush()
            primary_user.telegram_id = secondary_telegram_id
            primary_user.telegram_username = secondary_user.telegram_username
            primary_user.is_bot_active = secondary_user.is_bot_active
            primary_user.bot_state = secondary_user.bot_state
            primary_user.last_bot_interaction = secondary_user.last_bot_interaction

        # Step 4: Clear unique constraints on secondary before delete
        if secondary_user.email:
            secondary_user.email = f"deleted_{secondary_id}@bluestream.local"
        if secondary_user.phone:
            secondary_user.phone = None
        db.session.flush()

        # Step 5: Delete the secondary user entirely
        db.session.delete(secondary_user)
        db.session.commit()

        logger.info(f"Successfully merged and deleted user {secondary_id} into user {primary_id}")

        return {
            "success": True,
            "primary_user_id": primary_id,
            "deleted_user_id": secondary_id,
            "link_type": "full_merge",
            "message": "Accounts fully merged and secondary user deleted",
        }

    def get_user_platform_status(self, user: User) -> Dict[str, Any]:
        """
        Get comprehensive platform access status for a user

        Args:
            user: User to analyze

        Returns:
            Dictionary with platform access information
        """
        status = {
            "user_id": user.id,
            "registration_source": user.registration_source,
            "platforms": {
                "web": {
                    "has_access": bool(user.email and not user.email.startswith("telegram_")),
                    "email": user.email if not user.email.startswith("telegram_") else None,
                    "verified": bool(user.email_verified_at),
                    "can_login": bool(user.password_hash and user.password_hash != "telegram_user"),
                },
                "telegram": {
                    "has_access": bool(user.telegram_id),
                    "telegram_id": user.telegram_id,
                    "username": user.telegram_username,
                    "is_active": user.is_bot_active,
                    "last_interaction": user.last_bot_interaction.isoformat() if user.last_bot_interaction else None,
                },
            },
            "is_fully_linked": False,
            "linking_opportunities": [],
        }

        # Check if fully linked
        status["is_fully_linked"] = (
            status["platforms"]["web"]["has_access"] and status["platforms"]["telegram"]["has_access"]
        )

        # Identify linking opportunities
        if not status["platforms"]["web"]["has_access"]:
            status["linking_opportunities"].append(
                {
                    "type": "link_web_account",
                    "description": "Link with web account for full platform access",
                    "benefits": ["Advanced account management", "Web-based ordering", "Email notifications"],
                }
            )

        if not status["platforms"]["telegram"]["has_access"]:
            status["linking_opportunities"].append(
                {
                    "type": "link_telegram_account",
                    "description": "Link with Telegram for instant bot access",
                    "benefits": ["Quick ordering via bot", "Instant notifications", "Mobile convenience"],
                }
            )

        return status


# Global service instance
cross_platform_sync_service = CrossPlatformSyncService()
