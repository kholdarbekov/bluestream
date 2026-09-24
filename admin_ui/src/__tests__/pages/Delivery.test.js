import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Modal, message } from 'antd';

import Delivery from '../../pages/Delivery';
import adminService from '../../services/adminService';

vi.mock('../../services/adminService', () => ({
  __esModule: true,
  default: {
    getDeliveries: vi.fn(),
    updateDelivery: vi.fn(),
    redispatchDelivery: vi.fn(),
  },
}));

vi.mock('../../services/staffService', () => ({
  __esModule: true,
  default: {
    getDeliveryPersons: vi.fn(),
    assignDelivery: vi.fn(),
    reassignDelivery: vi.fn(),
  },
}));

// `t(key, 'English fallback', vars)` returns the fallback with its {{vars}} filled in,
// and the key itself when there is no fallback.
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key, fallback, vars) => (typeof fallback === 'string'
      ? fallback.replace(/{{\s*(\w+)\s*}}/g, (_, name) => String(vars?.[name]))
      : key),
  }),
}));

// antd's Dropdown renders its items only once opened. Render them as plain buttons
// so a row's actions can be read straight off the row (the Users.dob / Orders.golden
// convention). `message` is spied so a test can count the page's own toasts.
vi.mock('antd', async () => {
  const actual = await vi.importActual('antd');
  return {
    ...actual,
    message: {
      success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn(), loading: vi.fn(), destroy: vi.fn(), open: vi.fn(),
    },
    Dropdown: ({ menu, children }) => (
      <div>
        {children}
        {menu?.items?.map((item) => (
          <button key={item.key} type="button" onClick={item.onClick}>{item.label}</button>
        ))}
      </div>
    ),
  };
});

// A row as AdminDeliveryService.serialize_delivery publishes it. The last three keys
// are the backend's answers the page must follow rather than re-derive.
const row = (overrides) => ({
  delivery_id: 'DLV-000000',
  tracking_number: 'TRK202609230000AAAAAA',
  order_id: 100,
  order_number: 'ORD-100',
  status: 'scheduled',
  priority: 'low',
  customer_name: 'Ann Lee',
  customer_phone: '+998901112233',
  driver_id: null,
  driver_name: null,
  driver_phone: null,
  delivery_address: 'Chilonzor 1',
  scheduled_date: '2026-09-24T00:00:00+00:00',
  scheduled_time_slot: 'anytime',
  estimated_delivery_time: null,
  notes: null,
  failed_delivery_reason: null,
  status_history: [],
  allowed_status_transitions: [],
  can_redispatch: false,
  can_assign: false,
  ...overrides,
});

const HELD = row({ id: 1, delivery_id: 'DLV-000001', status: 'rescheduled' });
const FAILED_LIVE = row({
  id: 2, delivery_id: 'DLV-000002', status: 'failed', driver_id: 7, driver_name: 'Rel Driver',
  can_redispatch: true, can_assign: true,
});
// Same status, but its order was cancelled, so the backend says no. The old
// `status === 'failed'` check offered Re-dispatch here.
const FAILED_DEAD = row({
  id: 3, delivery_id: 'DLV-000003', status: 'failed', driver_id: 7, driver_name: 'Rel Driver',
  can_redispatch: false, can_assign: true,
});
// Driverless PENDING: the backend drops `assigned`, which its write path refuses without
// a driver. The old hand-copied map offered it.
const PENDING = row({
  id: 4, delivery_id: 'DLV-000004', status: 'pending', notes: 'Gate code 42',
  allowed_status_transitions: ['returned'], can_assign: true,
});
const IN_TRANSIT = row({
  id: 5, delivery_id: 'DLV-000005', status: 'in_transit', driver_id: 7, driver_name: 'Rel Driver',
  allowed_status_transitions: ['arrived', 'failed', 'returned'], can_assign: true,
});

const newQueryClient = () => new QueryClient({ defaultOptions: { queries: { retry: false } } });

