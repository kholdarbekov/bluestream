// base.js on every storefront page (spec 2026-10-27 §4.6; step 4 Task 9).
//
// The SHIPPED _page-data.js and base.js run in jsdom as the page loads them; only `fetch` is replaced, and it
// answers each request in turn from the list a case gives. The page's cookies are what the backend sets:
// the httpOnly JWT cookies never reach script, so the page holds only the readable CSRF values.
// - apiRequest renews an expired session through `POST /auth/refresh-cookie` (the httpOnly refresh cookie,
//   with `csrf_refresh_token` in X-CSRF-TOKEN), then repeats the request with the CSRF value the renewal
//   handed back; it never sends an Authorization header. It does so only on a 401 whose body the backend marks
//   `reauth: true` (the JWT layer's answers, business_app/__init__.py); any other 401, such as a wrong password,
//   goes back to the caller unchanged and unread, sent once.
// - A page reached through the same-site hop (business_app/utils/page_auth.py HOP_MARKER) drops the marker
//   from the address bar and keeps the rest of the address.
// - Log out proves the login with the refresh cookie's CSRF value, deletes the readable CSRF halves, and treats
//   only a successful call as signed out: otherwise the browser goes to the server's sign-out page `/logout`,
//   which clears the httpOnly cookies script cannot reach. jsdom does not navigate, so `navigations` records
//   where the page sent the browser (Location's own navigate step, not a stand-in for the script's calls).
// - The same-site hop page's script (same-site-hop.js) reloads the target the server wrote into a data
//   attribute, keeping the #fragment.
import fs from 'node:fs';
import path from 'node:path';
import { JSDOM } from 'jsdom';
import jsdomUtils from 'jsdom/lib/jsdom/living/generated/utils.js';

const REPO = path.resolve(process.cwd(), '..');
// Repo files named in this test (never user input).
// eslint-disable-next-line security/detect-non-literal-fs-filename
const read = (file) => fs.readFileSync(path.join(REPO, file), 'utf8');
const PAGE_DATA_JS = 'business_app/static/js/pages/_page-data.js';
const BASE_JS = 'business_app/static/js/pages/base.js';
const LOGIN_JS = 'business_app/static/js/pages/login.js';
const HOP_JS = 'business_app/static/js/pages/same-site-hop.js';
const BASE = {
  api_base_url: '/api/v1', current_user: null, is_authenticated: false, login_url: '/login', logout_url: '/logout', i18n: {},
};
// The backend's expired-token answer (business_app/__init__.py expired_token_loader): the login lapsed.
const LAPSED = { status: 401, body: { error: 'Token Expired', message: 'The token has expired.', reauth: true } };

function boot({
  search = '', cookies = [], answers = [], body = '', scripts = [PAGE_DATA_JS, BASE_JS], path = '/my-orders',
  htmlAttributes = '',
} = {}) {
  const dom = new JSDOM(
    `<!doctype html><html${htmlAttributes}><head></head><body><script type="application/json" id="base-page-data">${JSON.stringify(BASE)}</script>${body}</body></html>`,
    { url: `http://localhost${path}${search}`, runScripts: 'outside-only', pretendToBeVisual: true },
  );
  const { window } = dom;
  cookies.forEach((cookie) => { window.document.cookie = cookie; });
  const requests = [];
  // Each answer is a real Response: its body can be read once, as in a browser.
  window.fetch = async (url, options = {}) => {
    requests.push({ method: (options.method || 'GET').toUpperCase(), url, options });
    const answer = answers[requests.length - 1];
    if (!answer) return new Response(JSON.stringify({ success: false }), { status: 599 });
    if (answer.offline) throw new window.TypeError('Failed to fetch');
    if (answer.setCookie) window.document.cookie = answer.setCookie;
    return new Response(JSON.stringify(answer.body || {}), {
      status: answer.status, headers: { 'Content-Type': 'application/json' },
    });
  };
  const navigations = [];
  jsdomUtils.implForWrapper(window.location)._locationObjectNavigate = (url, { replacement = false } = {}) => {
    const query = url.query ? `?${url.query}` : '';
    const fragment = url.fragment ? `#${url.fragment}` : '';
    navigations.push({ to: `/${url.path.join('/')}${query}${fragment}`, replacement });
  };
  scripts.map(read).forEach((source) => window.eval(source));
  return { window, requests, navigations };
}

const sent = (requests) => requests.map((r) => `${r.method} ${r.url}`);

