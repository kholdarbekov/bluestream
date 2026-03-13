"""
Comprehensive audit logging system for the BlueStream platform.
This module provides centralized audit logging for all sensitive operations.
"""

import json
import time
import uuid
from datetime import datetime, UTC
from typing import Dict, Any, Optional, List
from enum import Enum
from functools import wraps

from flask import request, g, current_app, has_app_context, has_request_context
from flask_jwt_extended import get_jwt_identity, get_jwt

from business_app import db
from business_app.models.audit import AuditLog, AuditEventType, AuditSeverity


class AuditLogger:
    """Centralized audit logging service."""
    
    def __init__(self):
        self.enabled = True
        self.log_to_database = True
        self.log_to_file = True
        self.sensitive_fields = [
            'password', 'password_hash', 'token', 'secret', 'key',
            'credit_card', 'ssn', 'tax_id'
        ]
    
    def _get_request_context(self) -> Dict[str, Any]:
        """Extract request context information."""
        context = {}

        if has_request_context():
            context.update({
                'ip_address': request.remote_addr,
                'user_agent': request.headers.get('User-Agent'),
                'endpoint': request.endpoint,
                'method': request.method,
                'url': request.url,
                'referer': request.headers.get('Referer')
            })
        
        return context
    
    def _get_user_context(self) -> Dict[str, Any]:
        """Extract user context information."""
        context = {}
        
        try:
            user_id = None
            if has_request_context() and (hasattr(g, 'current_user_id') or request.headers.get('Authorization')):
                user_id = get_jwt_identity()
            if user_id:
                context['user_id'] = user_id

                claims = get_jwt() if has_request_context() else {}
                if claims:
                    context['user_role'] = claims.get('role')
                    context['session_id'] = claims.get('jti')  # JWT ID
            
            # Get additional context from Flask g if available
            if has_app_context() and hasattr(g, 'current_user'):
                context['user_id'] = g.current_user.id
                # Convert enum to string value for database storage
                role = g.current_user.role
                context['user_role'] = role.value if hasattr(role, 'value') else str(role)
            
        except Exception as e:
            current_app.logger.debug(f"Could not extract user context: {e}")
        
        return context
    
    def _sanitize_data(self, data: Any) -> Any:
        """Remove sensitive information from data before logging."""
        if not data:
            return data
        
        if isinstance(data, dict):
            sanitized = {}
            for key, value in data.items():
                if any(sensitive in key.lower() for sensitive in self.sensitive_fields):
                    sanitized[key] = "[REDACTED]"
                else:
                    sanitized[key] = self._sanitize_data(value)
            return sanitized
        
        elif isinstance(data, list):
            return [self._sanitize_data(item) for item in data]
        
        elif isinstance(data, str) and len(data) > 1000:
            return data[:1000] + "... [TRUNCATED]"
        
        return data
    
    def log_event(self,
                  event_type: AuditEventType,
                  action: str,
                  severity: AuditSeverity = AuditSeverity.MEDIUM,
                  resource_type: str = None,
                  resource_id: str = None,
                  description: str = None,
                  old_values: Dict = None,
                  new_values: Dict = None,
                  success: bool = True,
                  error_message: str = None,
                  duration_ms: int = None,
                  additional_data: Dict = None) -> str:
        """
        Log an audit event.
        
        Returns:
            The unique event ID for the logged event
        """
        if not self.enabled:
            return None
        
        # Generate unique event ID
        event_id = str(uuid.uuid4())
        
        # Get context information
        request_context = self._get_request_context()
        user_context = self._get_user_context()
        
        # Sanitize sensitive data
        old_values = self._sanitize_data(old_values)
        new_values = self._sanitize_data(new_values)
        additional_data = self._sanitize_data(additional_data)
        
        # Create audit log entry
        audit_entry = {
            'event_id': event_id,
            'event_type': event_type,
            'severity': severity,
            'action': action,
            'resource_type': resource_type,
            'resource_id': str(resource_id) if resource_id else None,
            'description': description,
            'old_values': old_values,
            'new_values': new_values,
            'success': success,
            'error_message': error_message,
            'duration_ms': duration_ms,
            'additional_data': additional_data,
            'timestamp': datetime.now(UTC).isoformat()
        }
        
        # Add context information
        audit_entry.update(request_context)
        audit_entry.update(user_context)
        
        # Log to application logger
        if self.log_to_file:
            self._log_to_file(audit_entry)
        
        # Log to database
        if self.log_to_database:
            self._log_to_database(audit_entry)
        
        return event_id
    
    def _log_to_file(self, audit_entry: Dict):
        """Log audit entry to application logger."""
        log_message = (
            f"AUDIT [{audit_entry['event_id']}]: "
            f"{audit_entry['event_type'].value} - {audit_entry['action']} "
            f"by user {audit_entry.get('user_id', 'anonymous')} "
            f"from {audit_entry.get('ip_address', 'unknown')} "
            f"{'SUCCESS' if audit_entry['success'] else 'FAILED'}"
        )
        
        if audit_entry['severity'] == AuditSeverity.CRITICAL:
            current_app.logger.critical(log_message)
        elif audit_entry['severity'] == AuditSeverity.HIGH:
            current_app.logger.error(log_message)
        elif audit_entry['severity'] == AuditSeverity.MEDIUM:
            current_app.logger.warning(log_message)
        else:
            current_app.logger.info(log_message)
        
        # Log additional details at debug level
        current_app.logger.debug(f"AUDIT DETAILS [{audit_entry['event_id']}]: {json.dumps(audit_entry, default=str)}")
    
    def _log_to_database(self, audit_entry: Dict):
        """Log audit entry to database."""
        try:
            audit_log = AuditLog(
                event_id=audit_entry['event_id'],
                event_type=audit_entry['event_type'],
                severity=audit_entry['severity'],
                user_id=audit_entry.get('user_id'),
                user_role=audit_entry.get('user_role'),
                session_id=audit_entry.get('session_id'),
                ip_address=audit_entry.get('ip_address'),
                user_agent=audit_entry.get('user_agent'),
                endpoint=audit_entry.get('endpoint'),
                method=audit_entry.get('method'),
                resource_type=audit_entry.get('resource_type'),
                resource_id=audit_entry.get('resource_id'),
                action=audit_entry['action'],
                description=audit_entry.get('description'),
                old_values=audit_entry.get('old_values'),
                new_values=audit_entry.get('new_values'),
                duration_ms=audit_entry.get('duration_ms'),
                success=audit_entry['success'],
                error_message=audit_entry.get('error_message'),
                additional_data=audit_entry.get('additional_data')
            )
            
            db.session.add(audit_log)
            db.session.commit()
            
        except Exception as e:
            current_app.logger.error(f"Failed to log audit entry to database: {e}")
            # Don't re-raise to avoid breaking the main application flow
    
    def log_authentication_event(self, event_type: AuditEventType, user_id: int = None, 
                                success: bool = True, error_message: str = None):
        """Log authentication-related events."""
        return self.log_event(
            event_type=event_type,
            action=event_type.value,
            severity=AuditSeverity.MEDIUM if success else AuditSeverity.HIGH,
            resource_type="authentication",
            resource_id=str(user_id) if user_id else None,
            success=success,
            error_message=error_message
        )
    
    def log_data_change(self, event_type: AuditEventType, resource_type: str, 
                       resource_id: str, old_values: Dict = None, 
                       new_values: Dict = None, action: str = None):
        """Log data modification events."""
        return self.log_event(
            event_type=event_type,
            action=action or event_type.value,
            severity=AuditSeverity.MEDIUM,
            resource_type=resource_type,
            resource_id=str(resource_id),
            old_values=old_values,
            new_values=new_values
        )
    
    def log_security_event(self, event_type: AuditEventType, description: str,
                          severity: AuditSeverity = AuditSeverity.HIGH,
                          additional_data: Dict = None):
        """Log security-related events."""
        return self.log_event(
            event_type=event_type,
            action=event_type.value,
            severity=severity,
            resource_type="security",
            description=description,
            additional_data=additional_data
        )
    
    def log_system_event(self, action: str, description: str = None,
                        severity: AuditSeverity = AuditSeverity.MEDIUM,
                        additional_data: Dict = None):
        """Log system administration events."""
        return self.log_event(
            event_type=AuditEventType.SYSTEM_MAINTENANCE,
            action=action,
            severity=severity,
            resource_type="system",
            description=description,
            additional_data=additional_data
        )