const createWrapper = (queryClient = newQueryClient()) =>
  ({ children }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;

const renderPage = async (queryClient) => {
  render(<Delivery />, { wrapper: createWrapper(queryClient) });
  await screen.findByText('DLV-000001');
};

const rowOf = (code) => screen.getByText(code).closest('tr');
const actionsOf = (code) => within(rowOf(code))
  .queryAllByRole('button')
  .map((button) => button.textContent)
  .filter(Boolean);
const statusOption = (label) => document.querySelector(
  `.ant-select-dropdown .ant-select-item-option[title="${label}"]`,
);

beforeEach(() => {
  vi.clearAllMocks();
  adminService.getDeliveries.mockResolvedValue({
    data: { items: [HELD, FAILED_LIVE, FAILED_DEAD, PENDING, IN_TRANSIT] },
    meta: { total: 5, summary: {} },
  });
  adminService.updateDelivery.mockResolvedValue({ message: 'Delivery updated successfully' });
});

describe('Delivery page — actions follow what the backend publishes', () => {
  it('a held delivery offers Track, Details and a notes edit, nothing else', async () => {
    await renderPage();

    expect(actionsOf('DLV-000001')).toEqual(['ui.delivery.track_delivery', 'ui.delivery.view_details', 'Edit notes']);
  });

  it('labels a held delivery "Rescheduled" in its own colour', async () => {
    await renderPage();

    const tag = within(rowOf('DLV-000001')).getByText('Rescheduled').closest('.ant-tag');
    expect(tag).toHaveClass('ant-tag-lime');
  });

  it('offers Re-dispatch on can_redispatch, not on the failed status', async () => {
    await renderPage();

    expect(actionsOf('DLV-000002')).toContain('ui.delivery.redispatch_delivery');
    expect(actionsOf('DLV-000003')).not.toContain('ui.delivery.redispatch_delivery');
  });

  it('offers Update status only with published moves, and Assign/Reassign only on can_assign', async () => {
    await renderPage();

    expect(actionsOf('DLV-000004')).toEqual([
      'ui.delivery.track_delivery', 'ui.delivery.view_details', 'ui.delivery.update_status', 'ui.delivery.assign_driver',
    ]);
    expect(actionsOf('DLV-000005')).toEqual([
      'ui.delivery.track_delivery', 'ui.delivery.view_details', 'ui.delivery.update_status', 'Reassign driver',
    ]);
    // FAILED publishes no moves, so its update action is a notes edit, not Update status.
    expect(actionsOf('DLV-000002')).toEqual([
      'ui.delivery.track_delivery', 'ui.delivery.view_details', 'Edit notes', 'Reassign driver',
      'ui.delivery.redispatch_delivery',
    ]);
  });

  it('the status dropdown shows the current status disabled plus the published moves only', async () => {
    await renderPage();
    fireEvent.click(within(rowOf('DLV-000004')).getByRole('button', { name: 'ui.delivery.update_status' }));
    fireEvent.mouseDown((await screen.findByTestId('delivery-update-status')).querySelector('.ant-select-selector'));

    await waitFor(() => expect(statusOption('Returned')).toBeTruthy());
    expect(statusOption('Pending')).toHaveClass('ant-select-item-option-disabled');
    expect(statusOption('Returned')).not.toHaveClass('ant-select-item-option-disabled');
    expect(statusOption('Assigned')).toBeNull();
    expect(document.querySelectorAll('.ant-select-dropdown .ant-select-item-option')).toHaveLength(2);

    fireEvent.click(statusOption('Returned'));
    fireEvent.click(screen.getByRole('button', { name: 'ui.delivery.update_delivery_button' }));

    await waitFor(() => expect(adminService.updateDelivery).toHaveBeenCalledWith(4, {
      status: 'returned',
      notes: 'Gate code 42',
    }));
  });

  it('a row with no status move still edits its notes, and saves them with its status unchanged', async () => {
    await renderPage();
    fireEvent.click(within(rowOf('DLV-000002')).getByRole('button', { name: 'Edit notes' }));
    fireEvent.mouseDown((await screen.findByTestId('delivery-update-status')).querySelector('.ant-select-selector'));

    // The current status is the only option, and it cannot be picked as a move.
    await waitFor(() => expect(statusOption('Failed')).toBeTruthy());
    expect(statusOption('Failed')).toHaveClass('ant-select-item-option-disabled');
    expect(document.querySelectorAll('.ant-select-dropdown .ant-select-item-option')).toHaveLength(1);
    // A failure reason is asked for only when a failure is being recorded, so a row that
    // failed with none on record can still save its notes.
    expect(screen.queryByText('Failure reason')).toBeNull();

    fireEvent.change(screen.getByPlaceholderText('ui.delivery.notes_placeholder'), {
      target: { value: 'Customer asked to call first' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'ui.delivery.update_delivery_button' }));

    // The same body the page has always sent for a notes-only save: the unchanged status
    // beside the notes, which the backend saves as notes only.
    await waitFor(() => expect(adminService.updateDelivery).toHaveBeenCalledWith(2, {
      status: 'failed',
      notes: 'Customer asked to call first',
    }));
  });

  it('the detail modal offers a held delivery its notes edit but no status move and no Assign', async () => {
    await renderPage();
    fireEvent.click(within(rowOf('DLV-000001')).getByRole('button', { name: 'ui.delivery.view_details' }));

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByRole('button', { name: /ui\.delivery\.track_delivery/ })).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: /Edit notes/ })).toBeInTheDocument();
    expect(within(dialog).queryByRole('button', { name: /ui\.delivery\.update_status/ })).toBeNull();
    expect(within(dialog).queryByRole('button', { name: /ui\.delivery\.assign_driver|Reassign driver/ })).toBeNull();
  });

  it('the detail modal of a row with moves still says Update status', async () => {
    await renderPage();
    fireEvent.click(within(rowOf('DLV-000005')).getByRole('button', { name: 'ui.delivery.view_details' }));

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByRole('button', { name: /ui\.delivery\.update_status/ })).toBeInTheDocument();
    expect(within(dialog).queryByRole('button', { name: /Edit notes/ })).toBeNull();
  });

  it('offers Rescheduled in the status filter and sends it as ?status=rescheduled', async () => {
    await renderPage();
    fireEvent.mouseDown(screen.getByTestId('delivery-status-filter').querySelector('.ant-select-selector'));
    fireEvent.click(await screen.findByTitle('Rescheduled'));

    await waitFor(() => expect(adminService.getDeliveries).toHaveBeenLastCalledWith(
      expect.objectContaining({ status: 'rescheduled', page: 1 }),
    ));
  });
});

