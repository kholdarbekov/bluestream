import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import BottleTracking from '../../pages/BottleTracking';
import adminService from '../../services/adminService';

// Spy map derived from the real service so a renamed call site cannot keep
// resolving to a stale hand-listed spy.
vi.mock('../../services/adminService', async () => {
  const actual = await vi.importActual('../../services/adminService');
  const real = actual.default;
  const names = new Set();
  for (let obj = real; obj && obj !== Object.prototype; obj = Object.getPrototypeOf(obj)) {
    for (const name of Object.getOwnPropertyNames(obj)) {
      if (name !== 'constructor' && typeof real[name] === 'function') names.add(name);
    }
  }
  return {
    __esModule: true,
    default: Object.fromEntries([...names].map((name) => [name, vi.fn()])),
  };
});

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key, opts) => {
      let value = (opts && opts.defaultValue) || key;
      if (opts) {
        value = value.replace(/\{\{(\w+)\}\}/g, (_, token) => (
          // eslint-disable-next-line security/detect-object-injection
          opts[token] !== undefined ? String(opts[token]) : `{{${token}}}`
        ));
      }
      return value;
    },
  }),
}));

// Keys mirror serialize_bottle_transfer (business_app/serializers/bottle_serializers.py).
const transfer = (overrides = {}) => ({
  id: 7,
  transfer_ref: 'BT-7',
  status: 'confirmed',
  sender_driver_id: 11,
  receiver_driver_id: 12,
  sender_name: 'Sender Sam',
  receiver_name: 'Receiver Rita',
  declared_quantity: 9,
  confirmed_quantity: 6,
  declared_full: 9,
  declared_empty: 4,
  confirmed_full: 6,
  confirmed_empty: 3,
  declared_total: 13,
  confirmed_total: 9,
  sent_at: '2026-11-09T08:00:00',
  ...overrides,
});

const session = (overrides = {}) => ({
  id: 3,
  driver_user_id: 11,
  driver_name: 'Sender Sam',
  status: 'open',
  started_at: '2026-11-09T07:00:00',
  closed_at: null,
  bottles_loaded: 20,
  bottles_delivered: 0,
  bottles_collected_from_customers: 0,
  bottles_transferred_out: 1,
  bottles_transferred_in: 2,
  empties_transferred_out: 3,
  empties_transferred_in: 4,
  current_inventory: 21,
  full_bottles_on_board: 17,
  empties_on_board: 5,
  free_full_bottles: 13,
  ...overrides,
});

const createWrapper = () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
};

const mockTransfers = ({ disputedTotal = 0, items = [] } = {}) => {
  adminService.getBottleTransfers.mockImplementation(async (params) => {
    if (params?.status === 'disputed' && params?.per_page === 1) {
      return { data: { items: [], total: disputedTotal } };
    }
    return { data: { items, total: items.length } };
  });
};

const tabLabel = (name) => screen.getByRole('tab', { name: new RegExp(name) });

beforeEach(() => {
  vi.clearAllMocks();
  adminService.getBottleDashboard.mockResolvedValue({ data: {} });
  adminService.getBottleBalances.mockResolvedValue({ data: { items: [], total: 0 } });
  adminService.getBottleLedger.mockResolvedValue({ data: { items: [], total: 0 } });
  adminService.getBottleFines.mockResolvedValue({ data: { items: [], total: 0 } });
  adminService.getBottleSessions.mockResolvedValue({ data: { items: [], total: 0 } });
});

