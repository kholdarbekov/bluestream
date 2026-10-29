// The website's three order surfaces (plan 2026-10-27 Task 10a; spec 2026-10-27 §5.3, §8, D1, D10-D13): the
// /my-orders cards, the shared order modal and the tracking page draw Pay, Skip/Cancel/Decline and the agent's
// Confirm from the `customer_actions` the backend publishes, through ONE script, order-actions.js.
//
// The SHIPPED scripts run in jsdom exactly as the pages load them: jquery.js, popper and bootstrap.min.js (the
// modal), _page-data.js, base.js (its real apiRequest: cookie credentials, X-Platform, the X-CSRF-TOKEN it copies
// from the readable csrf cookie), order-actions.js and then the page script, over the shell the server renders
// (tests/fixtures/customer_order_actions/shell.json). Only `fetch` is replaced:
// - every read is answered by its key from the case fixture, which
//   tests/integration/test_customer_order_actions_fixtures.py captured from the real endpoints (and keeps fresh);
// - each write is a strict SEQUENCE: the script must send exactly the request the backend answered in the case's
//   `exchanges`, with that body, and gets back what the backend answered there.
// An unexpected request fails the case. window.confirm, prompt and alert throw. `OrderActions.navigate` is a spy.
import fs from 'node:fs';
import path from 'node:path';
import { JSDOM, VirtualConsole } from 'jsdom';

const REPO = path.resolve(process.cwd(), '..');
// Repo files named in this test (never user input).
// eslint-disable-next-line security/detect-non-literal-fs-filename
const read = (file) => fs.readFileSync(path.join(REPO, file), 'utf8');
const BASE_SCRIPTS = [
  'business_app/static/js/jquery.js',
  'business_app/static/js/popper.min.js',
  'business_app/static/js/bootstrap.min.js',
  'business_app/static/js/pages/_page-data.js',
  'business_app/static/js/pages/base.js',
  'business_app/static/js/pages/order-actions.js',
].map(read);
const MODAL_SCRIPT = read('business_app/static/js/pages/order-detail-modal.js');
const ORDERS_SCRIPT = read('business_app/static/js/pages/orders.js');
const TRACKING_SCRIPT = read('business_app/static/js/pages/order-tracking.js');
const FIXTURES = path.join(REPO, 'tests/fixtures/customer_order_actions');
// eslint-disable-next-line security/detect-non-literal-fs-filename
const loadCase = (name) => JSON.parse(fs.readFileSync(path.join(FIXTURES, `${name}.json`), 'utf8'));
const SHELL = loadCase('shell');
const CSRF = 'csrf-cookie-value';
const CLICK_URL = 'http://localhost/my-orders#click-checkout';
const REFRESH = 'This changed. Refresh to see the latest.';

const LIST = 'GET /api/v1/orders?page=1&per_page=10';
const detailKey = (id) => `GET /api/v1/orders/${id}`;
const canonical = (value) => JSON.stringify(value, (key, item) => (
  item && typeof item === 'object' && !Array.isArray(item)
    ? Object.keys(item).sort().reduce((sorted, name) => ({ ...sorted, [name]: item[name] }), {})
    : item
));
const squash = (value) => (value || '').replace(/[ \t\n\r]+/g, ' ').trim();

async function settle(window, rounds = 30) {
  for (let i = 0; i < rounds; i += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, 0));
  }
}

const escapeJson = (value) => JSON.stringify(value).replace(/</g, '\\u003c');

