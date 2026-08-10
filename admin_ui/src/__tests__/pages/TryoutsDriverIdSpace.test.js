import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import Tryouts from '../../pages/Tryouts';
import adminService from '../../services/adminService';
import tryoutService from '../../services/tryoutService';

/**
 * The driver Selects on this page must emit `user_id` (the users.id that every
 * *_user_id column FKs to), never `id` (the delivery_persons PK).
 *
 * Production shipped `driver.user_id || driver.id` while the backing endpoint
 * (`GET /admin/delivery-personnel`) did not serialize `user_id` at all, so the
 * fallback always fired and a delivery_persons.id was written into
 * `tryout_tasks.assigned_driver_user_id`. The two id spaces overlap numerically,
 * so the task silently pointed at an unrelated customer and the Tasks tab
 * rendered that customer's name in the Driver column.
 *
 * The fixture keeps `id` and `user_id` far apart on purpose. The pre-existing
 * suite could not catch this: the page test mocked an EMPTY driver list (so the
 * `||` never evaluated) and the component test mocked `{user_id: 9}` — a shape
 * the real endpoint never returned — which exercised the correct branch and left
 * the buggy fallback dead under test.
 */
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
    exportTryouts: vi.fn(),
    assignTask: vi.fn(),
  },
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, opts) => (opts && opts.defaultValue) || key }),
}));

const DELIVERY_PERSON_ID = 7;
const DRIVER_USER_ID = 175;
const DRIVER_NAME = 'Nurdaulet';

const createWrapper = () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
};

// The driver filter is the 3rd Select in the filter card (status, pickup_state,
// driver). Matching on the placeholder text is unambiguous — the table's own
// "Driver" column header lives outside `.ant-select`.
function driverFilterSelector() {
  const placeholder = Array.from(document.querySelectorAll('.ant-select-selection-placeholder'))
    .find((node) => node.textContent === 'Driver');
  return placeholder ? placeholder.closest('.ant-select').querySelector('.ant-select-selector') : null;
}

function driverOption() {
  return document.querySelector(`.ant-select-dropdown .ant-select-item-option[title="${DRIVER_NAME}"]`);
}

describe('Tryouts driver Selects use the users.id space', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    adminService.getProducts.mockResolvedValue({ data: { items: [], total: 0 } });
    adminService.getDeliveryPersonnel.mockResolvedValue({
      data: {
        items: [{
          id: DELIVERY_PERSON_ID,
          user_id: DRIVER_USER_ID,
          full_name: DRIVER_NAME,
          phone: '+998900000001',
        }],
        total: 1,
      },
    });
    tryoutService.getTryouts.mockResolvedValue({ items: [], total: 0, summary: {} });
  });

  it('filters by the driver user_id, not the delivery_persons id', async () => {
    render(<Tryouts />, { wrapper: createWrapper() });

    await waitFor(() => expect(driverFilterSelector()).toBeTruthy());
    fireEvent.mouseDown(driverFilterSelector());

    await waitFor(() => expect(driverOption()).toBeTruthy());
    fireEvent.click(driverOption());

    await waitFor(() => {
      expect(tryoutService.getTryouts).toHaveBeenCalledWith(
        expect.objectContaining({ driver_id: DRIVER_USER_ID }),
      );
    });
    expect(tryoutService.getTryouts).not.toHaveBeenCalledWith(
      expect.objectContaining({ driver_id: DELIVERY_PERSON_ID }),
    );
  });

  it('omits a driver whose user_id is missing rather than falling back to its id', async () => {
    // A serializer regression must fail loudly, not silently target the wrong
    // account — the exact failure mode that produced the production incident.
    adminService.getDeliveryPersonnel.mockResolvedValue({
      data: {
        items: [{ id: DELIVERY_PERSON_ID, full_name: DRIVER_NAME, phone: '+998900000001' }],
        total: 1,
      },
    });

    render(<Tryouts />, { wrapper: createWrapper() });

    await waitFor(() => expect(driverFilterSelector()).toBeTruthy());
    fireEvent.mouseDown(driverFilterSelector());

    // The dropdown opens, but the unusable driver must not be offered at all —
    // the old `|| driver.id` fallback rendered it and shipped the wrong id space.
    await waitFor(() => expect(document.querySelector('.ant-select-dropdown')).toBeTruthy());
    expect(driverOption()).toBeNull();

    expect(tryoutService.getTryouts).not.toHaveBeenCalledWith(
      expect.objectContaining({ driver_id: DELIVERY_PERSON_ID }),
    );
  });
});