describe('Bottle Transfers tab', () => {
  it('renders sender, receiver and the declared / confirmed Full and Empty straight from the payload', async () => {
    mockTransfers({ items: [transfer()] });
    const user = userEvent.setup();
    render(<BottleTracking />, { wrapper: createWrapper() });
    await user.click(tabLabel('Bottle Transfers'));

    const row = (await screen.findByText('Sender Sam')).closest('tr');
    expect(within(row).getByText('Receiver Rita')).toBeInTheDocument();
    const headers = [...document.querySelectorAll('.ant-tabs-tabpane-active th')].map((th) => th.textContent);
    const cell = (title) => row.querySelectorAll('td')[headers.indexOf(title)].textContent;
    expect(cell('Declared Full')).toBe('9');
    expect(cell('Declared Empty')).toBe('4');
    expect(cell('Confirmed Full')).toBe('6');
    expect(cell('Confirmed Empty')).toBe('3');
  });

  it('shows an em dash for a transfer that has not been counted yet', async () => {
    mockTransfers({
      items: [transfer({ status: 'pending', confirmed_quantity: null, confirmed_full: null, confirmed_empty: null, confirmed_total: null })],
    });
    const user = userEvent.setup();
    render(<BottleTracking />, { wrapper: createWrapper() });
    await user.click(tabLabel('Bottle Transfers'));

    const row = (await screen.findByText('Sender Sam')).closest('tr');
    const headers = [...document.querySelectorAll('.ant-tabs-tabpane-active th')].map((th) => th.textContent);
    expect(row.querySelectorAll('td')[headers.indexOf('Confirmed Full')].textContent).toBe('—');
    expect(row.querySelectorAll('td')[headers.indexOf('Confirmed Empty')].textContent).toBe('—');
  });

  it('resolves a dispute with exactly {resolvedFull, resolvedEmpty, resolutionNotes}', async () => {
    mockTransfers({ disputedTotal: 1, items: [transfer({ status: 'disputed' })] });
    adminService.resolveBottleTransferDispute.mockResolvedValue({ success: true });
    const user = userEvent.setup();
    render(<BottleTracking />, { wrapper: createWrapper() });
    await user.click(tabLabel('Bottle Transfers'));
    await screen.findByText('Sender Sam');
    await user.click(within(document.querySelector('.ant-tabs-tabpane-active .ant-table-tbody')).getByText('Resolve'));

    const modal = [...document.querySelectorAll('.ant-modal-wrap')].filter((m) => m.style.display !== 'none').pop();
    expect(within(modal).getByText(/Sender declared 9 full and 4 empty, receiver confirmed 6 full and 3 empty\./)).toBeInTheDocument();
    const field = (label) => [...modal.querySelectorAll('.ant-form-item')]
      .find((el) => el.querySelector('label')?.textContent?.includes(label));
    await user.type(within(field('Resolved Full')).getByRole('spinbutton'), '5');
    await user.type(within(field('Resolved Empty')).getByRole('spinbutton'), '2');
    await user.type(within(field('Resolution Notes')).getByRole('textbox'), 'counted twice');
    await user.click(within(modal).getByText('Resolve', { selector: '.ant-modal-footer span' }).closest('button'));

    await waitFor(() => expect(adminService.resolveBottleTransferDispute).toHaveBeenCalledTimes(1));
    expect(adminService.resolveBottleTransferDispute).toHaveBeenCalledWith(7, {
      resolvedFull: 5,
      resolvedEmpty: 2,
      resolutionNotes: 'counted twice',
    });
  });
});