// The page the fixture's language renders, its fetch answering from the case, and the scripts loaded as the
// page loads them. `writes` are the exchanges (by name) the script is allowed to send, in order.
async function boot(kind, name, { writes = [], intervals = false, pending = false } = {}) {
  const fixture = loadCase(name);
  const shell = SHELL[fixture.language];
  const base = { api_base_url: '/api/v1', current_user: null, is_authenticated: false, login_url: '/login', i18n: {} };
  let island = shell[kind === 'tracking' ? 'tracking' : 'orders'].island;
  if (kind === 'tracking') island = { ...island, order: fixture.requests.tracking_island };
  const body = kind === 'tracking'
    ? `<div class="action-buttons-host">${shell.tracking.html}</div>`
    : `${shell.orders.html}${shell.modal.html}` +
      `<script type="application/json" id="order-detail-modal-data">${escapeJson(shell.modal.island)}</script>`;
  const virtualConsole = new VirtualConsole();
  const reloads = [];
  virtualConsole.on('jsdomError', (error) => {
    if (/navigation/i.test(error.message)) reloads.push(error.message);
  });
  const dom = new JSDOM(
    `<!doctype html><html lang="${fixture.language}"><body>` +
    `<script type="application/json" id="base-page-data">${escapeJson(base)}</script>${body}` +
    `<script type="application/json" id="page-data">${escapeJson(island)}</script></body></html>`,
    {
      url: kind === 'tracking' ? 'http://localhost/order-tracking?order_id=1' : 'http://localhost/my-orders',
      runScripts: 'outside-only',
      pretendToBeVisual: true,
      virtualConsole,
    },
  );
  const { window } = dom;
  window.document.cookie = `csrf_access_token=${CSRF}`;
  const requests = [];
  const mismatches = [];
  const queue = writes.map((exchangeName) => fixture.exchanges[exchangeName]);
  window.fetch = (url, options = {}) => {
    const method = (options.method || 'GET').toUpperCase();
    const key = `${method} ${url}`;
    const sent = options.body === undefined || options.body === null ? null : JSON.parse(options.body);
    requests.push({ key, method, url, body: sent, options });
    let answer = null;
    if (method === 'GET') {
      answer = fixture.requests[key] || null;
    } else {
      const expected = queue.shift();
      const same = expected && expected.method === method && expected.path === url &&
        canonical(expected.body) === canonical(sent);
      if (same) answer = { status: expected.status, body: expected.response };
      else mismatches.push({ sent: { method, url, body: sent }, expected: expected && { url: expected.path, body: expected.body } });
    }
    if (!answer) {
      return Promise.resolve({ ok: false, status: 599, json: async () => ({ success: false, message: `unexpected ${key}` }) });
    }
    if (pending && method !== 'GET') return new Promise(() => {});
    return Promise.resolve({ ok: answer.status < 400, status: answer.status, json: async () => JSON.parse(JSON.stringify(answer.body)) });
  };
  window.L = { map: () => ({ setView: () => ({}), remove() {}, invalidateSize() {} }), tileLayer: () => ({ addTo() {} }), marker: () => ({ addTo: () => ({ bindPopup: () => ({ openPopup() {} }) }) }) };
  ['confirm', 'prompt', 'alert'].forEach((dialog) => {
    window[dialog] = () => { throw new Error(`native ${dialog}() is never used`); };
  });
  const ticks = [];
  if (intervals) window.setInterval = (callback) => { ticks.push(callback); return ticks.length; };
  BASE_SCRIPTS.forEach((source) => window.eval(source));
  window.OrderActions.navigate = vi.fn();
  if (kind === 'tracking') {
    window.eval(TRACKING_SCRIPT);
  } else {
    window.eval(MODAL_SCRIPT);
    window.eval(ORDERS_SCRIPT);
  }
  if (window.document.readyState !== 'loading') window.document.dispatchEvent(new window.Event('DOMContentLoaded'));
  await settle(window);
  const document = window.document;
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const $1 = (selector, root = document) => root.querySelector(selector);
  const order = fixture.requests[detailKey(fixture.order_id)].body.data.order;
  return { window, document, requests, mismatches, fixture, order, actions: order.customer_actions, ticks, reloads, $$, $1, shell, words: shell.orders.island.order_actions };
}

async function press(page, el) {
  el.dispatchEvent(new page.window.MouseEvent('click', { bubbles: true, cancelable: true }));
  await settle(page.window);
}