// An axios rejection shaped like the admin re-dispatch route's refusal. validation_error_response
// puts the service's English sentence in `errors[0]`, which api.js toasts unless the request said
// it handles the code, and the code in `data.error_code`.
const refusal = (status, errorCode) => Object.assign(new Error(`Request failed with status code ${status}`), {
  response: {
    status,
    data: {
      success: false,
      message: 'Validation failed',
      errors: ['Order ORD-100 is cancelled; its delivery can no longer be re-dispatched.'],
      ...(errorCode ? { data: { error_code: errorCode } } : {}),
    },
  },
});

// The body of POST /admin/deliveries/<id>/redispatch as Task 12 answers it: `release_at` sits in
// `data` beside `delivery`, and it is null when the re-dispatch landed `scheduled`.
const redispatched = (releaseAt) => ({
  success: true,
  message: 'Delivery re-dispatched to pool',
  data: {
    delivery: {
      ...FAILED_LIVE,
      status: releaseAt ? 'rescheduled' : 'scheduled',
      driver_id: null,
      driver_name: null,
      can_redispatch: false,
    },
    release_at: releaseAt,
  },
});

// The row's Re-dispatch, then the confirmation's OK, as the admin clicks them.
const redispatchRow = async (code, queryClient) => {
  await renderPage(queryClient);
  fireEvent.click(within(rowOf(code)).getByRole('button', { name: 'ui.delivery.redispatch_delivery' }));
  await waitFor(() => expect(document.querySelector('.ant-modal-confirm')).toBeTruthy());
  fireEvent.click(
    within(document.querySelector('.ant-modal-confirm'))
      .getByRole('button', { name: 'ui.delivery.redispatch_delivery' }),
  );
  await waitFor(() => expect(adminService.redispatchDelivery).toHaveBeenCalledTimes(1));
};