# Global audit logger instance
audit_logger = AuditLogger()


def audit_event(event_type: AuditEventType, action: str = None,
               severity: AuditSeverity = AuditSeverity.MEDIUM,
               resource_type: str = None):
    """
    Decorator for automatic audit logging of function calls.
    
    Args:
        event_type: Type of audit event
        action: Action description (defaults to function name)
        severity: Event severity level
        resource_type: Type of resource being operated on
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            start_time = time.time()
            event_id = None
            success = True
            error_message = None
            
            try:
                # Execute the function
                result = f(*args, **kwargs)
                
                # Log successful execution
                duration_ms = int((time.time() - start_time) * 1000)
                event_id = audit_logger.log_event(
                    event_type=event_type,
                    action=action or f.__name__,
                    severity=severity,
                    resource_type=resource_type,
                    success=True,
                    duration_ms=duration_ms
                )
                
                return result
                
            except Exception as e:
                # Log failed execution
                success = False
                error_message = str(e)
                duration_ms = int((time.time() - start_time) * 1000)
                
                event_id = audit_logger.log_event(
                    event_type=event_type,
                    action=action or f.__name__,
                    severity=AuditSeverity.HIGH,
                    resource_type=resource_type,
                    success=False,
                    error_message=error_message,
                    duration_ms=duration_ms
                )
                
                raise
        
        return decorated_function
    return decorator


def audit_data_change(resource_type: str, event_type: AuditEventType = None):
    """
    Decorator for auditing data changes with before/after values.
    
    Args:
        resource_type: Type of resource being modified
        event_type: Type of audit event (auto-detected if not provided)
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # Try to capture old values before execution
            old_values = None
            resource_id = None
            
            # Look for resource ID in arguments
            if 'id' in kwargs:
                resource_id = kwargs['id']
            elif args and hasattr(args[0], 'id'):
                resource_id = args[0].id
            
            # Try to get old values if we have a resource ID
            if resource_id and resource_type:
                try:
                    # This is a simplified approach - in practice, you'd want to
                    # implement specific logic for each resource type
                    old_values = {"resource_id": resource_id, "note": "Old values capture not implemented"}
                except Exception:
                    pass
            
            start_time = time.time()
            
            try:
                # Execute the function
                result = f(*args, **kwargs)
                
                # Try to capture new values after execution
                new_values = None
                if hasattr(result, 'to_dict'):
                    new_values = result.to_dict()
                elif isinstance(result, dict):
                    new_values = result
                
                # Determine event type if not provided
                actual_event_type = event_type
                if not actual_event_type:
                    function_name = f.__name__.lower()
                    if 'create' in function_name:
                        actual_event_type = AuditEventType.USER_CREATED  # This would be dynamic based on resource_type
                    elif 'update' in function_name:
                        actual_event_type = AuditEventType.USER_UPDATED
                    elif 'delete' in function_name:
                        actual_event_type = AuditEventType.USER_DELETED
                    else:
                        actual_event_type = AuditEventType.SYSTEM_MAINTENANCE
                
                # Log the data change
                duration_ms = int((time.time() - start_time) * 1000)
                audit_logger.log_data_change(
                    event_type=actual_event_type,
                    resource_type=resource_type,
                    resource_id=str(resource_id) if resource_id else "unknown",
                    old_values=old_values,
                    new_values=new_values,
                    action=f.__name__
                )
                
                return result
                
            except Exception as e:
                # Log failed operation
                duration_ms = int((time.time() - start_time) * 1000)
                audit_logger.log_event(
                    event_type=event_type or AuditEventType.SYSTEM_MAINTENANCE,
                    action=f.__name__,
                    severity=AuditSeverity.HIGH,
                    resource_type=resource_type,
                    resource_id=str(resource_id) if resource_id else "unknown",
                    success=False,
                    error_message=str(e),
                    duration_ms=duration_ms
                )
                raise
        
        return decorated_function
    return decorator


