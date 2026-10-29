// The payment return pages, the checkout's wait and the cash rescue (plan 2026-10-27 Task 10b; spec 2026-10-27 §4.5,
// §4.7, D7-D9). The SHIPPED scripts (base.js with its real apiRequest, order-actions.js, payment-cancelled.js,
// payment-pending.js) run in jsdom over the sections the server rendered and the answers the real endpoints gave
// (tests/fixtures/customer_order_actions/payment_*.json, checkout_*.json, rescue_*.json, written and kept fresh by
// tests/integration/test_web_payment_return_pages.py). Only `fetch`, the timers and `OrderActions.navigate` are
// replaced; a request the script sends that the fixture did not answer fails the case.
//
// checkout.js itself IS booted (the last describe): over a minimal checkout shell, with the cart in localStorage, it
// places the Click order by pressing the real button, and the POST answers are the fixtures the backend wrote. The
// first describe also drives order-actions.js's two entry points directly.
import fs from 'node:fs';
import path from 'node:path';
import { JSDOM, VirtualConsole } from 'jsdom';

const REPO = path.resolve(process.cwd(), '..');
// Repo files named in this test (never user input).
// eslint-disable-next-line security/detect-non-literal-fs-filename
const read = (file) => fs.readFileSync(path.join(REPO, file), 'utf8');
const SCRIPTS = [
  'business_app/static/js/jquery.js',
  'business_app/static/js/pages/_page-data.js',
  'business_app/static/js/pages/base.js',
  'business_app/static/js/pages/order-actions.js',
].map(read);
const CANCELLED_SCRIPT = read('business_app/static/js/pages/payment-cancelled.js');
const PENDING_SCRIPT = read('business_app/static/js/pages/payment-pending.js');
const FIXTURES = path.join(REPO, 'tests/fixtures/customer_order_actions');
// eslint-disable-next-line security/detect-non-literal-fs-filename
const loadCase = (name) => JSON.parse(fs.readFileSync(path.join(FIXTURES, `${name}.json`), 'utf8'));
const CSRF = 'csrf-cookie-value';
const CLICK_URL = 'http://localhost/my-orders#click-checkout';
const escapeJson = (value) => JSON.stringify(value).replace(/</g, '\\u003c');
const squash = (value) => (value || '').replace(/[ \t\n\r]+/g, ' ').trim();

