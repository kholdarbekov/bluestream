import fs from 'node:fs';
import path from 'node:path';
import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { message } from 'antd';

import Subscriptions from '../../pages/Subscriptions';
import adminService from '../../services/adminService';

vi.mock('../../services/adminService', () => ({
  __esModule: true,
  default: {
    getSubscriptions: vi.fn(),
    getSubscription: vi.fn(),
    createSubscription: vi.fn(),
    updateSubscription: vi.fn(),
    pauseSubscription: vi.fn(),
    resumeSubscription: vi.fn(),
    cancelSubscription: vi.fn(),
    processSubscriptionBilling: vi.fn(),
    addSubscriptionItem: vi.fn(),
    updateSubscriptionItem: vi.fn(),
    removeSubscriptionItem: vi.fn(),
    getUsers: vi.fn(),
    getUserAddresses: vi.fn(),
    getProducts: vi.fn(),
    getTimeSlots: vi.fn(),
    getCustomerSubscriptionPaymentMethods: vi.fn(),
    getSubscriptionTimeSlots: vi.fn(),
    getDeliveryWeekdays: vi.fn(),
  },
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, opts) => (opts && opts.defaultValue) || key }),
}));

// The backend's published answer (admin actor); the page reads it instead of deriving from status.
const ALL_ALLOWED = Object.fromEntries(
  ['pause', 'resume', 'cancel', 'edit', 'remove_item', 'create_next_order'].map((a) => [a, { allowed: true, block_reason: null }]),
);

const createWrapper = () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
};

// Shared drivers for the form tests below: open the create or edit form, pick an option of a named Select.
// Matched by attribute value, not a `[title="…"]` selector: jsdom's selector engine misreads a `+` (a phone number).
const optionIn = async (title) => waitFor(() => {
  const el = [...document.querySelectorAll('.ant-select-item-option')].filter((o) => o.getAttribute('title') === title).at(-1);
  expect(el).toBeTruthy();
  return el;
});
const formItem = (dialog, label) => within(dialog).getByText(label, { selector: 'label' }).closest('.ant-form-item');
const pick = async (dialog, label, title) => {
  fireEvent.mouseDown(formItem(dialog, label).querySelector('.ant-select-selector'));
  fireEvent.click(await optionIn(title));
};
const selected = (dialog, label) => formItem(dialog, label).querySelector('.ant-select-selection-item')?.getAttribute('title') ?? null;
const openCreateForm = async () => {
  render(<Subscriptions />, { wrapper: createWrapper() });
  await screen.findByText('SUB-1');
  fireEvent.click(screen.getByRole('button', { name: /create subscription/i }));
  return screen.findByRole('dialog');
};
const openEditForm = async (detail) => {
  adminService.getSubscription.mockResolvedValue(detail);
  render(<Subscriptions />, { wrapper: createWrapper() });
  await screen.findByText('SUB-1');
  fireEvent.click(document.querySelector('.anticon-edit').closest('button'));
  const dialog = await screen.findByRole('dialog');
  await within(dialog).findByDisplayValue(detail.name);
  return dialog;
};
const editOf = (fields) => ({
  id: 1, subscription_number: 'SUB-1', user: { id: 7, name: 'Test User' }, name: 'Existing Sub',
  frequency: 'weekly', payment_method: 'cash', delivery_address_id: 3, auto_renew: true,
  discount_percentage: 0, status: 'active', allowed_actions: ALL_ALLOWED, items: [], ...fields,
});
const USERS = [
  { id: 7, first_name: 'Ann', last_name: 'Lee', phone: '+998901112233' },
  { id: 8, first_name: 'Bob', last_name: 'Kim', phone: '+998904445566' },
];

