import api, { fetchCSRFToken } from './api';

class AuthService {
  async login(credentials) {
    // Cannot read properties of undefined (reading 'role')
    try {
      // Use identifier instead of email to support both email and phone
      const loginData = {
        identifier: credentials.email || credentials.identifier,
        password: credentials.password
      };

      const response = await api.post('/auth/login', loginData);
      // UI-001: the `tokens` field is ignored on the admin UI — the server already
      // placed them in HttpOnly cookies. We intentionally do NOT destructure or
      // store the raw tokens here so XSS cannot exfiltrate them from JS scope.
      const { user, permissions } = response.data.data;

      // Check if user has admin/manager role (no registration allowed, only predefined admin users)
      const allowedRoles = ['admin', 'manager'];
      if (!allowedRoles.includes(user.role)) {
        throw new Error('Access denied. Administrative privileges required.');
      }

      // Check admin panel access permission directly from login response
      if (!permissions.can_view_admin_panel) {
        throw new Error('Access denied. Admin panel access not permitted.');
      }

      // Store only non-sensitive profile data for UX (name, role display, perms).
      // JWTs live exclusively in HttpOnly cookies managed by the server.
      localStorage.setItem('admin_user', JSON.stringify(user));
      localStorage.setItem('admin_permissions', JSON.stringify(permissions));

      // Fetch CSRF token for subsequent requests
      await fetchCSRFToken();

      return { user, permissions };
    } catch (error) {
      // Clear any stored auth data on failed login
      this.clearStoredAuth();
      throw error;
    }
  }

  async logout() {
    try {
      await api.post('/auth/logout');
    } catch (error) {
      // Continue with logout even if API call fails
      console.error('Logout API error:', error);
    } finally {
      this.clearStoredAuth();
    }
  }

  async refreshToken() {
    try {
      // Token refresh is now handled via httpOnly cookies
      const response = await api.post('/auth/refresh-token');
      return response.status === 200; // Return success status
    } catch (error) {
      this.logout();
      throw error;
    }
  }

  getCurrentUser() {
    const user = localStorage.getItem('admin_user');
    return user ? JSON.parse(user) : null;
  }

  getPermissions() {
    const permissions = localStorage.getItem('admin_permissions');
    return permissions ? JSON.parse(permissions) : {};
  }

  hasPermission(permission) {
    const permissions = this.getPermissions();
    // eslint-disable-next-line security/detect-object-injection
    return permissions[permission] === true;
  }

  isAuthenticated() {
    const user = this.getCurrentUser();
    const permissions = this.getPermissions();

    // With httpOnly cookies, we rely on user data and permissions in localStorage
    // The actual token validation happens on the server side
    return Boolean(user &&
      ['admin', 'manager'].includes(user.role) &&
      permissions.can_view_admin_panel);
  }

  clearStoredAuth() {
    // UI-001: `admin_token` is no longer written, but we still remove it to
    // flush any value persisted by older builds during the rollout window.
    localStorage.removeItem('admin_token');
    localStorage.removeItem('admin_user');
    localStorage.removeItem('admin_permissions');
  }

  async checkAuthStatus() {
    try {
      // Verify authentication by calling profile endpoint
      // The httpOnly cookie will be sent automatically
      const response = await api.get('/auth/profile');
      const user = response.data.data;

      // Update stored user data
      localStorage.setItem('admin_user', JSON.stringify(user));

      // Check permissions are still valid
      if (!['admin', 'manager'].includes(user.role)) {
        this.clearStoredAuth();
        return false;
      }

      return true;
    } catch (error) {
      this.clearStoredAuth();
      return false;
    }
  }
}

export default new AuthService();
