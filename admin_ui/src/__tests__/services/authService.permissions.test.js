/**
 * An admin who logged in BEFORE a permission flag existed keeps the old flags in localStorage until
 * they log in again. `checkAuthStatus` (run by `initialize` on every page load) already reads
 * `/auth/profile`, which publishes the same `permissions` block as the login response, so it must
 * refresh the stored flags too: otherwise a new page (Lapsed customers) is hidden from an admin who
 * is already signed in.
 */
import authService from '../../services/authService';
import api from '../../services/api';

vi.mock('../../services/api', () => ({
  __esModule: true,
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
  fetchCSRFToken: vi.fn(),
  getCookie: vi.fn(),
}));

// setupTests.js replaces localStorage with bare vi.fn()s; back them with a real in-memory store.
const memory = new Map();
const useMemoryLocalStorage = () => {
  memory.clear();
  localStorage.getItem.mockImplementation((key) => (memory.has(key) ? memory.get(key) : null));
  localStorage.setItem.mockImplementation((key, value) => { memory.set(key, String(value)); });
  localStorage.removeItem.mockImplementation((key) => { memory.delete(key); });
  localStorage.clear.mockImplementation(() => { memory.clear(); });
};

const STALE_PERMISSIONS = { can_view_admin_panel: true, can_manage_orders: true };
const profile = (overrides = {}) => ({
  data: {
    success: true,
    data: {
      id: 7,
      first_name: 'Aziza',
      role: 'manager',
      permissions: {
        can_view_admin_panel: true,
        can_manage_orders: true,
        can_manage_lapsed_customers: true,
        can_manage_sales_pay: false,
      },
      ...overrides,
    },
  },
});

beforeEach(() => {
  vi.clearAllMocks();
  useMemoryLocalStorage();
  localStorage.setItem('admin_user', JSON.stringify({ id: 7, role: 'manager' }));
  localStorage.setItem('admin_permissions', JSON.stringify(STALE_PERMISSIONS));
});

describe('AuthService.checkAuthStatus', () => {
  it('replaces the stored permissions with the ones /auth/profile publishes', async () => {
    api.get.mockResolvedValue(profile());

    await expect(authService.checkAuthStatus()).resolves.toBe(true);

    expect(api.get).toHaveBeenCalledWith('/auth/profile');
    expect(JSON.parse(localStorage.getItem('admin_permissions'))).toEqual({
      can_view_admin_panel: true,
      can_manage_orders: true,
      can_manage_lapsed_customers: true,
      can_manage_sales_pay: false,
    });
    expect(authService.hasPermission('can_manage_lapsed_customers')).toBe(true);
    expect(authService.hasPermission('can_manage_sales_pay')).toBe(false);
    expect(JSON.parse(localStorage.getItem('admin_user')).first_name).toBe('Aziza');
  });

  it('takes a permission away that the server no longer grants', async () => {
    localStorage.setItem('admin_permissions', JSON.stringify({ ...STALE_PERMISSIONS, can_manage_lapsed_customers: true }));
    api.get.mockResolvedValue(profile({ permissions: { can_view_admin_panel: true, can_manage_lapsed_customers: false } }));

    await authService.checkAuthStatus();

    expect(authService.hasPermission('can_manage_lapsed_customers')).toBe(false);
  });

  it('keeps the stored permissions when the profile carries none', async () => {
    api.get.mockResolvedValue(profile({ permissions: undefined }));

    await expect(authService.checkAuthStatus()).resolves.toBe(true);

    expect(JSON.parse(localStorage.getItem('admin_permissions'))).toEqual(STALE_PERMISSIONS);
  });

  it('clears the stored session, permissions included, for a role that is not staff', async () => {
    api.get.mockResolvedValue(profile({ role: 'customer' }));

    await expect(authService.checkAuthStatus()).resolves.toBe(false);

    expect(localStorage.getItem('admin_permissions')).toBeNull();
    expect(localStorage.getItem('admin_user')).toBeNull();
  });

  it('clears the stored session when the profile call fails', async () => {
    api.get.mockRejectedValue(new Error('401'));

    await expect(authService.checkAuthStatus()).resolves.toBe(false);

    expect(localStorage.getItem('admin_permissions')).toBeNull();
  });
});
