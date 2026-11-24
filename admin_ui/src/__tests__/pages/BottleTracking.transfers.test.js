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
      resolvedUnits: [],
      resolutionNotes: 'counted twice',
    }, { handledErrorCodes: ['BOTTLE_SESSION_MEMBERSHIP_ALREADY_ACTIVE'] });
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

// Keys mirror serialize_bottle_session `units` (_unit_lines in bottle_serializers.py).
const tenLitreLine = (overrides = {}) => ({
  product_id: 3,
  product_name: '10 Litr suv',
  loaded: 12,
  delivered: 4,
  transferred_in: 0,
  transferred_out: 0,
  on_board: 8,
  transferable: 8,
  free: 8,
  returned: null,
  discrepancy: null,
  ...overrides,
});

const sessionHeaders = () => [...document.querySelectorAll('.ant-tabs-tabpane-active th')].map((th) => th.textContent);
const cellOf = (row, title) => row.querySelectorAll('td')[sessionHeaders().indexOf(title)].textContent;

const openSessionsTab = async (user) => {
  render(<BottleTracking />, { wrapper: createWrapper() });
  await user.click(tabLabel('Driver Sessions'));
};

const activeModal = () => [...document.querySelectorAll('.ant-modal-wrap')].filter((m) => m.style.display !== 'none').pop();
const fieldOf = (modal, label) => [...modal.querySelectorAll('.ant-form-item')]
  .find((el) => el.querySelector('label')?.textContent?.includes(label));

