import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import Notifications from '../../pages/Notifications';
import adminService from '../../services/adminService';

vi.mock('../../services/adminService', () => ({
  __esModule: true,
  default: {
    getNotificationCampaigns: vi.fn(),
    getNotificationTemplates: vi.fn(),
    getNotificationCampaign: vi.fn(),
    getNotificationTemplate: vi.fn(),
    getNotificationTemplateTypes: vi.fn(),
    getNotificationTemplateChannels: vi.fn(),
    getNotificationCampaignSegments: vi.fn(),
    previewNotificationTemplate: vi.fn(),
    testSendNotificationTemplate: vi.fn(),
    createNotificationCampaign: vi.fn(),
    updateNotificationCampaign: vi.fn(),
    sendNotificationCampaign: vi.fn(),
    deleteNotificationCampaign: vi.fn(),
    duplicateNotificationCampaign: vi.fn(),
    cancelNotificationCampaign: vi.fn(),
    createNotificationTemplate: vi.fn(),
    updateNotificationTemplate: vi.fn(),
  },
}));

vi.mock('../../utils/exportUtils', () => ({
  __esModule: true,
  default: {
    exportNotificationCampaigns: vi.fn(),
  },
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, opts) => (opts && opts.defaultValue) || key }),
}));

const createWrapper = () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
};

describe('Notifications page', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    adminService.getNotificationCampaigns.mockResolvedValue({
      campaigns: [{
        id: 1,
        name: 'Retention Push',
        notification_type: 'delivery_reminder',
        category: 'transactional',
        channel: 'telegram',
        recipient_count: 100,
        sent_count: 50,
        status: 'draft',
        scheduled_at: null,
      }],
      pagination: { total: 1 },
    });
    adminService.getNotificationTemplates.mockResolvedValue({ templates: [], pagination: { total: 0 } });
    adminService.getNotificationTemplateTypes.mockResolvedValue([]);
    adminService.getNotificationTemplateChannels.mockResolvedValue([
      { value: 'telegram', label: 'Telegram', available: true },
    ]);
    adminService.getNotificationCampaignSegments.mockResolvedValue([]);
  });

  it('renders the Campaigns tab title and a campaign row using the translated (defaultValue) text', async () => {
    render(<Notifications />, { wrapper: createWrapper() });
    expect(await screen.findByText('Total Campaigns')).toBeInTheDocument();
    expect(await screen.findByText('Retention Push')).toBeInTheDocument();
    expect(screen.getAllByText('Actions').length).toBeGreaterThan(0);
  });

  // The wire contract the backend integration tests replay: the picked moment travels as the exact
  // `toISOString()` string ("...T09:00:00.000Z"), never as local wall-clock digits.
  it('sends the picked schedule as the exact toISOString() instant when a draft is saved', async () => {
    adminService.getNotificationCampaign.mockResolvedValue({
      id: 1,
      name: 'Retention Push',
      template_id: null,
      notification_type: 'delivery_reminder',
      channel: 'telegram',
      subject: '',
      content: 'Hi',
      target_audience: 'all_customers',
      target_segment_id: null,
      specific_user_ids: [],
      priority: 'normal',
      scheduled_at: '2026-11-30T14:00:00+05:00',
    });
    adminService.updateNotificationCampaign.mockResolvedValue({ id: 1 });

    render(<Notifications />, { wrapper: createWrapper() });
    await screen.findByText('Retention Push');
    const row = screen.getByText('Retention Push').closest('tr');
    fireEvent.click(row.querySelector('button'));
    fireEvent.click(await screen.findByText('Edit'));
    fireEvent.click(await screen.findByRole('button', { name: /save draft/i }));

    await waitFor(() => expect(adminService.updateNotificationCampaign).toHaveBeenCalledTimes(1));
    const [campaignId, payload] = adminService.updateNotificationCampaign.mock.calls[0];
    expect(campaignId).toBe(1);
    expect(payload.scheduled_at).toBe('2026-11-30T09:00:00.000Z');
    expect(payload.name).toBe('Retention Push');
  });
});
