import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import authService from '../services/authService';
import toast from 'react-hot-toast';

export const useAuthStore = create(
  persist(
    (set, get) => ({
      user: null,
      token: null,
      permissions: {},
      isAuthenticated: false,
      isLoading: false,

      // Initialize auth state from localStorage
      initialize: async () => {
        const user = authService.getCurrentUser();
        const token = authService.getToken();
        const permissions = authService.getPermissions();
        let isAuthenticated = authService.isAuthenticated();

        // Verify auth status with server if we have a token
        if (token && user) {
          try {
            isAuthenticated = await authService.checkAuthStatus();
            if (!isAuthenticated) {
              // Clear local storage if server says auth is invalid
              authService.clearStoredAuth();
              set({
                user: null,
                token: null,
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
          token,
          permissions,
          isAuthenticated
        });
      },

      // Login action
      login: async (credentials) => {
        set({ isLoading: true });
        try {
          const { user, token, permissions } = await authService.login(credentials);
          set({
            user,
            token,
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
            token: null,
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
          token: null,
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
        token: state.token,
        permissions: state.permissions,
        isAuthenticated: state.isAuthenticated
      })
    }
  )
);