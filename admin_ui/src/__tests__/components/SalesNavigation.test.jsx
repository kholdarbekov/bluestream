import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import App from '../../App';
import AdminLayout from '../../components/layout/AdminLayout';
import salesPayService from '../../services/salesPayService';
import salesService from '../../services/salesService';

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
// C14 and the manager proposals page, reduced to markers like the two above; the dashboard is the
// order-approvals route's fallback, so it is a marker too. The badge's one read is the assertion
// target, so the sales service is a mock with that one method.
vi.mock('../../pages/SalesOrderApprovals', () => ({ default: () => <div>order-approvals-page</div> }));
vi.mock('../../pages/PenaltyProposals', () => ({ default: () => <div>penalty-proposals-page</div> }));
vi.mock('../../pages/Dashboard', () => ({ default: () => <div>dashboard-page</div> }));
vi.mock('../../services/salesService', () => ({
  __esModule: true,
  default: { listOrderApprovals: vi.fn() },
  ORDER_APPROVAL_HANDLED_CODES: [],
}));

// OA1 asked for one row: the badge reads `pending_count`, never the rows.
const PENDING_COUNT_PAGE = {
  items: [],
  meta: { page: 1, per_page: 1, total: 3, pages: 3, has_next: true, has_prev: false },
  statuses: ['pending', 'approved', 'rejected', 'cancelled'],
  pending_count: 3,
};
vi.mock('../../pages/SalesCompensation', () => ({ default: () => <div>compensation-page</div> }));

// The layout's Compensation badge reads A1. Every method is a spy, so a manager's session can
// be shown to call none of them (§10.6).
vi.mock('../../services/salesPayService', async () => {
  const actual = await vi.importActual('../../services/salesPayService');
  const methods = Object.getOwnPropertyNames(Object.getPrototypeOf(actual.default)).filter((name) => name !== 'constructor');
  return { ...actual, default: Object.fromEntries(methods.map((name) => [name, vi.fn()])) };
});

const LocationProbe = () => {
  const location = useLocation();
  return <div data-testid="location">{location.pathname}</div>;
};

// The layout now reads the periods query for its badge, so both renders need a query client.
const withClient = (ui) => (
  <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{ui}</QueryClientProvider>
);

const renderApp = (entry) => render(withClient(
  <MemoryRouter initialEntries={[entry]}>
    <App />
    <LocationProbe />
  </MemoryRouter>,
));

const renderLayout = () => render(withClient(
  <MemoryRouter initialEntries={['/dashboard']}>
    <AdminLayout><div>content</div></AdminLayout>
  </MemoryRouter>,
));

// A manager: every existing permission, but not the admin-only pay flag (C13).
const asManager = () => mockAuth.hasPermission.mockImplementation((flag) => flag !== 'can_manage_sales_pay');