describe('Disputed-count badge on the Bottle Transfers tab', () => {
  it('asks for exactly the disputed total, whichever tab is active', async () => {
    mockTransfers({ disputedTotal: 3 });
    render(<BottleTracking />, { wrapper: createWrapper() });
    await waitFor(() => {
      expect(adminService.getBottleTransfers).toHaveBeenCalledWith({ status: 'disputed', page: 1, per_page: 1 });
    });
  });

  it('shows the count on the tab label', async () => {
    mockTransfers({ disputedTotal: 3 });
    render(<BottleTracking />, { wrapper: createWrapper() });
    await waitFor(() => {
      expect(within(tabLabel('Bottle Transfers')).getByTitle('3')).toBeInTheDocument();
    });
  });

  it('shows no badge at zero', async () => {
    mockTransfers({ disputedTotal: 0 });
    render(<BottleTracking />, { wrapper: createWrapper() });
    await waitFor(() => expect(adminService.getBottleTransfers).toHaveBeenCalled());
    expect(tabLabel('Bottle Transfers').querySelector('.ant-badge')).toBeNull();
  });

  it('refreshes after a dispute is resolved', async () => {
    let disputed = 1;
    adminService.getBottleTransfers.mockImplementation(async (params) => {
      if (params?.status === 'disputed' && params?.per_page === 1) {
        return { data: { items: [], total: disputed } };
      }
      return { data: { items: [transfer({ status: 'disputed' })], total: 1 } };
    });
    adminService.resolveBottleTransferDispute.mockImplementation(async () => {
      disputed = 0;
      return { success: true };
    });
    const user = userEvent.setup();
    render(<BottleTracking />, { wrapper: createWrapper() });
    await waitFor(() => expect(within(tabLabel('Bottle Transfers')).getByTitle('1')).toBeInTheDocument());

    await user.click(tabLabel('Bottle Transfers'));
    await screen.findByText('Sender Sam');
    await user.click(within(document.querySelector('.ant-tabs-tabpane-active .ant-table-tbody')).getByText('Resolve'));
    const modal = [...document.querySelectorAll('.ant-modal-wrap')].filter((m) => m.style.display !== 'none').pop();
    const field = (label) => [...modal.querySelectorAll('.ant-form-item')]
      .find((el) => el.querySelector('label')?.textContent?.includes(label));
    await user.type(within(field('Resolved Full')).getByRole('spinbutton'), '5');
    await user.type(within(field('Resolved Empty')).getByRole('spinbutton'), '2');
    await user.type(within(field('Resolution Notes')).getByRole('textbox'), 'ok');
    await user.click(within(modal).getByText('Resolve', { selector: '.ant-modal-footer span' }).closest('button'));

    await waitFor(() => {
      expect(tabLabel('Bottle Transfers').querySelector('.ant-badge')).toBeNull();
    });
  });
});

describe('Driver session bottle numbers', () => {
  beforeEach(() => {
    mockTransfers();
    adminService.getBottleSessions.mockResolvedValue({ data: { items: [session()], total: 1 } });
    adminService.getBottleSession.mockResolvedValue({ data: session({ members: [] }) });
  });

  it('shows full on board, empties on board and free full in the sessions table', async () => {
    const user = userEvent.setup();
    render(<BottleTracking />, { wrapper: createWrapper() });
    await user.click(tabLabel('Driver Sessions'));
    const row = (await screen.findByText('Sender Sam')).closest('tr');
    const headers = [...document.querySelectorAll('.ant-tabs-tabpane-active th')].map((th) => th.textContent);
    const cell = (title) => row.querySelectorAll('td')[headers.indexOf(title)].textContent;
    expect(cell('Full on Board')).toBe('17');
    expect(cell('Empties on Board')).toBe('5');
    expect(cell('Free Full')).toBe('13');
  });

  it('shows the same numbers and the split transfer counters in the drawer', async () => {
    const user = userEvent.setup();
    render(<BottleTracking />, { wrapper: createWrapper() });
    await user.click(tabLabel('Driver Sessions'));
    await screen.findByText('Sender Sam');
    await user.click(within(document.querySelector('.ant-tabs-tabpane-active .ant-table-tbody')).getByText('Detail'));
    const drawer = await waitFor(() => {
      const d = document.querySelector('.ant-drawer-content');
      expect(d).not.toBeNull();
      expect(within(d).getByText('Full on Board')).toBeInTheDocument();
      return d;
    });
    const value = (label) => within(drawer).getByText(label).closest('th').nextElementSibling.textContent;
    expect(value('Full on Board')).toBe('17');
    expect(value('Empties on Board')).toBe('5');
    expect(value('Free Full')).toBe('13');
    // bottles_transferred_out / _in count FULL bottles only since the split, so
    // the label says so; empties have their own pair.
    expect(value('Full Transferred Out')).toBe('1');
    expect(value('Full Transferred In')).toBe('2');
    expect(value('Empties Transferred Out')).toBe('3');
    expect(value('Empties Transferred In')).toBe('4');
    expect(within(drawer).queryByText('Transferred Out')).toBeNull();
    expect(within(drawer).queryByText('Transferred In')).toBeNull();
  });
});