describe('apiRequest', () => {
  it('renews through the refresh-cookie route and repeats the request', async () => {
    const page = boot({
      cookies: ['csrf_access_token=csrf-access-value', 'csrf_refresh_token=csrf-refresh-value'],
      answers: [LAPSED, { status: 200 }, { status: 200, body: { success: true } }],
    });

    const response = await page.window.apiRequest('/orders/');

    expect(response.status).toBe(200);
    expect(sent(page.requests)).toEqual(['GET /api/v1/orders/', 'POST /api/v1/auth/refresh-cookie', 'GET /api/v1/orders/']);
    const renewal = page.requests[1].options;
    expect(renewal.headers['X-CSRF-TOKEN']).toBe('csrf-refresh-value');
    expect(renewal.credentials).toBe('include');
    expect(renewal.body).toBeUndefined();
  });

  it('never sends an Authorization header', async () => {
    const page = boot({
      cookies: ['csrf_access_token=csrf-access-value', 'csrf_refresh_token=csrf-refresh-value'],
      answers: [LAPSED, { status: 200 }, { status: 200 }],
    });
    page.window.localStorage.setItem('access_token', 'stale-token');

    await page.window.apiRequest('/cart/items', { method: 'POST', body: JSON.stringify({ product_id: 1, quantity: 1 }) });

    expect(page.requests).toHaveLength(3);
    page.requests.forEach(({ options }) => {
      const names = Object.keys(options.headers || {}).map((name) => name.toLowerCase());
      expect(names).not.toContain('authorization');
      expect(options.credentials).toBe('include');
    });
  });

  it('a POST repeated after renewal carries the new csrf_access_token', async () => {
    const page = boot({
      cookies: ['csrf_access_token=csrf-before', 'csrf_refresh_token=csrf-refresh-value'],
      answers: [LAPSED, { status: 200, setCookie: 'csrf_access_token=csrf-after' }, { status: 200 }],
    });
    const body = JSON.stringify({ reason_code: 'other' });

    await page.window.apiRequest('/orders/5/cancel', { method: 'POST', body });

    expect(sent(page.requests)).toEqual(['POST /api/v1/orders/5/cancel', 'POST /api/v1/auth/refresh-cookie', 'POST /api/v1/orders/5/cancel']);
    const [first, , repeated] = page.requests.map((r) => r.options);
    expect(first.headers['X-CSRF-TOKEN']).toBe('csrf-before');
    expect(repeated.headers['X-CSRF-TOKEN']).toBe('csrf-after');
    expect(repeated.body).toBe(body);
    expect(repeated.headers['X-Platform']).toBe('web');
  });

  it('a 401 that does not ask to sign in again goes back to the caller, sent once', async () => {
    const refusal = { success: false, message: 'The current password is not right.' };
    const page = boot({
      cookies: ['csrf_access_token=csrf-access-value', 'csrf_refresh_token=csrf-refresh-value'],
      answers: [{ status: 401, body: refusal }],
    });

    const response = await page.window.apiRequest('/auth/change-password', { method: 'POST', body: '{}' });

    expect(response.status).toBe(401);
    expect(await response.json()).toEqual(refusal);
    expect(sent(page.requests)).toEqual(['POST /api/v1/auth/change-password']);
    expect(page.navigations).toEqual([]);
  });

  it('a 401 that asks to sign in again hands the repeat to the caller with its body unread', async () => {
    const page = boot({
      cookies: ['csrf_refresh_token=csrf-refresh-value'],
      answers: [LAPSED, { status: 200 }, { status: 200, body: { success: true, data: { id: 5 } } }],
    });

    const response = await page.window.apiRequest('/orders/5');

    expect(await response.json()).toEqual({ success: true, data: { id: 5 } });
    expect(sent(page.requests)).toEqual(['GET /api/v1/orders/5', 'POST /api/v1/auth/refresh-cookie', 'GET /api/v1/orders/5']);
  });

  it('a renewal the backend refuses sends the browser to the login page and back', async () => {
    const page = boot({
      search: '?page=2',
      cookies: ['csrf_refresh_token=csrf-refresh-value'],
      answers: [LAPSED, { status: 401, body: { success: false, message: 'Invalid token' } }],
    });

    await expect(page.window.apiRequest('/orders/')).rejects.toThrow('Session expired');

    expect(sent(page.requests)).toEqual(['GET /api/v1/orders/', 'POST /api/v1/auth/refresh-cookie']);
    expect(page.navigations).toEqual([{ to: '/login?next=%2Fmy-orders%3Fpage%3D2', replacement: false }]);
  });
});