describe('Driver Sessions: one-way unit columns', () => {
  beforeEach(() => {
    mockTransfers();
  });

  it('shows the 10 L columns and a dash for a trip without the line', async () => {
    adminService.getBottleSessions.mockResolvedValue({
      data: {
        items: [
          session({ id: 3, driver_name: 'Has Line', units: [tenLitreLine({ returned: 7, discrepancy: 1 })] }),
          session({ id: 4, driver_name: 'No Line', units: [] }),
        ],
        total: 2,
      },
    });
    const user = userEvent.setup();
    await openSessionsTab(user);

    const withLine = (await screen.findByText('Has Line')).closest('tr');
    expect(cellOf(withLine, '10 Litr suv loaded')).toBe('12');
    expect(cellOf(withLine, '10 Litr suv delivered')).toBe('4');
    expect(cellOf(withLine, '10 Litr suv on board')).toBe('8');
    expect(cellOf(withLine, '10 Litr suv returned')).toBe('7');
    expect(cellOf(withLine, '10 Litr suv discrepancy')).toBe('1');
    const withoutLine = screen.getByText('No Line').closest('tr');
    expect(cellOf(withoutLine, '10 Litr suv loaded')).toBe('—');
    expect(cellOf(withoutLine, '10 Litr suv delivered')).toBe('—');
    expect(cellOf(withoutLine, '10 Litr suv on board')).toBe('—');
    expect(cellOf(withoutLine, '10 Litr suv returned')).toBe('—');
    expect(cellOf(withoutLine, '10 Litr suv discrepancy')).toBe('—');
  });

  it('marks a trip red for a 10 L-only discrepancy', async () => {
    adminService.getBottleSessions.mockResolvedValue({
      data: {
        items: [session({ discrepancy: 0, has_discrepancy: true, units: [tenLitreLine({ returned: 6, discrepancy: 2 })] })],
        total: 1,
      },
    });
    const user = userEvent.setup();
    await openSessionsTab(user);
    const row = (await screen.findByText('Sender Sam')).closest('tr');
    expect(row).toHaveClass('ant-table-row-danger');
  });

  it('colours the row from has_discrepancy alone', async () => {
    adminService.getBottleSessions.mockResolvedValue({
      data: { items: [session({ discrepancy: 3, has_discrepancy: false, units: [] })], total: 1 },
    });
    const user = userEvent.setup();
    await openSessionsTab(user);
    const row = (await screen.findByText('Sender Sam')).closest('tr');
    expect(row).not.toHaveClass('ant-table-row-danger');
  });

  it('sends the 10 L returned box with Force Close', async () => {
    adminService.getBottleSessions.mockResolvedValue({
      data: { items: [session({ units: [tenLitreLine()] })], total: 1 },
    });
    adminService.forceCloseBottleSession.mockResolvedValue({ success: true });
    const user = userEvent.setup();
    await openSessionsTab(user);
    await screen.findByText('Sender Sam');
    await user.click(within(document.querySelector('.ant-tabs-tabpane-active .ant-table-tbody')).getByText('Force Close'));

    const modal = activeModal();
    await user.clear(within(fieldOf(modal, 'Bottles returned to warehouse')).getByRole('spinbutton'));
    await user.type(within(fieldOf(modal, 'Bottles returned to warehouse')).getByRole('spinbutton'), '2');
    await user.type(within(fieldOf(modal, '10 Litr suv returned (if known)')).getByRole('spinbutton'), '5');
    await user.type(within(fieldOf(modal, 'Reason')).getByRole('textbox'), 'driver left');
    await user.click(within(modal).getByText('Force Close', { selector: '.ant-modal-footer span' }).closest('button'));

    await waitFor(() => expect(adminService.forceCloseBottleSession).toHaveBeenCalledTimes(1));
    expect(adminService.forceCloseBottleSession).toHaveBeenCalledWith(3, {
      reason: 'driver left',
      bottles_returned_to_warehouse: 2,
      units_returned: [{ product_id: 3, quantity: 5 }],
    });
  });

  it('shows each line with sent and received, and marks a rejected transfer, in the drawer', async () => {
    adminService.getBottleSessions.mockResolvedValue({ data: { items: [session({ units: [tenLitreLine()] })], total: 1 } });
    const lines = (confirmed) => [
      { kind: 'full', product_id: null, product_name: null, declared: 9, confirmed, resolved: null },
      {
        kind: 'empty', product_id: null, product_name: null, declared: 4, confirmed: confirmed === null ? null : 3, resolved: null,
      },
      { kind: 'product', product_id: 3, product_name: '10 Litr suv', declared: 6, confirmed, resolved: null },
    ];
    // What the backend publishes for this trip: its only transfer was rejected, so what it carried went back to it
    // (spec §6.11) and counts in no transfer total. 18.9 L: 20 loaded, nothing delivered or collected. 10 L:
    // on board = loaded 12 - delivered 4.
    adminService.getBottleSession.mockResolvedValue({
      data: session({
        bottles_transferred_out: 0,
        bottles_transferred_in: 0,
        empties_transferred_out: 0,
        empties_transferred_in: 0,
        current_inventory: 20,
        full_bottles_on_board: 20,
        empties_on_board: 0,
        free_full_bottles: 20,
        units: [tenLitreLine({ transferred_out: 0, transferred_in: 0, on_board: 8, transferable: 8, free: 8 })],
        members: [],
        transfers_out: [transfer({ id: 8, status: 'rejected', rejected_at: '2026-11-09T09:30:00', lines: lines(null) })],
        transfers_in: [],
      }),
    });
    const user = userEvent.setup();
    await openSessionsTab(user);
    await screen.findByText('Sender Sam');
    await user.click(within(document.querySelector('.ant-tabs-tabpane-active .ant-table-tbody')).getByText('Detail'));
    const drawer = await waitFor(() => {
      const d = document.querySelector('.ant-drawer-content');
      expect(within(d).getByText('Receiver Rita')).toBeInTheDocument();
      return d;
    });
    expect(within(drawer).getByText('Rejected')).toBeInTheDocument();
    // The trip's own 10 L block: a rejected transfer counts in neither total, so Sent and Received stay 0.
    const unitBlock = within(drawer).getByText('10 Litr suv', { selector: '.ant-descriptions-title' })
      .closest('.ant-descriptions');
    const figure = (label) => within(unitBlock).getByText(label).closest('th').nextElementSibling.textContent;
    expect(figure('10 Litr suv loaded')).toBe('12');
    expect(figure('10 Litr suv delivered')).toBe('4');
    expect(figure('10 Litr suv on board')).toBe('8');
    expect(figure('Sent')).toBe('0');
    expect(figure('Received')).toBe('0');
    expect(figure('10 Litr suv returned')).toBe('—');
    expect(figure('10 Litr suv discrepancy')).toBe('—');
    const tripFigure = (label) => within(drawer).getByText(label).closest('th').nextElementSibling.textContent;
    expect(tripFigure('Full on Board')).toBe('20');
    expect(tripFigure('Full Transferred Out')).toBe('0');
    expect(tripFigure('Empties Transferred Out')).toBe('0');
    // The transfer row still shows what the sender declared, and nothing counted.
    expect(within(drawer).getByText(/10 Litr suv: Sent 6 · Received —/)).toBeInTheDocument();
  });

  it('shows what the office resolved as received on each line of a resolved transfer, as the trip does', async () => {
    adminService.getBottleSessions.mockResolvedValue({ data: { items: [session({ units: [tenLitreLine()] })], total: 1 } });
    // What the backend publishes after Bobur counted 10 full + 4 x 10 L of 10 + 6 and the office resolved 10 / 6: the
    // full line's count was overwritten by the resolve, the 10 L line keeps his 4 beside the resolved 6, and his trip's
    // 10 L line was credited 6.
    adminService.getBottleSession.mockResolvedValue({
      data: session({
        units: [tenLitreLine({ transferred_out: 0, transferred_in: 6, on_board: 14, transferable: 14, free: 14 })],
        members: [],
        transfers_out: [],
        transfers_in: [transfer({
          id: 10,
          status: 'resolved',
          lines: [
            { kind: 'full', product_id: null, product_name: null, declared: 10, confirmed: 10, resolved: 10 },
            { kind: 'empty', product_id: null, product_name: null, declared: 0, confirmed: 0, resolved: 0 },
            { kind: 'product', product_id: 3, product_name: '10 Litr suv', declared: 6, confirmed: 4, resolved: 6 },
          ],
        })],
      }),
    });
    const user = userEvent.setup();
    await openSessionsTab(user);
    await screen.findByText('Sender Sam');
    await user.click(within(document.querySelector('.ant-tabs-tabpane-active .ant-table-tbody')).getByText('Detail'));
    const drawer = await waitFor(() => {
      const d = document.querySelector('.ant-drawer-content');
      expect(within(d).getByText('Receiver Rita')).toBeInTheDocument();
      return d;
    });
    const unitBlock = within(drawer).getByText('10 Litr suv', { selector: '.ant-descriptions-title' })
      .closest('.ant-descriptions');
    const figure = (label) => within(unitBlock).getByText(label).closest('th').nextElementSibling.textContent;
    expect(figure('Received')).toBe('6');
    expect(within(drawer).getByText(/10 Litr suv: Sent 6 · Received 6/)).toBeInTheDocument();
    expect(within(drawer).queryByText(/10 Litr suv: Sent 6 · Received 4/)).toBeNull();
  });
});

