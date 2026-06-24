// admin_ui/src/__tests__/pages/SupportInbox.test.js
import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import SupportInbox from '../../pages/SupportInbox';
import adminService from '../../services/adminService';

vi.mock('../../services/adminService', () => ({
  __esModule: true,
  default: {
    getSupportConversations: vi.fn(),
    getSupportThread: vi.fn(),
    markSupportRead: vi.fn(),
    replySupportMessage: vi.fn(),
    startSupportConversation: vi.fn(),
    getUsers: vi.fn(),
  },
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, opts) => (opts && opts.defaultValue) || (typeof opts === 'string' ? opts : key) }),
}));

const wrapper = () => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
};

describe('SupportInbox page', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    adminService.getSupportConversations.mockResolvedValue({
      data: {
        items: [
          { id: 1, user: { id: 9, name: 'Ann Lee', phone: '+99890', telegram_username: 'ann' },
            last_message_preview: 'I need water', unread_count: 2, last_message_at: '2026-06-24T10:00:00Z' },
        ],
        total: 1, total_unread: 2, page: 1, per_page: 20,
      },
    });
    adminService.getSupportThread.mockResolvedValue({
      data: {
        conversation: { id: 1, user: { id: 9, name: 'Ann Lee' } },
        items: [
          { id: 11, direction: 'inbound', content: 'I need water', created_at: '2026-06-24T10:00:00Z', is_read: false },
        ],
        total: 1, page: 1, per_page: 50,
      },
    });
    adminService.markSupportRead.mockResolvedValue({ data: { marked_read: 2 } });
    adminService.replySupportMessage.mockResolvedValue({ data: { message: { id: 12, direction: 'outbound', content: 'Hello', delivery_status: 'sent' }, delivery: { success: true } } });
  });

  it('lists conversations and loads a thread on click', async () => {
    render(<SupportInbox />, { wrapper: wrapper() });
    expect(await screen.findByText('Ann Lee')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Ann Lee'));
    expect(await screen.findByText('I need water')).toBeInTheDocument();
    await waitFor(() => expect(adminService.markSupportRead).toHaveBeenCalledWith(1));
  });

  it('sends a reply with the typed content', async () => {
    render(<SupportInbox />, { wrapper: wrapper() });
    fireEvent.click(await screen.findByText('Ann Lee'));
    const box = await screen.findByPlaceholderText('Type a message…');
    fireEvent.change(box, { target: { value: 'Hello Ann' } });
    fireEvent.click(screen.getByRole('button', { name: /Send/i }));
    await waitFor(() => expect(adminService.replySupportMessage).toHaveBeenCalledWith(1, 'Hello Ann'));
  });
});