const card = (page, id = page.fixture.order_id) => page.$1(`.order-card[data-id="${id}"]`);
const actionsOf = (page, id) => page.$1('.order-actions', card(page, id));
const buttons = (root) => Array.from(root.querySelectorAll('[data-order-action]'));
const labels = (root) => buttons(root).map((b) => squash(b.textContent));
const panel = (page, id) => page.$1('.order-actions__confirm', actionsOf(page, id));
const toasts = (page) => page.$$('#toast-container .toast-message').map((t) => t.textContent);
const posts = (page) => page.requests.filter((r) => r.method === 'POST');
const reads = (page, key) => page.requests.filter((r) => r.key === key).length;

// ------------------------------------------------------------------ the cards

describe('My orders cards draw only what customer_actions publishes', () => {
  it('click_unpaid: Pay and Cancel order, with the backend labels and the pay-by line', async () => {
    const page = await boot('orders', 'click_unpaid');
    expect(page.mismatches).toEqual([]);
    const { pay, cancel } = page.actions;
    expect(labels(actionsOf(page))).toEqual([pay.label, cancel.label]);
    expect(buttons(actionsOf(page)).map((b) => b.dataset.orderAction)).toEqual(['pay', 'cancel']);
    expect(squash(page.$1('.order-actions__deadline', actionsOf(page)).textContent)).toBe(pay.deadline_label);
    expect(page.$1('.order-actions__confirm', actionsOf(page)).hidden).toBe(true);
  });

  it('subscription_later_order: Skip this delivery and the subscription link', async () => {
    const page = await boot('orders', 'subscription_later_order');
    const { cancel } = page.actions;
    expect(cancel.via).toBe('skip');
    expect(labels(actionsOf(page, 2))).toEqual([page.actions.pay.label, cancel.label]);
    const link = page.$1('a.order-actions__subscription', actionsOf(page, 2));
    expect(link.getAttribute('href')).toBe('/my-subscriptions?sub=1#subscription');
    expect(squash(link.textContent)).toBe(page.words.open_subscription);
  });

  it('subscription_leftover_paused: Cancel order', async () => {
    const page = await boot('orders', 'subscription_leftover_paused');
    expect(page.actions.cancel.via).toBe('cancel');
    expect(labels(actionsOf(page))).toEqual([page.actions.pay.label, 'Cancel order']);
  });

  it.each(['cash_pending', 'partly_paid', 'paid_delivered', 'click_past_deadline'])('%s: no Pay', async (name) => {
    const page = await boot('orders', name);
    expect(page.actions.pay.allowed).toBe(false);
    expect(page.$1('[data-order-action="pay"]')).toBeNull();
    expect(page.$1('.order-actions__deadline')).toBeNull();
    expect(labels(actionsOf(page))).toEqual(page.actions.cancel.allowed ? [page.actions.cancel.label] : []);
  });

  it('ru: no English page word, no key and no unfilled {placeholder}', async () => {
    const page = await boot('orders', 'click_unpaid_ru');
    const english = loadCase('click_unpaid').requests[detailKey(1)].body.data.order.customer_actions;
    const drawn = squash(actionsOf(page).textContent);
    expect(drawn).toContain(page.actions.pay.label);
    [english.pay.label, english.pay.deadline_label, english.cancel.label].forEach((word) => {
      expect(drawn).not.toContain(word);
    });
    expect(drawn).not.toMatch(/\{\w+\}|\bapi\.|\bweb\.|\bcabinet\./);
    await press(page, page.$1('[data-order-action="cancel"]'));
    const asked = squash(panel(page).textContent);
    expect(asked).toContain(page.actions.cancel.confirm_text);
    expect(asked).toContain(page.words.keep);
    expect(asked).not.toContain('Keep it');
    expect(asked).not.toMatch(/\{\w+\}|\bapi\.|\bweb\.|\bcabinet\./);
  });
});

// ------------------------------------------------------------------ one inline confirm

