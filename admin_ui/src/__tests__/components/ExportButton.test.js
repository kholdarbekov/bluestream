import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import ExportButton from '../../components/common/ExportButton';

const exportMocks = vi.hoisted(() => ({
  exportToExcel: vi.fn(),
  exportToCSV: vi.fn(),
  exportToPDF: vi.fn(),
  exportData: vi.fn(),
}));

vi.mock('../../utils/exportUtils', () => ({
  __esModule: true,
  default: exportMocks,
}));

// antd's Dropdown only renders menu items in a portal on hover/click. Replace
// with a simple inline list so tests can click items directly without dealing
// with the portal/positioning internals.
vi.mock('antd', async () => {
  const actual = await vi.importActual('antd');
  return {
    ...actual,
    Dropdown: ({ menu, children, disabled }) => (
      <div data-testid="dropdown-stub">
        {children}
        {!disabled && (
          <div role="menu">
            {menu?.items
              ?.filter((item) => item && !item.disabled)
              .map((item) => (
                <button
                  type="button"
                  key={item.key}
                  onClick={item.onClick}
                >
                  {typeof item.label === 'string' ? item.label : item.key}
                </button>
              ))}
          </div>
        )}
      </div>
    ),
    message: {
      ...actual.message,
      success: vi.fn(),
      error: vi.fn(),
    },
  };
});

describe('ExportButton', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('exports inline data via exportToExcel when "excel" is chosen', async () => {
    exportMocks.exportToExcel.mockResolvedValueOnce({ success: true, message: 'ok' });
    const data = [{ a: 1 }];
    const user = userEvent.setup();
    render(<ExportButton data={data} filename="orders" />);

    await user.click(screen.getByText(/Excel/));
    await waitFor(() => {
      expect(exportMocks.exportToExcel).toHaveBeenCalledTimes(1);
    });
    expect(exportMocks.exportToExcel).toHaveBeenCalledWith(data, 'orders');
  });

  it('exports inline data via exportToCSV when "csv" is chosen', async () => {
    exportMocks.exportToCSV.mockReturnValueOnce({ success: true, message: 'ok' });
    const user = userEvent.setup();
    render(<ExportButton data={[{ x: 1 }]} filename="report" />);

    await user.click(screen.getByText(/CSV/));
    await waitFor(() => {
      expect(exportMocks.exportToCSV).toHaveBeenCalledTimes(1);
    });
    expect(exportMocks.exportToCSV).toHaveBeenCalledWith([{ x: 1 }], 'report');
  });

  it('falls back to API exportData when no inline data is provided', async () => {
    exportMocks.exportData.mockResolvedValueOnce({ success: true, message: 'ok' });
    const user = userEvent.setup();
    render(<ExportButton type="orders" filters={{ status: 'paid' }} />);

    await user.click(screen.getByText(/PDF/));
    await waitFor(() => {
      expect(exportMocks.exportData).toHaveBeenCalledTimes(1);
    });
    expect(exportMocks.exportData).toHaveBeenCalledWith('orders', { status: 'paid' }, 'pdf');
  });

  it('does not render menu items when disabled', () => {
    render(<ExportButton type="orders" disabled />);
    // Disabled passes through to the stubbed Dropdown which suppresses items.
    expect(screen.queryByText(/Excel/)).not.toBeInTheDocument();
  });
});
