import axios, { AxiosError } from 'axios';
import toast from 'react-hot-toast';

vi.mock('react-hot-toast', () => ({
  __esModule: true,
  default: { error: vi.fn(), success: vi.fn() },
}));

// The REAL interceptor, driven through the real service methods the pages call. Only the wire is
// stubbed: `api.defaults.adapter` stands in for the HTTP round trip.
let api;
let salesService;

beforeAll(async () => {
  // api.js fetches a CSRF token with the bare axios client the moment it is imported; answer it
  // here rather than over the network. Hence the dynamic imports: a static one runs first.
  vi.spyOn(axios, 'get').mockResolvedValue({ data: { csrf_token: 'test-csrf' } });
  api = (await import('../../services/api')).default;
  salesService = (await import('../../services/salesService')).default;
});

const failWith = (status, data) => (config) => Promise.reject(
  new AxiosError(`Request failed with status code ${status}`, AxiosError.ERR_BAD_REQUEST, config, null, {
    status, statusText: '', data, headers: {}, config,
  }),
);

describe('api response interceptor', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  // D27's accepted consequence: a replaced staff bot or a Telegram outage turns every photo on an
  // outlet into a 404, and each `VisitPhotoThumb` draws its own "Photo unavailable" tile. A toast per
  // tile on top of that was a storm of "Request failed with status code 404" (the body is a Blob).
  it.each([404, 502])('a failed blob request (%i) rejects to its caller without a toast', async (status) => {
    const body = new Blob([JSON.stringify({ success: false, error_code: 'SALES_PHOTO_UNAVAILABLE' })], {
      type: 'application/json',
    });
    api.defaults.adapter = failWith(status, body);

    await expect(salesService.getVisitPhotoBlob(31)).rejects.toMatchObject({ response: { status } });

    expect(toast.error).not.toHaveBeenCalled();
  });

  it('a failed JSON request still toasts the server message', async () => {
    api.defaults.adapter = failWith(404, { success: false, message: 'Outlet not found', error_code: 'SALES_OUTLET_NOT_FOUND' });

    await expect(salesService.getOutlet(999)).rejects.toMatchObject({ response: { status: 404 } });

    expect(toast.error).toHaveBeenCalledTimes(1);
    expect(toast.error).toHaveBeenCalledWith('Outlet not found');
  });
});
