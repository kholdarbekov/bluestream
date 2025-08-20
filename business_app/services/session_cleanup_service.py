"""
Session Cleanup Service for Blue Stream Water Business Platform

Handles cleanup of expired sessions, orphaned data, and user maintenance tasks.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from sqlalchemy import and_, or_, text
from sqlalchemy.orm import load_only

from business_app import db
from business_app.models.user import User, UserSession
from business_app.services.token_service import TokenService

logger = logging.getLogger(__name__)


class SessionCleanupService:
    """Service for cleaning up expired sessions and maintaining user data"""
    
    def __init__(self):
        self.token_service = TokenService()
        
        # Configurable cleanup thresholds
        self.expired_session_threshold = timedelta(days=30)  # Remove sessions expired for 30+ days
        self.inactive_user_threshold = timedelta(days=365)   # Mark users inactive after 1 year
        self.orphaned_data_threshold = timedelta(days=90)    # Remove orphaned data after 90 days
    
    def cleanup_expired_sessions(self, batch_size: int = 1000) -> Dict[str, int]:
        """
        Clean up expired user sessions from database
        
        Args:
            batch_size: Number of records to process per batch
            
        Returns:
            Dictionary with cleanup statistics
        """
        stats = {
            'expired_sessions_removed': 0,
            'inactive_sessions_updated': 0,
            'redis_sessions_cleaned': 0,
            'errors': 0
        }
        
        try:
            logger.info("Starting expired session cleanup")
            
            # Get current time
            now = datetime.now(timezone.utc)
            expiry_cutoff = now - self.expired_session_threshold
            
            # Find expired sessions that should be removed
            expired_sessions = UserSession.query.filter(
                and_(
                    UserSession.expires_at < expiry_cutoff,
                    UserSession.is_active == False
                )
            ).options(load_only('id', 'session_token')).limit(batch_size).all()
            
            # Remove expired sessions
            if expired_sessions:
                session_ids = [session.id for session in expired_sessions]
                deleted_count = UserSession.query.filter(
                    UserSession.id.in_(session_ids)
                ).delete(synchronize_session='fetch')
                
                stats['expired_sessions_removed'] = deleted_count
                logger.info(f"Removed {deleted_count} expired sessions")
            
            # Mark sessions as inactive if they've expired
            inactive_count = UserSession.query.filter(
                and_(
                    UserSession.expires_at < now,
                    UserSession.is_active == True
                )
            ).update({
                'is_active': False,
                'ended_at': now
            }, synchronize_session='fetch')
            
            stats['inactive_sessions_updated'] = inactive_count
            if inactive_count > 0:
                logger.info(f"Marked {inactive_count} sessions as inactive")
            
            # Clean up Redis sessions via TokenService
            redis_cleaned = self.token_service.cleanup_expired_sessions()
            stats['redis_sessions_cleaned'] = redis_cleaned
            
            # Commit changes
            db.session.commit()
            
            logger.info(f"Session cleanup completed: {stats}")
            return stats
            
        except Exception as e:
            logger.error(f"Error during session cleanup: {e}")
            db.session.rollback()
            stats['errors'] = 1
            return stats
    
    def cleanup_inactive_users(self, batch_size: int = 500) -> Dict[str, int]:
        """
        Clean up inactive users and their associated data
        
        Args:
            batch_size: Number of users to process per batch
            
        Returns:
            Dictionary with cleanup statistics
        """
        stats = {
            'users_marked_inactive': 0,
            'orphaned_sessions_removed': 0,
            'bot_states_cleared': 0,
            'errors': 0
        }
        
        try:
            logger.info("Starting inactive user cleanup")
            
            now = datetime.now(timezone.utc)
            inactive_cutoff = now - self.inactive_user_threshold
            
            # Find users who haven't logged in for a long time
            inactive_users = User.query.filter(
                and_(
                    or_(
                        User.last_login < inactive_cutoff,
                        User.last_login.is_(None)
                    ),
                    User.status == 'active',
                    User.created_at < inactive_cutoff  # Don't mark recently created users
                )
            ).options(load_only('id', 'status')).limit(batch_size).all()
            
            if inactive_users:
                user_ids = [user.id for user in inactive_users]
                
                # Mark users as inactive
                updated_count = User.query.filter(
                    User.id.in_(user_ids)
                ).update({
                    'status': 'inactive'
                }, synchronize_session='fetch')
                
                stats['users_marked_inactive'] = updated_count
                logger.info(f"Marked {updated_count} users as inactive")
                
                # Remove their active sessions
                sessions_removed = UserSession.query.filter(
                    and_(
                        UserSession.user_id.in_(user_ids),
                        UserSession.is_active == True
                    )
                ).update({
                    'is_active': False,
                    'ended_at': now
                }, synchronize_session='fetch')
                
                stats['orphaned_sessions_removed'] = sessions_removed
                if sessions_removed > 0:
                    logger.info(f"Deactivated {sessions_removed} sessions for inactive users")
            
            # Clean up bot states for users inactive for extended periods
            bot_cleanup_cutoff = now - timedelta(days=180)  # 6 months
            bot_states_cleared = User.query.filter(
                and_(
                    or_(
                        User.last_bot_interaction < bot_cleanup_cutoff,
                        User.last_bot_interaction.is_(None)
                    ),
                    User.bot_state.isnot(None),
                    User.is_bot_active == False
                )
            ).update({
                'bot_state': None,
                'is_bot_active': False
            }, synchronize_session='fetch')
            
            stats['bot_states_cleared'] = bot_states_cleared
            if bot_states_cleared > 0:
                logger.info(f"Cleared bot states for {bot_states_cleared} inactive users")
            
            # Commit changes
            db.session.commit()
            
            logger.info(f"Inactive user cleanup completed: {stats}")
            return stats
            
        except Exception as e:
            logger.error(f"Error during inactive user cleanup: {e}")
            db.session.rollback()
            stats['errors'] = 1
            return stats
    
    def cleanup_orphaned_data(self) -> Dict[str, int]:
        """
        Clean up orphaned data and inconsistent records
        
        Returns:
            Dictionary with cleanup statistics
        """
        stats = {
            'orphaned_sessions_removed': 0,
            'invalid_tokens_cleaned': 0,
            'password_reset_tokens_cleared': 0,
            'errors': 0
        }
        
        try:
            logger.info("Starting orphaned data cleanup")
            
            now = datetime.now(timezone.utc)
            
            # Remove sessions for users that no longer exist
            orphaned_sessions = db.session.execute(
                text("""
                DELETE FROM user_sessions 
                WHERE user_id NOT IN (SELECT id FROM users)
                """)
            )
            stats['orphaned_sessions_removed'] = orphaned_sessions.rowcount
            
            # Clear expired password reset tokens
            reset_tokens_cleared = User.query.filter(
                and_(
                    User.password_reset_expires < now,
                    User.password_reset_token.isnot(None)
                )
            ).update({
                'password_reset_token': None,
                'password_reset_expires': None
            }, synchronize_session='fetch')
            
            stats['password_reset_tokens_cleared'] = reset_tokens_cleared
            
            # Clean up invalid verification tokens
            verification_cutoff = now - timedelta(days=7)  # Verification tokens expire after 7 days
            verification_tokens_cleared = User.query.filter(
                and_(
                    User.created_at < verification_cutoff,
                    User.email_verified_at.is_(None),
                    User.email_verification_token.isnot(None)
                )
            ).update({
                'email_verification_token': None
            }, synchronize_session='fetch')
            
            if verification_tokens_cleared > 0:
                logger.info(f"Cleared {verification_tokens_cleared} expired verification tokens")
            
            # Commit changes
            db.session.commit()
            
            logger.info(f"Orphaned data cleanup completed: {stats}")
            return stats
            
        except Exception as e:
            logger.error(f"Error during orphaned data cleanup: {e}")
            db.session.rollback()
            stats['errors'] = 1
            return stats
    
    def get_cleanup_statistics(self) -> Dict[str, any]:
        """
        Get statistics about sessions and users that need cleanup
        
        Returns:
            Dictionary with cleanup statistics
        """
        try:
            now = datetime.now(timezone.utc)
            
            stats = {
                'total_users': User.query.count(),
                'active_users': User.query.filter(User.status == 'active').count(),
                'inactive_users': User.query.filter(User.status == 'inactive').count(),
                'total_sessions': UserSession.query.count(),
                'active_sessions': UserSession.query.filter(UserSession.is_active == True).count(),
                'expired_sessions': UserSession.query.filter(
                    and_(
                        UserSession.expires_at < now,
                        UserSession.is_active == True
                    )
                ).count(),
                'old_expired_sessions': UserSession.query.filter(
                    and_(
                        UserSession.expires_at < now - self.expired_session_threshold,
                        UserSession.is_active == False
                    )
                ).count(),
                'users_needing_cleanup': User.query.filter(
                    and_(
                        or_(
                            User.last_login < now - self.inactive_user_threshold,
                            User.last_login.is_(None)
                        ),
                        User.status == 'active',
                        User.created_at < now - self.inactive_user_threshold
                    )
                ).count(),
                'expired_reset_tokens': User.query.filter(
                    and_(
                        User.password_reset_expires < now,
                        User.password_reset_token.isnot(None)
                    )
                ).count()
            }
            
            return stats
            
        except Exception as e:
            logger.error(f"Error getting cleanup statistics: {e}")
            return {}
    
    def full_cleanup(self, batch_size: int = 1000) -> Dict[str, any]:
        """
        Perform a comprehensive cleanup of all session and user data
        
        Args:
            batch_size: Batch size for processing
            
        Returns:
            Combined statistics from all cleanup operations
        """
        logger.info("Starting full session and user cleanup")
        
        # Get initial statistics
        initial_stats = self.get_cleanup_statistics()
        
        # Perform all cleanup operations
        session_stats = self.cleanup_expired_sessions(batch_size)
        user_stats = self.cleanup_inactive_users(batch_size // 2)
        orphaned_stats = self.cleanup_orphaned_data()
        
        # Get final statistics
        final_stats = self.get_cleanup_statistics()
        
        # Combine results
        combined_stats = {
            'initial_state': initial_stats,
            'final_state': final_stats,
            'operations': {
                'session_cleanup': session_stats,
                'user_cleanup': user_stats,
                'orphaned_cleanup': orphaned_stats
            },
            'total_errors': (
                session_stats.get('errors', 0) + 
                user_stats.get('errors', 0) + 
                orphaned_stats.get('errors', 0)
            )
        }
        
        logger.info(f"Full cleanup completed: {combined_stats}")
        return combined_stats
    
    def schedule_cleanup_task(self) -> bool:
        """
        Schedule periodic cleanup task (implementation depends on task queue)
        
        Returns:
            True if task was scheduled successfully
        """
        try:
            # This would integrate with Celery or similar task queue
            # For now, just log the intent
            logger.info("Session cleanup task scheduling requested")
            return True
            
        except Exception as e:
            logger.error(f"Failed to schedule cleanup task: {e}")
            return False