// The test's own (Node) timer: the page's `window.setTimeout` is replaced by hand-run timers below.
async function settle(window, rounds = 30) {
  for (let i = 0; i < rounds; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
}

// A page over `html` and `island`, its fetch answering `answers` ({ 'METHOD /url': {status, body} }), timers run by
// hand (`advance(ms)`), native dialogs forbidden and `OrderActions.navigate` a spy.
async function boot({ html = '', island = {}, answers = {}, script = null, user = null, cart = null }) {
  const base = { api_base_url: '/api/v1', current_user: user, is_authenticated: !!user, login_url: '/login', i18n: {} };
  const virtualConsole = new VirtualConsole();
  const dom = new JSDOM(
    `<!doctype html><html lang="en"><body>` +
    `<script type="application/json" id="base-page-data">${escapeJson(base)}</script>${html}` +
    `<script type="application/json" id="page-data">${escapeJson(island)}</script></body></html>`,
    { url: 'http://localhost/payment/cancel?order_id=1', runScripts: 'outside-only', pretendToBeVisual: true, virtualConsole },
  );
  const { window } = dom;
  window.document.cookie = `csrf_access_token=${CSRF}`;
  if (cart) window.localStorage.setItem('cart', JSON.stringify(cart));

  const requests = [];
  window.fetch = (url, options = {}) => {
    const method = (options.method || 'GET').toUpperCase();
    const key = `${method} ${url}`;
    requests.push({ key, method, url, body: options.body ? JSON.parse(options.body) : null, options });
    const answer = answers[key];
    const next = Array.isArray(answer) ? (answer.length > 1 ? answer.shift() : answer[0]) : answer;
    if (!next) {
      return Promise.resolve({ ok: false, status: 599, json: async () => ({ success: false, message: `unexpected ${key}` }) });
    }
    return Promise.resolve({ ok: next.status < 400, status: next.status, json: async () => JSON.parse(JSON.stringify(next.body)) });
  };
  ['confirm', 'prompt', 'alert'].forEach((dialog) => {
    window[dialog] = () => { throw new Error(`native ${dialog}() is never used`); };
  });

  // The page's timers, run by hand: `advance(ms)` moves the clock and runs what came due.
  const timers = [];
  let clock = 0;
  window.setTimeout = (fn, ms = 0) => { timers.push({ fn, at: clock + ms, ms }); return timers.length; };
  const advance = async (ms) => {
    clock += ms;
    let due;
    // eslint-disable-next-line no-cond-assign
    while ((due = timers.filter((t) => !t.done && t.at <= clock).sort((a, b) => a.at - b.at)[0])) {
      due.done = true;
      due.fn();
      await settle(window, 5);
    }
  };
  const pending = () => timers.filter((t) => !t.done && t.ms >= 1000).map((t) => t.ms);

  SCRIPTS.forEach((source) => window.eval(source));
  window.OrderActions.navigate = vi.fn();
  if (script) window.eval(script);
  if (script && window.document.readyState !== 'loading') window.document.dispatchEvent(new window.Event('DOMContentLoaded'));
  await settle(window);
  const document = window.document;
  return { window, document, requests, advance, pending, navigate: window.OrderActions.navigate, $1: (s, root = document) => root.querySelector(s), $$: (s, root = document) => Array.from(root.querySelectorAll(s)) };
}

async function press(page, el) {
  el.dispatchEvent(new page.window.MouseEvent('click', { bubbles: true, cancelable: true }));
  await settle(page.window);
}

const buttons = (root) => Array.from(root.querySelectorAll('[data-order-action]'));
const labels = (root) => buttons(root).map((b) => squash(b.textContent));
const posts = (page) => page.requests.filter((r) => r.method === 'POST');

// ------------------------------------------------------------------ the cancel page

describe('the payment-cancelled page', () => {
  it('the cancel page Pays with channel web and the published amount, through the cookie', async () => {
    const fixture = loadCase('payment_cancel');
    const exchange = fixture.exchanges.pay_web;
    const page = await boot({
      html: fixture.page.html,
      island: fixture.page.island,
      answers: { [`POST ${exchange.path}`]: { status: exchange.status, body: exchange.response } },
      script: CANCELLED_SCRIPT,
    });
    const published = fixture.page.island.order.customer_actions.pay;
    const host = page.$1('#paymentActions');

    expect(labels(host)).toEqual([published.label]);
    expect(buttons(host).map((b) => b.dataset.orderAction)).toEqual(['pay']);
    expect(squash(page.$1('.order-actions__deadline', host).textContent)).toBe(published.deadline_label);

    await press(page, page.$1('[data-order-action="pay"]'));

    expect(posts(page)).toHaveLength(1);
    const sent = posts(page)[0];
    expect(sent.url).toBe(exchange.path);
    expect(sent.body).toEqual({ channel: 'web', expected_amount: '30000.00' });
    expect(sent.body).toEqual(exchange.body);
    expect(sent.options.headers['X-CSRF-TOKEN']).toBe(CSRF);
    expect(sent.options.headers.Authorization).toBeUndefined();
    expect(sent.options.credentials).toBe('include');
    expect(page.navigate).toHaveBeenCalledWith(CLICK_URL);
  });

  it('shows the status and Pay only, never Cancel or Confirm', async () => {
    const fixture = loadCase('payment_cancel');
    const page = await boot({ html: fixture.page.html, island: fixture.page.island, script: CANCELLED_SCRIPT });
    // the published answer holds a cancel too; the page does not draw it
    expect(fixture.page.island.order.customer_actions.cancel.allowed).toBe(true);
    expect(page.$1('[data-order-action="cancel"]')).toBeNull();
    expect(page.$1('[data-order-action="confirm"]')).toBeNull();
    expect(squash(page.$1('.order-info').textContent)).toContain('Pending');
  });

  it('a dead order draws no Pay and sends nothing', async () => {
    const fixture = loadCase('payment_cancel_dead');
    const page = await boot({ html: fixture.page.html, island: fixture.page.island, script: CANCELLED_SCRIPT });
    expect(buttons(page.$1('#paymentActions'))).toEqual([]);
    expect(squash(page.$1('.order-info').textContent)).toContain('Cancelled');
    expect(page.requests).toEqual([]);
  });

  it('a signed-out cancel page draws no Pay and sends nothing', async () => {
    const fixture = loadCase('payment_cancel_neutral');
    const page = await boot({ html: fixture.page.html, island: fixture.page.island, script: CANCELLED_SCRIPT });
    expect(page.$1('#paymentActions')).toBeNull();
    expect(page.$$('[data-order-action]')).toEqual([]);
    expect(page.requests).toEqual([]);
    expect(squash(page.document.body.textContent)).toContain("The payment wasn't completed, and no money was taken.");
  });

  it('a stale Pay (409) reloads the page so it is drawn from the new answer', async () => {
    const fixture = loadCase('click_unpaid_stale');
    const stale = fixture.exchanges.pay_web;
    const cancel = loadCase('payment_cancel');
    const page = await boot({
      html: cancel.page.html,
      island: cancel.page.island,
      answers: { [`POST ${cancel.exchanges.pay_web.path}`]: { status: stale.status, body: stale.response } },
      script: CANCELLED_SCRIPT,
    });
    await press(page, page.$1('[data-order-action="pay"]'));
    expect(page.navigate).toHaveBeenCalledWith('http://localhost/payment/cancel?order_id=1');
    expect(page.navigate).not.toHaveBeenCalledWith(CLICK_URL);
  });
});

// ------------------------------------------------------------------ the pending page

describe('the payment-pending page', () => {
  const statusKey = (fixture) => `GET /api/v1/payments/${fixture.page.island.payment_id}/status`;

  it('the pending page polls with the cookie and opens the order page once paid', async () => {
    const fixture = loadCase('payment_pending');
    const { pending, completed } = fixture.polls;
    const page = await boot({
      html: fixture.page.html,
      island: fixture.page.island,
      answers: { [statusKey(fixture)]: [pending, completed] },
      script: PENDING_SCRIPT,
    });
    expect(page.pending()).toEqual([3000]);

    await page.advance(3000);
    expect(page.requests.map((r) => r.key)).toEqual([statusKey(fixture)]);
    expect(page.requests[0].options.credentials).toBe('include');
    expect(page.requests[0].options.headers.Authorization).toBeUndefined();
    expect(page.navigate).not.toHaveBeenCalled();
    expect(page.pending()).toEqual([6000]);

    await page.advance(6000);
    expect(page.requests).toHaveLength(2);
    expect(page.navigate).toHaveBeenCalledTimes(1);
    expect(page.navigate).toHaveBeenCalledWith('/order-tracking?order_id=1');
  });

  it('a payment that failed goes to the cancel page', async () => {
    const fixture = loadCase('payment_pending');
    const page = await boot({
      html: fixture.page.html,
      island: fixture.page.island,
      answers: { [statusKey(fixture)]: fixture.polls.failed },
      script: PENDING_SCRIPT,
    });
    await page.advance(3000);
    await page.advance(2000);
    expect(page.navigate).toHaveBeenCalledWith('/payment/cancel?order_id=1');
  });

  it('keeps polling, 20 times every 6 s, after an answer that is not OK', async () => {
    const fixture = loadCase('payment_pending');
    const page = await boot({
      html: fixture.page.html,
      island: fixture.page.island,
      answers: { [statusKey(fixture)]: { status: 500, body: { success: false, message: 'down' } } },
      script: PENDING_SCRIPT,
    });
    await page.advance(3000);
    for (let i = 0; i < 25; i += 1) await page.advance(6000);
    expect(page.requests).toHaveLength(20);
    expect(page.navigate).not.toHaveBeenCalled();
  });

  it('a signed-out pending page polls nothing', async () => {
    const fixture = loadCase('payment_pending_neutral');
    const page = await boot({ html: fixture.page.html, island: fixture.page.island, script: PENDING_SCRIPT });
    expect(page.pending()).toEqual([]);
    expect(page.requests).toEqual([]);
  });
});

// ------------------------------------------------------------------ the checkout

describe('the checkout wait and the cash rescue', () => {
  const WORDS = loadCase('payment_cancel').page.island.order_actions;
  const statusEl = (page) => page.$1('#checkoutPaymentState');
  const checkoutShell = '<div id="checkoutPaymentState" hidden></div>';

  it('checkout waits the published seconds before opening Click', async () => {
    const answer = loadCase('checkout_click_slow').exchange.response.data;
    expect(answer.payment_ready_in_seconds).toBe(45);
    const page = await boot({ html: checkoutShell });

    page.window.OrderActions.openPaymentWhenReady(answer, { words: WORDS, statusEl: statusEl(page) });

    expect(squash(statusEl(page).textContent)).toBe(WORDS.preparing_payment);
    expect(statusEl(page).hidden).toBe(false);
    await page.advance(44900);
    expect(page.navigate).not.toHaveBeenCalled();
    await page.advance(100);
    expect(page.navigate).toHaveBeenCalledTimes(1);
    expect(page.navigate).toHaveBeenCalledWith(CLICK_URL);
  });

  it('checkout opens Click at once when the backend says there is no wait', async () => {
    const answer = loadCase('checkout_click_fast').exchange.response.data;
    expect(answer.payment_ready_in_seconds).toBe(0);
    const page = await boot({ html: checkoutShell });

    page.window.OrderActions.openPaymentWhenReady(answer, { words: WORDS, statusEl: statusEl(page) });

    expect(page.navigate).toHaveBeenCalledWith(CLICK_URL);
    expect(statusEl(page).hidden).toBe(true);
  });

  it('opens Click without a status element too (the cabinet Pay has none)', async () => {
    const page = await boot({});
    page.window.OrderActions.openPaymentWhenReady({ payment_url: CLICK_URL }, { words: WORDS });
    expect(page.navigate).toHaveBeenCalledWith(CLICK_URL);
  });

  const rescue = async (outage, answers = {}, onRetry = vi.fn()) => {
    const failure = loadCase(outage).exchange.response;
    const page = await boot({ html: checkoutShell, answers });
    page.window.OrderActions.showCashRescue(statusEl(page), failure, { words: WORDS, onRetry });
    return { page, failure, onRetry };
  };

  it('an Asl belgisi outage offers Pay with cash and Try again; cash opens the new order', async () => {
    const ok = loadCase('rescue_ok').exchange;
    const failure = loadCase('rescue_asl_down').exchange.response;
    expect(ok.path).toBe(`/api/v1/orders/${failure.data.cancelled_order_id}/retry-with-cash`);
    const { page } = await rescue('rescue_asl_down', { [`POST ${ok.path}`]: { status: ok.status, body: ok.response } });

    expect(squash(statusEl(page).textContent)).toContain(failure.message);
    expect(labels(statusEl(page))).toEqual([WORDS.switch_to_cash, WORDS.try_again]);

    await press(page, page.$1('[data-order-action="switch-to-cash"]'));

    expect(posts(page)).toHaveLength(1);
    expect(posts(page)[0].body).toEqual({});
    expect(posts(page)[0].options.headers['X-CSRF-TOKEN']).toBe(CSRF);
    expect(posts(page)[0].options.headers.Authorization).toBeUndefined();
    expect(page.navigate).toHaveBeenCalledWith(`/order-confirmation?order_id=${ok.response.data.order.id}`);
  });

  it('Try again clears the rescue and retries the order', async () => {
    const { page, onRetry } = await rescue('rescue_asl_down');
    await press(page, page.$1('[data-order-action="try-again"]'));
    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(statusEl(page).hidden).toBe(true);
    expect(page.requests).toEqual([]);
  });

  it('over the cash-debt limit only Try again is offered', async () => {
    const capped = loadCase('rescue_asl_down_capped').exchange.response;
    expect(capped.data.payment_restrictions.cod_restricted).toBe(true);
    const { page } = await rescue('rescue_asl_down_capped');
    // The sentence is the backend's, and does not send the customer to a cash button that is not there.
    expect(squash(statusEl(page).textContent)).toContain(capped.message);
    expect(capped.message).not.toMatch(/cash|naqd|наличн/i);
    expect(labels(statusEl(page))).toEqual([WORDS.try_again]);
    expect(page.$1('[data-order-action="switch-to-cash"]')).toBeNull();
  });

  it('a refused rescue shows the backend message and stays', async () => {
    const refused = loadCase('rescue_cod_limit').exchange;
    expect(refused.status).toBe(400);
    const { page } = await rescue('rescue_asl_down', { [`POST ${refused.path}`]: { status: refused.status, body: refused.response } });

    await press(page, page.$1('[data-order-action="switch-to-cash"]'));

    expect(squash(page.$1('.order-actions__error', statusEl(page)).textContent)).toBe(refused.response.message);
    expect(page.navigate).not.toHaveBeenCalled();
    expect(labels(statusEl(page))).toEqual([WORDS.switch_to_cash, WORDS.try_again]);
    expect(page.$1('[data-order-action="switch-to-cash"]').getAttribute('aria-disabled')).toBeNull();
  });

  it('a stale rescue (409) shows its message inline, as a 400 does', async () => {
    const refused = loadCase('rescue_cod_limit').exchange;
    const message = 'This order is not eligible for cash retry';
    const { page } = await rescue('rescue_asl_down', { [`POST ${refused.path}`]: { status: 409, body: { success: false, message } } });
    await press(page, page.$1('[data-order-action="switch-to-cash"]'));
    expect(squash(page.$1('.order-actions__error', statusEl(page)).textContent)).toBe(message);
    expect(page.navigate).not.toHaveBeenCalled();
  });
});

// ------------------------------------------------------------------ checkout.js, booted

describe('checkout.js places a Click order and reads the answer', () => {
  const WORDS = loadCase('payment_cancel').page.island.order_actions;
  const CART = [{ product_id: 1, quantity: 2 }];
  const SHELL = `
    <div id="loading-overlay" style="display:none"></div>
    <input id="delivery-date" type="date"><div id="time-slots-container"></div>
    <div id="payment-methods-container"></div><input id="selected-payment-method">
    <div class="address-card selected" data-address-id="1"><input type="radio"></div>
    <textarea id="order-notes"></textarea><input type="checkbox" id="agree-terms" checked>
    <div id="summary-items"></div><span id="summary-subtotal"></span><span id="summary-delivery"></span>
    <span id="summary-total"></span>
    <div id="summary-discount-row"><span id="summary-discount-label"></span><span id="summary-discount"></span></div>
    <div id="summary-reward-row"><span id="summary-reward-label"></span><span id="summary-reward"></span></div>
    <div id="summary-tier-row"><span id="summary-tier-label"></span><span id="summary-tier"></span></div>
    <div id="summary-cod-savings-row"><span id="summary-cod-savings"></span></div>
    <div id="min-order-warning"><span id="min-order-title"></span><span id="min-order-text"></span></div>
    <button id="place-order-btn" data-action="place-order">Place order</button>
    <div id="checkoutPaymentState" hidden></div>`;
  const ISLAND = {
    min_order_amount: 10000,
    current_language: 'en',
    cart_url: '/cart',
    login_url: '/login',
    checkout_url: '/checkout',
    default_image: '/x.png',
    i18n: { tier_discount_line: '{tier} {percent}', window_anytime: 'Any time', place_order_failed: 'failed' },
    order_actions: WORDS,
  };
  const ORDERS = '/api/v1/orders/';
  const ok = (body, status = 200) => ({ status, body });
  const exchangeOf = (name) => loadCase(name).exchange;
  const asAnswer = (name) => ok(exchangeOf(name).response, exchangeOf(name).status);
  const bootCheckout = (orderAnswers, extra = {}) => boot({
    html: SHELL,
    island: ISLAND,
    user: { id: 7 },
    cart: CART,
    script: read('business_app/static/js/pages/checkout.js'),
    answers: {
      'GET /api/v1/payments/methods?context=order': ok({ success: true, data: { available_methods: [{ method: 'click', display_name: 'Click' }] } }),
      'POST /api/v1/orders/cart/estimate': ok({ success: true, data: { items: [], pricing: { items_subtotal: 60000, delivery_fee: 0, final_total: 60000, tier_discount: 0 } } }),
      [`POST ${ORDERS}`]: orderAnswers,
      ...extra,
    },
  });
  const placeOrder = (page) => press(page, page.$1('#place-order-btn'));
  const orderPosts = (page) => page.requests.filter((r) => r.key === `POST ${ORDERS}`);
  const stateEl = (page) => page.$1('#checkoutPaymentState');
  const overlay = (page) => page.$1('#loading-overlay').style.display;

  it('sends the order it was built from, then waits the published seconds before opening Click', async () => {
    const exchange = exchangeOf('checkout_click_slow');
    const page = await bootCheckout(asAnswer('checkout_click_slow'));
    await settle(page.window);
    expect(page.$1('#place-order-btn').disabled).toBe(false);

    await placeOrder(page);

    expect(orderPosts(page)).toHaveLength(1);
    expect(orderPosts(page)[0].body).toEqual({ ...exchange.body, delivery_date: page.$1('#delivery-date').value });
    expect(squash(stateEl(page).textContent)).toBe(WORDS.preparing_payment);
    expect(stateEl(page).hidden).toBe(false);
    expect(overlay(page)).toBe('none');
    expect(page.$1('#place-order-btn').disabled).toBe(true);
    expect(page.window.localStorage.getItem('cart')).toBeNull();
    await page.advance(44900);
    expect(page.navigate).not.toHaveBeenCalled();
    await page.advance(100);
    expect(page.navigate).toHaveBeenCalledTimes(1);
    expect(page.navigate).toHaveBeenCalledWith(CLICK_URL);
  });

  it('opens Click at once when the backend says there is no wait', async () => {
    const page = await bootCheckout(asAnswer('checkout_click_fast'));
    await placeOrder(page);
    expect(page.navigate).toHaveBeenCalledWith(CLICK_URL);
  });

  it('an Asl belgisi outage offers the backend sentence, Pay with cash and Try again; the button is usable again', async () => {
    const failure = exchangeOf('rescue_asl_down').response;
    const cash = exchangeOf('rescue_ok');
    const page = await bootCheckout(asAnswer('rescue_asl_down'), { [`POST ${cash.path}`]: ok(cash.response, cash.status) });

    await placeOrder(page);

    expect(squash(stateEl(page).textContent)).toContain(failure.message);
    expect(labels(stateEl(page))).toEqual([WORDS.switch_to_cash, WORDS.try_again]);
    expect(overlay(page)).toBe('none');
    expect(page.$1('#place-order-btn').disabled).toBe(false);
    expect(page.window.localStorage.getItem('cart')).not.toBeNull();

    await press(page, page.$1('[data-order-action="switch-to-cash"]'));

    expect(page.requests.filter((r) => r.key === `POST ${cash.path}`)).toHaveLength(1);
    expect(page.navigate).toHaveBeenCalledWith(`/order-confirmation?order_id=${cash.response.data.order.id}`);
  });

  it('Try again places the order again and then waits for Click', async () => {
    const page = await bootCheckout([asAnswer('rescue_asl_down'), asAnswer('checkout_click_slow')]);
    await placeOrder(page);
    expect(stateEl(page).querySelector('[data-order-action="try-again"]')).not.toBeNull();

    await press(page, page.$1('[data-order-action="try-again"]'));

    expect(orderPosts(page)).toHaveLength(2);
    expect(squash(stateEl(page).textContent)).toBe(WORDS.preparing_payment);
    expect(page.$1('[data-order-action="try-again"]')).toBeNull();
    await page.advance(45000);
    expect(page.navigate).toHaveBeenCalledWith(CLICK_URL);
  });

  it('a customer over the cash-debt limit is told to try again, with no cash button', async () => {
    const failure = exchangeOf('rescue_asl_down_capped').response;
    const page = await bootCheckout(asAnswer('rescue_asl_down_capped'));

    await placeOrder(page);

    expect(squash(stateEl(page).textContent)).toContain(failure.message);
    expect(failure.message).not.toMatch(/cash|naqd|наличн/i);
    expect(labels(stateEl(page))).toEqual([WORDS.try_again]);
    expect(page.$1('[data-order-action="switch-to-cash"]')).toBeNull();
    expect(page.$1('#place-order-btn').disabled).toBe(false);
  });

  it('any other refusal shows its message and frees the button', async () => {
    const page = await bootCheckout(ok({ success: false, message: 'Order creation failed' }, 400));
    await placeOrder(page);
    expect(page.$1('#place-order-btn').disabled).toBe(false);
    expect(overlay(page)).toBe('none');
    expect(page.navigate).not.toHaveBeenCalled();
  });
});
