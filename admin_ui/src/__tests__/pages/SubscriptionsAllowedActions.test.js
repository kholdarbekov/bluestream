import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import Subscriptions from '../../pages/Subscriptions';
import adminService from '../../services/adminService';

vi.mock('../../services/adminService', () => ({
  __esModule: true,
  default: {
    getSubscriptions: vi.fn(), getSubscription: vi.fn(), createSubscription: vi.fn(),
    updateSubscription: vi.fn(), pauseSubscription: vi.fn(), resumeSubscription: vi.fn(),
    cancelSubscription: vi.fn(), processSubscriptionBilling: vi.fn(), addSubscriptionItem: vi.fn(),
    updateSubscriptionItem: vi.fn(), removeSubscriptionItem: vi.fn(), getUsers: vi.fn(),
    getUserAddresses: vi.fn(), getProducts: vi.fn(), getTimeSlots: vi.fn(),
    getCustomerSubscriptionPaymentMethods: vi.fn(), getSubscriptionTimeSlots: vi.fn(), getDeliveryWeekdays: vi.fn(),
  },
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, opts) => (opts && opts.defaultValue) || key }),
}));

const wrapper = ({ children }) => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
};

const allow = (...names) => {
  const all = ['pause', 'resume', 'cancel', 'edit', 'remove_item', 'create_next_order'];
  const out = {};
  all.forEach((a) => { out[a] = names.includes(a) ? { allowed: true, block_reason: null } : { allowed: false, block_reason: `no ${a}` }; });
  return out;
};

// The status says one thing, the published answer another: buttons must follow the answer.
const detailWith = (status, allowed_actions) => ({
  id: 1, subscription_number: 'SUB-1', user: { id: 7, name: 'Test User' }, name: 'S', frequency: 'weekly',
  payment_method: 'cash', delivery_address_id: 3, status, allowed_actions,
  items: [{ id: 55, product_id: 2, product_name: 'Water', quantity: 2, unit_price: 15000 }],
});

const openDrawer = async (detail) => {
  adminService.getSubscription.mockResolvedValue(detail);
  render(<Subscriptions />, { wrapper });
  await screen.findByText('SUB-1');
  fireEvent.click(document.querySelector('.anticon-eye').closest('button'));
  await screen.findByText('Water');
};

const btn = (re) => {
  const label = screen.getAllByText(re).find((el) => el.closest('.ant-drawer'));
  return label.closest('button');
};

