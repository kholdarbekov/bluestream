import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';

import App from '../../App';
import AdminLayout from '../../components/layout/AdminLayout';

// One mutable auth object (ProtectedRoute.test.js:9-19): the whole inner <Routes> sits inside one
// <ProtectedRoute>, so an unauthenticated store would render a spinner instead of the route table.
const mockAuth = {
  isAuthenticated: true,
  isLoading: false,
  initialize: vi.fn().mockResolvedValue(undefined),
  hasPermission: vi.fn(() => true),
  getUserRole: vi.fn(() => 'admin'),
  user: { first_name: 'Ada', last_name: 'Admin', role: 'admin' },
  logout: vi.fn(),
};
vi.mock('../../stores/authStore', () => ({ useAuthStore: () => mockAuth }));

// The realtime hook is BOTH a side effect to silence (it opens a socket and a 30s poll) and the
// assertion target: the query keys a page invalidates are only ever named in this options object.
let realtimeOptions = null;
vi.mock('../../hooks/useRealTimeUpdates', () => ({
  useRealTimeWithFallback: (options) => {
    realtimeOptions = options;
    return { isConnected: true, connectionType: 'websocket' };
  },
  useRealTimeUpdates: () => ({ isConnected: true }),
  usePollingUpdates: () => ({ isConnected: true }),
  default: () => ({ isConnected: true }),
}));

// LanguageSwitcher imports ../../i18n, which would initialise the http backend and fire real
// translation loads from jsdom. Mocking the component means its imports never execute.
vi.mock('../../components/common/LanguageSwitcher', () => ({ default: () => <div data-testid="lang-switcher" /> }));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, opts) => (typeof opts === 'string' ? opts : opts?.defaultValue) || key }),
}));

// The two pages the redirects must LAND on, reduced to markers: this file is about the route
// table, and the real pages would drag their queries, services and geo-config fetches in.
vi.mock('../../pages/Outlets', () => ({ default: () => <div>outlets-page</div> }));
vi.mock('../../pages/SalesAgents', () => ({ default: () => <div>sales-agents-page</div> }));

const LocationProbe = () => {
  const location = useLocation();
  return <div data-testid="location">{location.pathname}</div>;
};

const renderApp = (entry) => render(
  <MemoryRouter initialEntries={[entry]}>
    <App />
    <LocationProbe />
  </MemoryRouter>,
);

const renderLayout = () => render(
  <MemoryRouter initialEntries={['/dashboard']}>
    <AdminLayout><div>content</div></AdminLayout>
  </MemoryRouter>,
);

beforeEach(() => {
  realtimeOptions = null;
  vi.clearAllMocks();
  mockAuth.initialize.mockResolvedValue(undefined);
  mockAuth.hasPermission.mockReturnValue(true);
  mockAuth.getUserRole.mockReturnValue('admin');
});

it('gathers agents, outlets and visits under one Sales group', async () => {
  renderLayout();

  // rc-menu mounts an inline submenu's children only once it opens (InlineSubMenuList renders a
  // CSSMotion with visible=false and forceRender=false), so the group has to be opened first.
  fireEvent.click(screen.getByText('Sales'));

  expect(await screen.findByText('Visits')).toBeInTheDocument();
  expect(screen.getByText('Outlets')).toBeInTheDocument();
  expect(screen.getByText('Sales Agents')).toBeInTheDocument();
  // The KEY is the route, and the key is what handleMenuClick navigates to.
  expect(document.querySelector('[data-menu-id$="/sales/agents"]')).toBeTruthy();
  expect(document.querySelector('[data-menu-id$="/sales/outlets"]')).toBeTruthy();
  expect(document.querySelector('[data-menu-id$="/sales/visits"]')).toBeTruthy();
});

it('leaves no sales agents entry behind under Staff', async () => {
  renderLayout();

  // Opened on purpose: an unopened submenu has no children in the DOM, so asserting the absence
  // of /staff/sales-agents without opening Staff would pass whether or not it had been moved.
  fireEvent.click(screen.getByText('ui.nav.staff'));
  expect(await screen.findByText('ui.nav.delivery_persons')).toBeInTheDocument();

  expect(document.querySelector('[data-menu-id$="/staff/sales-agents"]')).toBeNull();
});

it('subscribes the sales read surfaces to the realtime refresh list', () => {
  renderLayout();

  expect(realtimeOptions.queries).toEqual([
    'dashboard', 'orders', 'users', 'products', 'deliveries', 'translations', 'loyalty-members',
    'loyalty-programs', 'loyalty-rewards', 'analytics-loyalty', 'salesAgents', 'outlets',
    'visits', 'salesExceptions', 'salesPlanVsFact', 'agentMetrics', 'agentsMetrics',
  ]);
});

it('redirects the old /outlets path to /sales/outlets', async () => {
  renderApp('/outlets');

  expect(await screen.findByText('outlets-page')).toBeInTheDocument();
  // `replace` matters: without it Back lands on /outlets and bounces forward again.
  await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/sales/outlets'));
});

it('redirects the old /staff/sales-agents path to /sales/agents', async () => {
  renderApp('/staff/sales-agents');

  expect(await screen.findByText('sales-agents-page')).toBeInTheDocument();
  await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/sales/agents'));
});

it('sends the bare /sales group key to the outlets page', async () => {
  // antd SubMenu parents do not fire handleMenuClick, but the key is a real URL a human can type.
  renderApp('/sales');

  expect(await screen.findByText('outlets-page')).toBeInTheDocument();
  await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/sales/outlets'));
});
