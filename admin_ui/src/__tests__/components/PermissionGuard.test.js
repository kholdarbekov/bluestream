import React from 'react';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import PermissionGuard, {
  PermissionCheck,
} from '../../components/common/PermissionGuard';

const mockAuth = {
  hasPermission: vi.fn(() => true),
  getUserRole: vi.fn(() => 'admin'),
  permissions: {},
};

vi.mock('../../stores/authStore', () => ({
  useAuthStore: () => mockAuth,
}));

const renderInRouter = (ui) =>
  render(<MemoryRouter>{ui}</MemoryRouter>);

describe('PermissionGuard', () => {
  beforeEach(() => {
    mockAuth.hasPermission.mockReturnValue(true);
    mockAuth.getUserRole.mockReturnValue('admin');
  });

  it('renders children when single permission is granted', () => {
    renderInRouter(
      <PermissionGuard permission="can_manage_orders">
        <div>order-tools</div>
      </PermissionGuard>,
    );
    expect(screen.getByText('order-tools')).toBeInTheDocument();
  });

  it('renders 403 fallback when single permission is denied', () => {
    mockAuth.hasPermission.mockReturnValue(false);
    renderInRouter(
      <PermissionGuard permission="can_manage_orders">
        <div>order-tools</div>
      </PermissionGuard>,
    );
    expect(screen.queryByText('order-tools')).not.toBeInTheDocument();
    expect(screen.getByText(/don't have permission/i)).toBeInTheDocument();
  });

  it('renders nothing when showFallback is false and permission is denied', () => {
    mockAuth.hasPermission.mockReturnValue(false);
    const { container } = renderInRouter(
      <PermissionGuard permission="can_manage_orders" showFallback={false}>
        <div>order-tools</div>
      </PermissionGuard>,
    );
    expect(container.querySelector('.ant-result')).toBeNull();
    expect(screen.queryByText('order-tools')).not.toBeInTheDocument();
  });

  it('respects requireAll: any-vs-all semantics on a permission list', () => {
    mockAuth.hasPermission.mockImplementation((p) => p === 'can_view_analytics');

    // requireAll=false (default): one match is enough → render.
    const { rerender } = renderInRouter(
      <PermissionGuard permissions={['can_manage_users', 'can_view_analytics']}>
        <div>guarded</div>
      </PermissionGuard>,
    );
    expect(screen.getByText('guarded')).toBeInTheDocument();

    // requireAll=true: missing one → fallback.
    rerender(
      <MemoryRouter>
        <PermissionGuard
          permissions={['can_manage_users', 'can_view_analytics']}
          requireAll
        >
          <div>guarded</div>
        </PermissionGuard>
      </MemoryRouter>,
    );
    expect(screen.queryByText('guarded')).not.toBeInTheDocument();
  });

  it('uses the supplied custom fallback when provided', () => {
    mockAuth.hasPermission.mockReturnValue(false);
    renderInRouter(
      <PermissionGuard
        permission="can_manage_orders"
        fallback={<div>custom-fallback</div>}
      >
        <div>order-tools</div>
      </PermissionGuard>,
    );
    expect(screen.getByText('custom-fallback')).toBeInTheDocument();
    expect(screen.queryByText(/don't have permission/i)).not.toBeInTheDocument();
  });
});

describe('PermissionCheck (conditional renderer)', () => {
  beforeEach(() => {
    mockAuth.hasPermission.mockReturnValue(true);
    mockAuth.getUserRole.mockReturnValue('admin');
  });

  it('renders children when role matches', () => {
    renderInRouter(
      <PermissionCheck role="admin">
        <div>admin-only</div>
      </PermissionCheck>,
    );
    expect(screen.getByText('admin-only')).toBeInTheDocument();
  });

  it('renders fallback (or nothing) when role does not match', () => {
    mockAuth.getUserRole.mockReturnValue('operator');
    renderInRouter(
      <PermissionCheck role="admin" fallback={<div>nope</div>}>
        <div>admin-only</div>
      </PermissionCheck>,
    );
    expect(screen.queryByText('admin-only')).not.toBeInTheDocument();
    expect(screen.getByText('nope')).toBeInTheDocument();
  });

  it('matches any of the listed roles', () => {
    mockAuth.getUserRole.mockReturnValue('manager');
    renderInRouter(
      <PermissionCheck roles={['admin', 'manager']}>
        <div>elevated</div>
      </PermissionCheck>,
    );
    expect(screen.getByText('elevated')).toBeInTheDocument();
  });
});