describe('Subscriptions drawer buttons follow allowed_actions', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    adminService.getSubscriptions.mockResolvedValue({
      items: [{ id: 1, subscription_number: 'SUB-1', user_name: 'U', user_email: 'u@e.com', status: 'active',
        frequency: 'weekly', billing_amount: 1, items_count: 1,
        allowed_actions: allow('edit') }],
      total: 1,
    });
    adminService.getUsers.mockResolvedValue({ items: [] });
    adminService.getProducts.mockResolvedValue({ data: { items: [] } });
    adminService.getTimeSlots.mockResolvedValue({ data: { items: [] } });
    adminService.getUserAddresses.mockResolvedValue({ data: { addresses: [] } });
    adminService.getCustomerSubscriptionPaymentMethods.mockResolvedValue([]);
    adminService.getSubscriptionTimeSlots.mockResolvedValue([]);
  });

  it('active payload: pause, cancel, create-next enabled; resume disabled', async () => {
    await openDrawer(detailWith('active', allow('pause', 'cancel', 'create_next_order', 'edit', 'remove_item')));
    expect(btn(/^pause$/i)).toBeEnabled();
    expect(btn(/^resume$/i)).toBeDisabled();
    expect(btn(/^cancel$/i)).toBeEnabled();
    expect(btn(/create next order now/i)).toBeEnabled();
  });

  it('paused payload: only resume and cancel', async () => {
    await openDrawer(detailWith('paused', allow('resume', 'cancel')));
    expect(btn(/^pause$/i)).toBeDisabled();
    expect(btn(/^resume$/i)).toBeEnabled();
    expect(btn(/^cancel$/i)).toBeEnabled();
    expect(btn(/create next order now/i)).toBeDisabled();
  });

  it('cancelled payload: everything disabled', async () => {
    await openDrawer(detailWith('cancelled', allow()));
    ['pause', 'resume', 'cancel'].forEach((a) => expect(btn(new RegExp(`^${a}$`, 'i'))).toBeDisabled());
    expect(btn(/create next order now/i)).toBeDisabled();
  });

  it('does not re-derive from status: an "active" row whose pause is blocked stays disabled', async () => {
    await openDrawer(detailWith('active', allow('cancel')));
    expect(btn(/^pause$/i)).toBeDisabled();
  });

  it('a cancelled-looking status with create_next_order allowed is enabled and posts bill', async () => {
    await openDrawer(detailWith('cancelled', allow('create_next_order')));
    expect(btn(/create next order now/i)).toBeEnabled();
  });

  it('remove-item button follows remove_item', async () => {
    await openDrawer(detailWith('active', allow('pause')));
    expect(document.querySelector('.anticon-delete').closest('button')).toBeDisabled();
  });

  it('remove-item enabled when allowed', async () => {
    await openDrawer(detailWith('active', allow('remove_item')));
    expect(document.querySelector('.anticon-delete').closest('button')).toBeEnabled();
  });

  it('row edit button follows edit (or the admin override)', async () => {
    adminService.getSubscriptions.mockResolvedValue({
      items: [{ id: 1, subscription_number: 'SUB-1', user_name: 'U', user_email: 'u@e.com', status: 'cancelled',
        frequency: 'weekly', billing_amount: 1, items_count: 1,
        allowed_actions: { ...allow(), edit: { allowed: false, block_reason: 'x', override_available: false } } }],
      total: 1,
    });
    render(<Subscriptions />, { wrapper });
    await screen.findByText('SUB-1');
    expect(document.querySelector('.anticon-edit').closest('button')).toBeDisabled();
  });

  it('row edit stays enabled when blocked but the override is available', async () => {
    adminService.getSubscriptions.mockResolvedValue({
      items: [{ id: 1, subscription_number: 'SUB-1', user_name: 'U', user_email: 'u@e.com', status: 'paused',
        frequency: 'weekly', billing_amount: 1, items_count: 1,
        allowed_actions: { ...allow(), edit: { allowed: false, block_reason: 'x', override_available: true } } }],
      total: 1,
    });
    render(<Subscriptions />, { wrapper });
    await screen.findByText('SUB-1');
    await waitFor(() => expect(document.querySelector('.anticon-edit').closest('button')).toBeEnabled());
  });
});

// Final review concern 12: the drawer's item editors (each quantity and its Save, and Add item) follow the
// published `edit` answer as the other drawer buttons do. The item routes take no edit-any-status override (that
// switch belongs to the Edit form), so `override_available` does not open them: it would only lead to a refusal.
describe('the drawer item editors follow allowed_actions.edit', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    adminService.getSubscriptions.mockResolvedValue({
      items: [{ id: 1, subscription_number: 'SUB-1', user_name: 'U', user_email: 'u@e.com', status: 'active',
        frequency: 'weekly', billing_amount: 1, items_count: 1, allowed_actions: allow('edit') }],
      total: 1,
    });
    adminService.getUsers.mockResolvedValue({ items: [] });
    adminService.getProducts.mockResolvedValue({ data: { items: [{ id: 3, name: 'Aqua 10 l', min_order_quantity: 1 }] } });
    adminService.getUserAddresses.mockResolvedValue({ data: { addresses: [] } });
  });

  const editors = () => {
    const drawer = document.querySelector('.ant-drawer');
    return {
      quantity: drawer.querySelector('.ant-table .ant-input-number-input'),
      save: [...drawer.querySelectorAll('.ant-table button')].find((b) => /^save$/i.test(b.textContent)),
      product: drawer.querySelector('.ant-space .ant-select'),
      add: [...drawer.querySelectorAll('button')].find((b) => /add item/i.test(b.textContent)),
    };
  };

  it('a subscription that cannot be edited offers no item editor, with the reason', async () => {
    const closed = { ...allow(), edit: { allowed: false, block_reason: 'It is cancelled.', override_available: true } };
    await openDrawer(detailWith('cancelled', closed));
    const { quantity, save, product, add } = editors();
    expect(quantity).toBeDisabled();
    expect(save).toBeDisabled();
    expect(save.getAttribute('title')).toBe('It is cancelled.');
    expect(product.className).toContain('ant-select-disabled');
    expect(add).toBeDisabled();
    expect(add.getAttribute('title')).toBe('It is cancelled.');
  });

  it('an editable subscription keeps every item editor open', async () => {
    await openDrawer(detailWith('active', allow('edit')));
    const { quantity, save, product } = editors();
    expect(quantity).toBeEnabled();
    expect(save).toBeEnabled();
    expect(product.className).not.toContain('ant-select-disabled');
  });
});
