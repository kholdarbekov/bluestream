import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { describe, it, expect, vi } from 'vitest';

vi.mock('../components/CustomerMap', () => ({
  default: ({ onViewUser }) => (
    <button onClick={() => onViewUser(123)}>mock-view-profile</button>
  ),
}));
// Partial mock with a Proxy fallback: getUsers/getUserDetails are assertable;
// every other adminService method handleViewUser triggers resolves harmlessly.
vi.mock('../services/adminService', () => {
  const explicit = {
    getUsers: vi.fn().mockResolvedValue({ data: { items: [] }, meta: { total: 0 } }),
    getUserDetails: vi.fn().mockResolvedValue({
      data: { user: { id: 123, role: 'customer', first_name: 'Deep', last_name: 'Link' } },
    }),
  };
  return {
    default: new Proxy(explicit, {
      // eslint-disable-next-line security/detect-object-injection
      get: (target, prop) => (prop in target ? target[prop] : vi.fn().mockResolvedValue({ data: {} })),
    }),
  };
});
// staffService is imported by Users.js; any method resolves harmlessly.
vi.mock('../services/staffService', () => ({
  default: new Proxy({}, { get: () => vi.fn().mockResolvedValue({ data: {} }) }),
}));

import { MemoryRouter } from 'react-router-dom';
import adminService from '../services/adminService';
import Users from './Users';

function renderUsers() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><Users /></MemoryRouter>
    </QueryClientProvider>
  );
}

describe('Users page Map tab', () => {
  it('shows a Map tab and opens the detail modal via onViewUser', async () => {
    renderUsers();
    fireEvent.click(screen.getByText(/^Map$/));
    fireEvent.click(await screen.findByText('mock-view-profile'));
    await waitFor(() => expect(adminService.getUserDetails).toHaveBeenCalledWith(123));
  });
});