describe('Delivery page — a re-dispatch tells the admin what happened, once', () => {
  // Modal.confirm portals outside Testing Library's root. An undismissed one would leak a second
  // confirmation into the next test.
  afterEach(() => {
    Modal.destroyAll();
  });

  it('a re-dispatch that went straight to the pool says so through its own key, not the backend prose', async () => {
    // The backend's `message` is English whatever the admin's language. A marker stands in
    // for it here, because the key's English fallback is the same sentence.
    adminService.redispatchDelivery.mockResolvedValue({ ...redispatched(null), message: 'backend prose' });
    await redispatchRow('DLV-000002');

    await waitFor(() => expect(message.success).toHaveBeenCalledTimes(1));
    expect(message.success).toHaveBeenCalledWith('Delivery re-dispatched to pool');
    // The page names the refusals it explains itself, so api.js does not toast them as well.
    expect(adminService.redispatchDelivery).toHaveBeenCalledWith(2, {
      handledErrorCodes: ['ORDER_NOT_RESCHEDULABLE', 'STAFF_DELIVERY_NOT_REDISPATCHABLE'],
    });
  });

  it('a re-dispatch held until the shift opens says when drivers will see it, in local time (R25)', async () => {
    // 03:00 UTC is 08:00 in Tashkent (UTC+5), the day's first shift start.
    adminService.redispatchDelivery.mockResolvedValue(redispatched('2026-09-24T03:00:00+00:00'));
    await redispatchRow('DLV-000002');

    await waitFor(() => expect(message.success).toHaveBeenCalledTimes(1));
    expect(message.success).toHaveBeenCalledWith(
      "Re-dispatched. Drivers will see it when today's shift opens at 08:00.",
    );
  });

  it.each([
    [
      'ORDER_NOT_RESCHEDULABLE',
      'This order is delivered, cancelled or returned, so its delivery can no longer be re-dispatched.',
    ],
    [
      'STAFF_DELIVERY_NOT_REDISPATCHABLE',
      'This delivery is no longer failed, so there is nothing to re-dispatch. Refresh the list.',
    ],
  ])("a %s refusal is explained once, in the admin's language", async (code, text) => {
    adminService.redispatchDelivery.mockRejectedValue(refusal(400, code));
    await redispatchRow('DLV-000002');

    await waitFor(() => expect(message.error).toHaveBeenCalledTimes(1));
    expect(message.error).toHaveBeenCalledWith(text);
    expect(message.success).not.toHaveBeenCalled();
  });

  it.each([
    ['a refusal the page does not explain', 400, 'DELIVERY_NOT_RESCHEDULABLE'],
    ['a failure with no code', 500, undefined],
  ])('%s gets no second toast from the page', async (_label, status, code) => {
    const queryClient = newQueryClient();
    adminService.redispatchDelivery.mockRejectedValue(refusal(status, code));
    await redispatchRow('DLV-000002', queryClient);

    // TanStack Query runs onError before it records the error, so once the mutation reads
    // 'error' the page has had its chance to toast. api.js has already toasted this response.
    await waitFor(() => expect(queryClient.getMutationCache().getAll()[0]?.state.status).toBe('error'));
    expect(message.error).not.toHaveBeenCalled();
    expect(message.success).not.toHaveBeenCalled();
  });
});
