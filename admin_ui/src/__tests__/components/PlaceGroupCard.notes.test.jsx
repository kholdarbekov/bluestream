import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import PlaceGroupCard from '../../components/PlaceGroupCard';
import { invalidatePlaceGroupQueries } from '../../components/placeGroupQueries';
import adminService from '../../services/adminService';
import { useAuthStore } from '../../stores/authStore';

vi.mock('../../services/adminService');

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

const detail = (notes, label = 'Office') => ({
  notes,
  place_group_id: 5,
  label,
  place_balance: 2,
  members: [],
  cod: { total_outstanding_amount: 0, active_cod_debt_count: 0, items: [] },
  removal_dissolves_group: false,
  events: [],
});

const renderCard = (notes, label) => {
  adminService.getPlaceGroup.mockResolvedValue({ data: detail(notes, label) });
  adminService.renamePlaceGroup.mockResolvedValue({ data: { place_group_id: 5 } });
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={queryClient}>
      <PlaceGroupCard groupId={5} />
    </QueryClientProvider>
  );
};

describe('PlaceGroupCard rename and the place note', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAuthStore.setState({ permissions: { can_manage_users: true } });
  });
  afterEach(() => useAuthStore.setState({ permissions: {} }));

  it('a changed note rides as the fourth argument, with the unchanged label kept (null)', async () => {
    const user = userEvent.setup();
    renderCard(null);
    await user.click(await screen.findByRole('button', { name: /^Rename$/ }));
    const noteBox = await screen.findByPlaceholderText('Place note (optional)');
    expect(noteBox).toHaveValue('');
    await user.type(noteBox, 'Gate code 12');
    await user.type(screen.getByPlaceholderText(/Reason/), 'reason');
    await user.click(screen.getByRole('button', { name: /^OK$/ }));
    await waitFor(() => expect(adminService.renamePlaceGroup).toHaveBeenCalled());
    expect(adminService.renamePlaceGroup.mock.calls[0]).toEqual([5, null, 'reason', { notes: 'Gate code 12' }]);
  });

  it('an unlabeled place takes a note alone: PATCH {label: null, reason, notes}', async () => {
    const user = userEvent.setup();
    renderCard(null, null);
    await user.click(await screen.findByRole('button', { name: /^Rename$/ }));
    expect(screen.getByPlaceholderText(/Label/)).toHaveValue('');
    await user.type(await screen.findByPlaceholderText('Place note (optional)'), 'Gate code 12');
    await user.type(screen.getByPlaceholderText(/Reason/), 'gate code from the driver');
    await user.click(screen.getByRole('button', { name: /^OK$/ }));
    await waitFor(() => expect(adminService.renamePlaceGroup).toHaveBeenCalled());
    expect(adminService.renamePlaceGroup.mock.calls).toEqual([
      [5, null, 'gate code from the driver', { notes: 'Gate code 12' }],
    ]);
  });

  it('shows the place note read-only on the card, to a viewer without write access too', async () => {
    useAuthStore.setState({ permissions: {} });
    renderCard('Gate code 12, side door');
    expect(await screen.findByText('Gate code 12, side door')).toBeInTheDocument();
    expect(screen.getByText('Note')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Rename$/ })).toBeNull();
  });

  it('shows no note line when the place has none', async () => {
    renderCard(null);
    await screen.findByRole('button', { name: /^Rename$/ });
    expect(screen.queryByText('Note')).toBeNull();
  });

  it('only the label changed stays a three-argument call', async () => {
    const user = userEvent.setup();
    renderCard('Gate code 12');
    await user.click(await screen.findByRole('button', { name: /^Rename$/ }));
    const noteBox = await screen.findByPlaceholderText('Place note (optional)');
    expect(noteBox).toHaveValue('Gate code 12');
    const labelBox = screen.getByPlaceholderText(/Label/);
    await user.clear(labelBox);
    await user.type(labelBox, 'Office 2');
    await user.type(screen.getByPlaceholderText(/Reason/), 'reason');
    await user.click(screen.getByRole('button', { name: /^OK$/ }));
    await waitFor(() => expect(adminService.renamePlaceGroup).toHaveBeenCalled());
    expect(adminService.renamePlaceGroup.mock.calls[0]).toEqual([5, 'Office 2', 'reason']);
  });
});

describe('invalidatePlaceGroupQueries', () => {
  it('also refreshes the shared-place case queue', () => {
    const queryClient = { invalidateQueries: vi.fn() };
    invalidatePlaceGroupQueries(queryClient);
    expect(queryClient.invalidateQueries).toHaveBeenCalledWith({ queryKey: ['sharedPlaceCases'] });
  });
});