describe('login.js', () => {
  const FORM = `
    <script type="application/json" id="page-data">${JSON.stringify({
    after_login_url: '/my-account',
    i18n: { login_success: 'Signed in', login_failed: 'Login failed', network_error: 'Network error' },
  })}</script>
    <form id="loginForm">
      <input name="identifier" value="test@example.com"><input name="password" value="wrong">
      <button type="submit" class="btn-auth">Sign in</button>
    </form>`;

  it('shows the message of a wrong password and stays on the page', async () => {
    const page = boot({
      path: '/login',
      body: FORM,
      scripts: [PAGE_DATA_JS, BASE_JS, LOGIN_JS],
      answers: [{ status: 401, body: { success: false, message: 'The email or password is not right.' } }],
    });
    const form = page.window.document.getElementById('loginForm');

    form.dispatchEvent(new page.window.Event('submit', { cancelable: true }));

    await vi.waitFor(() => {
      expect(page.window.document.querySelector('.toast-message')?.textContent).toBe('The email or password is not right.');
    });
    expect(sent(page.requests)).toEqual(['POST /api/v1/auth/login']);
    expect(page.navigations).toEqual([]);
    expect(form.querySelector('.btn-auth').classList.contains('loading')).toBe(false);
  });
});

describe('the same-site hop page', () => {
  it('reloads the target the server wrote into the page, keeping the #fragment', () => {
    const page = boot({
      path: '/my-subscriptions',
      search: '?sub=7#history',
      scripts: [HOP_JS],
      htmlAttributes: ' data-hop-target="/my-subscriptions?sub=7&amp;_hop=1"',
    });

    expect(page.navigations).toEqual([{ to: '/my-subscriptions?sub=7&_hop=1#history', replacement: true }]);
  });
});

describe('the same-site hop marker', () => {
  it('drops the hop marker from the address and keeps the rest', () => {
    const page = boot({ search: '?page=2&_hop=1#top' });

    expect(page.window.location.pathname).toBe('/my-orders');
    expect(page.window.location.search).toBe('?page=2');
    expect(page.window.location.hash).toBe('#top');
  });

  it('leaves an address without the marker as it is', () => {
    const page = boot({ search: '?page=2&hop=1#top' });

    expect(page.window.location.search).toBe('?page=2&hop=1');
    expect(page.window.location.hash).toBe('#top');
  });
});

describe('logout', () => {
  const SIGNED_IN = ['csrf_access_token=csrf-access-value', 'csrf_refresh_token=csrf-refresh-value'];

  it('proves the login with the refresh cookie CSRF value and lands on the login page', async () => {
    const page = boot({ cookies: SIGNED_IN, answers: [{ status: 200, body: { success: true } }] });

    await page.window.logout();

    expect(sent(page.requests)).toEqual(['POST /api/v1/auth/logout']);
    const call = page.requests[0].options;
    expect(call.headers['X-CSRF-TOKEN']).toBe('csrf-refresh-value');
    expect(call.credentials).toBe('include');
    expect(call.body).toBeUndefined();
    expect(Object.keys(call.headers).map((name) => name.toLowerCase())).not.toContain('authorization');
    expect(page.navigations).toEqual([{ to: '/login', replacement: true }]);
    expect(page.window.document.cookie).toBe('');
  });

  it.each([
    ['refused', { status: 401 }],
    ['rate limited by nginx', { status: 429 }],
    ['a server error', { status: 503 }],
    ['offline', { offline: true }],
  ])('a call that did not succeed (%s) sends the browser to the sign-out page', async (_case, answer) => {
    const page = boot({ cookies: SIGNED_IN, answers: [answer] });
    const logged = [];
    page.window.console.error = (...args) => logged.push(args[0]);

    await page.window.logout();

    expect(sent(page.requests)).toEqual(['POST /api/v1/auth/logout']);
    expect(page.navigations).toEqual([{ to: '/logout', replacement: true }]);
    expect(page.window.document.cookie).toBe('');
    expect(logged).toEqual(answer.offline ? ['Logout API call failed:'] : []);
  });

  it('falls back to the access cookie CSRF value when the refresh one is gone', async () => {
    const page = boot({ cookies: ['csrf_access_token=csrf-access-value'], answers: [{ status: 200 }] });

    await page.window.logout();

    expect(page.requests[0].options.headers['X-CSRF-TOKEN']).toBe('csrf-access-value');
  });
});