describe('one inline confirm, never a native dialog', () => {
  it('Cancel opens the panel with cancel.confirm_text and Keep; Keep closes it and returns focus', async () => {
    const page = await boot('orders', 'click_unpaid');
    const cancelButton = page.$1('[data-order-action="cancel"]');
    await press(page, cancelButton);
    const ask = panel(page);
    expect(ask.hidden).toBe(false);
    expect(ask.getAttribute('role')).toBe('group');
    expect(squash(ask.querySelector('p').textContent)).toBe(page.actions.cancel.confirm_text);
    expect(page.$$('input[type=radio]', ask)).toHaveLength(0);
    const [yes, keep] = Array.from(ask.querySelectorAll('button'));
    expect([squash(yes.textContent), squash(keep.textContent)]).toEqual([page.actions.cancel.label, page.words.keep]);
    expect(page.document.activeElement).toBe(keep);
    // Opening it did not open the order's details behind it.
    expect(page.$1('#orderDetailModal').classList.contains('show')).toBe(false);
    await press(page, keep);
    expect(panel(page).hidden).toBe(true);
    expect(page.document.activeElement).toBe(cancelButton);
    expect(posts(page)).toEqual([]);
  });

  it('Skip offers the published reasons and sends the one picked', async () => {
    const page = await boot('orders', 'subscription_later_order', { writes: ['skip_reason'] });
    await press(page, page.$1('[data-order-action="cancel"]', actionsOf(page, 2)));
    const ask = panel(page, 2);
    const radios = page.$$('input[type=radio][name="order-skip-reason-2"]', ask);
    expect(radios.map((r) => r.value)).toEqual(page.actions.cancel.reasons.map((r) => r.code));
    expect(page.$$('label', ask).map((l) => squash(l.textContent)).slice(0, 3)).toEqual(page.actions.cancel.reasons.map((r) => r.label));
    expect(squash(ask.textContent)).toContain(page.words.reason_prompt);
    expect(page.$1('textarea.order-actions__reason-text', ask).hidden).toBe(true);
    await press(page, radios.find((r) => r.value === 'away'));
    const before = reads(page, LIST);
    await press(page, ask.querySelector('button'));
    expect(page.mismatches).toEqual([]);
    expect(posts(page).map((r) => [r.url, r.body])).toEqual([['/api/v1/orders/2/cancel', { expected_via: 'skip', reason_code: 'away' }]]);
    expect(posts(page)[0].options.headers['X-CSRF-TOKEN']).toBe(CSRF);
    expect(toasts(page)).toEqual([page.fixture.exchanges.skip_reason.response.message]);
    expect(reads(page, LIST)).toBe(before + 1);
  });

  it('Other with words sends reason_text', async () => {
    const page = await boot('orders', 'subscription_later_order', { writes: ['skip_other_text'] });
    await press(page, page.$1('[data-order-action="cancel"]', actionsOf(page, 2)));
    const ask = panel(page, 2);
    await press(page, page.$1('input[type=radio][value="other"]', ask));
    const text = page.$1('textarea.order-actions__reason-text', ask);
    expect(text.hidden).toBe(false);
    expect(text.getAttribute('placeholder') || text.getAttribute('aria-label')).toBe(page.words.reason_text);
    text.value = 'Going to the dacha';
    await press(page, ask.querySelector('button'));
    expect(page.mismatches).toEqual([]);
    expect(posts(page)[0].body).toEqual({ expected_via: 'skip', reason_code: 'other', reason_text: 'Going to the dacha' });
  });

  it('a skip with no reason picked sends only expected_via', async () => {
    const page = await boot('orders', 'subscription_later_order', { writes: ['skip_no_reason'] });
    await press(page, page.$1('[data-order-action="cancel"]', actionsOf(page, 2)));
    await press(page, panel(page, 2).querySelector('button'));
    expect(page.mismatches).toEqual([]);
    expect(posts(page)[0].body).toEqual({ expected_via: 'skip' });
  });

  it('a plain cancel sends only expected_via and shows no reasons', async () => {
    const page = await boot('orders', 'subscription_leftover_paused', { writes: ['cancel'] });
    await press(page, page.$1('[data-order-action="cancel"]'));
    const ask = panel(page);
    expect(page.$$('input[type=radio], textarea', ask)).toHaveLength(0);
    await press(page, ask.querySelector('button'));
    expect(page.mismatches).toEqual([]);
    expect(posts(page).map((r) => r.body)).toEqual([{ expected_via: 'cancel' }]);
    expect(toasts(page)).toEqual([page.fixture.exchanges.cancel.response.message]);
  });

  it('a 409 shows the translated refresh message and reloads the list', async () => {
    const page = await boot('orders', 'stale_via', { writes: ['skip'] });
    const before = reads(page, LIST);
    await press(page, page.$1('[data-order-action="cancel"]'));
    await press(page, panel(page).querySelector('button'));
    expect(page.mismatches).toEqual([]);
    expect(page.fixture.exchanges.skip.status).toBe(409);
    expect(toasts(page)).toEqual([REFRESH]);
    expect(reads(page, LIST)).toBe(before + 1);
  });

  it('a refused cancel (any other failure) shows its sentence in the panel and keeps it open', async () => {
    const page = await boot('orders', 'click_unpaid', { writes: [] });
    await press(page, page.$1('[data-order-action="cancel"]'));
    await press(page, panel(page).querySelector('button'));
    // The unexpected POST is answered 599 with a message; the panel stays and says it.
    const error = page.$1('.order-actions__error[role="alert"]', actionsOf(page));
    expect(error.hidden).toBe(false);
    expect(squash(error.textContent)).toBe('unexpected POST /api/v1/orders/1/cancel');
    expect(panel(page).hidden).toBe(false);
    expect(page.mismatches).toHaveLength(1);
  });
});

