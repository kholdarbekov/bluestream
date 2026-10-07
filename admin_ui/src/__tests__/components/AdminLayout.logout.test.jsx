import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import AdminLayout from '../../components/layout/AdminLayout';

const mockAuth = {
  user: { first_name: 'Ada', last_name: 'Admin', role: 'admin' },
  logout: vi.fn(),
  hasPermission: vi.fn(() => false),
  getUserRole: vi.fn(() => 'admin'),
};
vi.mock('../../stores/authStore', () => ({ useAuthStore: () => mockAuth }));
vi.mock('../../hooks/useRealTimeUpdates', () => ({
  useRealTimeWithFallback: () => ({ isConnected: true, connectionType: 'websocket' }),
  useRealTimeUpdates: () => ({ isConnected: true }),
  usePollingUpdates: () => ({ isConnected: true }),
  default: () => ({ isConnected: true }),
}));
vi.mock('../../components/common/LanguageSwitcher', () => ({ default: () => <div data-testid="lang-switcher" /> }));
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, opts) => (typeof opts === 'string' ? opts : opts?.defaultValue) || key }),
}));

// Review gaming F10: on a shared PC the next person must not read the last admin's cached pay.
it('clears every cached query before it logs out', async () => {
  const queryClient = new QueryClient();
  queryClient.setQueryData(['salesPay', 'periods'], { pending_penalty_count: 1 });
  queryClient.setQueryData(['salesPay', 'statement', '2026-10', 41], { summary: { total: 3529231 } });
  const clear = vi.spyOn(queryClient, 'clear');
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/dashboard']}>
        <AdminLayout><div>content</div></AdminLayout>
      </MemoryRouter>
    </QueryClientProvider>,
  );

  fireEvent.click(screen.getByText(/Ada/));
  fireEvent.click(await screen.findByText('ui.user_menu.logout'));

  await waitFor(() => expect(mockAuth.logout).toHaveBeenCalledTimes(1));
  expect(clear).toHaveBeenCalledTimes(1);
  expect(clear.mock.invocationCallOrder[0]).toBeLessThan(mockAuth.logout.mock.invocationCallOrder[0]);
  expect(queryClient.getQueryData(['salesPay', 'periods'])).toBeUndefined();
  expect(queryClient.getQueryData(['salesPay', 'statement', '2026-10', 41])).toBeUndefined();
});
