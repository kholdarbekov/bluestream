import React from 'react';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import Translations from '../../pages/Translations';
import adminService from '../../services/adminService';

vi.mock('../../services/adminService', () => ({
  __esModule: true,
  default: {
    getTranslatableEntities: vi.fn(),
    getTranslations: vi.fn(),
    getTranslationCompletion: vi.fn(),
    getMissingTranslations: vi.fn(),
    createTranslation: vi.fn(),
    updateTranslation: vi.fn(),
    deleteTranslation: vi.fn(),
    syncEntityTranslations: vi.fn(),
    importTranslations: vi.fn(),
    exportTranslations: vi.fn(),
  },
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, opts) => (opts && opts.defaultValue) || key }),
}));

const createWrapper = () => {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
};

describe('Translations page', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    adminService.getTranslatableEntities.mockResolvedValue({ data: { entities: [] } });
    adminService.getTranslations.mockResolvedValue({
      data: {
        translations: [{
          id: 1, category: 'ui', key: 'ui.nav.home', language: 'en', value: 'Home', is_active: true,
        }],
      },
      meta: { total: 1 },
    });
    adminService.getTranslationCompletion.mockResolvedValue({
      data: {
        overall_stats: {
          overall_completion_percentage: 80,
          language_breakdown: {
            en: { percentage: 100, translated: 10, total: 10 },
            uz: { percentage: 80, translated: 8, total: 10 },
            ru: { percentage: 60, translated: 6, total: 10 },
          },
        },
        completion_stats: [],
      },
    });
    adminService.getMissingTranslations.mockResolvedValue({
      data: { missing_translations: [], summary: { total_missing: 0, high_priority: 0, medium_priority: 0 } },
      meta: { total: 0 },
    });
  });

  it('renders the page title and a translation row using the translated (defaultValue) text', async () => {
    render(<Translations />, { wrapper: createWrapper() });
    expect(await screen.findByText('Translation Management')).toBeInTheDocument();
    expect(await screen.findByText('ui.nav.home')).toBeInTheDocument();
    expect(screen.getAllByText('Actions').length).toBeGreaterThan(0);
  });
});