// ------------------------------------------------------------------ Pay

describe('Pay', () => {
  it('sends channel web and the published expected_amount, then opens Click', async () => {
    const page = await boot('orders', 'click_unpaid', { writes: ['pay_web'] });
    await press(page, page.$1('[data-order-action="pay"]'));
    expect(page.mismatches).toEqual([]);
    expect(posts(page).map((r) => [r.url, r.body])).toEqual([
      ['/api/v1/orders/1/retry-payment', { channel: 'web', expected_amount: page.actions.pay.expected_amount }],
    ]);
    expect(page.window.OrderActions.navigate).toHaveBeenCalledTimes(1);
    expect(page.window.OrderActions.navigate).toHaveBeenCalledWith(CLICK_URL);
  });

  it('once Click is on its way the button stays working and the page stays busy, so a second tap sends nothing', async () => {
    const page = await boot('orders', 'click_unpaid', { writes: ['pay_web'] });
    const pay = page.$1('[data-order-action="pay"]');
    await press(page, pay);
    expect(page.window.OrderActions.navigate).toHaveBeenCalledTimes(1);
    expect(pay.getAttribute('aria-disabled')).toBe('true');
    expect(squash(pay.textContent)).toBe(page.words.working);
    expect(page.window.OrderActions.isBusy()).toBe(true);
    await press(page, pay);
    expect(posts(page)).toHaveLength(1);
  });

  it('a double click sends one request', async () => {
    const page = await boot('orders', 'click_unpaid', { writes: ['pay_web'] });
    const pay = page.$1('[data-order-action="pay"]');
    pay.dispatchEvent(new page.window.MouseEvent('click', { bubbles: true, cancelable: true }));
    pay.dispatchEvent(new page.window.MouseEvent('click', { bubbles: true, cancelable: true }));
    await settle(page.window);
    expect(posts(page)).toHaveLength(1);
    expect(page.mismatches).toEqual([]);
  });

  it('while the write is in flight the button is aria-disabled and says it is working', async () => {
    const page = await boot('orders', 'click_unpaid', { writes: ['pay_web'], pending: true });
    const pay = page.$1('[data-order-action="pay"]');
    await press(page, pay);
    expect(pay.getAttribute('aria-disabled')).toBe('true');
    expect(squash(pay.textContent)).toBe(page.words.working);
    expect(page.window.OrderActions.isBusy()).toBe(true);
  });

  it('a stale amount shows the 409 message and reloads the list', async () => {
    const page = await boot('orders', 'click_unpaid_stale', { writes: ['pay_web'] });
    const before = reads(page, LIST);
    await press(page, page.$1('[data-order-action="pay"]'));
    expect(page.mismatches).toEqual([]);
    expect(toasts(page)).toEqual([REFRESH]);
    expect(reads(page, LIST)).toBe(before + 1);
    expect(page.window.OrderActions.navigate).not.toHaveBeenCalled();
  });

  it('a pool-short Payme order shows the backend sentence verbatim and opens no Click link', async () => {
    const page = await boot('orders', 'payme_unpaid', { writes: ['payme_pool_short'] });
    await press(page, page.$1('[data-order-action="pay"]'));
    expect(page.mismatches).toEqual([]);
    const exchange = page.fixture.exchanges.payme_pool_short;
    expect(exchange.status).toBe(400);
    const error = page.$1('.order-actions__error[role="alert"]', actionsOf(page));
    expect(error.hidden).toBe(false);
    expect(error.textContent).toBe(exchange.response.message);
    expect(page.window.OrderActions.navigate).not.toHaveBeenCalled();
  });

  it('pay() answers a refusal in words and draws nothing', async () => {
    const page = await boot('orders', 'click_past_deadline', { writes: ['pay_web'] });
    const result = await page.window.OrderActions.pay(1, { expected_amount: '30000.00' }, { words: page.words });
    expect(result).toEqual({
      ok: false,
      status: 409,
      message: 'The time to pay for this order online has ended.',
    });
    expect(toasts(page)).toEqual([]);
    expect(page.window.OrderActions.navigate).not.toHaveBeenCalled();
  });
});

