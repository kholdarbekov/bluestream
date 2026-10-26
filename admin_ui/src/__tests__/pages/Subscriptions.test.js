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
    getPaymentMethods: vi.fn(),
  },
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, opts) => (opts && opts.defaultValue) || key }),
}));

const createWrapper = () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
};

describe('Subscriptions page', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    adminService.getSubscriptions.mockResolvedValue({
      items: [{
        id: 1, subscription_number: 'SUB-1', user_name: 'Test User', user_email: 't@e.com',
        status: 'active', frequency: 'monthly', billing_amount: 30000,
        next_delivery_date: '2026-08-01',
        upcoming_delivery: { date: '2026-08-01', window_start: '09:00', window_end: '12:00', order_id: null },
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
      name: 'Existing Sub', description: 'd', frequency: 'weekly',
      payment_method: 'cash', delivery_address_id: 3, auto_payment: true, auto_renew: true,
      discount_percentage: 0, status: 'active', items: [],
    });
    adminService.updateSubscription.mockResolvedValue({ id: 1 });
    adminService.getPaymentMethods.mockResolvedValue([
      { method: 'cash', display_name: 'Cash on Delivery', is_active: true },
      { method: 'click', display_name: 'Click', is_active: true },
    ]);
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
      discount_percentage: 0, status: 'active',
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

  it('populates the payment-method select from the API, not a hardcoded list', async () => {
    render(<Subscriptions />, { wrapper: createWrapper() });
    await screen.findByText('SUB-1');
    fireEvent.click(screen.getByRole('button', { name: /create subscription/i }));

    const dialog = await screen.findByRole('dialog');

    await waitFor(() => {
      expect(adminService.getPaymentMethods).toHaveBeenCalledWith('subscription');
    });

    const paymentMethodLabel = within(dialog).getByText('Payment method');
    const formItem = paymentMethodLabel.closest('.ant-form-item');
    const selector = formItem.querySelector('.ant-select-selector');
    fireEvent.mouseDown(selector);

    const dropdown = await waitFor(() => {
      const el = document.querySelector('.ant-select-dropdown');
      expect(el).toBeTruthy();
      return el;
    });

    expect(await within(dropdown).findByText('Cash on Delivery')).toBeInTheDocument();
    expect(within(dropdown).getByText('Click')).toBeInTheDocument();
    expect(within(dropdown).queryByText(/payme/i)).not.toBeInTheDocument();
  });

  describe('per-product minimum order quantity', () => {
    // A subscription line below the minimum is one billing's create_order refuses
    // every cycle (prod subscription 8: 1 x a product whose minimum is 2).
    const detailWithItem = (quantity) => ({
      id: 1, subscription_number: 'SUB-1', user: { id: 7, name: 'Test User' },
      name: 'Existing Sub', frequency: 'weekly',
      payment_method: 'click', delivery_address_id: 3, auto_renew: true,
      discount_percentage: 0, status: 'active',
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
      discount_percentage: 0, status: 'active', items: [],
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
      discount_percentage: 0, status: 'active', items: [],
      next_delivery_date: '2026-10-29',
      upcoming_delivery: { date: '2026-10-29', window_start: null, window_end: null, order_id: null },
    });
    render(<Subscriptions />, { wrapper: createWrapper() });
    expect(await screen.findByText('Aug 01, 2026, 09:00-12:00')).toBeInTheDocument();
    expect(screen.queryByText(/billing/i)).not.toBeInTheDocument();
    fireEvent.click(document.querySelector('.anticon-eye').closest('button'));
    expect(await screen.findByText('Oct 29, 2026 (time to be confirmed)')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /create next order now/i })).toBeInTheDocument();
    expect(screen.queryByText(/process billing/i)).not.toBeInTheDocument();
  });

  describe('"Create next order now"', () => {
    // The endpoint answers 200 whether or not it created an order (a refusal for a delivery not yet due
    // changes nothing and says why). The page must say what happened, never a bare "Done".
    const pressCreateNextOrderNow = async () => {
      render(<Subscriptions />, { wrapper: createWrapper() });
      await screen.findByText('SUB-1');
      fireEvent.click(document.querySelector('.anticon-eye').closest('button'));
      fireEvent.click(await screen.findByRole('button', { name: /create next order now/i }));
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
      discount_percentage: 0, status: 'active', items: [], next_delivery_date: '2026-10-29',
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
});