# Convenience functions for common audit scenarios
def audit_login_success(user_id: int):
    """Log successful login."""
    return audit_logger.log_authentication_event(
        AuditEventType.LOGIN_SUCCESS, user_id=user_id, success=True
    )


def audit_login_failure(user_id: int = None, error_message: str = None):
    """Log failed login attempt."""
    return audit_logger.log_authentication_event(
        AuditEventType.LOGIN_FAILURE, user_id=user_id, success=False, error_message=error_message
    )


def audit_permission_denied(resource_type: str = None, required_permission: str = None):
    """Log permission denied event."""
    return audit_logger.log_security_event(
        AuditEventType.PERMISSION_DENIED,
        description=f"Permission denied for {resource_type or 'resource'}",
        severity=AuditSeverity.MEDIUM,
        additional_data={'required_permission': required_permission}
    )


def audit_suspicious_activity(description: str, additional_data: Dict = None):
    """Log suspicious activity."""
    return audit_logger.log_security_event(
        AuditEventType.SUSPICIOUS_ACTIVITY,
        description=description,
        severity=AuditSeverity.HIGH,
        additional_data=additional_data
    )


def audit_emergency_operation(operation_name: str, additional_data: Dict = None):
    """Log emergency operation."""
    return audit_logger.log_security_event(
        AuditEventType.EMERGENCY_OPERATION,
        description=f"Emergency operation: {operation_name}",
        severity=AuditSeverity.CRITICAL,
        additional_data=additional_data
    )