beforeEach(() => {
  realtimeOptions = null;
  vi.clearAllMocks();
  mockAuth.initialize.mockResolvedValue(undefined);
  mockAuth.hasPermission.mockReturnValue(true);
  mockAuth.getUserRole.mockReturnValue('admin');
  salesPayService.getPeriods.mockResolvedValue({
    started: true, startable_month: null, items: [], statuses: [], actions: [], pending_penalty_count: 3, as_of: null,
  });
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

it('gives an admin a Compensation child carrying the pending-penalty badge', async () => {
  renderLayout();
  fireEvent.click(screen.getByText('Sales'));

  expect(await screen.findByText('Compensation')).toBeInTheDocument();
  expect(document.querySelector('[data-menu-id$="/sales/compensation"]')).toBeTruthy();
  await waitFor(() => expect(document.querySelector('[data-menu-id$="/sales/compensation"]')).toHaveTextContent('3'));
  expect(salesPayService.getPeriods).toHaveBeenCalledTimes(1);
});

it('gives a manager no Compensation child and calls no pay route at all', async () => {
  asManager();
  renderLayout();
  fireEvent.click(screen.getByText('Sales'));

  expect(await screen.findByText('Visits')).toBeInTheDocument();
  expect(screen.queryByText('Compensation')).toBeNull();
  expect(document.querySelector('[data-menu-id$="/sales/compensation"]')).toBeNull();
  await new Promise((resolve) => { setTimeout(resolve, 0); });
  Object.values(salesPayService).forEach((fn) => expect(fn).not.toHaveBeenCalled());
});

it('serves /sales/compensation to an admin', async () => {
  renderApp('/sales/compensation');

  expect(await screen.findByText('compensation-page')).toBeInTheDocument();
});

it('sends a manager who types /sales/compensation to the proposals path instead', async () => {
  asManager();
  renderApp('/sales/compensation');

  // `replace`, so Back does not bounce into the guard again. The page at that path is Task 13's.
  await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/sales/penalty-proposals'));
  expect(screen.queryByText('compensation-page')).toBeNull();
});

// Every render asks for the count while the flag holds (the default mock grants every permission).
beforeEach(() => {
  salesService.listOrderApprovals.mockResolvedValue(PENDING_COUNT_PAGE);
});

describe('order approvals (C14) and penalty proposals in the Sales group', () => {
  const openSales = async () => {
    fireEvent.click(screen.getByText('Sales'));
    await screen.findByText('Visits');
  };
  const child = (path) => document.querySelector(`[data-menu-id$="${path}"]`);

  it('shows Order approvals with the pending count to a user who can review agent orders', async () => {
    renderLayout();
    await openSales();

    expect(within(child('/sales/order-approvals')).getByText('Order approvals')).toBeInTheDocument();
    await waitFor(() => expect(child('/sales/order-approvals').querySelector('.ant-badge-count')).toHaveAttribute('title', '3'));
    expect(salesService.listOrderApprovals).toHaveBeenCalledWith({ status: 'pending', perPage: 1 });
  });

  it('draws no Order approvals child, and never asks for the count, without can_review_agent_orders', async () => {
    mockAuth.hasPermission.mockImplementation((permission) => permission !== 'can_review_agent_orders');
    renderLayout();
    await openSales();

    expect(child('/sales/order-approvals')).toBeNull();
    expect(salesService.listOrderApprovals).not.toHaveBeenCalled();
  });

  it('gives a user without can_manage_sales_pay the Penalty proposals child', async () => {
    mockAuth.hasPermission.mockImplementation((permission) => permission !== 'can_manage_sales_pay');
    mockAuth.getUserRole.mockReturnValue('manager');
    renderLayout();
    await openSales();

    expect(within(child('/sales/penalty-proposals')).getByText('Penalty proposals')).toBeInTheDocument();
    expect(child('/sales/compensation')).toBeNull();
  });

  it('gives an administrator Compensation instead of Penalty proposals', async () => {
    renderLayout();
    await openSales();

    expect(child('/sales/compensation')).toBeTruthy();
    expect(child('/sales/penalty-proposals')).toBeNull();
  });

  it('opens /sales/order-approvals for a reviewer', async () => {
    renderApp('/sales/order-approvals');

    expect(await screen.findByText('order-approvals-page')).toBeInTheDocument();
  });

  it('sends anyone without can_review_agent_orders from /sales/order-approvals to the dashboard', async () => {
    mockAuth.hasPermission.mockImplementation((permission) => permission !== 'can_review_agent_orders');
    renderApp('/sales/order-approvals');

    expect(await screen.findByText('dashboard-page')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/dashboard'));
    expect(screen.queryByText('order-approvals-page')).toBeNull();
  });

  it('serves /sales/penalty-proposals to a signed-in user (the page itself sends an admin on)', async () => {
    mockAuth.hasPermission.mockImplementation((permission) => permission !== 'can_manage_sales_pay');
    renderApp('/sales/penalty-proposals');

    expect(await screen.findByText('penalty-proposals-page')).toBeInTheDocument();
  });
});
