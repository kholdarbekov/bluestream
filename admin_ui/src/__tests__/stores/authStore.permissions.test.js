/**
 * `initialize` runs on every page load behind ProtectedRoute, which shows a spinner until it
 * finishes. The store must publish the profile and the permissions `checkAuthStatus` just refreshed
 * from the server, not the copies it read from localStorage before that call, so the role and the
 * flags it publishes never disagree.
 */
import { useAuthStore } from '../../stores/authStore';
import api from '../../services/api';

vi.mock('../../services/api', () => ({
  __esModule: true,
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
  fetchCSRFToken: vi.fn(),
  getCookie: vi.fn(),
}));
vi.mock('react-hot-toast', () => ({ __esModule: true, default: { success: vi.fn(), error: vi.fn() } }));

// setupTests.js replaces localStorage with bare vi.fn()s; back them with a real in-memory store.
const memory = new Map();
const useMemoryLocalStorage = () => {
  memory.clear();
  localStorage.getItem.mockImplementation((key) => (memory.has(key) ? memory.get(key) : null));
  localStorage.setItem.mockImplementation((key, value) => { memory.set(key, String(value)); });
  localStorage.removeItem.mockImplementation((key) => { memory.delete(key); });
  localStorage.clear.mockImplementation(() => { memory.clear(); });
};

const STALE = { can_view_admin_panel: true, can_manage_orders: true };
const FRESH = { ...STALE, can_manage_lapsed_customers: true };

beforeEach(() => {
  vi.clearAllMocks();
  useMemoryLocalStorage();
  localStorage.setItem('admin_user', JSON.stringify({ id: 7, role: 'admin' }));
  localStorage.setItem('admin_permissions', JSON.stringify(STALE));
  useAuthStore.setState({ user: null, permissions: STALE, isAuthenticated: true });
});

it('lets an already signed-in admin see a page whose flag was added after they logged in', async () => {
  expect(useAuthStore.getState().hasPermission('can_manage_lapsed_customers')).toBe(false);
  api.get.mockResolvedValue({ data: { success: true, data: { id: 7, role: 'admin', permissions: FRESH } } });

  await useAuthStore.getState().initialize();

  expect(useAuthStore.getState().permissions).toEqual(FRESH);
  expect(useAuthStore.getState().hasPermission('can_manage_lapsed_customers')).toBe(true);
  expect(useAuthStore.getState().isAuthenticated).toBe(true);
});

it('signs the admin out when the server no longer recognises them', async () => {
  api.get.mockRejectedValue(new Error('401'));

  await useAuthStore.getState().initialize();

  expect(useAuthStore.getState().permissions).toEqual({});
  expect(useAuthStore.getState().isAuthenticated).toBe(false);
});

it('publishes the refreshed profile with the refreshed permissions, so a demoted manager shows both', async () => {
  const ADMIN_FLAGS = { ...STALE, can_manage_sales_pay: true, can_manage_settings: true };
  localStorage.setItem('admin_user', JSON.stringify({ id: 7, role: 'admin', first_name: 'Aziza' }));
  localStorage.setItem('admin_permissions', JSON.stringify(ADMIN_FLAGS));
  useAuthStore.setState({ user: { id: 7, role: 'admin' }, permissions: ADMIN_FLAGS, isAuthenticated: true });
  const profile = { id: 7, role: 'manager', first_name: 'Aziza', permissions: { ...STALE, can_manage_lapsed_customers: true } };
  api.get.mockResolvedValue({ data: { success: true, data: profile } });

  await useAuthStore.getState().initialize();

  const state = useAuthStore.getState();
  expect(state.user).toEqual(profile);
  expect(state.getUserRole()).toBe('manager');
  expect(state.permissions).toEqual(profile.permissions);
  expect(state.hasPermission('can_manage_sales_pay')).toBe(false);
  expect(state.hasPermission('can_manage_lapsed_customers')).toBe(true);
});

it('keeps the stored flags when the profile response carries no permissions', async () => {
  const profile = { id: 7, role: 'admin', first_name: 'Aziza' };
  api.get.mockResolvedValue({ data: { success: true, data: profile } });

  await useAuthStore.getState().initialize();

  const state = useAuthStore.getState();
  expect(state.permissions).toEqual(STALE);
  expect(state.hasPermission('can_view_admin_panel')).toBe(true);
  expect(state.isAuthenticated).toBe(true);
  expect(JSON.parse(localStorage.getItem('admin_permissions'))).toEqual(STALE);
  // The profile itself is still refreshed.
  expect(state.user).toEqual(profile);
});
