import React from 'react';
import { act, render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Modal, message } from 'antd';
import dayjs from 'dayjs';

import { RESCHEDULE_ERROR_MESSAGES } from '../../components/orders/RescheduleOrderModal';
import Delivery from '../../pages/Delivery';
import adminService from '../../services/adminService';

vi.mock('../../services/adminService', () => ({
  __esModule: true,
  default: {
    getDeliveries: vi.fn(),
    updateDelivery: vi.fn(),
    // RescheduleOrderModal's read and write, which the row's Reschedule opens.
    getOrderDetails: vi.fn(),
    rescheduleOrder: vi.fn(),
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

// A row as AdminDeliveryService.serialize_delivery publishes it. The last four keys
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
  reason_required_statuses: ['returned'],
  ...overrides,
});

const HELD = row({ id: 1, delivery_id: 'DLV-000001', status: 'rescheduled' });
// A failed row keeps its driver, but the backend refuses to hand it to another (F2): it
// leaves `failed` only through a reschedule or by cancelling its order, so it publishes
// can_assign: false.
const FAILED_LIVE = row({
  id: 2, delivery_id: 'DLV-000002', status: 'failed', driver_id: 7, driver_name: 'Rel Driver',
  can_redispatch: true, can_assign: false,
});
// Same status, but its order was cancelled, so the backend says no. The old
// `status === 'failed'` check offered Re-dispatch here.
const FAILED_DEAD = row({
  id: 3, delivery_id: 'DLV-000003', status: 'failed', driver_id: 7, driver_name: 'Rel Driver',
  can_redispatch: false, can_assign: false,
});
// Driverless PENDING: the backend drops `assigned`, which its write path refuses without
// a driver. The old hand-copied map offered it.
const PENDING = row({
  id: 4, delivery_id: 'DLV-000004', status: 'pending', notes: 'Gate code 42',
  allowed_status_transitions: ['returned'], can_assign: true,
});
const IN_TRANSIT = row({
  id: 5, delivery_id: 'DLV-000005', status: 'in_transit', driver_id: 7, driver_name: 'Rel Driver',
  notes: 'Ring twice', allowed_status_transitions: ['arrived', 'failed', 'returned'], can_assign: true,
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
// The return's reason is the Orders page's AdminReasonField, under this page's
// `ui.delivery.return_reason_*` copy (the same words as the Orders field's).
const RETURN_REASON_LABEL = 'Reason (internal, not shown to the customer)';
// The refusals of a return the page explains itself, named on every save so api.js does not
// toast them as well: one refusal, one message. The interceptor side is pinned in
// services/api.test.js.
const REASON_REFUSALS = { handledErrorCodes: ['ADMIN_REASON_REQUIRED', 'ADMIN_REASON_TOO_LONG'] };

// Lets every pending promise settle, so a check that nothing was sent proves it was not sent.
const flush = () => act(() => new Promise((resolve) => { setTimeout(resolve, 0); }));

// The row's Update status, then `label` in its dropdown, as the admin picks them.
const pickStatusOn = async (code, label) => {
  fireEvent.click(within(rowOf(code)).getByRole('button', { name: 'ui.delivery.update_status' }));
  fireEvent.mouseDown((await screen.findByTestId('delivery-update-status')).querySelector('.ant-select-selector'));
  await waitFor(() => expect(statusOption(label)).toBeTruthy());
  fireEvent.click(statusOption(label));
};

// Returned, picked as above. Resolves to the reason field that move brings up.
const pickReturnedOn = async (code) => {
  await pickStatusOn(code, 'Returned');
  return screen.findByLabelText(RETURN_REASON_LABEL);
};

// The return's confirmation, which antd portals outside the page, and its OK.
const confirmDialog = async () => {
  await waitFor(() => expect(document.querySelector('.ant-modal-confirm')).toBeTruthy());
  return document.querySelector('.ant-modal-confirm');
};
const confirmReturn = async () => {
  fireEvent.click(within(await confirmDialog()).getByRole('button', { name: 'Mark as returned' }));
};

beforeEach(() => {
  vi.clearAllMocks();
  adminService.getDeliveries.mockResolvedValue({
    data: { items: [HELD, FAILED_LIVE, FAILED_DEAD, PENDING, IN_TRANSIT] },
    meta: { total: 5, summary: {} },
  });
  adminService.updateDelivery.mockResolvedValue({ message: 'Delivery updated successfully' });
});

// Modal.confirm portals outside Testing Library's root. An undismissed one would leak into the
// next test.
afterEach(() => {
  Modal.destroyAll();
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

  it('offers Reschedule on can_redispatch, not on the failed status', async () => {
    await renderPage();

    expect(actionsOf('DLV-000002')).toContain('Reschedule');
    expect(actionsOf('DLV-000003')).not.toContain('Reschedule');
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
    // It still names its driver, but publishes can_assign: false, so there is no Reassign.
    expect(actionsOf('DLV-000002')).toEqual([
      'ui.delivery.track_delivery', 'ui.delivery.view_details', 'Edit notes', 'Reschedule',
    ]);
    expect(actionsOf('DLV-000003')).toEqual([
      'ui.delivery.track_delivery', 'ui.delivery.view_details', 'Edit notes',
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
    // A move to Returned closes the order, so it names an internal reason and is confirmed (F6).
    fireEvent.change(await screen.findByLabelText(RETURN_REASON_LABEL), {
      target: { value: 'Customer moved away' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'ui.delivery.update_delivery_button' }));
    await confirmReturn();

    await waitFor(() => expect(adminService.updateDelivery).toHaveBeenCalledWith(4, {
      status: 'returned',
      notes: 'Gate code 42',
      reason: 'Customer moved away',
    }, REASON_REFUSALS));
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
    }, REASON_REFUSALS));
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

  it('the detail modal of a failed row offers no Reassign, although the row names its driver', async () => {
    await renderPage();
    fireEvent.click(within(rowOf('DLV-000002')).getByRole('button', { name: 'ui.delivery.view_details' }));

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByRole('button', { name: /ui\.delivery\.track_delivery/ })).toBeInTheDocument();
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

describe('Delivery page — a move to Returned is confirmed and names its reason (F6)', () => {
  it('asks for the reason first, and a blank one sends nothing', async () => {
    await renderPage();
    fireEvent.change(await pickReturnedOn('DLV-000005'), { target: { value: '   ' } });
    fireEvent.click(screen.getByRole('button', { name: 'ui.delivery.update_delivery_button' }));

    expect(await screen.findByText('Reason is required')).toBeInTheDocument();
    expect(document.querySelector('.ant-modal-confirm')).toBeNull();
    expect(adminService.updateDelivery).not.toHaveBeenCalled();
  });

  it('names the order it will close, and a cancelled confirmation sends nothing', async () => {
    await renderPage();
    const reason = await pickReturnedOn('DLV-000005');
    fireEvent.change(reason, { target: { value: 'Refused at the door' } });
    fireEvent.click(screen.getByRole('button', { name: 'ui.delivery.update_delivery_button' }));

    const dialog = await confirmDialog();
    expect(within(dialog).getByText('Order ORD-100 will be closed as returned and its driver released.'))
      .toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: 'ui.delivery.cancel' }));
    await flush();

    expect(adminService.updateDelivery).not.toHaveBeenCalled();
    // The form stays open with what the admin typed.
    expect(reason).toHaveValue('Refused at the door');
  });

  it('once confirmed, sends the trimmed reason beside the status and the notes', async () => {
    await renderPage();
    fireEvent.change(await pickReturnedOn('DLV-000005'), { target: { value: '  Refused at the door  ' } });
    fireEvent.click(screen.getByRole('button', { name: 'ui.delivery.update_delivery_button' }));
    await confirmReturn();

    await waitFor(() => expect(adminService.updateDelivery).toHaveBeenCalledWith(5, {
      status: 'returned',
      notes: 'Ring twice',
      reason: 'Refused at the door',
    }, REASON_REFUSALS));
  });

  it('a reason typed for one row, then cancelled, never reaches the next row', async () => {
    await renderPage();
    fireEvent.change(await pickReturnedOn('DLV-000005'), { target: { value: 'Refused at the door' } });
    fireEvent.click(screen.getByRole('button', { name: 'ui.delivery.cancel' }));

    expect(await pickReturnedOn('DLV-000004')).toHaveValue('');
  });

  it('"Edit notes" on a row already returned asks for no reason and sends none (Review Focus 5)', async () => {
    const RETURNED = row({ id: 6, delivery_id: 'DLV-000006', status: 'returned', notes: 'Left with the guard' });
    adminService.getDeliveries.mockResolvedValue({
      data: { items: [HELD, RETURNED] },
      meta: { total: 2, summary: {} },
    });
    await renderPage();
    fireEvent.click(within(rowOf('DLV-000006')).getByRole('button', { name: 'Edit notes' }));
    fireEvent.change(await screen.findByPlaceholderText('ui.delivery.notes_placeholder'), {
      target: { value: 'Collected at the depot' },
    });

    expect(screen.queryByLabelText(RETURN_REASON_LABEL)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'ui.delivery.update_delivery_button' }));

    // The body every notes-only save sends: the unchanged status beside the notes. The backend
    // saves it as notes only, so there is nothing to confirm and no reason to give.
    await waitFor(() => expect(adminService.updateDelivery).toHaveBeenCalledWith(6, {
      status: 'returned',
      notes: 'Collected at the depot',
    }, REASON_REFUSALS));
    expect(document.querySelector('.ant-modal-confirm')).toBeNull();
  });

  it('asks for a reason on exactly the moves the row publishes, never on a status name', async () => {
    // A backend whose reason rule named Arrived and not Returned
    // (AdminDeliveryService's ADMIN_DELIVERY_REASON_REQUIRED_STATUSES): the page follows the row.
    adminService.getDeliveries.mockResolvedValue({
      data: { items: [HELD, { ...IN_TRANSIT, reason_required_statuses: ['arrived'] }] },
      meta: { total: 2, summary: {} },
    });
    await renderPage();

    await pickStatusOn('DLV-000005', 'Arrived');
    expect(await screen.findByLabelText(RETURN_REASON_LABEL)).toBeInTheDocument();

    await pickStatusOn('DLV-000005', 'Returned');
    await flush();
    expect(screen.queryByLabelText(RETURN_REASON_LABEL)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'ui.delivery.update_delivery_button' }));

    await waitFor(() => expect(adminService.updateDelivery).toHaveBeenCalledWith(5, {
      status: 'returned',
      notes: 'Ring twice',
    }, REASON_REFUSALS));
    expect(document.querySelector('.ant-modal-confirm')).toBeNull();
  });

  it.each([
    ['ADMIN_REASON_REQUIRED', 'Enter a reason for cancelling or returning this order.'],
    ['ADMIN_REASON_TOO_LONG', 'The reason can be at most 100 characters.'],
  ])('explains the refusal %s once, in the words the Orders page uses', async (code, copy) => {
    // A tab on an older bundle, or a reason the field let through that the backend would not take.
    adminService.updateDelivery.mockRejectedValue(Object.assign(new Error('Request failed with status code 400'), {
      response: {
        status: 400,
        data: { success: false, message: 'Validation failed', errors: ['server sentence'], data: { error_code: code } },
      },
    }));
    await renderPage();
    fireEvent.change(await pickReturnedOn('DLV-000005'), { target: { value: 'Refused at the door' } });
    fireEvent.click(screen.getByRole('button', { name: 'ui.delivery.update_delivery_button' }));
    await confirmReturn();

    await waitFor(() => expect(message.error).toHaveBeenCalled());
    // The request named the code, so api.js stays silent: this is the only message.
    expect(adminService.updateDelivery).toHaveBeenCalledWith(5, expect.any(Object), REASON_REFUSALS);
    expect(message.error.mock.calls).toEqual([[copy]]);
    expect(message.success).not.toHaveBeenCalled();
  });
});

// GET /admin/orders/<id> for FAILED_LIVE's order, as RescheduleOrderModal reads it. The delivery
// failed on its own day, so the modal opens on the date the order already has.
const TODAY = dayjs().format('YYYY-MM-DD');
const FAILED_LIVE_ORDER = {
  id: 100,
  order_number: 'ORD-100',
  status: 'out_for_delivery',
  delivery_date: TODAY,
  delivery_window: { start: null, end: null, kind: 'anytime', label: 'anytime' },
  delivery: { id: 2, status: 'failed', tracking_number: 'TRK202609230000AAAAAA' },
  awaiting_new_date: true,
  can_reschedule: true,
  reschedule_block_code: null,
  reschedule_min_date: TODAY,
  reschedule_max_date: dayjs().add(15, 'day').format('YYYY-MM-DD'),
  reschedule_notifies_customer: true,
  reschedule_customer_channel: 'telegram',
  reschedule_driver_losing_stop: null,
};

describe("Delivery page — Reschedule opens the Orders modal on the row's order (F13)", () => {
  it('re-reads the order, re-dates the failed delivery to the day it has, and refreshes the list', async () => {
    adminService.getOrderDetails.mockResolvedValue({ success: true, data: { order: FAILED_LIVE_ORDER } });
    adminService.rescheduleOrder.mockResolvedValue({
      success: true,
      data: { order: { ...FAILED_LIVE_ORDER, awaiting_new_date: false } },
    });
    await renderPage();
    fireEvent.click(within(rowOf('DLV-000002')).getByRole('button', { name: 'Reschedule' }));

    const modal = await screen.findByRole('dialog', { name: 'Reschedule delivery' });
    // The row's order (order_id 100), never the delivery's own id (2).
    expect(adminService.getOrderDetails).toHaveBeenCalledWith(100);
    const save = await within(modal).findByRole('button', { name: 'Save' });
    // F11: for a delivery awaiting a new date, the same day is a real re-date. It rests on the
    // modal's read, so the body asks the backend to check that read again under its lock.
    await waitFor(() => expect(save).toBeEnabled());
    fireEvent.click(save);

    await waitFor(() => expect(adminService.rescheduleOrder).toHaveBeenCalledWith(100, {
      delivery_date: TODAY,
      delivery_window_start: null,
      delivery_window_end: null,
      expect_awaiting_new_date: true,
    }, { handledErrorCodes: [...RESCHEDULE_ERROR_MESSAGES.keys()] }));
    // The modal invalidates ['deliveries'], so this page reads its rows again, and it closes.
    await waitFor(() => expect(adminService.getDeliveries).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Reschedule delivery' })).toBeNull());
  });
});
