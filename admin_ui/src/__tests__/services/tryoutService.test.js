import tryoutService from '../../services/tryoutService';
import api from '../../services/api';

vi.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
  },
}));

describe('TryoutService', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('fetches try-outs with params', async () => {
    const mockData = {
      data: {
        items: [{ id: 1, tryout_number: 'TRY_000001_26' }],
        total: 1,
      },
    };

    api.get.mockResolvedValue({ data: mockData });

    const result = await tryoutService.getTryouts({ status: 'active' });

    expect(api.get).toHaveBeenCalledWith('/admin/tryouts', {
      params: { status: 'active' },
    });
    expect(result).toEqual(mockData.data);
  });

  it('updates a try-out and unwraps the payload', async () => {
    const mockResponse = {
      data: {
        tryout: { id: 4, status: 'closed' },
      },
    };

    api.put.mockResolvedValue({ data: mockResponse });

    const result = await tryoutService.updateTryout(4, { status: 'closed' });

    expect(api.put).toHaveBeenCalledWith('/admin/tryouts/4', { status: 'closed' });
    expect(result).toEqual({ id: 4, status: 'closed' });
  });

  it('converts a try-out and returns conversion metadata', async () => {
    api.post.mockResolvedValue({
      data: {
        data: {
          tryout: { id: 7, outcome: 'converted' },
          conversion: {
            action: 'linked_existing_user',
            user: { id: 42, full_name: 'Existing User', phone: '+998901112233' },
          },
        },
      },
    });

    const result = await tryoutService.convertTryout(7);

    expect(api.post).toHaveBeenCalledWith('/admin/tryouts/7/convert', {});
    expect(result).toEqual({
      tryout: { id: 7, outcome: 'converted' },
      conversion: {
        action: 'linked_existing_user',
        user: { id: 42, full_name: 'Existing User', phone: '+998901112233' },
      },
    });
  });

  it('exports try-outs as a blob response', async () => {
    const csvBlob = new Blob(['id,tryout_number'], { type: 'text/csv' });
    api.get.mockResolvedValue({ data: csvBlob });

    const result = await tryoutService.exportTryouts({ pickup_state: 'overdue' });

    expect(api.get).toHaveBeenCalledWith('/admin/tryouts/export', {
      params: { pickup_state: 'overdue' },
      responseType: 'blob',
    });
    expect(result).toBe(csvBlob);
  });
});
