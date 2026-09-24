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
let adminService;

beforeAll(async () => {
  // api.js fetches a CSRF token with the bare axios client the moment it is imported; answer it
  // here rather than over the network. Hence the dynamic imports: a static one runs first.
  vi.spyOn(axios, 'get').mockResolvedValue({ data: { csrf_token: 'test-csrf' } });
  api = (await import('../../services/api')).default;
  salesService = (await import('../../services/salesService')).default;
  adminService = (await import('../../services/adminService')).default;
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

  // A caller that explains a refusal itself, in the admin's language, names its code on the
  // request (RescheduleOrderModal names its fence codes). The interceptor must not toast that code
  // as well (one refusal, one message), and must keep toasting everything the request did not name.
  it('a refusal whose code the request handles rejects to its caller without a toast', async () => {
    api.defaults.adapter = failWith(400, {
      success: false,
      message: 'Validation failed',
      errors: ['delivery_date 2026-10-09 is after the contract end 2026-09-30'],
      data: { error_code: 'ORDER_RESCHEDULE_PAST_CONTRACT_END' },
    });

    await expect(
      adminService.rescheduleOrder(321, { delivery_date: '2026-10-09' }, {
        handledErrorCodes: ['ORDER_RESCHEDULE_PAST_CONTRACT_END'],
      }),
    ).rejects.toMatchObject({
      response: { status: 400, data: { data: { error_code: 'ORDER_RESCHEDULE_PAST_CONTRACT_END' } } },
    });

    expect(toast.error).not.toHaveBeenCalled();
  });

  const NAMED = { handledErrorCodes: ['ORDER_NOT_RESCHEDULABLE'] };
  const SENTENCE = 'Order ORD-321 is delivered; it can no longer be rescheduled.';

  it.each([
    ['a code the request does not name', 400, 'DELIVERY_NOT_RESCHEDULABLE', NAMED, SENTENCE],
    ['a request that names no codes', 400, 'ORDER_NOT_RESCHEDULABLE', undefined, SENTENCE],
    // Status branches come first: naming a code never hides a permission or server failure.
    ['a 403 whose code the request names', 403, 'ORDER_NOT_RESCHEDULABLE', NAMED, 'Access denied. Insufficient permissions.'],
    ['a 5xx whose code the request names', 500, 'ORDER_NOT_RESCHEDULABLE', NAMED, 'Server error. Please try again later.'],
  ])('%s is still toasted, once', async (_label, status, code, options, text) => {
    api.defaults.adapter = failWith(status, { success: false, message: SENTENCE, data: { error_code: code } });

    await expect(adminService.rescheduleOrder(321, { delivery_date: '2026-10-09' }, options))
      .rejects.toMatchObject({ response: { status } });

    expect(toast.error).toHaveBeenCalledTimes(1);
    expect(toast.error).toHaveBeenCalledWith(text);
  });
});