describe('Subscriptions page', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    adminService.getSubscriptions.mockResolvedValue({
      items: [{
        id: 1, subscription_number: 'SUB-1', user_name: 'Test User', user_email: 't@e.com',
        status: 'active', allowed_actions: ALL_ALLOWED, frequency: 'monthly', billing_amount: 30000,
        next_delivery_date: '2026-08-01',
        upcoming_delivery: { date: '2026-08-01', window_start: '09:00', window_end: '12:00', order_id: null, time_label: '09:00–12:00' },
        items_count: 2,
      }],
      total: 1,
    });
    adminService.getUsers.mockResolvedValue({ items: [], total: 0 });
    adminService.getProducts.mockResolvedValue({ data: { items: [] } });
    adminService.getTimeSlots.mockResolvedValue({ data: { items: [] } });
    adminService.getUserAddresses.mockResolvedValue({ data: { addresses: [{ id: 3, full_address: 'Amir Temur 1' }] } });
    adminService.getSubscription.mockResolvedValue({
      id: 1, subscription_number: 'SUB-1', user: { id: 7, name: 'Test User' },
      name: 'Existing Sub', description: 'd', frequency: 'weekly', delivery_day_of_week: 3,
      payment_method: 'cash', delivery_address_id: 3, auto_payment: true, auto_renew: true,
      discount_percentage: 0, status: 'active', allowed_actions: ALL_ALLOWED, items: [],
    });
    adminService.updateSubscription.mockResolvedValue({ id: 1 });
    adminService.getCustomerSubscriptionPaymentMethods.mockResolvedValue([
      { method: 'cash', display_name: 'Cash on Delivery', is_active: true },
      { method: 'click', display_name: 'Click', is_active: true },
    ]);
    adminService.getSubscriptionTimeSlots.mockResolvedValue([]);
    adminService.getDeliveryWeekdays.mockResolvedValue([0, 1, 2, 3, 4, 5, 6]);
  });

  it('renders a subscription row from the list endpoint', async () => {
    render(<Subscriptions />, { wrapper: createWrapper() });
    expect(await screen.findByText('SUB-1')).toBeInTheDocument();
    expect(screen.getByText('Test User')).toBeInTheDocument();
  });

  it('opens the create modal with a Name field', async () => {
    render(<Subscriptions />, { wrapper: createWrapper() });
    await screen.findByText('SUB-1');
    fireEvent.click(screen.getByRole('button', { name: /create subscription/i }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Name')).toBeInTheDocument();
  });

  it('prefills the edit modal and submits an update payload', async () => {
    render(<Subscriptions />, { wrapper: createWrapper() });
    await screen.findByText('SUB-1');

    // Click the row's edit (pencil) action.
    const editIcon = document.querySelector('.anticon-edit');
    fireEvent.click(editIcon.closest('button'));

    // Prefilled name appears.
    expect(await screen.findByDisplayValue('Existing Sub')).toBeInTheDocument();

    // Submit the update.
    const dialog = screen.getByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: /^update$/i }));

    await waitFor(() => {
      expect(adminService.updateSubscription).toHaveBeenCalledWith(
        1,
        expect.objectContaining({ name: 'Existing Sub' }),
      );
    });
  });

  it('saves an edited item quantity from the drawer with the typed value', async () => {
    adminService.getSubscription.mockResolvedValue({
      id: 1, subscription_number: 'SUB-1', user: { id: 7, name: 'Test User' },
      name: 'Existing Sub', frequency: 'weekly',
      payment_method: 'cash', delivery_address_id: 3, auto_payment: true, auto_renew: true,
      discount_percentage: 0, status: 'active', allowed_actions: ALL_ALLOWED,
      items: [{ id: 55, product_id: 2, product_name: 'Water', quantity: 2, unit_price: 15000 }],
    });
    adminService.updateSubscriptionItem.mockResolvedValue({ data: {} });
    render(<Subscriptions />, { wrapper: createWrapper() });
    await screen.findByText('SUB-1');
    fireEvent.click(document.querySelector('.anticon-eye').closest('button'));
    expect(await screen.findByText('Water')).toBeInTheDocument();
    const qtyInput = document.querySelectorAll('.ant-drawer .ant-input-number-input')[0];
    fireEvent.change(qtyInput, { target: { value: '7' } });
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));
    await waitFor(() => {
      expect(adminService.updateSubscriptionItem).toHaveBeenCalledWith(1, 55, { quantity: 7 });
    });
  });

  // Final review I1: the form offers the CUSTOMER's subscription menu at the chosen address (the list the
  // backend's B8 guard checks), never the signed-in admin's own /payments/methods.
  describe('the payment menu is the customer\'s, at the chosen address', () => {
    const openEditOf = async (detail) => {
      adminService.getSubscription.mockResolvedValue(detail);
      render(<Subscriptions />, { wrapper: createWrapper() });
      await screen.findByText('SUB-1');
      fireEvent.click(document.querySelector('.anticon-edit').closest('button'));
      const dialog = await screen.findByRole('dialog');
      await within(dialog).findByDisplayValue('Existing Sub');
      return dialog;
    };
    const editDetail = {
      id: 1, subscription_number: 'SUB-1', user: { id: 7, name: 'Test User' }, name: 'Existing Sub',
      frequency: 'weekly', delivery_day_of_week: 3, payment_method: 'business_account', delivery_address_id: 3,
      auto_renew: true, discount_percentage: 0, status: 'active', allowed_actions: ALL_ALLOWED, items: [],
    };
    const openDropdown = async (dialog, label) => {
      const formItem = within(dialog).getByText(label).closest('.ant-form-item');
      fireEvent.mouseDown(formItem.querySelector('.ant-select-selector'));
      return waitFor(() => {
        const open = [...document.querySelectorAll('.ant-select-dropdown')].at(-1);
        expect(open).toBeTruthy();
        return open;
      });
    };

    it('asks for the customer and the address the form names, and lists exactly what it answers', async () => {
      adminService.getCustomerSubscriptionPaymentMethods.mockResolvedValue([
        { method: 'click', display_name: 'Card', is_active: true },
        { method: 'business_account', display_name: 'Business Account', is_active: true },
      ]);
      const dialog = await openEditOf(editDetail);

      await waitFor(() => expect(adminService.getCustomerSubscriptionPaymentMethods).toHaveBeenCalledWith(7, 3));
      const dropdown = await openDropdown(dialog, 'Payment method');
      expect([...dropdown.querySelectorAll('.ant-select-item-option')].map((o) => o.getAttribute('title')))
        .toEqual(['Card', 'Business Account']);
    });

    it('asks again when the address changes', async () => {
      adminService.getUserAddresses.mockResolvedValue({
        data: { addresses: [{ id: 3, full_address: 'Amir Temur 1' }, { id: 4, full_address: 'Office 9' }] },
      });
      const dialog = await openEditOf(editDetail);
      await waitFor(() => expect(adminService.getCustomerSubscriptionPaymentMethods).toHaveBeenCalledWith(7, 3));

      const dropdown = await openDropdown(dialog, 'Delivery address');
      fireEvent.click(await within(dropdown).findByText('Office 9'));

      await waitFor(() => expect(adminService.getCustomerSubscriptionPaymentMethods).toHaveBeenLastCalledWith(7, 4));
    });

    it('asks nothing before a customer is chosen on create', async () => {
      render(<Subscriptions />, { wrapper: createWrapper() });
      await screen.findByText('SUB-1');
      fireEvent.click(screen.getByRole('button', { name: /create subscription/i }));
      await screen.findByRole('dialog');
      await waitFor(() => expect(adminService.getProducts).toHaveBeenCalled());
      expect(adminService.getCustomerSubscriptionPaymentMethods).not.toHaveBeenCalled();
    });
  });

  // Final review I2: the slot Select lists only the slots the backend offers this weekday and frequency
  // (`/delivery/time-slots?weekday=&frequency=`, the rule create and update now enforce).
  describe('the slot list is the backend\'s for the day and frequency', () => {
    const slotDetail = {
      id: 1, subscription_number: 'SUB-1', user: { id: 7, name: 'Test User' }, name: 'Existing Sub',
      frequency: 'weekly', delivery_day_of_week: 3, payment_method: 'cash', delivery_address_id: 3,
      auto_renew: true, discount_percentage: 0, status: 'active', allowed_actions: ALL_ALLOWED, items: [],
    };

    // The open list of THIS Select (antd keeps every dropdown it has opened in the document).
    const optionsOf = (formItem) => {
      const listbox = formItem.querySelector('[role="combobox"]').getAttribute('aria-controls');
      const dropdown = document.getElementById(listbox)?.closest('.ant-select-dropdown');
      return dropdown ? [...dropdown.querySelectorAll('.ant-select-item-option')].map((o) => o.getAttribute('title')) : [];
    };

    it('asks with the weekday for weekly, with any for monthly, and lists only what it answers', async () => {
      adminService.getSubscriptionTimeSlots.mockImplementation(async ({ frequency }) => (
        frequency === 'monthly'
          ? [{ id: 1, name: 'Every day 10-12' }]
          : [{ id: 1, name: 'Every day 10-12' }, { id: 2, name: 'Weekdays 9-18' }]
      ));
      adminService.getSubscription.mockResolvedValue(slotDetail);
      render(<Subscriptions />, { wrapper: createWrapper() });
      await screen.findByText('SUB-1');
      fireEvent.click(document.querySelector('.anticon-edit').closest('button'));
      const dialog = await screen.findByRole('dialog');
      await within(dialog).findByDisplayValue('Existing Sub');

      await waitFor(() => expect(adminService.getSubscriptionTimeSlots)
        .toHaveBeenCalledWith({ weekday: 3, frequency: 'weekly' }));
      expect(adminService.getTimeSlots).not.toHaveBeenCalled();
      const slotItem = within(dialog).getByText('Time slot').closest('.ant-form-item');
      fireEvent.mouseDown(slotItem.querySelector('.ant-select-selector'));
      await waitFor(() => expect(optionsOf(slotItem)).toEqual(['Any time', 'Every day 10-12', 'Weekdays 9-18']));

      const frequencyItem = within(dialog).getByText('Frequency').closest('.ant-form-item');
      fireEvent.mouseDown(frequencyItem.querySelector('.ant-select-selector'));
      fireEvent.click(await waitFor(() => {
        const el = [...document.querySelectorAll('.ant-select-item-option[title="monthly"]')].at(-1);
        expect(el).toBeTruthy();
        return el;
      }));

      await waitFor(() => expect(adminService.getSubscriptionTimeSlots)
        .toHaveBeenLastCalledWith({ weekday: 'any', frequency: 'monthly' }));
      fireEvent.mouseDown(slotItem.querySelector('.ant-select-selector'));
      await waitFor(() => expect(optionsOf(slotItem)).toEqual(['Any time', 'Every day 10-12']));
    });
  });

  describe('per-product minimum order quantity', () => {
    // A subscription line below the minimum is one billing's create_order refuses
    // every cycle (prod subscription 8: 1 x a product whose minimum is 2).
    const detailWithItem = (quantity) => ({
      id: 1, subscription_number: 'SUB-1', user: { id: 7, name: 'Test User' },
      name: 'Existing Sub', frequency: 'weekly',
      payment_method: 'click', delivery_address_id: 3, auto_renew: true,
      discount_percentage: 0, status: 'active', allowed_actions: ALL_ALLOWED,
      items: [{ id: 55, product_id: 2, product_name: 'Aqua 18.9 l', quantity, unit_price: 18000 }],
    });

    const pickOption = async (selectRoot, label) => {
      fireEvent.mouseDown(selectRoot.querySelector('.ant-select-selector'));
      const option = await waitFor(() => {
        const el = document.querySelector(`.ant-select-item-option[title="${label}"]`);
        expect(el).toBeTruthy();
        return el;
      });
      fireEvent.click(option);
    };

    const openDrawer = async () => {
      render(<Subscriptions />, { wrapper: createWrapper() });
      await screen.findByText('SUB-1');
      fireEvent.click(document.querySelector('.anticon-eye').closest('button'));
      expect(await screen.findByText('Aqua 18.9 l')).toBeInTheDocument();
      await waitFor(() => expect(adminService.getProducts).toHaveBeenCalled());
    };

    beforeEach(() => {
      adminService.getProducts.mockResolvedValue({
        data: {
          items: [
            { id: 2, name: 'Aqua 18.9 l', min_order_quantity: 2 },
            { id: 3, name: 'Aqua 10 l', min_order_quantity: 3 },
          ],
        },
      });
      adminService.updateSubscriptionItem.mockResolvedValue({ data: {} });
      adminService.addSubscriptionItem.mockResolvedValue({ data: {} });
    });

    it('saves a drawer quantity typed below the minimum as the minimum', async () => {
      adminService.getSubscription.mockResolvedValue(detailWithItem(4));
      await openDrawer();

      const qtyInput = document.querySelectorAll('.ant-drawer .ant-input-number-input')[0];
      fireEvent.change(qtyInput, { target: { value: '1' } });
      fireEvent.blur(qtyInput);
      fireEvent.click(screen.getByRole('button', { name: /^save$/i }));

      await waitFor(() => {
        expect(adminService.updateSubscriptionItem).toHaveBeenCalledWith(1, 55, { quantity: 2 });
      });
    });

    it('adds a drawer item at the chosen product minimum', async () => {
      adminService.getSubscription.mockResolvedValue(detailWithItem(2));
      await openDrawer();

      await pickOption(document.querySelector('.ant-drawer .ant-select'), 'Aqua 10 l');
      fireEvent.click(screen.getByRole('button', { name: /add item/i }));

      await waitFor(() => {
        expect(adminService.addSubscriptionItem).toHaveBeenCalledWith(1, { product_id: 3, quantity: 3 });
      });
    });

    it('raises a create-form item to the chosen product minimum', async () => {
      render(<Subscriptions />, { wrapper: createWrapper() });
      await screen.findByText('SUB-1');
      fireEvent.click(screen.getByRole('button', { name: /create subscription/i }));
      const dialog = await screen.findByRole('dialog');
      await waitFor(() => expect(adminService.getProducts).toHaveBeenCalled());

      const productSelect = [...dialog.querySelectorAll('.ant-select-selection-placeholder')]
        .find((el) => el.textContent === 'Product')
        .closest('.ant-select');
      await pickOption(productSelect, 'Aqua 10 l');

      await waitFor(() => {
        expect(within(dialog).getByPlaceholderText('Qty').value).toBe('3');
      });
    });
  });

  it('shows payment_method in the detail drawer', async () => {
    adminService.getSubscription.mockResolvedValue({
      id: 1, subscription_number: 'SUB-1', user: { id: 7, name: 'Test User' },
      name: 'Existing Sub', description: 'd', frequency: 'weekly',
      payment_method: 'click', delivery_address_id: 3, auto_renew: true,
      discount_percentage: 0, status: 'active', allowed_actions: ALL_ALLOWED, items: [],
    });
    render(<Subscriptions />, { wrapper: createWrapper() });
    await screen.findByText('SUB-1');
    fireEvent.click(document.querySelector('.anticon-eye').closest('button'));
    expect(await screen.findByText('click')).toBeInTheDocument();
  });

  it('shows the next delivery date and window in the list and the drawer, not a billing date', async () => {
    adminService.getSubscription.mockResolvedValue({
      id: 1, subscription_number: 'SUB-1', user: { id: 7, name: 'Test User' }, name: 'Existing Sub',
      frequency: 'weekly', payment_method: 'cash', delivery_address_id: 3, auto_renew: true,
      discount_percentage: 0, status: 'active', allowed_actions: ALL_ALLOWED, items: [],
      next_delivery_date: '2026-10-29',
      upcoming_delivery: { date: '2026-10-29', window_start: null, window_end: null, order_id: null, time_label: 'Time to be confirmed' },
    });
    render(<Subscriptions />, { wrapper: createWrapper() });
    expect(await screen.findByText('Aug 01, 2026, 09:00–12:00')).toBeInTheDocument();
    expect(screen.queryByText(/billing/i)).not.toBeInTheDocument();
    fireEvent.click(document.querySelector('.anticon-eye').closest('button'));
    expect(await screen.findByText('Oct 29, 2026, Time to be confirmed')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /create next order now/i })).toBeInTheDocument();
    expect(screen.queryByText(/process billing/i)).not.toBeInTheDocument();
  });

  it("the list and the drawer show the backend's time label", async () => {
    adminService.getSubscriptions.mockResolvedValue({
      items: [{
        id: 1, subscription_number: 'SUB-1', user_name: 'Test User', status: 'active', allowed_actions: ALL_ALLOWED,
        frequency: 'daily', billing_amount: 1, next_delivery_date: '2026-08-01', items_count: 1,
        upcoming_delivery: { date: '2026-08-01', window_start: null, window_end: null, order_id: null, time_label: 'Any time' },
      }],
      total: 1,
    });
    adminService.getSubscription.mockResolvedValue(editOf({
      next_delivery_date: '2026-08-01',
      upcoming_delivery: { date: '2026-08-01', window_start: null, window_end: null, order_id: null, time_label: 'Any time' },
    }));
    render(<Subscriptions />, { wrapper: createWrapper() });
    expect(await screen.findByText('Aug 01, 2026, Any time')).toBeInTheDocument();
    fireEvent.click(document.querySelector('.anticon-eye').closest('button'));
    await waitFor(() => expect(screen.getAllByText('Aug 01, 2026, Any time')).toHaveLength(2));
  });

  it('a paused subscription with no upcoming delivery shows its own time label after the date', async () => {
    adminService.getSubscription.mockResolvedValue(editOf({
      status: 'paused', next_delivery_date: '2026-08-05', upcoming_delivery: null, time_label: 'Any time',
    }));
    render(<Subscriptions />, { wrapper: createWrapper() });
    await screen.findByText('SUB-1');
    fireEvent.click(document.querySelector('.anticon-eye').closest('button'));
    expect(await screen.findByText('Aug 05, 2026, Any time')).toBeInTheDocument();
  });

  it('the page holds no slot rule of its own', () => {
    const source = fs.readFileSync(path.resolve(process.cwd(), 'src/pages/Subscriptions.js'), 'utf8');
    expect(source).not.toMatch(/slotRequired|time_to_be_confirmed/);
  });

  describe('"Create next order now"', () => {
    // The endpoint answers 200 whether or not it created an order (a refusal for a delivery not yet due
    // changes nothing and says why). The page must say what happened, never a bare "Done".
    const pressCreateNextOrderNow = async () => {
      render(<Subscriptions />, { wrapper: createWrapper() });
      await screen.findByText('SUB-1');
      fireEvent.click(document.querySelector('.anticon-eye').closest('button'));
      const billNow = await screen.findByRole('button', { name: /create next order now/i });
      await waitFor(() => expect(billNow).toBeEnabled());
      fireEvent.click(billNow);
      const confirm = await waitFor(() => {
        const el = document.querySelector('.ant-popconfirm .ant-btn-primary');
        expect(el).toBeTruthy();
        return el;
      });
      fireEvent.click(confirm);
      await waitFor(() => expect(adminService.processSubscriptionBilling).toHaveBeenCalledWith(1));
    };

    let warning;
    let success;
    beforeEach(() => {
      warning = vi.spyOn(message, 'warning').mockImplementation(() => {});
      success = vi.spyOn(message, 'success').mockImplementation(() => {});
    });
    afterEach(() => {
      warning.mockRestore();
      success.mockRestore();
    });

    it('shows the backend message as a warning when no order was created', async () => {
      adminService.processSubscriptionBilling.mockResolvedValue({
        success: true,
        message: 'No order created: Phone number is required to place an order. Nothing was changed.',
        data: { success: false, advanced: false, created_order_ids: [], reason: 'customer_barred' },
      });

      await pressCreateNextOrderNow();

      await waitFor(() => expect(warning).toHaveBeenCalledWith(
        'No order created: Phone number is required to place an order. Nothing was changed.',
      ));
      expect(success).not.toHaveBeenCalledWith('Done');
    });

    it('falls back to the translated message when the backend sends none', async () => {
      adminService.processSubscriptionBilling.mockResolvedValue({
        success: true, data: { success: false, created_order_ids: [] },
      });

      await pressCreateNextOrderNow();

      await waitFor(() => expect(warning).toHaveBeenCalledWith('No order was created'));
      expect(success).not.toHaveBeenCalledWith('Done');
    });

    it('says Done when the order was created', async () => {
      adminService.processSubscriptionBilling.mockResolvedValue({
        success: true, message: 'Next order created', data: { success: true, created_order_ids: [901], order_id: 901 },
      });

      await pressCreateNextOrderNow();

      await waitFor(() => expect(success).toHaveBeenCalledWith('Done'));
      expect(warning).not.toHaveBeenCalled();
    });
  });

  it('filters the list by frequency, not billing_cycle', async () => {
    render(<Subscriptions />, { wrapper: createWrapper() });
    await screen.findByText('SUB-1');
    const filter = [...document.querySelectorAll('.ant-select-selection-placeholder')]
      .find((el) => el.textContent === 'Frequency').closest('.ant-select');
    fireEvent.mouseDown(filter.querySelector('.ant-select-selector'));
    const option = await waitFor(() => {
      const el = document.querySelector('.ant-select-item-option[title="daily"]');
      expect(el).toBeTruthy();
      return el;
    });
    fireEvent.click(option);
    await waitFor(() => {
      expect(adminService.getSubscriptions).toHaveBeenLastCalledWith(expect.objectContaining({ frequency: 'daily' }));
    });
    const lastCall = adminService.getSubscriptions.mock.calls.at(-1)[0];
    expect(lastCall).not.toHaveProperty('billing_cycle');
  });

  it('edits with ONE Frequency select and sends frequency plus the weekday, with no billing fields', async () => {
    adminService.getSubscription.mockResolvedValue({
      id: 1, subscription_number: 'SUB-1', user: { id: 7, name: 'Test User' }, name: 'Existing Sub',
      frequency: 'weekly', delivery_day_of_week: 3, delivery_time_slot_id: 2,
      payment_method: 'cash', delivery_address_id: 3, auto_renew: true,
      discount_percentage: 0, status: 'active', allowed_actions: ALL_ALLOWED, items: [], next_delivery_date: '2026-10-29',
    });
    render(<Subscriptions />, { wrapper: createWrapper() });
    await screen.findByText('SUB-1');
    fireEvent.click(document.querySelector('.anticon-edit').closest('button'));
    const dialog = await screen.findByRole('dialog');
    await within(dialog).findByDisplayValue('Existing Sub');

    expect(within(dialog).getAllByText('Frequency')).toHaveLength(1);
    expect(within(dialog).queryByText(/billing cycle/i)).not.toBeInTheDocument();
    expect(within(dialog).queryByText(/delivery frequency/i)).not.toBeInTheDocument();
    // weekly shows the weekday by name, never "0=Mon"; the day-of-month field is hidden
    expect(within(dialog).getByText('Thursday')).toBeInTheDocument();
    expect(within(dialog).queryByText('Day of month')).not.toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole('button', { name: /^update$/i }));
    await waitFor(() => expect(adminService.updateSubscription).toHaveBeenCalled());
    const [id, payload] = adminService.updateSubscription.mock.calls[0];
    expect(id).toBe(1);
    expect(payload).toEqual(expect.objectContaining({
      frequency: 'weekly', delivery_day_of_week: 3, delivery_day_of_month: null, delivery_time_slot_id: 2,
    }));
    ['billing_cycle', 'delivery_frequency', 'next_billing_date', 'last_billing_date'].forEach((old) => {
      expect(payload).not.toHaveProperty(old);
    });
  });

  it('creating defaults to weekly and offers no billing-cycle control', async () => {
    render(<Subscriptions />, { wrapper: createWrapper() });
    await screen.findByText('SUB-1');
    fireEvent.click(screen.getByRole('button', { name: /create subscription/i }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getAllByText('Frequency')).toHaveLength(1);
    expect(within(dialog).queryByText(/billing cycle/i)).not.toBeInTheDocument();
    expect(within(dialog).getByText('Delivery day')).toBeInTheDocument();
  });

  // Re-review G1 (spec 2026-10-26 B9): weekly and biweekly need their weekday, monthly its day of the month, as the
  // website builder and the bot require; and the slot list is asked for that real day, never `weekday=any` for a
  // weekly or biweekly schedule (create would judge the slot on a day the admin never chose).
  describe('the delivery day is required, and the slot list is asked for it', () => {
    it.each([
      ['weekly', 'Delivery day'],
      ['biweekly', 'Delivery day'],
      ['monthly', 'Day of month'],
    ])('a %s subscription without its day is not saved', async (frequency, dayLabel) => {
      const dialog = await openEditForm(editOf({ frequency }));

      fireEvent.click(within(dialog).getByRole('button', { name: /^update$/i }));

      await waitFor(() => expect(formItem(dialog, dayLabel).querySelector('.ant-form-item-explain-error')).toBeTruthy());
      expect(adminService.updateSubscription).not.toHaveBeenCalled();
    });

    it('saves once the day is chosen, and sends it', async () => {
      const dialog = await openEditForm(editOf({ frequency: 'weekly' }));
      await pick(dialog, 'Delivery day', 'Wednesday');

      fireEvent.click(within(dialog).getByRole('button', { name: /^update$/i }));

      await waitFor(() => expect(adminService.updateSubscription).toHaveBeenCalled());
      expect(adminService.updateSubscription.mock.calls[0][1]).toEqual(expect.objectContaining({
        frequency: 'weekly', delivery_day_of_week: 2,
      }));
    });

    it('create asks no slot list until the weekday is chosen, then asks with it and the frequency', async () => {
      adminService.getSubscriptionTimeSlots.mockResolvedValue([{ id: 4, name: 'Weekdays 9-18' }]);
      const dialog = await openCreateForm();
      await waitFor(() => expect(adminService.getProducts).toHaveBeenCalled());
      expect(adminService.getSubscriptionTimeSlots).not.toHaveBeenCalled();
      expect(formItem(dialog, 'Time slot').querySelector('.ant-select').className).toContain('ant-select-disabled');

      await pick(dialog, 'Delivery day', 'Wednesday');
      await waitFor(() => expect(adminService.getSubscriptionTimeSlots).toHaveBeenLastCalledWith({ weekday: 2, frequency: 'weekly' }));
      await pick(dialog, 'Frequency', 'biweekly');
      await waitFor(() => expect(adminService.getSubscriptionTimeSlots).toHaveBeenLastCalledWith({ weekday: 2, frequency: 'biweekly' }));

      const weekdayQuestions = adminService.getSubscriptionTimeSlots.mock.calls
        .map(([query]) => query).filter((query) => ['weekly', 'biweekly'].includes(query.frequency));
      expect(weekdayQuestions.every((query) => query.weekday === 2)).toBe(true);
    });
  });

  // Re-review G3, G11: the form never holds a value its own list does not offer. Create starts with no payment
  // method (the menu's own default when it flags one), and a re-asked menu or slot list that no longer has the
  // current value clears it; the menu is asked again when the customer changes.
  describe('the form values follow the lists the backend offers', () => {
    beforeEach(() => {
      adminService.getUsers.mockResolvedValue({ items: USERS });
      adminService.getUserAddresses.mockImplementation(async (userId) => ({
        data: { addresses: userId === 7 ? [{ id: 3, full_address: 'Amir Temur 1' }] : [{ id: 9, full_address: 'Office 9' }] },
      }));
    });

    it('create pre-selects nothing before a customer is chosen, and no cash their menu lacks', async () => {
      adminService.getCustomerSubscriptionPaymentMethods.mockResolvedValue([
        { method: 'click', display_name: 'Card', is_active: true },
      ]);
      const dialog = await openCreateForm();
      expect(selected(dialog, 'Payment method')).toBeNull();

      await pick(dialog, 'Customer', 'Ann Lee +998901112233');
      await waitFor(() => expect(adminService.getCustomerSubscriptionPaymentMethods).toHaveBeenCalledWith(7, undefined));

      expect(selected(dialog, 'Payment method')).toBeNull();
    });

    it('create takes the method the menu flags as its default', async () => {
      adminService.getCustomerSubscriptionPaymentMethods.mockResolvedValue([
        { method: 'click', display_name: 'Card', is_active: true },
        { method: 'business_account', display_name: 'Business Account', is_active: true, is_default: true },
      ]);
      const dialog = await openCreateForm();

      await pick(dialog, 'Customer', 'Ann Lee +998901112233');

      await waitFor(() => expect(selected(dialog, 'Payment method')).toBe('Business Account'));
    });

    it('asks the menu again when the customer changes, and drops a method the new one does not offer', async () => {
      adminService.getCustomerSubscriptionPaymentMethods.mockImplementation(async (userId) => (userId === 7
        ? [{ method: 'click', display_name: 'Card' }, { method: 'business_account', display_name: 'Business Account' }]
        : [{ method: 'click', display_name: 'Card' }]));
      const dialog = await openCreateForm();
      await pick(dialog, 'Customer', 'Ann Lee +998901112233');
      await waitFor(() => expect(adminService.getCustomerSubscriptionPaymentMethods).toHaveBeenLastCalledWith(7, undefined));
      await pick(dialog, 'Delivery address', 'Amir Temur 1');
      await waitFor(() => expect(adminService.getCustomerSubscriptionPaymentMethods).toHaveBeenLastCalledWith(7, 3));
      await pick(dialog, 'Payment method', 'Business Account');

      await pick(dialog, 'Customer', 'Bob Kim +998904445566');

      await waitFor(() => expect(adminService.getCustomerSubscriptionPaymentMethods).toHaveBeenLastCalledWith(8, undefined));
      await waitFor(() => expect(selected(dialog, 'Payment method')).toBeNull());
      await waitFor(() => expect(formItem(dialog, 'Payment method').querySelector('.ant-form-item-explain-error')).toBeTruthy());
    });

    it('edit: moving to an address whose menu drops the method clears it', async () => {
      adminService.getUserAddresses.mockResolvedValue({
        data: { addresses: [{ id: 3, full_address: 'Amir Temur 1' }, { id: 4, full_address: 'Office 4' }] },
      });
      adminService.getCustomerSubscriptionPaymentMethods.mockImplementation(async (_user, addressId) => (addressId === 4
        ? [{ method: 'click', display_name: 'Card' }]
        : [{ method: 'cash', display_name: 'Cash on Delivery' }, { method: 'click', display_name: 'Card' }]));
      const dialog = await openEditForm(editOf({ delivery_day_of_week: 3 }));
      await waitFor(() => expect(selected(dialog, 'Payment method')).toBe('Cash on Delivery'));

      await pick(dialog, 'Delivery address', 'Office 4');

      await waitFor(() => expect(selected(dialog, 'Payment method')).toBeNull());
      fireEvent.click(within(dialog).getByRole('button', { name: /^update$/i }));
      await waitFor(() => expect(formItem(dialog, 'Payment method').querySelector('.ant-form-item-explain-error')).toBeTruthy());
      expect(adminService.updateSubscription).not.toHaveBeenCalled();
    });

    it('edit: a stored method the menu no longer offers stays while nothing it depends on changes', async () => {
      adminService.getCustomerSubscriptionPaymentMethods.mockResolvedValue([{ method: 'click', display_name: 'Card' }]);
      const dialog = await openEditForm(editOf({ delivery_day_of_week: 3, name: 'Renamed later' }));
      await waitFor(() => expect(adminService.getCustomerSubscriptionPaymentMethods).toHaveBeenCalledWith(7, 3));

      fireEvent.click(within(dialog).getByRole('button', { name: /^update$/i }));

      await waitFor(() => expect(adminService.updateSubscription).toHaveBeenCalled());
      expect(adminService.updateSubscription.mock.calls[0][1]).toEqual(expect.objectContaining({ payment_method: 'cash' }));
    });

    // Spec 2026-10-27 §6.2: "Any time" is a choice on its own, sent as an explicit null (Task 6b's PUT clears the
    // slot on it). A day change whose list drops the stored slot asks for a time slot or Any time.
    it('edit: a day change whose slot list drops the slot asks for a time slot or Any time', async () => {
      adminService.getSubscriptionTimeSlots.mockImplementation(async ({ weekday }) => (weekday === 5
        ? [{ id: 5, name: 'Saturday 10-12' }]
        : [{ id: 1, name: 'Every day 10-12' }]));
      const dialog = await openEditForm(editOf({ delivery_day_of_week: 5, delivery_time_slot_id: 5 }));
      await waitFor(() => expect(selected(dialog, 'Time slot')).toBe('Saturday 10-12'));

      await pick(dialog, 'Delivery day', 'Monday');

      await waitFor(() => expect(adminService.getSubscriptionTimeSlots).toHaveBeenLastCalledWith({ weekday: 0, frequency: 'weekly' }));
      await waitFor(() => expect(selected(dialog, 'Time slot')).toBeNull());
      fireEvent.click(within(dialog).getByRole('button', { name: /^update$/i }));
      await waitFor(() => expect(formItem(dialog, 'Time slot').querySelector('.ant-form-item-explain-error')).toBeTruthy());
      expect(within(dialog).getByText('Choose a time slot or Any time')).toBeInTheDocument();
      expect(adminService.updateSubscription).not.toHaveBeenCalled();

      await pick(dialog, 'Time slot', 'Any time');
      fireEvent.click(within(dialog).getByRole('button', { name: /^update$/i }));
      await waitFor(() => expect(adminService.updateSubscription).toHaveBeenCalled());
      expect(adminService.updateSubscription.mock.calls[0][1]).toEqual(expect.objectContaining({
        delivery_day_of_week: 0, delivery_time_slot_id: null,
      }));
    });

    it('edit: a subscription without a slot opens on Any time and saves it as null', async () => {
      adminService.getSubscriptionTimeSlots.mockResolvedValue([{ id: 1, name: 'Every day 10-12' }]);
      const dialog = await openEditForm(editOf({ delivery_day_of_week: 3, delivery_time_slot_id: null }));
      await waitFor(() => expect(adminService.getSubscriptionTimeSlots).toHaveBeenCalledWith({ weekday: 3, frequency: 'weekly' }));
      expect(selected(dialog, 'Time slot')).toBe('Any time');

      fireEvent.click(within(dialog).getByRole('button', { name: /^update$/i }));

      await waitFor(() => expect(adminService.updateSubscription).toHaveBeenCalled());
      expect(adminService.updateSubscription.mock.calls[0][1]).toEqual(expect.objectContaining({ delivery_time_slot_id: null }));
    });

    it('edit: choosing Any time for a slotted subscription sends null', async () => {
      adminService.getSubscriptionTimeSlots.mockResolvedValue([{ id: 1, name: 'Every day 10-12' }]);
      const dialog = await openEditForm(editOf({ delivery_day_of_week: 3, delivery_time_slot_id: 1 }));
      await waitFor(() => expect(selected(dialog, 'Time slot')).toBe('Every day 10-12'));

      await pick(dialog, 'Time slot', 'Any time');
      fireEvent.click(within(dialog).getByRole('button', { name: /^update$/i }));

      await waitFor(() => expect(adminService.updateSubscription).toHaveBeenCalled());
      expect(adminService.updateSubscription.mock.calls[0][1]).toEqual(expect.objectContaining({ delivery_time_slot_id: null }));
    });

    it('create starts on Any time, lists it first, and creates with null', async () => {
      adminService.getUsers.mockResolvedValue({ items: USERS });
      adminService.getProducts.mockResolvedValue({ data: { items: [{ id: 2, name: 'Water 19L', min_order_quantity: 1 }] } });
      adminService.getSubscriptionTimeSlots.mockResolvedValue([{ id: 4, name: 'Morning 9-12' }]);
      adminService.createSubscription.mockResolvedValue({ id: 2 });
      const dialog = await openCreateForm();
      expect(selected(dialog, 'Time slot')).toBe('Any time');

      await pick(dialog, 'Frequency', 'daily');
      await waitFor(() => expect(adminService.getSubscriptionTimeSlots).toHaveBeenLastCalledWith({ weekday: 'any', frequency: 'daily' }));
      fireEvent.mouseDown(formItem(dialog, 'Time slot').querySelector('.ant-select-selector'));
      await optionIn('Morning 9-12');
      const titles = [...document.querySelectorAll('.ant-select-item-option')].map((o) => o.getAttribute('title'));
      expect(titles.slice(-2)).toEqual(['Any time', 'Morning 9-12']);
      fireEvent.click(await optionIn('Any time'));

      await pick(dialog, 'Customer', 'Ann Lee +998901112233');
      fireEvent.change(within(dialog).getByRole('textbox', { name: 'Name' }), { target: { value: 'Daily water' } });
      fireEvent.mouseDown(dialog.querySelector('.ant-form-item [id$="_product_id"]').closest('.ant-select').querySelector('.ant-select-selector'));
      fireEvent.click(await optionIn('Water 19L'));
      await pick(dialog, 'Delivery address', 'Amir Temur 1');
      await pick(dialog, 'Payment method', 'Cash on Delivery');
      fireEvent.click(within(dialog).getByRole('button', { name: /^create$/i }));

      await waitFor(() => expect(adminService.createSubscription).toHaveBeenCalled());
      expect(adminService.createSubscription.mock.calls[0][0]).toEqual(expect.objectContaining({
        frequency: 'daily', delivery_time_slot_id: null,
      }));
    });

    it('the weekly day select offers only delivery days', async () => {
      adminService.getDeliveryWeekdays.mockResolvedValue([0, 1, 2, 3, 4]);
      const dialog = await openEditForm(editOf({ frequency: 'weekly', delivery_day_of_week: 0 }));
      await waitFor(() => expect(adminService.getDeliveryWeekdays).toHaveBeenCalled());

      fireEvent.mouseDown(formItem(dialog, 'Delivery day').querySelector('.ant-select-selector'));

      await waitFor(() => {
        const titles = [...document.querySelectorAll('.ant-select-item-option')].map((o) => o.getAttribute('title'));
        expect(titles).toEqual(['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']);
      });
    });

    it('a stored weekday that is no longer a delivery day still shows its name', async () => {
      adminService.getDeliveryWeekdays.mockResolvedValue([0, 1, 2, 3, 4]);
      const dialog = await openEditForm(editOf({ frequency: 'weekly', delivery_day_of_week: 5 }));
      await waitFor(() => expect(adminService.getDeliveryWeekdays).toHaveBeenCalled());

      // the options narrow to the delivery days plus the stored day (Sunday, not stored, drops out once the query resolves)
      fireEvent.mouseDown(formItem(dialog, 'Delivery day').querySelector('.ant-select-selector'));
      await waitFor(() => {
        const titles = [...document.querySelectorAll('.ant-select-item-option')].map((o) => o.getAttribute('title'));
        expect(titles).toEqual(['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']);
      });
      expect(within(formItem(dialog, 'Delivery day')).queryByText('5')).not.toBeInTheDocument();
    });

    // Re-review tests-G3-unchanged-slot-B9-unpinned: a stored slot the list no longer offers (deactivated, or narrowed
    // to other days) stays while the schedule is untouched, and is resent: the update judges only a real change.
    it('edit: a stored slot the list no longer offers is resent when only the name changes', async () => {
      adminService.getSubscriptionTimeSlots.mockResolvedValue([{ id: 1, name: 'Every day 10-12' }]);
      const dialog = await openEditForm(editOf({ delivery_day_of_week: 5, delivery_time_slot_id: 5 }));
      await waitFor(() => expect(adminService.getSubscriptionTimeSlots).toHaveBeenCalledWith({ weekday: 5, frequency: 'weekly' }));
      fireEvent.change(within(dialog).getByDisplayValue('Existing Sub'), { target: { value: 'Renamed by admin' } });

      fireEvent.click(within(dialog).getByRole('button', { name: /^update$/i }));

      await waitFor(() => expect(adminService.updateSubscription).toHaveBeenCalled());
      expect(adminService.updateSubscription.mock.calls[0][1]).toEqual(expect.objectContaining({
        name: 'Renamed by admin', delivery_day_of_week: 5, delivery_time_slot_id: 5,
      }));
    });

    // Re-review admin-bot-backend-2: the menu the form read the first time it opened (asked before the fields mounted,
    // for no address) is still cached when the same edit is opened again; it must not clear the stored method.
    it('edit: reopening the same edit keeps a stored method the menu no longer offers', async () => {
      adminService.getCustomerSubscriptionPaymentMethods.mockResolvedValue([{ method: 'click', display_name: 'Card' }]);
      render(<Subscriptions />, { wrapper: createWrapper() });
      await screen.findByText('SUB-1');
      adminService.getSubscription.mockResolvedValue(editOf({ delivery_day_of_week: 3 }));
      const openIt = async () => {
        fireEvent.click(document.querySelector('.anticon-edit').closest('button'));
        const dialog = await screen.findByRole('dialog');
        await within(dialog).findByDisplayValue('Existing Sub');
        await waitFor(() => expect(adminService.getCustomerSubscriptionPaymentMethods).toHaveBeenCalledWith(7, 3));
        return dialog;
      };
      const first = await openIt();
      await waitFor(() => expect(selected(first, 'Payment method')).toBe('cash'));
      fireEvent.click(within(first).getByRole('button', { name: /^cancel$/i }));
      await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());

      const dialog = await openIt();
      await new Promise((resolve) => { setTimeout(resolve, 50); });

      expect(selected(dialog, 'Payment method')).toBe('cash');
      fireEvent.click(within(dialog).getByRole('button', { name: /^update$/i }));
      await waitFor(() => expect(adminService.updateSubscription).toHaveBeenCalled());
      expect(adminService.updateSubscription.mock.calls[0][1]).toEqual(expect.objectContaining({ payment_method: 'cash' }));
    });
  });
});
