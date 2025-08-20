"""
Service-level logging decorators and utilities for BlueStream Platform
"""
import time
import functools
from typing import Any, Dict, Optional
from flask import g, has_request_context

from business_app.utils.logging_config import (
    performance_logger, security_logger, business_logger, database_logger
)
from business_app.utils.monitoring import app_metrics


def log_service_call(operation_type: str = 'general', 
                    log_args: bool = False, 
                    log_result: bool = False,
                    track_performance: bool = True):
    """
    Decorator to log service method calls with structured logging
    
    Args:
        operation_type: Type of operation (order, payment, delivery, etc.)
        log_args: Whether to log method arguments
        log_result: Whether to log method results
        track_performance: Whether to track performance metrics
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            service_name = args[0].__class__.__name__ if args else 'Unknown'
            method_name = func.__name__
            operation_name = f"{service_name}.{method_name}"
            
            # Get user context if available
            user_id = getattr(g, 'current_user_id', None) if has_request_context() else None
            request_id = getattr(g, 'request_id', None) if has_request_context() else None
            
            start_time = time.time()
            
            # Log method entry
            log_data = {
                'operation': operation_name,
                'operation_type': operation_type,
                'user_id': user_id,
                'request_id': request_id,
                'phase': 'start'
            }
            
            if log_args:
                # Sanitize args to avoid logging sensitive data
                sanitized_args = _sanitize_args(args[1:], kwargs)  # Skip self
                log_data['args'] = sanitized_args
            
            performance_logger.logger.debug(f"Starting {operation_name}", extra=log_data)
            
            try:
                # Execute the method
                with performance_logger.measure_time(operation_name, 
                                                   operation_type=operation_type,
                                                   user_id=user_id):
                    result = func(*args, **kwargs)
                
                # Calculate duration
                duration = time.time() - start_time
                
                # Log successful completion
                success_data = {
                    'operation': operation_name,
                    'operation_type': operation_type,
                    'user_id': user_id,
                    'request_id': request_id,
                    'phase': 'success',
                    'duration_ms': round(duration * 1000, 2)
                }
                
                if log_result:
                    success_data['result'] = _sanitize_result(result)
                
                performance_logger.logger.info(f"Completed {operation_name}", extra=success_data)
                
                # Track metrics
                if track_performance:
                    app_metrics.increment_counter(f'service_calls_total', 1, {
                        'service': service_name,
                        'method': method_name,
                        'operation_type': operation_type,
                        'status': 'success'
                    })
                    
                    app_metrics.record_timer(f'service_call_duration', duration, {
                        'service': service_name,
                        'method': method_name,
                        'operation_type': operation_type
                    })
                
                return result
                
            except Exception as e:
                duration = time.time() - start_time
                
                # Log error
                error_data = {
                    'operation': operation_name,
                    'operation_type': operation_type,
                    'user_id': user_id,
                    'request_id': request_id,
                    'phase': 'error',
                    'duration_ms': round(duration * 1000, 2),
                    'error_type': type(e).__name__,
                    'error_message': str(e)
                }
                
                performance_logger.logger.error(f"Failed {operation_name}: {e}", 
                                              extra=error_data, exc_info=True)
                
                # Track error metrics
                if track_performance:
                    app_metrics.increment_counter(f'service_calls_total', 1, {
                        'service': service_name,
                        'method': method_name,
                        'operation_type': operation_type,
                        'status': 'error'
                    })
                    
                    app_metrics.increment_counter(f'service_errors_total', 1, {
                        'service': service_name,
                        'method': method_name,
                        'operation_type': operation_type,
                        'error_type': type(e).__name__
                    })
                
                raise
        
        return wrapper
    return decorator


def log_business_event(event_type: str, entity_type: str = None):
    """
    Decorator to log business events (orders, payments, deliveries, etc.)
    
    Args:
        event_type: Type of business event (created, updated, cancelled, etc.)
        entity_type: Type of entity (order, payment, delivery, etc.)
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            result = func(*args, **kwargs)
            
            # Extract entity information from result or arguments
            entity_id = None
            additional_info = {}
            
            # Try to extract entity ID from result
            if hasattr(result, 'id'):
                entity_id = result.id
            elif hasattr(result, 'get') and callable(result.get):
                entity_id = result.get('id')
            elif isinstance(result, dict) and 'id' in result:
                entity_id = result['id']
            
            # Get user context
            user_id = getattr(g, 'current_user_id', None) if has_request_context() else None
            
            # Log the business event
            if entity_type == 'order':
                business_logger.log_order_event(
                    order_id=entity_id,
                    event=event_type,
                    user_id=user_id,
                    amount=getattr(result, 'total_amount', None) if hasattr(result, 'total_amount') else None,
                    additional_info=additional_info
                )
            elif entity_type == 'payment':
                business_logger.log_payment_event(
                    payment_id=entity_id,
                    event=event_type,
                    amount=getattr(result, 'amount', None) if hasattr(result, 'amount') else None,
                    method=getattr(result, 'method', None) if hasattr(result, 'method') else None,
                    user_id=user_id,
                    additional_info=additional_info
                )
            elif entity_type == 'delivery':
                business_logger.log_delivery_event(
                    delivery_id=entity_id,
                    event=event_type,
                    order_id=getattr(result, 'order_id', None) if hasattr(result, 'order_id') else None,
                    driver_id=getattr(result, 'delivery_person_id', None) if hasattr(result, 'delivery_person_id') else None,
                    additional_info=additional_info
                )
            else:
                # Generic business event
                business_logger.logger.info(
                    f"Business event: {event_type} for {entity_type} {entity_id}",
                    extra={
                        'event_type': 'generic_business_event',
                        'entity_type': entity_type,
                        'entity_id': entity_id,
                        'event': event_type,
                        'user_id': user_id,
                        'additional_info': additional_info
                    }
                )
            
            return result
        
        return wrapper
    return decorator


