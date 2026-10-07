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
let salesPayService;

beforeAll(async () => {
  // api.js fetches a CSRF token with the bare axios client the moment it is imported; answer it
  // here rather than over the network. Hence the dynamic imports: a static one runs first.
  vi.spyOn(axios, 'get').mockResolvedValue({ data: { csrf_token: 'test-csrf' } });
  api = (await import('../../services/api')).default;
  salesService = (await import('../../services/salesService')).default;
  adminService = (await import('../../services/adminService')).default;
  salesPayService = (await import('../../services/salesPayService')).default;
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
    // A 403 or a 5xx the request did NOT name keeps its generic status toast: naming codes
    // never hides a permission or server failure the caller did not expect.
    ['a 403 whose code the request does not name', 403, 'SOME_OTHER_CODE', NAMED, 'Access denied. Insufficient permissions.'],
    ['a 5xx whose code the request does not name', 500, 'SOME_OTHER_CODE', NAMED, 'Server error. Please try again later.'],
  ])('%s is still toasted, once', async (_label, status, code, options, text) => {
    api.defaults.adapter = failWith(status, { success: false, message: SENTENCE, data: { error_code: code } });

    await expect(adminService.rescheduleOrder(321, { delivery_date: '2026-10-09' }, options))
      .rejects.toMatchObject({ response: { status } });

    expect(toast.error).toHaveBeenCalledTimes(1);
    expect(toast.error).toHaveBeenCalledWith(text);
  });

  // §6.5 (review SSOT I4): a request that names a code explains that refusal itself, so the
  // branch runs BEFORE the status branches. `handle_api_exception` answers envelope (a), whose
  // `error_code` sits at the top of the body rather than under `data`, and a self-decision or
  // self-approval refusal is a 403 — the old order toasted "Access denied" over the modal's own
  // sentence.
  it('a 403 in envelope (a) whose code the pay request names rejects without a toast', async () => {
    api.defaults.adapter = failWith(403, {
      success: false,
      error: 'forbidden',
      message: 'You cannot decide about your own pay',
      error_code: 'SALES_PAY_SELF_DECISION',
      details: { agent_user_id: 41 },
    });

    await expect(salesPayService.proposePenalty({ agent_user_id: 41 })).rejects.toMatchObject({
      response: { status: 403, data: { error_code: 'SALES_PAY_SELF_DECISION' } },
    });

    expect(toast.error).not.toHaveBeenCalled();
  });

  it('a 409 in envelope (a) whose code the pay request names rejects without a toast', async () => {
    api.defaults.adapter = failWith(409, {
      success: false,
      message: 'Pay terms are missing',
      error_code: 'SALES_PAY_TERMS_MISSING',
      details: { agents: [44] },
    });

    await expect(salesPayService.closePeriod('2026-10')).rejects.toMatchObject({ response: { status: 409 } });

    expect(toast.error).not.toHaveBeenCalled();
  });

  it('a 403 the pay request did not name still toasts "Access denied"', async () => {
    api.defaults.adapter = failWith(403, { success: false, message: 'Admin only', error_code: 'ADMIN_ONLY' });

    await expect(salesPayService.closePeriod('2026-10')).rejects.toMatchObject({ response: { status: 403 } });

    expect(toast.error).toHaveBeenCalledTimes(1);
    expect(toast.error).toHaveBeenCalledWith('Access denied. Insufficient permissions.');
  });

  it('a pay read names no codes, so its refusal is toasted as before', async () => {
    api.defaults.adapter = failWith(404, { success: false, message: 'Pay period not found', error_code: 'SALES_PAY_NOT_FOUND' });

    await expect(salesPayService.getPeriod('2026-01')).rejects.toMatchObject({ response: { status: 404 } });

    expect(toast.error).toHaveBeenCalledTimes(1);
    expect(toast.error).toHaveBeenCalledWith('Pay period not found');
  });
});
