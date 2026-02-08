"""
CSRF test helpers for testing CSRF-protected endpoints
"""
import json
from typing import Dict, Any, Optional
from flask import url_for
from flask.testing import FlaskClient


class CSRFTestClient:
    """Test client with CSRF token support"""
    
    def __init__(self, client: FlaskClient):
        self.client = client
        self._csrf_token = None
    
    def get_csrf_token(self) -> str:
        """Get CSRF token from the server"""
        if not self._csrf_token:
            response = self.client.get('/api/csrf-token')
            if response.status_code == 200:
                data = json.loads(response.data)
                self._csrf_token = data.get('csrf_token')
            else:
                raise Exception("Failed to get CSRF token")
        return self._csrf_token
    
    def post_with_csrf(self, url: str, data: Dict[str, Any], 
                      headers: Optional[Dict[str, str]] = None,
                      content_type: str = 'application/json') -> Any:
        """
        Make POST request with CSRF token
        
        Args:
            url: URL to post to
            data: Data to send
            headers: Optional headers
            content_type: Content type
            
        Returns:
            Response object
        """
        if headers is None:
            headers = {}
        
        # Add CSRF token to headers
        csrf_token = self.get_csrf_token()
        headers['X-CSRFToken'] = csrf_token
        
        # If JSON content, add to data as well for redundancy
        if content_type == 'application/json':
            data['csrf_token'] = csrf_token
            return self.client.post(url, json=data, headers=headers)
        else:
            data['csrf_token'] = csrf_token
            return self.client.post(url, data=data, headers=headers, 
                                  content_type=content_type)
    
    def put_with_csrf(self, url: str, data: Dict[str, Any],
                     headers: Optional[Dict[str, str]] = None) -> Any:
        """Make PUT request with CSRF token"""
        if headers is None:
            headers = {}
        
        csrf_token = self.get_csrf_token()
        headers['X-CSRFToken'] = csrf_token
        data['csrf_token'] = csrf_token
        
        return self.client.put(url, json=data, headers=headers)
    
    def patch_with_csrf(self, url: str, data: Dict[str, Any],
                       headers: Optional[Dict[str, str]] = None) -> Any:
        """Make PATCH request with CSRF token"""
        if headers is None:
            headers = {}
        
        csrf_token = self.get_csrf_token()
        headers['X-CSRFToken'] = csrf_token
        data['csrf_token'] = csrf_token
        
        return self.client.patch(url, json=data, headers=headers)
    
    def delete_with_csrf(self, url: str,
                        headers: Optional[Dict[str, str]] = None) -> Any:
        """Make DELETE request with CSRF token"""
        if headers is None:
            headers = {}
        
        csrf_token = self.get_csrf_token()
        headers['X-CSRFToken'] = csrf_token
        
        return self.client.delete(url, headers=headers)
    
    def refresh_csrf_token(self):
        """Refresh the CSRF token"""
        self._csrf_token = None
        self.get_csrf_token()


def create_test_client_with_csrf(app) -> CSRFTestClient:
    """
    Create a test client with CSRF support
    
    Args:
        app: Flask application
        
    Returns:
        CSRFTestClient instance
    """
    return CSRFTestClient(app.test_client())


def disable_csrf_for_testing(app):
    """
    Disable CSRF protection for testing
    
    Args:
        app: Flask application
    """
    app.config['WTF_CSRF_ENABLED'] = False
    app.config['TESTING'] = True


def enable_csrf_for_testing(app):
    """
    Enable CSRF protection for testing
    
    Args:
        app: Flask application
    """
    app.config['WTF_CSRF_ENABLED'] = True
    app.config['TESTING'] = False