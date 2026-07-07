import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import BottleTracking from '../../pages/BottleTracking';
import adminService from '../../services/adminService';

vi.mock('../../services/adminService', () => ({
  __esModule: true,
  default: {
    getBottleDashboard: vi.fn(),
    getBottleBalances: vi.fn(),
    getBottleLedger: vi.fn(),
    getBottleFines: vi.fn(),
    getBottleSessions: vi.fn(),
    getBottleTransfers: vi.fn(),
  },
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, opts) => (opts && opts.defaultValue) || key }),
}));

const createWrapper = () => {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
};

describe('BottleTracking pagination resets on filter change', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    adminService.getBottleDashboard.mockResolvedValue({ data: {} });
    adminService.getBottleBalances.mockResolvedValue({ data: { items: [], total: 0 } });
    // 25 rows at the default page size (20) => a real page 2 exists.
    adminService.getBottleSessions.mockResolvedValue({ data: { items: [], total: 25 } });
    adminService.getBottleTransfers.mockResolvedValue({ data: { items: [], total: 25 } });
  });

  it('resets session pagination to page 1 when the status filter changes', async () => {
    render(<BottleTracking />, { wrapper: createWrapper() });

    fireEvent.click(screen.getByText('Driver Sessions'));

    await waitFor(() => {
      expect(adminService.getBottleSessions).toHaveBeenCalledWith(
        expect.objectContaining({ page: 1 })
      );
    });

    // Move to page 2.
    fireEvent.click(await screen.findByTitle('2'));
    await waitFor(() => {
      expect(adminService.getBottleSessions).toHaveBeenCalledWith(
        expect.objectContaining({ page: 2 })
      );
    });

    // Change the status filter while on page 2 — pagination must reset to page 1,
    // otherwise the table renders empty (page 2 of a now-smaller filtered result set).
    const statusSelector = document.querySelector('.ant-select-selector');
    fireEvent.mouseDown(statusSelector);
    fireEvent.click(await screen.findByTitle('Closed'));

    await waitFor(() => {
      expect(adminService.getBottleSessions).toHaveBeenCalledWith(
        expect.objectContaining({ page: 1, status: 'closed' })
      );
    });
  });

  it('resets session pagination to page 1 when "Discrepancies only" is toggled', async () => {
    render(<BottleTracking />, { wrapper: createWrapper() });

    fireEvent.click(screen.getByText('Driver Sessions'));
    await waitFor(() => {
      expect(adminService.getBottleSessions).toHaveBeenCalledWith(
        expect.objectContaining({ page: 1 })
      );
    });

    fireEvent.click(await screen.findByTitle('2'));
    await waitFor(() => {
      expect(adminService.getBottleSessions).toHaveBeenCalledWith(
        expect.objectContaining({ page: 2 })
      );
    });

    fireEvent.click(screen.getByText('Discrepancies only'));

    await waitFor(() => {
      expect(adminService.getBottleSessions).toHaveBeenCalledWith(
        expect.objectContaining({ page: 1, only_discrepancies: true })
      );
    });
  });

  it('resets transfer pagination to page 1 when the status filter changes', async () => {
    render(<BottleTracking />, { wrapper: createWrapper() });

    fireEvent.click(screen.getByText('Bottle Transfers'));
    await waitFor(() => {
      expect(adminService.getBottleTransfers).toHaveBeenCalledWith(
        expect.objectContaining({ page: 1 })
      );
    });

    fireEvent.click(await screen.findByTitle('2'));
    await waitFor(() => {
      expect(adminService.getBottleTransfers).toHaveBeenCalledWith(
        expect.objectContaining({ page: 2 })
      );
    });

    const statusSelector = document.querySelector('.ant-select-selector');
    fireEvent.mouseDown(statusSelector);
    fireEvent.click(await screen.findByTitle('Confirmed'));

    await waitFor(() => {
      expect(adminService.getBottleTransfers).toHaveBeenCalledWith(
        expect.objectContaining({ page: 1, status: 'confirmed' })
      );
    });
  });
});
