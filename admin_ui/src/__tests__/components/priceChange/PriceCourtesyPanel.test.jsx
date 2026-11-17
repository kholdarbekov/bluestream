import React from 'react';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { message } from 'antd';

import PriceCourtesyPanel from '../../../components/priceChange/PriceCourtesyPanel';
import adminService from '../../../services/adminService';

vi.mock('../../../services/adminService', () => ({
  __esModule: true,
  default: {
    getCustomerPriceCourtesy: vi.fn(),
    grantCustomerPriceCourtesy: vi.fn(),
  },
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key, fallback, opts) => String(typeof fallback === 'string' ? fallback : key)
      .replace(/\{\{(\w+)\}\}/g, (_, name) => String(opts?.[name] ?? '')),
  }),
}));

vi.mock('antd', async () => {
  const actual = await vi.importActual('antd');
  return { ...actual, message: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() } };
});

// tests/integration/test_price_courtesy_admin_ui_contract.py diffs each Set with the real route's answer.
const COURTESY_RESPONSE_KEYS = new Set(['courtesy_status', 'allowed_actions']);
const COURTESY_STATUS_KEYS = new Set(['state', 'expires_on', 'caps', 'used_order_id', 'used_order_number']);
const COURTESY_CAP_KEYS = new Set(['product_id', 'product_name', 'cap']);
const COURTESY_ACTION_KEYS = new Set(['allowed', 'block_reason']);

const checked = (name, value, keys) => {
  const wrong = Object.keys(value).filter((key) => !keys.has(key));
  const missing = [...keys].filter((key) => !(key in value));
  if (wrong.length || missing.length) throw new Error(`${name}: extra ${wrong} missing ${missing}`);
  return value;
};
const status = (overrides = {}) => checked('status', {
  state: 'available',
  expires_on: '2027-02-14',
  caps: [checked('cap', { product_id: 3, product_name: 'Water 18.9 L', cap: 2 }, COURTESY_CAP_KEYS)],
  used_order_id: null,
  used_order_number: null,
  ...overrides,
}, COURTESY_STATUS_KEYS);
const action = (allowed, block_reason = null) => checked('action', { allowed, block_reason }, COURTESY_ACTION_KEYS);
const response = (statusOverrides = {}, grant = action(true)) => checked('response', {
  courtesy_status: status(statusOverrides),
  allowed_actions: { grant },
}, COURTESY_RESPONSE_KEYS);
const NONE = { state: 'none', expires_on: null, caps: [] };

const renderPanel = () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={queryClient}>
      <PriceCourtesyPanel user={{ id: 11, role: 'customer' }} />
    </QueryClientProvider>,
  );
  return { ...view, queryClient };
};

describe('PriceCourtesyPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows the kept old price with every cap and its last day', async () => {
    adminService.getCustomerPriceCourtesy.mockResolvedValue(response({
      caps: [
        { product_id: 3, product_name: 'Water 18.9 L', cap: 2 },
        { product_id: 4, product_name: 'Water 10 L', cap: 3 },
      ],
    }, action(false)));

    renderPanel();

    expect(await screen.findByText(
      'Old price kept for this customer until Feb 14, 2027: up to 2 × Water 18.9 L, up to 3 × Water 10 L',
    )).toBeInTheDocument();
    expect(adminService.getCustomerPriceCourtesy).toHaveBeenCalledWith(11);
  });

  it('names the order that used it', async () => {
    adminService.getCustomerPriceCourtesy.mockResolvedValue(response(
      { state: 'used', caps: [], used_order_id: 91, used_order_number: 'TG_000091_26' }, action(false),
    ));

    renderPanel();

    expect(await screen.findByText('Already used on order TG_000091_26')).toBeInTheDocument();
  });

  it('gives it with the trimmed reason and redraws from the answer', async () => {
    const user = userEvent.setup();
    adminService.getCustomerPriceCourtesy.mockResolvedValue(response(NONE));
    adminService.grantCustomerPriceCourtesy.mockResolvedValue(response({}, action(false, 'Already given')));

    const { queryClient } = renderPanel();
    expect(await screen.findByText('No old-price order for this customer')).toBeInTheDocument();
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries');

    await user.click(screen.getByRole('button', { name: 'Give the old-price order' }));
    const dialog = await screen.findByRole('dialog');
    const ok = within(dialog).getByRole('button', { name: 'OK' });
    expect(ok).toBeDisabled();
    await user.type(within(dialog).getByPlaceholderText('Why this customer gets it'), '  Was abroad during the notice  ');
    expect(ok).toBeEnabled();
    await user.click(ok);

    await waitFor(() => expect(adminService.grantCustomerPriceCourtesy)
      .toHaveBeenCalledWith(11, 'Was abroad during the notice'));
    expect(await screen.findByText(/Old price kept for this customer until Feb 14, 2027/)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['priceChanges'] });
    expect(message.success).toHaveBeenCalledWith('Old-price order given');
    expect(adminService.getCustomerPriceCourtesy).toHaveBeenCalledTimes(1);
  });

  it('keeps the reason from growing past the limit the route accepts', async () => {
    const user = userEvent.setup();
    adminService.getCustomerPriceCourtesy.mockResolvedValue(response(NONE));

    renderPanel();
    await user.click(await screen.findByRole('button', { name: 'Give the old-price order' }));

    expect(await screen.findByPlaceholderText('Why this customer gets it')).toHaveAttribute('maxlength', '500');
  });

  it('renders nothing for an operator when the customer has none', async () => {
    adminService.getCustomerPriceCourtesy.mockResolvedValue(response(NONE, action(false)));

    const { container } = renderPanel();

    await waitFor(() => expect(adminService.getCustomerPriceCourtesy).toHaveBeenCalled());
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });

  it('renders nothing for a manager while no courtesy window is open', async () => {
    adminService.getCustomerPriceCourtesy.mockResolvedValue(response(NONE, action(false, null)));

    const { container } = renderPanel();

    await waitFor(() => expect(adminService.getCustomerPriceCourtesy).toHaveBeenCalled());
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });

  it('disables the action with the backend reason', async () => {
    const user = userEvent.setup();
    adminService.getCustomerPriceCourtesy.mockResolvedValue(response(
      { state: 'used', caps: [], used_order_id: 91, used_order_number: 'TG_000091_26' },
      action(false, 'The old price was already given to this customer'),
    ));

    renderPanel();

    const button = await screen.findByRole('button', { name: 'Give the old-price order' });
    expect(button).toBeDisabled();
    await user.hover(button.parentElement);
    expect(await screen.findByText('The old price was already given to this customer')).toBeInTheDocument();
  });

  it('keeps the modal open on a refusal and re-reads the status', async () => {
    const user = userEvent.setup();
    adminService.getCustomerPriceCourtesy.mockResolvedValue(response(NONE));
    adminService.grantCustomerPriceCourtesy.mockRejectedValue({
      response: { data: { message: 'Already given to this customer', error_code: 'PRICE_COURTESY_ALREADY_GIVEN' } },
    });

    renderPanel();
    await user.click(await screen.findByRole('button', { name: 'Give the old-price order' }));
    const dialog = await screen.findByRole('dialog');
    await user.type(within(dialog).getByPlaceholderText('Why this customer gets it'), 'Was abroad');
    await user.click(within(dialog).getByRole('button', { name: 'OK' }));

    await waitFor(() => expect(message.error).toHaveBeenCalledWith('Already given to this customer'));
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    await waitFor(() => expect(adminService.getCustomerPriceCourtesy).toHaveBeenCalledTimes(2));
  });
});
