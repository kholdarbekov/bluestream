import React from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';

import Tryouts from '../../pages/Tryouts';
import adminService from '../../services/adminService';
import tryoutService from '../../services/tryoutService';

const { act } = React;

vi.mock('../../components/AddressMapPicker', () => ({
  default: () => <div data-testid="address-map-picker">Map</div>,
}));

vi.mock('../../services/adminService', () => ({
  __esModule: true,
  default: {
    getProducts: vi.fn(),
    getDeliveryPersonnel: vi.fn(),
  },
}));

vi.mock('../../services/tryoutService', () => ({
  __esModule: true,
  default: {
    getTryouts: vi.fn(),
    getTryout: vi.fn(),
    createTryout: vi.fn(),
    assignTask: vi.fn(),
    convertTryout: vi.fn(),
    adjustBottles: vi.fn(),
    updateTryout: vi.fn(),
    exportTryouts: vi.fn(),
  },
}));

const listResponse = {
  items: [
    {
      id: 1,
      tryout_number: 'TRY_000001_26',
      status: 'active',
      outcome: 'pending',
      pickup_state: 'overdue',
      return_due_at: '2026-03-01T10:00:00Z',
      outstanding_bottles_total: 3,
      trial_contact: {
        full_name: 'Trial Customer',
        phone: '+998901112233',
      },
      converted_user: {
        id: 42,
        full_name: 'Existing User',
        phone: '+998901112233',
      },
      tasks: [
        {
          id: 12,
          task_type: 'pickup',
          status: 'open',
        },
      ],
    },
  ],
  total: 1,
  page: 1,
  per_page: 20,
  summary: {
    active_tryouts: 1,
    outstanding_bottles_total: 3,
    due_soon_count: 0,
    overdue_count: 1,
    converted_count: 0,
    collection_rate: 0,
  },
};

const detailResponse = {
  ...listResponse.items[0],
  trial_contact: {
    first_name: 'Trial',
    last_name: 'Customer',
    full_name: 'Trial Customer',
    phone: '+998901112233',
    preferred_language: 'uz',
    notes: 'Original contact note',
  },
  notes: 'Bring back bottles on next route',
  internal_notes: 'High-priority recovery',
  address_snapshot: {
    label: 'Office',
    full_address: '12 Sample Street',
    district: 'Yunusabad',
    city: 'Tashkent',
    delivery_notes: 'Side entrance',
  },
  items: [
    {
      id: 44,
      product_id: 5,
      product_name: 'Pure Water 19L',
      quantity: 3,
      unit_price_snapshot: 15000,
      returnable_bottles_due: 3,
    },
  ],
  ledger: [
    {
      id: 77,
      event_type: 'handoff',
      product_name: 'Pure Water 19L',
      units: 3,
      occurred_at: '2026-03-01T09:00:00Z',
      notes: 'Try-out handoff completed',
    },
  ],
};

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

const waitForText = async (text, { timeout = 2000 } = {}) => {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    if (document.body.textContent.includes(text)) return true;
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
  }
  return false;
};

const createWrapper = (children) => {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  return (
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        {children}
      </QueryClientProvider>
    </MemoryRouter>
  );
};

const findButtonByText = (label) =>
  Array.from(document.querySelectorAll('button')).find((button) =>
    button.textContent.includes(label)
  );

beforeAll(() => {
  global.IS_REACT_ACT_ENVIRONMENT = true;
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: vi.fn().mockImplementation((query) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  });
});

describe('Tryouts Page', () => {
  let container;
  let root;

  beforeEach(() => {
    vi.clearAllMocks();
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);

    tryoutService.getTryouts.mockResolvedValue(listResponse);
    tryoutService.getTryout.mockResolvedValue(detailResponse);
    adminService.getProducts.mockResolvedValue({
      data: {
        items: [
          {
            id: 5,
            name: 'Pure Water 19L',
            is_tryout_eligible: true,
            tracks_returnable_bottles: true,
          },
        ],
      },
    });
    adminService.getDeliveryPersonnel.mockResolvedValue({
      data: {
        items: [
          {
            user_id: 9,
            full_name: 'Driver One',
          },
        ],
      },
    });
  });

  afterEach(async () => {
    await act(async () => {
      root.unmount();
      await flush();
    });
    container.remove();
    document.body.innerHTML = '';
  });

  const renderPage = async () => {
    await act(async () => {
      root.render(createWrapper(<Tryouts />));
      await flush();
      await flush();
      await flush();
      await flush();
    });
  };

  it('renders the KPI cards and try-out row', async () => {
    await renderPage();
    await waitForText('TRY_000001_26');

    expect(document.body.textContent).toContain('Try-outs');
    expect(document.body.textContent).toContain('Free product handoffs and returnable bottle recovery');
    expect(tryoutService.getTryouts).toHaveBeenCalled();
    expect(document.body.textContent).toContain('Outstanding Bottles');
    expect(document.body.textContent).toContain('TRY_000001_26');
    expect(document.body.textContent).toContain('Trial Customer');
    expect(document.body.textContent).toContain('Linked user: Existing User');
    expect(document.body.textContent).toContain('overdue');
  });

  it('opens the detail drawer for a try-out', async () => {
    await renderPage();
    await waitForText('TRY_000001_26');

    const viewButton = findButtonByText('View');
    expect(viewButton).toBeTruthy();

    await act(async () => {
      viewButton.dispatchEvent(new MouseEvent('click', { bubbles: true }));
      await flush();
      await flush();
    });

    expect(tryoutService.getTryout).toHaveBeenCalledWith(1);
    expect(document.body.textContent).toContain('Overview');
    expect(document.body.textContent).toContain('12 Sample Street');
    expect(document.body.textContent).toContain('Bring back bottles on next route');
    expect(document.body.textContent).toContain('#42 Existing User (+998901112233)');
  });

  it('opens the create modal from the page action', async () => {
    await renderPage();

    const createButton = findButtonByText('Create Try-out');
    expect(createButton).toBeTruthy();

    await act(async () => {
      createButton.dispatchEvent(new MouseEvent('click', { bubbles: true }));
      await flush();
      await flush();
    });

    expect(document.body.textContent).toContain('First Name');
    expect(document.body.textContent).toContain('Full Address');
  });

  it('opens the shared edit modal with phone and product fields', async () => {
    await renderPage();
    await waitForText('TRY_000001_26');

    const editButton = findButtonByText('Edit');
    expect(editButton).toBeTruthy();

    await act(async () => {
      editButton.dispatchEvent(new MouseEvent('click', { bubbles: true }));
      await flush();
      await flush();
    });

    expect(document.body.textContent).toContain('Edit Try-out');
    expect(document.body.textContent).toContain('Phone');
    expect(document.body.textContent).toContain('Product');
    expect(document.body.textContent).toContain('Qty');
    expect(document.body.textContent).toContain('Contact Notes');
    expect(document.body.textContent).toContain('Delivery Notes');
  });
});
