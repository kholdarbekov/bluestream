import React from 'react';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import {
  describeAllocationScope,
  describeCashEditWarning,
  hasPlaceScopedAllocation,
} from '../../utils/cashScopeDisplay';
import Orders from '../../pages/Orders';
import adminService from '../../services/adminService';
import api from '../../services/api';

vi.mock('../../services/adminService');
vi.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  },
  getCookie: vi.fn(),
}));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key, fallback) => fallback || key,
  }),
}));

vi.mock('../../components/common/PermissionGuard', async () => {
  const actual = await vi.importActual('../../components/common/PermissionGuard');
  return {
    ...actual,
    usePermissions: vi.fn(() => ({
      isAdmin: () => true,
      isManager: () => false,
      isOperator: () => false,
      hasPermission: () => true,
      canManageOrders: () => true,
    })),
  };
});

// antd's static `message`/`Modal` APIs mount a real portal outside act() and
// leak across tests — stub them so the assertions read the arguments instead.
const modalWarning = vi.fn();
vi.mock('antd', async () => {
  const actual = await vi.importActual('antd');
  return {
    ...actual,
    Modal: Object.assign(
      (props) => actual.Modal(props),
      actual.Modal,
      { warning: (...args) => modalWarning(...args) },
    ),
    message: {
      success: vi.fn(),
      error: vi.fn(),
      info: vi.fn(),
      warning: vi.fn(),
      loading: vi.fn(),
      destroy: vi.fn(),
      open: vi.fn(),
    },
    Dropdown: ({ menu, children }) => (
      <div>
        {children}
        {menu?.items
          ?.filter((item) => item && item.type !== 'divider' && item.onClick)
          .map((item) => (
            <button key={item.key} onClick={item.onClick} type="button" disabled={item.disabled}>
              {typeof item.label === 'string' ? item.label : item.key}
            </button>
          ))}
      </div>
    ),
  };
});

vi.setConfig({ testTimeout: 15000 });

const t = (key, fallback) => fallback || key;

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

describe('describeAllocationScope', () => {
  it('labels place allocations with the group label and attribution', () => {
    const label = describeAllocationScope({
      scope_type: 'place',
      scope_group_label: 'Acme office',
      source_customer_id: 7,
      beneficiary_user_id: 9,
    }, t);
    expect(label).toContain('Acme office');
    expect(label).toContain('#7');
    expect(label).toContain('#9');
  });

  it('labels cluster allocations and omits the arrow when payer == beneficiary', () => {
    const label = describeAllocationScope({
      scope_type: 'cluster',
      source_customer_id: 7,
      beneficiary_user_id: 7,
    }, t);
    expect(label).toBe('Linked-accounts collection');
  });

  it('falls back to a generic place label when the group has no name', () => {
    const label = describeAllocationScope({
      scope_type: 'place',
      scope_group_label: null,
      source_customer_id: 4,
      beneficiary_user_id: 5,
    }, t);
    expect(label).toContain('Place collection');
    expect(label).toContain('#4 → #5');
  });

  it('returns empty for personal allocations (no visual noise)', () => {
    expect(describeAllocationScope({ scope_type: 'personal' }, t)).toBe('');
  });

  it('returns empty for legacy rows with no scope at all', () => {
    expect(describeAllocationScope({}, t)).toBe('');
    expect(describeAllocationScope(null, t)).toBe('');
  });
});

describe('describeCashEditWarning', () => {
  // Wire form: OrderCashEditService emits "<code>: <text>" / "<code> - <text>",
  // never a bare code — assert on the real strings.
  it('maps known scope warnings and falls back to the raw string', () => {
    expect(describeCashEditWarning("customer_has_other_unpaid_cod_orders: corrected cash settles the scope's oldest unpaid order first, so the per-order figures above are approximate", t))
      .toMatch(/oldest unpaid/i);
    expect(describeCashEditWarning("correction_pushes_cod_over_cap - the customer's cluster or this place will be at/over the COD active-debt limit after this edit", t))
      .toMatch(/COD debt cap/i);
    expect(describeCashEditWarning('totally_unknown_warning', t))
      .toBe('totally_unknown_warning');
  });

  it('maps every warning code OrderCashEditService can emit', () => {
    // Verbatim from business_app/services/order_cash_edit_service.py.
    const wire = [
      'delivery_timestamp_missing - treating window as unlimited',
      'collected_below_order_total - order will not be fully paid; loyalty may need manual review',
      'order_already_settled_by_other_source - this order is already paid (card transfer '
        + 'or prepaid credit), so nothing applies to it and the full amount becomes customer credit',
      "surplus_credited_to_customer - auto-applies to the customer's other unpaid orders if any",
      'customer_has_other_unpaid_cod_orders: corrected cash settles the '
        + "scope's oldest unpaid order first, so the per-order figures above "
        + 'are approximate',
      "correction_pushes_cod_over_cap - the customer's cluster or this "
        + 'place will be at/over the COD active-debt limit after this edit',
    ];
    wire.forEach((warning) => {
      expect(describeCashEditWarning(warning, t)).not.toBe(warning);
    });
  });

  it('keeps an unknown code visible verbatim rather than swallowing it', () => {
    const unknown = 'brand_new_backend_warning: something the UI has never seen';
    expect(describeCashEditWarning(unknown, t)).toBe(unknown);
    expect(describeCashEditWarning('', t)).toBe('');
    expect(describeCashEditWarning(null, t)).toBe('');
  });
});

