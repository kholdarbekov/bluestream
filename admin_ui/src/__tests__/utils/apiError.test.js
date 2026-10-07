import { apiErrorCode } from '../../utils/apiError';

// §6.5 (review ruling T13-R1): ONE reader of a refusal's `error_code`. The interceptor decides
// "the request named this code, so no toast" with it, and every caller that explains a refusal
// itself (the pay modals, the order-approval modal, the try-out Convert alert) decides "this is
// mine to show" with it. Two copies of the read could drift apart, and a refusal would then be
// silenced by the interceptor and ignored by its caller: an admin who sees nothing at all.
describe('apiErrorCode', () => {
  const failure = (status, data) => ({ response: { status, data } });

  it('reads envelope (a): `handle_api_exception` puts the code at the top of the body', () => {
    expect(apiErrorCode(failure(403, { success: false, error_code: 'SALES_PAY_SELF_DECISION' })))
      .toBe('SALES_PAY_SELF_DECISION');
  });

  it('reads envelope (b): older routes answer the code under `data`', () => {
    expect(apiErrorCode(failure(409, { success: false, data: { error_code: 'SALES_ORDER_APPROVAL_NOT_PENDING' } })))
      .toBe('SALES_ORDER_APPROVAL_NOT_PENDING');
  });

  it('prefers the code under `data` when a body carries both', () => {
    expect(apiErrorCode(failure(400, { error_code: 'VALIDATION_ERROR', data: { error_code: 'ADMIN_REASON_REQUIRED' } })))
      .toBe('ADMIN_REASON_REQUIRED');
  });

  it.each([
    ['a network failure (no response)', { message: 'Network Error' }],
    ['a body with no code', failure(500, { success: false, message: 'boom' })],
    ['a blob body', failure(404, new Blob(['{}']))],
    ['nothing at all', undefined],
  ])('is undefined for %s', (_label, error) => {
    expect(apiErrorCode(error)).toBeUndefined();
  });
});

describe('the error-code read has one expression', () => {
  // Every admin UI source file (tests excluded), as text: Vite's own raw glob, so nothing here
  // builds a filesystem path at run time.
  const SOURCES = import.meta.glob(
    ['../../**/*.{js,jsx}', '!../../**/__tests__/**', '!../../**/*.test.{js,jsx}'],
    { query: '?raw', import: 'default', eager: true },
  );
  const HELPER = '../../utils/apiError.js';
  // The two-envelope read spelled out by hand: `…data?.data?.error_code ?? …error_code`.
  const HAND_COPY = /data\??\.data\??\.error_code\s*\?\?/;

  it('reads the admin UI sources', () => {
    expect(Object.keys(SOURCES)).toContain(HELPER);
    expect(Object.keys(SOURCES).length).toBeGreaterThan(100);
  });

  it('no source file outside utils/apiError.js reads both envelopes itself', () => {
    const copies = Object.entries(SOURCES)
      .filter(([file, text]) => file !== HELPER && HAND_COPY.test(text))
      .map(([file]) => file);
    expect(copies).toEqual([]);
  });

  // Final-review M8: not only hand copies of the two-envelope read. ANY `.error_code` property
  // read outside the helper picks one envelope, so a route that moves to `handle_api_exception`
  // (a top-level `error_code`) would silently miss its translated sentence. Comment lines are not
  // reads. A read that is not a refusal's code goes in ALLOWED with its reason; none is today.
  const ALLOWED = new Map();
  const ERROR_CODE_READ = /\.error_code\b/;
  const isComment = (line) => /^\s*(\/\/|\/?\*)/.test(line);

  it('no source file outside utils/apiError.js reads .error_code itself', () => {
    const reads = Object.entries(SOURCES)
      .filter(([file]) => file !== HELPER && !ALLOWED.has(file))
      .flatMap(([file, text]) => text.split('\n')
        .map((line, index) => [file, index + 1, line])
        .filter(([, , line]) => !isComment(line) && ERROR_CODE_READ.test(line)))
      .map(([file, line]) => `${file}:${line}`);
    expect(reads).toEqual([]);
  });

  it.each([
    '../../services/api.js',
    '../../components/sales/pay/payErrors.js',
    '../../pages/Tryouts.js',
    '../../pages/SalesOrderApprovals.js',
    '../../pages/Orders.js',
    '../../pages/Delivery.js',
    '../../pages/LoyaltyPrograms.js',
    '../../components/orders/RescheduleOrderModal.jsx',
    '../../components/placeGroupCopy.js',
  ])('%s reads the code through apiErrorCode', (file) => {
    expect(SOURCES[file]).toMatch(/\bapiErrorCode\(/);
  });
});
