import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import LinkAccountsConfirm from './LinkAccountsConfirm';
import adminService from '../services/adminService';
import { SHARED_PLACE_CASES_KEY } from '../utils/constants';

vi.mock('../services/adminService');

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, fallback) => fallback || key }),
}));

vi.mock('antd', async () => {
  const actual = await vi.importActual('antd');
  return {
    ...actual,
    message: {
      success: vi.fn(),
      error: vi.fn(),
      info: vi.fn(),
      warning: vi.fn(),
      loading: vi.fn(),
      destroy: vi.fn(),
      open: vi.fn(),
    },
  };
});

const CANDIDATE = { id: 21, first_name: 'Carl', last_name: 'Candidate', phone: '+998900000099' };

const setup = (props = {}) => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(queryClient, 'invalidateQueries');
  const onLinked = vi.fn();
  const onClose = vi.fn();
  render(
    <QueryClientProvider client={queryClient}>
      <LinkAccountsConfirm
        userId={11}
        candidate={CANDIDATE}
        open
        onClose={onClose}
        onLinked={onLinked}
        {...props}
      />
    </QueryClientProvider>
  );
  return { invalidate, onLinked, onClose };
};

describe('LinkAccountsConfirm', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    adminService.linkAccounts.mockResolvedValue({ data: { canonical_customer_id: 5 } });
  });

  it('requires a reason before OK is enabled', async () => {
    const user = userEvent.setup();
    setup();
    const ok = screen.getByRole('button', { name: /confirm/i });
    expect(ok).toBeDisabled();
    await user.type(screen.getByPlaceholderText('Enter reason'), '   ');
    expect(ok).toBeDisabled();
  });

  it('links, invalidates the three queries and reports the resolved data', async () => {
    const user = userEvent.setup();
    const { invalidate, onLinked } = setup();

    await user.type(screen.getByPlaceholderText('Enter reason'), 'Confirmed by phone');
    await user.click(screen.getByRole('button', { name: /confirm/i }));

    await waitFor(() => expect(onLinked).toHaveBeenCalledTimes(1));
    expect(adminService.linkAccounts.mock.calls[0]).toEqual([11, 21, 'Confirmed by phone']);
    expect(onLinked.mock.calls[0]).toEqual([
      { data: { canonical_customer_id: 5 } },
      'Confirmed by phone',
    ]);
    const keys = invalidate.mock.calls.map((c) => c[0].queryKey);
    expect(keys).toEqual(
      expect.arrayContaining([['linkedAccounts', 11], ['linkSuggestions', 11], SHARED_PLACE_CASES_KEY])
    );
  });

  it('pre-fills the reason from defaultReason', () => {
    setup({ defaultReason: 'Same person per driver' });
    expect(screen.getByDisplayValue('Same person per driver')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /confirm/i })).toBeEnabled();
  });

  it('does not call onLinked when the backend refuses', async () => {
    adminService.linkAccounts.mockRejectedValue({ response: { data: { message: 'nope' } } });
    const user = userEvent.setup();
    const { onLinked } = setup();
    await user.type(screen.getByPlaceholderText('Enter reason'), 'r');
    await user.click(screen.getByRole('button', { name: /confirm/i }));
    await waitFor(() => expect(adminService.linkAccounts).toHaveBeenCalled());
    expect(onLinked).not.toHaveBeenCalled();
  });
});
