import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import authService from '../services/authService';
import toast from 'react-hot-toast';

export const useAuthStore = create(
  persist(
    (set, get) => ({
      user: null,
      permissions: {},
      isAuthenticated: false,
      isLoading: false,

      // UI-001: JWT lives in HttpOnly cookies; this store only tracks
      // non-sensitive UX state (user profile, permissions, auth flag).
      // The server is the sole source of truth for auth validity.
      initialize: async () => {
        const user = authService.getCurrentUser();
        const permissions = authService.getPermissions();
        let isAuthenticated = authService.isAuthenticated();

        // If we have a cached user, verify with the server. The HttpOnly
        // cookie is sent automatically; we don't inspect it here.
        if (user) {
          try {
            isAuthenticated = await authService.checkAuthStatus();
            if (!isAuthenticated) {
              authService.clearStoredAuth();
              set({
                user: null,
                permissions: {},
                isAuthenticated: false
              });
              return;
            }
          } catch (error) {
            console.error('Auth verification failed:', error);
            isAuthenticated = false;
            authService.clearStoredAuth();
          }
        }

        set({
          user,
          permissions,
          isAuthenticated
        });
      },

      // Login action
      login: async (credentials) => {
        set({ isLoading: true });
        try {
          const { user, permissions } = await authService.login(credentials);
          set({
            user,
            permissions,
            isAuthenticated: true,
            isLoading: false
          });
          toast.success(`Welcome back, ${user.first_name}!`);
          return { success: true };
        } catch (error) {
          set({ isLoading: false });
          const message = error.response?.data?.message || error.message || 'Login failed';
          toast.error(message);
          return { success: false, error: message };
        }
      },

      // Logout action
      logout: async () => {
        set({ isLoading: true });
        try {
          await authService.logout();
        } catch (error) {
          console.error('Logout error:', error);
        } finally {
          set({
            user: null,
            permissions: {},
            isAuthenticated: false,
            isLoading: false
          });
          toast.success('Logged out successfully');
        }
      },

      // Update user info
      updateUser: (userData) => {
        const updatedUser = { ...get().user, ...userData };
        localStorage.setItem('admin_user', JSON.stringify(updatedUser));
        set({ user: updatedUser });
      },

      // Check if user has specific permission
      hasPermission: (permission) => {
        const { permissions } = get();
        // Prevent object injection by validating permission parameter
        if (typeof permission !== 'string' || permission.includes('__proto__') || permission.includes('constructor')) {
          return false;
        }
        // eslint-disable-next-line security/detect-object-injection
        return Object.prototype.hasOwnProperty.call(permissions, permission) && permissions[permission] === true;
      },

      // Get user role
      getUserRole: () => {
        const { user } = get();
        return user?.role || null;
      },

      // Clear auth state
      clearAuth: () => {
        authService.clearStoredAuth();
        set({
          user: null,
          permissions: {},
          isAuthenticated: false,
          isLoading: false
        });
      }
    }),
    {
      name: 'admin-auth-storage',
      partialize: (state) => ({
        user: state.user,
        permissions: state.permissions,
        isAuthenticated: state.isAuthenticated
      })
    }
  )
);