// ------------------------------------------------------------------ the agent's proposal

describe('the agent proposal', () => {
  it('shows Confirm and Decline; Confirm sends {action:"confirm", channel:"web"}', async () => {
    const page = await boot('orders', 'agent_pending', { writes: ['confirm_web'] });
    expect(page.actions.pay.allowed).toBe(false);
    expect(labels(actionsOf(page))).toEqual([page.actions.confirm.label, page.actions.cancel.label]);
    expect(buttons(actionsOf(page)).map((b) => b.dataset.orderAction)).toEqual(['confirm', 'cancel']);
    const before = reads(page, LIST);
    await press(page, page.$1('[data-order-action="confirm"]'));
    expect(page.mismatches).toEqual([]);
    expect(posts(page).map((r) => [r.url, r.body])).toEqual([
      [`/api/v1/orders/${page.fixture.order_id}/agent-confirmation`, { action: 'confirm', channel: 'web' }],
    ]);
    expect(toasts(page)).toEqual([page.words.confirmed]);
    expect(reads(page, LIST)).toBe(before + 1);
  });

  it('Decline shows confirm_text and sends expected_via decline', async () => {
    const page = await boot('orders', 'agent_pending', { writes: ['decline'] });
    await press(page, page.$1('[data-order-action="cancel"]'));
    expect(squash(panel(page).querySelector('p').textContent)).toBe(page.actions.cancel.confirm_text);
    await press(page, panel(page).querySelector('button'));
    expect(page.mismatches).toEqual([]);
    expect(posts(page).map((r) => r.body)).toEqual([{ expected_via: 'decline' }]);
    expect(toasts(page)).toEqual([page.fixture.exchanges.decline.response.message]);
  });
});

// ------------------------------------------------------------------ the modal and the tracking page