describe('Bottle Transfers tab: unit lines, Rejected, Resolve', () => {
  it('filters and tags Rejected and offers no Resolve on it', async () => {
    const rejected = transfer({
      id: 9,
      status: 'rejected',
      rejected_at: '2026-11-09T09:30:00',
      confirmed_quantity: null,
      confirmed_full: null,
      confirmed_empty: null,
      confirmed_total: null,
    });
    mockTransfers({ items: [rejected] });
    const user = userEvent.setup();
    render(<BottleTracking />, { wrapper: createWrapper() });
    await user.click(tabLabel('Bottle Transfers'));
    const row = (await screen.findByText('Sender Sam')).closest('tr');
    const tag = within(row).getByText('Rejected');
    expect(tag.closest('.ant-tag')).toHaveClass('ant-tag-orange');
    expect(within(row).getByText(/Rejected at/)).toBeInTheDocument();
    expect(within(row).queryByText('Resolve')).toBeNull();

    // the filter offers Rejected and sends it as the status
    await user.click(document.querySelector('.ant-tabs-tabpane-active .ant-select-selector'));
    await user.click(await screen.findByTitle('Rejected'));
    await waitFor(() => {
      expect(adminService.getBottleTransfers).toHaveBeenCalledWith(expect.objectContaining({ status: 'rejected' }));
    });
  });

  it('shows declared, counted and resolved for each unit line', async () => {
    mockTransfers({
      items: [transfer({
        status: 'resolved',
        units: [{ product_id: 3, product_name: '10 Litr suv', declared: 6, confirmed: 5, resolved: 4 }],
      })],
    });
    const user = userEvent.setup();
    render(<BottleTracking />, { wrapper: createWrapper() });
    await user.click(tabLabel('Bottle Transfers'));
    const row = (await screen.findByText('Sender Sam')).closest('tr');
    expect(within(row).getByText('10 Litr suv: declared 6, counted 5, resolved 4')).toBeInTheDocument();
  });

  it('sends the 10 L line with Resolve', async () => {
    mockTransfers({
      disputedTotal: 1,
      items: [transfer({
        status: 'disputed',
        units: [{ product_id: 3, product_name: '10 Litr suv', declared: 6, confirmed: 5, resolved: null }],
      })],
    });
    adminService.resolveBottleTransferDispute.mockResolvedValue({ success: true });
    const user = userEvent.setup();
    render(<BottleTracking />, { wrapper: createWrapper() });
    await user.click(tabLabel('Bottle Transfers'));
    await screen.findByText('Sender Sam');
    await user.click(within(document.querySelector('.ant-tabs-tabpane-active .ant-table-tbody')).getByText('Resolve'));

    const modal = activeModal();
    await user.type(within(fieldOf(modal, 'Resolved Full')).getByRole('spinbutton'), '5');
    await user.type(within(fieldOf(modal, 'Resolved Empty')).getByRole('spinbutton'), '2');
    await user.type(within(fieldOf(modal, 'Resolved 10 Litr suv')).getByRole('spinbutton'), '5');
    await user.type(within(fieldOf(modal, 'Resolution Notes')).getByRole('textbox'), 'counted twice');
    await user.click(within(modal).getByText('Resolve', { selector: '.ant-modal-footer span' }).closest('button'));

    await waitFor(() => expect(adminService.resolveBottleTransferDispute).toHaveBeenCalledTimes(1));
    expect(adminService.resolveBottleTransferDispute).toHaveBeenCalledWith(7, {
      resolvedFull: 5,
      resolvedEmpty: 2,
      resolvedUnits: [{ productId: 3, quantity: 5 }],
      resolutionNotes: 'counted twice',
    }, { handledErrorCodes: ['BOTTLE_SESSION_MEMBERSHIP_ALREADY_ACTIVE'] });
  });

  it('says the receiver is co-driving, with the owner name, on the 409, and keeps the modal open', async () => {
    mockTransfers({ disputedTotal: 1, items: [transfer({ status: 'disputed' })] });
    adminService.resolveBottleTransferDispute.mockRejectedValue({
      response: {
        status: 409,
        data: {
          error_code: 'BOTTLE_SESSION_MEMBERSHIP_ALREADY_ACTIVE',
          message: 'Driver already has an active membership',
          details: { session_id: 31, owner_user_id: 5, owner_name: 'Olim Owner' },
        },
      },
    });
    const user = userEvent.setup();
    render(<BottleTracking />, { wrapper: createWrapper() });
    await user.click(tabLabel('Bottle Transfers'));
    await screen.findByText('Sender Sam');
    await user.click(within(document.querySelector('.ant-tabs-tabpane-active .ant-table-tbody')).getByText('Resolve'));
    const modal = activeModal();
    await user.type(within(fieldOf(modal, 'Resolved Full')).getByRole('spinbutton'), '5');
    await user.type(within(fieldOf(modal, 'Resolved Empty')).getByRole('spinbutton'), '2');
    await user.type(within(fieldOf(modal, 'Resolution Notes')).getByRole('textbox'), 'x');
    await user.click(within(modal).getByText('Resolve', { selector: '.ant-modal-footer span' }).closest('button'));

    expect(await screen.findByText("This driver is on Olim Owner's trip right now. Resolve this transfer after that trip ends.")).toBeInTheDocument();
    expect(activeModal()).toBe(modal);
  });
});