describe('hasPlaceScopedAllocation', () => {
  it('detects a place-scoped allocation anywhere in the timeline', () => {
    expect(hasPlaceScopedAllocation([
      { type: 'payment_created' },
      { type: 'cash_collection_allocation', scope_type: 'place' },
    ])).toBe(true);
  });

  it('is false for personal/cluster-only or missing timelines', () => {
    expect(hasPlaceScopedAllocation([{ scope_type: 'personal' }, { scope_type: 'cluster' }])).toBe(false);
    expect(hasPlaceScopedAllocation([])).toBe(false);
    expect(hasPlaceScopedAllocation(undefined)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Orders page wiring
// ---------------------------------------------------------------------------

const createWrapper = () => {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
};

const PLACE_TIMELINE = {
  order_id: 620,
  timeline: [
    {
      type: 'cash_collection_allocation',
      timestamp: '2026-07-20T09:00:00+00:00',
      allocated_amount: 54000,
      allocation_mode: 'auto',
      collection_event_id: 31,
      collection_amount: 108000,
      collection_source: 'delivery',
      notes: null,
      scope_type: 'place',
      scope_group_id: 3,
      scope_group_label: 'Acme office',
      source_customer_id: 7,
      beneficiary_user_id: 9,
    },
  ],
};

const DELIVERED_CASH_ORDER = {
  id: 620,
  order_number: 'TG_000183_26',
  user_id: 88,
  status: 'delivered',
  payment_method: 'cash',
  payment_status: 'completed',
  total_amount: 54000,
  amount_collected: 54000,
  outstanding_amount: 0,
  is_collected_cash_editable: true,
  collected_cash_event_amount: 54000,
  collected_cash_edit_window_remaining_hours: 23.5,
  customer_name: 'Test Driver Cash',
  customer_phone: '+998901234567',
  created_at: '2026-06-23T10:00:00+00:00',
  items_summary: [],
  items_count: 1,
};

function setupBaseMocks({ timeline = PLACE_TIMELINE, warnings = [] } = {}) {
  vi.clearAllMocks();
  modalWarning.mockClear();

  api.get.mockResolvedValue({
    data: { data: { statuses: [{ value: 'delivered', label: 'Delivered' }] } },
  });

  adminService.getOrders.mockResolvedValue({
    data: { items: [DELIVERED_CASH_ORDER] },
    meta: { total: 1 },
  });

  adminService.getOrderDetails.mockResolvedValue({
    success: true,
    data: { order: { ...DELIVERED_CASH_ORDER, items: [], payment_timeline: timeline } },
  });

  adminService.getOrderEditHistory.mockResolvedValue({ success: true, data: { entries: [] } });

  adminService.previewCollectedCashEdit.mockResolvedValue({
    data: {
      new_amount: 60000,
      applied_to_order: 54000,
      projected_outstanding: 0,
      customer_credit_delta: 6000,
      session_will_reopen: false,
      blocking_reasons: [],
      warnings,
    },
  });

  adminService.editCollectedCash.mockResolvedValue({ data: { order_id: 620, warnings } });
  adminService.getProducts.mockResolvedValue({ data: { items: [] } });
}

const openOrderDetails = async (user) => {
  render(<Orders />, { wrapper: createWrapper() });
  await waitFor(() => expect(adminService.getOrders).toHaveBeenCalled());
  await user.click(await screen.findByText(/view_details|View Details/i));
  await waitFor(() => expect(adminService.getOrderDetails).toHaveBeenCalledWith(620));
};

describe('Orders payment timeline money attribution', () => {
  it('renders the place group label and the payer → beneficiary stamps', async () => {
    setupBaseMocks();
    const user = userEvent.setup();

    await openOrderDetails(user);

    expect(await screen.findByText(/Acme office/)).toBeInTheDocument();
    expect(screen.getByText(/#7 → #9/)).toBeInTheDocument();
  });

  it('shows no scope noise for a personal (unlinked, ungrouped) allocation', async () => {
    setupBaseMocks({
      timeline: {
        order_id: 620,
        timeline: [{
          type: 'cash_collection_allocation',
          timestamp: '2026-07-20T09:00:00+00:00',
          allocated_amount: 54000,
          scope_type: 'personal',
          scope_group_id: null,
          scope_group_label: null,
          source_customer_id: 88,
          beneficiary_user_id: 88,
        }],
      },
    });
    const user = userEvent.setup();

    await openOrderDetails(user);

    await screen.findByText(/Payment Timeline/i);
    expect(screen.queryByText(/Place collection/i)).toBeNull();
    expect(screen.queryByText(/Linked-accounts collection/i)).toBeNull();
    expect(screen.queryByText(/→/)).toBeNull();
  });
});

describe('Orders collected-cash modal scope copy and warnings', () => {
  it('tells the admin the correction settles the PLACE oldest unpaid order first', async () => {
    setupBaseMocks();
    const user = userEvent.setup();

    await openOrderDetails(user);
    fireEvent.click(await screen.findByText(/Edit collected cash/i));

    expect(await screen.findByText(/Settles this place's oldest unpaid order first/i)).toBeInTheDocument();
  });

  it('falls back to cluster copy when nothing in the timeline is place-scoped', async () => {
    setupBaseMocks({ timeline: { order_id: 620, timeline: [] } });
    const user = userEvent.setup();

    await openOrderDetails(user);
    fireEvent.click(await screen.findByText(/Edit collected cash/i));

    expect(
      await screen.findByText(/Settles the customer's \(and linked accounts'\) oldest unpaid order first/i),
    ).toBeInTheDocument();
  });

  it('renders translated copy for real backend warning strings and keeps unknown codes verbatim', async () => {
    const rawScopeWarning = "customer_has_other_unpaid_cod_orders: corrected cash settles the "
      + "scope's oldest unpaid order first, so the per-order figures above are approximate";
    const rawCapWarning = "correction_pushes_cod_over_cap - the customer's cluster or this "
      + 'place will be at/over the COD active-debt limit after this edit';
    const rawUnknown = 'brand_new_backend_warning: the UI has never seen this one';
    setupBaseMocks({ warnings: [rawScopeWarning, rawCapWarning, rawUnknown] });
    const user = userEvent.setup();

    await openOrderDetails(user);
    fireEvent.click(await screen.findByText(/Edit collected cash/i));

    const reasonInputs = screen.getAllByRole('textbox');
    const reasonInput = reasonInputs[reasonInputs.length - 1];
    await user.clear(reasonInput);
    await user.type(reasonInput, 'driver miscounted the cash');

    fireEvent.click(screen.getByText(/Preview impact/i));
    await waitFor(() => expect(adminService.previewCollectedCashEdit).toHaveBeenCalled());

    // Mapped copy, not the raw backend sentence.
    expect(await screen.findByText(/Extra cash settles the scope's oldest unpaid order first/i)).toBeInTheDocument();
    expect(screen.queryByText(rawScopeWarning)).toBeNull();
    expect(screen.getByText(/COD debt cap/i)).toBeInTheDocument();
    expect(screen.queryByText(rawCapWarning)).toBeNull();

    // Unknown code still surfaces verbatim — never swallowed.
    expect(screen.getByText(rawUnknown)).toBeInTheDocument();
  });

  it('maps the post-apply warning modal copy too', async () => {
    const rawCapWarning = "correction_pushes_cod_over_cap - the customer's cluster or this "
      + 'place will be at/over the COD active-debt limit after this edit';
    setupBaseMocks({ warnings: [rawCapWarning] });
    const user = userEvent.setup();

    await openOrderDetails(user);
    fireEvent.click(await screen.findByText(/Edit collected cash/i));

    const reasonInputs = screen.getAllByRole('textbox');
    const reasonInput = reasonInputs[reasonInputs.length - 1];
    await user.clear(reasonInput);
    await user.type(reasonInput, 'driver miscounted the cash');

    fireEvent.click(screen.getByText(/Preview impact/i));
    await waitFor(() => expect(adminService.previewCollectedCashEdit).toHaveBeenCalled());

    fireEvent.click(await screen.findByText(/Apply correction/i));
    await waitFor(() => expect(adminService.editCollectedCash).toHaveBeenCalled());

    await waitFor(() => expect(modalWarning).toHaveBeenCalled());
    const { content } = modalWarning.mock.calls[0][0];
    expect(content).toMatch(/COD debt cap/i);
    expect(content).not.toContain(rawCapWarning);
  });
});