describe('the other two surfaces draw the same answer', () => {
  it('the order modal draws the same buttons from GET /orders/<id> and Pays from the modal', async () => {
    const page = await boot('orders', 'click_unpaid', { writes: ['pay_web'] });
    await press(page, page.$1('[data-action="show-order-detail"][data-stop-propagation="1"]'));
    expect(page.requests.map((r) => r.key)).toContain(detailKey(1));
    expect(page.$1('#orderDetailModal').classList.contains('show')).toBe(true);
    const inModal = page.$1('#orderDetailActions');
    expect(labels(inModal)).toEqual(labels(actionsOf(page)));
    expect(page.$$('.btn-sm', page.$1('#orderDetailModal'))).toHaveLength(0);
    await press(page, page.$1('[data-order-action="pay"]', inModal));
    expect(page.mismatches).toEqual([]);
    expect(page.window.OrderActions.navigate).toHaveBeenCalledWith(CLICK_URL);
  });

  it('a modal skip points to the subscription and reloads the modal after the write', async () => {
    const page = await boot('orders', 'subscription_leftover_paused', { writes: ['cancel'] });
    await press(page, page.$1('[data-action="show-order-detail"][data-stop-propagation="1"]'));
    const inModal = page.$1('#orderDetailActions');
    expect(page.$1('a.order-actions__subscription', inModal).getAttribute('href')).toBe('/my-subscriptions?sub=1#subscription');
    const reloads = reads(page, detailKey(1));
    await press(page, page.$1('[data-order-action="cancel"]', inModal));
    await press(page, page.$1('.order-actions__confirm button', inModal));
    expect(page.mismatches).toEqual([]);
    expect(reads(page, detailKey(1))).toBe(reloads + 1);
  });

  it('the tracking page draws from its island, the same answer the API gives', async () => {
    const page = await boot('tracking', 'click_unpaid', { writes: ['pay_web'] });
    expect(page.fixture.requests.tracking_island.customer_actions).toEqual(page.actions);
    const container = page.$1('#orderActions');
    expect(labels(container)).toEqual([page.actions.pay.label, page.actions.cancel.label]);
    await press(page, page.$1('[data-order-action="pay"]'));
    expect(page.mismatches).toEqual([]);
    expect(page.window.OrderActions.navigate).toHaveBeenCalledWith(CLICK_URL);
  });

  it('the tracking page hides the actions cell when nothing is published, so All Orders keeps its place', async () => {
    const page = await boot('tracking', 'paid_delivered');
    expect(page.$1('#orderActions').hidden).toBe(true);
  });

  it('the tracking page does not reload while a confirm is open or a write is in flight', async () => {
    const page = await boot('tracking', 'click_unpaid', { writes: ['cancel'], intervals: true, pending: true });
    expect(page.ticks).toHaveLength(1);
    page.ticks[0]();
    expect(page.reloads).toHaveLength(1);
    await press(page, page.$1('[data-order-action="cancel"]'));
    expect(page.window.OrderActions.isBusy()).toBe(true);
    page.ticks[0]();
    expect(page.reloads).toHaveLength(1);
    await press(page, page.$$('.order-actions__confirm button')[1]);
    expect(page.window.OrderActions.isBusy()).toBe(false);
    page.ticks[0]();
    expect(page.reloads).toHaveLength(2);
    await press(page, page.$1('[data-order-action="cancel"]'));
    await press(page, page.$$('.order-actions__confirm button')[0]); // the write never answers
    expect(page.window.OrderActions.isBusy()).toBe(true);
    page.ticks[0]();
    expect(page.reloads).toHaveLength(2);
  });
});

describe('the scripts hold no rule of their own', () => {
  const code = (source) => source.split('\n').map((line) => line.replace(/\/\/.*$/, '')).join('\n');
  it.each([['orders.js', ORDERS_SCRIPT], ['order-detail-modal.js', MODAL_SCRIPT], ['order-tracking.js', TRACKING_SCRIPT]])(
    '%s reads no retired field and opens no native dialog', (name, source) => {
      expect(code(source)).not.toMatch(/customer_cancel_via|can_customer_cancel|is_payable|can_pay|\bconfirm\(|\bprompt\(|\balert\(/);
    },
  );
});
