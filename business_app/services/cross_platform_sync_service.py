"""
Cross-Platform User Synchronization Service for BlueStream
Handles user account linking and synchronization between web app and Telegram bot
"""

import logging
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, timezone
from sqlalchemy import or_

from business_app import db
from business_app.models.user import User
from business_app.utils.constants import UserStatus
from business_app.utils.translations import get_translation
from business_app.services.token_service import TokenService

logger = logging.getLogger(__name__)


class CrossPlatformSyncService:
    """Service for handling cross-platform user synchronization and account linking"""
    
    def __init__(self):
        self.token_service = TokenService()
    
    def find_potential_matches(self, email: str = None, phone: str = None, 
                             telegram_id: str = None, exclude_user_id: int = None) -> List[User]:
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
            conditions.append(
                ~User.email.like(f"telegram_%@bluestream.local")
            )
        
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
            'can_link_telegram': False,
            'can_link_web': False,
            'potential_matches': [],
            'linking_benefits': []
        }
        
        # Check if user registered via web but no telegram link
        if current_user.registration_source == 'web' and not current_user.telegram_id:
            suggestions['can_link_telegram'] = True
            suggestions['linking_benefits'].append('Access via Telegram bot')
            suggestions['linking_benefits'].append('Unified notifications')
            
        # Check if user registered via telegram but limited web access
        elif current_user.registration_source == 'telegram':
            # Check if this is a placeholder email
            if current_user.email and current_user.email.startswith('telegram_') and current_user.email.endswith('@bluestream.local'):
                suggestions['can_link_web'] = True
                suggestions['linking_benefits'].append('Full web app access')
                suggestions['linking_benefits'].append('Advanced account management')
                
                # Find potential web accounts with same phone
                if current_user.phone:
                    potential_matches = self.find_potential_matches(
                        phone=current_user.phone,
                        exclude_user_id=current_user.id
                    )
                    suggestions['potential_matches'] = [
                        {
                            'id': match.id,
                            'email': match.email,
                            'name': match.full_name or f"{match.first_name} {match.last_name}".strip(),
                            'registration_source': match.registration_source,
                            'created_at': match.created_at.isoformat()
                        }
                        for match in potential_matches
                        if match.registration_source == 'web'
                    ]
        
        return suggestions
    
    def auto_link_accounts(self, primary_user: User, secondary_user: User, 
                          link_type: str = 'merge') -> Dict[str, Any]:
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
            
            if primary_user.status != UserStatus.ACTIVE.value:
                raise ValueError("Primary user account is not active")
            
            # Perform the linking based on registration sources
            if primary_user.registration_source == 'web' and secondary_user.registration_source == 'telegram':
                return self._link_web_primary_telegram_secondary(primary_user, secondary_user, link_type)
            elif primary_user.registration_source == 'telegram' and secondary_user.registration_source == 'web':
                return self._link_telegram_primary_web_secondary(primary_user, secondary_user, link_type)
            else:
                # Both from same platform - merge based on creation date (keep older account)
                if primary_user.created_at > secondary_user.created_at:
                    primary_user, secondary_user = secondary_user, primary_user
                return self._merge_same_platform_accounts(primary_user, secondary_user)
                
        except Exception as e:
            logger.error(f"Failed to link accounts {primary_user.id} and {secondary_user.id}: {e}")
            db.session.rollback()
            return {
                'success': False,
                'error': str(e)
            }
    
    def _link_web_primary_telegram_secondary(self, web_user: User, telegram_user: User, 
                                           link_type: str) -> Dict[str, Any]:
        """Link web account (primary) with telegram account (secondary)"""
        
        # Transfer telegram-specific data to web user
        web_user.telegram_id = telegram_user.telegram_id
        web_user.telegram_username = telegram_user.telegram_username
        web_user.telegram_first_name = telegram_user.telegram_first_name  
        web_user.telegram_last_name = telegram_user.telegram_last_name
        web_user.telegram_language_code = telegram_user.telegram_language_code
        web_user.is_bot_active = telegram_user.is_bot_active
        web_user.bot_state = telegram_user.bot_state
        web_user.last_bot_interaction = telegram_user.last_bot_interaction
        
        # Update name if web user has incomplete info
        if not web_user.first_name and telegram_user.first_name:
            web_user.first_name = telegram_user.first_name
        if not web_user.last_name and telegram_user.last_name:
            web_user.last_name = telegram_user.last_name
        
        # Update full name
        web_user.full_name = f"{web_user.first_name or ''} {web_user.last_name or ''}".strip()
        
        # Mark telegram user as merged
        telegram_user.status = UserStatus.MERGED.value
        telegram_user.telegram_id = None  # Remove to avoid conflicts
        
        db.session.commit()
        
        logger.info(f"Successfully linked web user {web_user.id} with telegram user {telegram_user.id}")
        
        return {
            'success': True,
            'primary_user_id': web_user.id,
            'secondary_user_id': telegram_user.id,
            'link_type': 'web_primary',
            'message': 'Web and Telegram accounts successfully linked'
        }
    
    def _link_telegram_primary_web_secondary(self, telegram_user: User, web_user: User,
                                           link_type: str) -> Dict[str, Any]:
        """Link telegram account (primary) with web account (secondary)"""
        
        # Transfer web-specific data to telegram user
        telegram_user.email = web_user.email
        telegram_user.password_hash = web_user.password_hash
        telegram_user.phone = web_user.phone or telegram_user.phone
        
        # Update names with more complete web info
        if web_user.first_name:
            telegram_user.first_name = web_user.first_name
        if web_user.last_name:
            telegram_user.last_name = web_user.last_name
        
        # Transfer preferences
        telegram_user.preferred_language = web_user.preferred_language
        telegram_user.preferred_currency = web_user.preferred_currency
        telegram_user.email_notifications = web_user.email_notifications
        telegram_user.sms_notifications = web_user.sms_notifications
        
        # Update verification status
        telegram_user.email_verified_at = web_user.email_verified_at
        telegram_user.phone_verified_at = web_user.phone_verified_at
        telegram_user.is_verified = web_user.is_verified
        
        # Update full name
        telegram_user.full_name = f"{telegram_user.first_name or ''} {telegram_user.last_name or ''}".strip()
        
        # Mark web user as merged
        web_user.status = UserStatus.MERGED.value
        web_user.email = f"merged_{web_user.id}_{web_user.email}"  # Avoid email conflicts
        
        db.session.commit()
        
        logger.info(f"Successfully linked telegram user {telegram_user.id} with web user {web_user.id}")
        
        return {
            'success': True,
            'primary_user_id': telegram_user.id,
            'secondary_user_id': web_user.id, 
            'link_type': 'telegram_primary',
            'message': 'Telegram and Web accounts successfully linked'
        }
    
    def _merge_same_platform_accounts(self, primary_user: User, secondary_user: User) -> Dict[str, Any]:
        """Merge two accounts from the same platform (keep the older one)"""
        
        # Transfer any missing data from secondary to primary
        if not primary_user.phone and secondary_user.phone:
            primary_user.phone = secondary_user.phone
            primary_user.phone_verified_at = secondary_user.phone_verified_at
        
        if not primary_user.email_verified_at and secondary_user.email_verified_at:
            primary_user.email_verified_at = secondary_user.email_verified_at
            primary_user.is_verified = True
        
        # Transfer preferences if primary doesn't have them set
        if not primary_user.preferred_language:
            primary_user.preferred_language = secondary_user.preferred_language
        
        # Mark secondary as merged
        secondary_user.status = UserStatus.MERGED.value
        if secondary_user.email:
            secondary_user.email = f"merged_{secondary_user.id}_{secondary_user.email}"
        if secondary_user.telegram_id:
            secondary_user.telegram_id = None
        
        db.session.commit()
        
        logger.info(f"Successfully merged same-platform accounts: kept {primary_user.id}, merged {secondary_user.id}")
        
        return {
            'success': True,
            'primary_user_id': primary_user.id,
            'secondary_user_id': secondary_user.id,
            'link_type': 'same_platform_merge',
            'message': 'Duplicate accounts successfully merged'
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
            'user_id': user.id,
            'registration_source': user.registration_source,
            'platforms': {
                'web': {
                    'has_access': bool(user.email and not user.email.startswith('telegram_')),
                    'email': user.email if not user.email.startswith('telegram_') else None,
                    'verified': bool(user.email_verified_at),
                    'can_login': bool(user.password_hash and user.password_hash != 'telegram_user')
                },
                'telegram': {
                    'has_access': bool(user.telegram_id),
                    'telegram_id': user.telegram_id,
                    'username': user.telegram_username,
                    'is_active': user.is_bot_active,
                    'last_interaction': user.last_bot_interaction.isoformat() if user.last_bot_interaction else None
                }
            },
            'is_fully_linked': False,
            'linking_opportunities': []
        }
        
        # Check if fully linked
        status['is_fully_linked'] = (
            status['platforms']['web']['has_access'] and 
            status['platforms']['telegram']['has_access']
        )
        
        # Identify linking opportunities
        if not status['platforms']['web']['has_access']:
            status['linking_opportunities'].append({
                'type': 'link_web_account',
                'description': 'Link with web account for full platform access',
                'benefits': ['Advanced account management', 'Web-based ordering', 'Email notifications']
            })
        
        if not status['platforms']['telegram']['has_access']:
            status['linking_opportunities'].append({
                'type': 'link_telegram_account', 
                'description': 'Link with Telegram for instant bot access',
                'benefits': ['Quick ordering via bot', 'Instant notifications', 'Mobile convenience']
            })
        
        return status


# Global service instance
cross_platform_sync_service = CrossPlatformSyncService()