def log_security_event(event_type: str, severity: str = 'medium'):
    """
    Decorator to log security-related events
    
    Args:
        event_type: Type of security event
        severity: Severity level (low, medium, high, critical)
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            user_id = getattr(g, 'current_user_id', None) if has_request_context() else None
            ip_address = getattr(g, 'remote_addr', None) if has_request_context() else None
            
            try:
                result = func(*args, **kwargs)
                
                # Log successful security operation
                security_logger.logger.info(
                    f"Security event: {event_type} successful",
                    extra={
                        'event_type': 'security_operation',
                        'operation': event_type,
                        'user_id': user_id,
                        'ip_address': ip_address,
                        'severity': severity,
                        'status': 'success'
                    }
                )
                
                return result
                
            except Exception as e:
                # Log failed security operation
                security_logger.logger.warning(
                    f"Security event: {event_type} failed - {e}",
                    extra={
                        'event_type': 'security_operation_failed',
                        'operation': event_type,
                        'user_id': user_id,
                        'ip_address': ip_address,
                        'severity': severity,
                        'status': 'failed',
                        'error_type': type(e).__name__,
                        'error_message': str(e)
                    }
                )
                raise
        
        return wrapper
    return decorator


def log_database_query(operation_type: str = 'query'):
    """
    Decorator to log database operations with performance tracking
    
    Args:
        operation_type: Type of database operation (select, insert, update, delete)
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            start_time = time.time()
            
            try:
                result = func(*args, **kwargs)
                duration = time.time() - start_time
                
                # Count results if possible
                result_count = None
                if hasattr(result, '__len__'):
                    try:
                        result_count = len(result)
                    except:
                        pass
                elif hasattr(result, 'rowcount'):
                    result_count = result.rowcount
                
                # Log the database operation
                database_logger.log_query(
                    query=func.__name__,
                    duration=duration,
                    result_count=result_count,
                    operation=operation_type,
                    user_id=getattr(g, 'current_user_id', None) if has_request_context() else None
                )
                
                # Track metrics
                app_metrics.record_timer('database_query_duration', duration, {
                    'operation': operation_type,
                    'function': func.__name__
                })
                
                app_metrics.increment_counter('database_queries_total', 1, {
                    'operation': operation_type,
                    'function': func.__name__,
                    'status': 'success'
                })
                
                return result
                
            except Exception as e:
                duration = time.time() - start_time
                
                # Log failed query
                database_logger.logger.error(
                    f"Database operation failed: {func.__name__} - {e}",
                    extra={
                        'operation': operation_type,
                        'function': func.__name__,
                        'duration_ms': round(duration * 1000, 2),
                        'error_type': type(e).__name__,
                        'error_message': str(e),
                        'user_id': getattr(g, 'current_user_id', None) if has_request_context() else None
                    }
                )
                
                app_metrics.increment_counter('database_queries_total', 1, {
                    'operation': operation_type,
                    'function': func.__name__,
                    'status': 'error'
                })
                
                raise
        
        return wrapper
    return decorator


def _sanitize_args(args, kwargs):
    """Sanitize arguments to remove sensitive data"""
    sanitized = {
        'args_count': len(args),
        'kwargs_keys': list(kwargs.keys())
    }
    
    # Add non-sensitive kwargs
    safe_keys = {'page', 'per_page', 'limit', 'offset', 'sort', 'order', 'status'}
    for key, value in kwargs.items():
        if key in safe_keys:
            sanitized[key] = value
    
    return sanitized


def _sanitize_result(result):
    """Sanitize result to remove sensitive data"""
    if hasattr(result, 'id'):
        return {'id': result.id, 'type': type(result).__name__}
    elif isinstance(result, dict) and 'id' in result:
        return {'id': result['id'], 'type': 'dict'}
    elif isinstance(result, (list, tuple)) and result:
        return {'count': len(result), 'type': 'collection'}
    else:
        return {'type': type(result).__name__}


# Export decorators
__all__ = [
    'log_service_call',
    'log_business_event', 
    'log_security_event',
    'log_database_query'
]