// /checkout (website redesign 2026-11-14, group "checkout", unit "checkout-flow").
//
// The SHIPPED scripts run in jsdom as the page loads them: jquery, popper, bootstrap (the wizard's modal),
// _page-data.js, base.js (its real apiRequest), order-actions.js, map-helper.js, address-wizard.js and
// pages/checkout.js, over the markup and data islands tests/integration/test_web_checkout_flow.py captured from the
// rendered page (tests/fixtures/web_checkout/shell.html: two addresses, the main one, 101, first). Only `fetch` is
// replaced. The wizard's POST body is the fixture the addresses suite replays through the real endpoint
// (tests/fixtures/web_addresses/new_payload.json). Placing a Click order and the cash rescue are covered by
// paymentPages.test.js; this file covers what the redesign changed around them.
import fs from 'node:fs';
import path from 'node:path';
import { JSDOM, VirtualConsole } from 'jsdom';

const REPO = path.resolve(process.cwd(), '..');
// Repo files named in this test (never user input).
// eslint-disable-next-line security/detect-non-literal-fs-filename
const read = (file) => fs.readFileSync(path.join(REPO, file), 'utf8');
const SCRIPTS = [
  'business_app/static/js/jquery.js',
  'business_app/static/js/popper.min.js',
  'business_app/static/js/bootstrap.min.js',
  'business_app/static/js/pages/_page-data.js',
  'business_app/static/js/pages/base.js',
  'business_app/static/js/pages/order-actions.js',
  'business_app/static/js/map-helper.js',
  'business_app/static/js/address-wizard.js',
  'business_app/static/js/pages/checkout.js',
].map(read);
const SHELL = read('tests/fixtures/web_checkout/shell.html');
const NEW = JSON.parse(read('tests/fixtures/web_addresses/new_payload.json'));
const escapeJson = (value) => JSON.stringify(value).replace(/</g, '\\u003c');
const ok = (data, status = 200) => ({ status, body: { success: true, data } });

const CART = [{ product_id: 5, quantity: 2 }];
const GEO = {
  center: { latitude: 41.2995, longitude: 69.2401 },
  polygon: [[41.0, 69.0], [41.0, 69.5], [41.5, 69.5], [41.5, 69.0]],
  districts: [{ key: 'chilanzar', name: 'Chilanzar' }],
};
const QUOTE = {
  items: [{ product_id: 5, product_name: '19 litrlik suv', quantity: 2, unit_price: 18000, subtotal: 36000, total_price: 36000 }],
  pricing: { items_subtotal: 36000, delivery_fee: 0, discount_amount: 0, loyalty_discount: 0, tier_discount: 0, cod_savings: 0, final_total: 36000 },
};
const QUOTE_NEW = {
  items: [{ product_id: 5, product_name: '19 litrlik suv', quantity: 2, unit_price: 20000, subtotal: 40000, total_price: 40000 }],
  pricing: { items_subtotal: 40000, delivery_fee: 0, discount_amount: 0, loyalty_discount: 0, tier_discount: 0, cod_savings: 0, final_total: 40000 },
};
const PRICE_CHANGED = {
  status: 409,
  body: { success: false, message: 'x', data: { reason_code: 'STATE_CHANGED', reason: 'price_changed', quote: QUOTE_NEW } },
};
const METHODS = { available_methods: [{ method: 'cash', display_name: 'Cash on Delivery' }, { method: 'click', display_name: 'Click' }] };
const PRODUCTS = { products: [{ id: 5, name: 'Pure water 19 L', min_order_quantity: 1, inventory: { min_order_quantity: 1 }, media: { images: ['/p.png'] } }] };
const ESTIMATE = 'POST /api/v1/orders/cart/estimate';
const ORDERS = 'POST /api/v1/orders/';

async function click_(window, element) {
  element.click();
  await settle(window);
}

async function settle(window, rounds = 40) {
  for (let i = 0; i < rounds; i += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, 0));
  }
}

/** `answers` maps "METHOD /api/v1/path" to an answer or a queue of answers; every request is recorded.
    `prepare(document)` runs before the scripts (to draw the page as the server would for another customer). */
async function boot(extra = {}, prepare = null) {
  const base = {
    api_base_url: '/api/v1', current_user: { id: 7 }, is_authenticated: true, login_url: '/login', language: 'en',
    money_template: '{amount} UZS', i18n: {},
  };
  const dom = new JSDOM(
    '<!doctype html><html lang="en"><body>' +
    `<script type="application/json" id="base-page-data">${escapeJson(base)}</script>` +
    `<div id="toast-container"></div>${SHELL}</body></html>`,
    { url: 'http://localhost/checkout', runScripts: 'outside-only', pretendToBeVisual: true, virtualConsole: new VirtualConsole() },
  );
  const { window } = dom;
  window.document.cookie = 'csrf_access_token=csrf-cookie-value';
  window.localStorage.setItem('cart', JSON.stringify(CART));
  window.HTMLElement.prototype.scrollIntoView = function scrollIntoView() {};
  if (prepare) prepare(window.document);
  const answers = {
    'GET /api/v1/cart/': ok({ cart: { cart_items: CART } }),
    'GET /api/v1/payments/methods': ok(METHODS),
    'POST /api/v1/products/bulk': ok(PRODUCTS),
    [ESTIMATE]: ok(QUOTE),
    'GET /api/v1/addresses/geo-config': ok(GEO),
    ...extra,
  };
  const requests = [];
  window.fetch = (url, options = {}) => {
    const method = (options.method || 'GET').toUpperCase();
    const key = `${method} ${url.split('?')[0]}`;
    requests.push({ key, url, body: options.body ? JSON.parse(options.body) : undefined });
    const queue = answers[key];
    const answer = Array.isArray(queue) ? (queue.length > 1 ? queue.shift() : queue[0]) : queue;
    if (!answer) return Promise.resolve({ ok: false, status: 599, json: async () => ({ success: false }) });
    return Promise.resolve({ ok: answer.status < 400, status: answer.status, json: async () => JSON.parse(JSON.stringify(answer.body)) });
  };
  SCRIPTS.forEach((source) => window.eval(source));
  if (window.document.readyState !== 'loading') window.document.dispatchEvent(new window.Event('DOMContentLoaded'));
  await settle(window);
  const document = window.document;
  const $1 = (selector) => document.querySelector(selector);
  const $$ = (selector) => Array.from(document.querySelectorAll(selector));
  const click = async (selector) => { $1(selector).click(); await settle(window); };
  const type = (selector, value) => {
    const input = $1(selector);
    input.value = value;
    input.dispatchEvent(new window.Event('input', { bubbles: true }));
  };
  const sent = (key) => requests.filter((request) => request.key === key);
  const island = JSON.parse(document.getElementById('page-data').textContent);
  return { window, document, requests, sent, $1, $$, click, type, island };
}

describe('/checkout', () => {
  it('opens on the main address and quotes once, for it, on the rail the backend offers first', async () => {
    const { $1, sent } = await boot();

    expect($1('.address-card.selected').dataset.addressId).toBe('101');
    expect(sent(ESTIMATE)).toHaveLength(1);
    // The day and window ride along, so the quote judges them as the order will (review checkout-r01).
    expect(sent(ESTIMATE)[0].body).toEqual({
      items: CART, delivery_address_id: 101, delivery_date: $1('#delivery-date').value,
      delivery_window_start: null, delivery_window_end: null, payment_method: 'cash',
    });
    expect($1('#summary-total').textContent).toBe('36 000 UZS');
    expect($1('#checkout-bar-total').textContent).toBe('36 000 UZS');
    expect($1('#place-order-btn').disabled).toBe(false);
  });

  // The quote a courtesy order gets: 2 units kept at the old price, 2 at today's (Task 22); the lines are the server's.
  const COURTESY_QUOTE = {
    items: [{
      product_id: 5, product_name: '19 litrlik suv', quantity: 4, unit_price: 20000, courtesy_quantity: 2,
      courtesy_unit_price: 18000, regular_quantity: 2, subtotal: 76000, total_price: 76000, upcoming_price: null,
    }],
    pricing: { items_subtotal: 76000, delivery_fee: 0, discount_amount: 0, loyalty_discount: 0, tier_discount: 0, cod_savings: 0, final_total: 76000 },
    price_courtesy: { applied: true, expires_on: '2027-02-14' },
  };
  const flat = (text) => text.replace(/\s/g, ' ');

  it('draws a courtesy line, its total and the thank-you sentence from the quote', async () => {
    const { $1, island, window } = await boot({ [ESTIMATE]: ok(COURTESY_QUOTE) });

    const words = { courtesy_part: island.i18n.courtesy_part };
    expect(flat($1('#summary-items .ae-co-line__qty').textContent)).toBe(flat(window.AE.linePriceText(COURTESY_QUOTE.items[0], words)));
    expect(flat($1('#summary-items .ae-co-line__qty').textContent)).toBe('2 × 18 000 UZS (your price, kept for you) + 2 × 20 000 UZS');
    expect(flat($1('#summary-items .ae-co-line__total').textContent)).toBe('76 000 UZS');
    expect($1('#summary-courtesy').hidden).toBe(false);
    expect($1('#summary-courtesy').textContent).toBe(island.i18n.courtesy_thanks);
  });

  it('a quote without a courtesy shows no thank-you sentence', async () => {
    const { $1 } = await boot();

    expect($1('#summary-courtesy').hidden).toBe(true);
    expect($1('#summary-courtesy').textContent).toBe('');
    expect(flat($1('#summary-items .ae-co-line__total').textContent)).toBe('36 000 UZS');
  });

  it('states the coming price under its line', async () => {
    const upcoming = { new_price: 20000, effective_date: '2027-02-01' };
    const quote = { ...QUOTE, items: [{ ...QUOTE.items[0], upcoming_price: upcoming }] };
    const { $1, $$, island, window } = await boot({ [ESTIMATE]: ok(quote) });

    const line = $1('#summary-items .ae-co-line__upcoming[data-upcoming-price="5"]');
    expect(line.textContent).toBe(window.AE.upcomingPriceText(upcoming, island.i18n.upcoming_price));
    expect(line.textContent).not.toBe('');
    expect($$('#summary-items .ae-co-line__upcoming')).toHaveLength(1);
  });

  it('names the product in the visitor’s language and the ways to pay in the page’s words', async () => {
    const { $1, $$, island } = await boot();

    expect($1('#summary-items .ae-co-line__name').textContent).toBe('Pure water 19 L');
    expect($1('#summary-items .ae-co-line__qty').textContent).toBe('2 × 18 000 UZS');
    expect($$('.payment-method').map((card) => card.dataset.method)).toEqual(['cash', 'click']);
    expect($1('.payment-method[data-method="cash"] .payment-method-name').textContent).toBe(island.method_labels.cash);
    expect($1('.payment-method[data-method="cash"] .payment-method-desc').textContent).toBe(island.method_notes.cash);
    expect($1('.payment-method[data-method="cash"] input[type=radio]').checked).toBe(true);
  });

  it('re-quotes on the rail the customer picks', async () => {
    const { $1, sent, window } = await boot();

    const radio = $1('.payment-method[data-method="click"] input[type=radio]');
    radio.checked = true;
    radio.dispatchEvent(new window.Event('change', { bubbles: true }));
    await settle(window);

    expect(sent(ESTIMATE).map((r) => r.body.payment_method)).toEqual(['cash', 'click']);
    expect($1('#selected-payment-method').value).toBe('click');
    expect($1('.payment-method[data-method="click"]').classList.contains('selected')).toBe(true);
  });

  it('“Add address” runs the shared wizard; the saved address joins the list, selected, and is quoted', async () => {
    const created = { ...NEW, id: 90, is_default: false, display_label: NEW.title };
    const { $1, sent, click, type } = await boot({
      'POST /api/v1/addresses/geocode': ok({ latitude: NEW.latitude, longitude: NEW.longitude, formatted_address: 'x' }),
      'POST /api/v1/addresses/reverse-geocode': ok({ formatted_address: NEW.full_address, district_key: 'chilanzar' }),
      'POST /api/v1/auth/addresses': ok({ address: created }, 201),
    });

    await click('#address-grid [data-action="show-add-address"]');
    expect($1('#addressWizardModal').classList.contains('show')).toBe(true);
    expect($1('#isDefault').checked).toBe(false);  // the customer has a main address already

    type('#addressSearchInput', 'Bunyodkor 5');
    await click('#searchAddressBtn');
    await click('#wizardNextBtn');
    type('#addressTitle', NEW.title);
    type('#streetAddress', NEW.street_address);
    type('#floorNumber', NEW.floor_number);
    type('#apartmentNumber', NEW.apartment_number);
    type('#landmark', NEW.landmark);
    await click('#wizardNextBtn');
    type('#deliveryInstructions', NEW.delivery_instructions);
    await click('#wizardSaveBtn');

    expect(sent('POST /api/v1/auth/addresses').map((r) => r.body)).toEqual([NEW]);
    const card = $1('.address-card[data-address-id="90"]');
    expect(card).not.toBeNull();
    expect(card.classList.contains('selected')).toBe(true);
    expect(card.querySelector('input[type=radio]').checked).toBe(true);
    expect(card.querySelector('.ae-co-addr__name').textContent).toBe(NEW.title);
    expect(card.querySelector('.ae-co-addr__note span').textContent).toBe(NEW.delivery_instructions);
    expect($1('.address-card[data-address-id="101"]').classList.contains('selected')).toBe(false);
    // The new card sits before the "Add address" tile, and the next quote is for it.
    expect(card.nextElementSibling.dataset.action).toBe('show-add-address');
    expect(sent(ESTIMATE).at(-1).body.delivery_address_id).toBe(90);
  });

  it('“Use this address” after a 409 selects the existing card with its door notes and quotes it', async () => {
    const existing = {
      id: 102, title: 'Home', display_label: 'Home', full_address: 'x', delivery_instructions: 'Gate 2, code 45', is_default: false, distance_m: 9,
    };
    const { $1, $$, sent, click, type } = await boot({
      'POST /api/v1/addresses/geocode': ok({ latitude: NEW.latitude, longitude: NEW.longitude, formatted_address: 'x' }),
      'POST /api/v1/addresses/reverse-geocode': ok({ formatted_address: NEW.full_address, district_key: 'chilanzar' }),
      'POST /api/v1/auth/addresses': {
        status: 409,
        body: { success: false, message: 'x', error_code: 'ADDRESS_POSSIBLE_DUPLICATE', details: { move_address_id: null, candidates: [existing] } },
      },
    });
    await click('#address-grid [data-action="show-add-address"]');
    type('#addressSearchInput', 'Bunyodkor 5');
    await click('#searchAddressBtn');
    await click('#wizardNextBtn');
    type('#addressTitle', NEW.title);
    await click('#wizardNextBtn');
    await click('#wizardSaveBtn');
    const quotes = sent(ESTIMATE).length;
    await click('#addressWizardDuplicate [data-candidate-id="102"] [data-action="duplicate-use"]');

    expect(sent('POST /api/v1/auth/addresses')).toHaveLength(1);
    expect($1('.address-card[data-address-id="102"]').classList.contains('selected')).toBe(true);
    expect($1('.address-card[data-address-id="101"]').classList.contains('selected')).toBe(false);
    expect($1('.address-card[data-address-id="102"] .ae-co-addr__note span').textContent).toBe('Gate 2, code 45');
    expect($$('.address-card[data-address-id="102"]')).toHaveLength(1);
    expect(sent(ESTIMATE).length).toBe(quotes + 1);
    expect(sent(ESTIMATE).at(-1).body.delivery_address_id).toBe(102);
  });

  it('a customer with no address gets the empty state, whose button opens the wizard ticking “main address”', async () => {
    const { $1, sent, click } = await boot({}, (document) => {
      document.querySelectorAll('#address-grid .address-card').forEach((card) => card.remove());
      document.getElementById('address-grid').hidden = true;
      document.getElementById('address-empty').hidden = false;
    });

    expect(sent(ESTIMATE)[0].body.delivery_address_id).toBeNull();
    await click('#address-empty [data-action="show-add-address"]');
    expect($1('#addressWizardModal').classList.contains('show')).toBe(true);
    expect($1('#isDefault').checked).toBe(true);
  });

  it('below the minimum it states the server’s floor and shortfall and holds the order back', async () => {
    const { $1, $$, island } = await boot({
      [ESTIMATE]: { status: 400, body: { success: false, message: 'x', error_code: 'ORDER_MIN_AMOUNT', details: { min_amount: 20000, items_subtotal: 15000 } } },
    });

    expect($1('#min-order-warning').style.display).toBe('flex');
    expect($1('#min-order-title').textContent).toBe(island.i18n.min_amount_title.replace('{minimum}', '20 000 UZS'));
    expect($1('#min-order-text').textContent).toBe(island.i18n.min_amount_text.replace('{remaining}', '5 000 UZS'));
    expect($$('[data-action="place-order"]').map((b) => b.disabled)).toEqual([true, true]);
    expect($1('#summary-total').textContent).toBe('—');
    // The cart's lines stay listed, without money.
    expect($1('#summary-items .ae-co-line__name').textContent).toBe('Pure water 19 L');
    expect($1('#summary-items .ae-co-line__total').textContent).toBe('');
  });

  it('a line under its smallest order is named, from the catalogue', async () => {
    const products = { products: [{ ...PRODUCTS.products[0], min_order_quantity: 3, inventory: { min_order_quantity: 3 } }] };
    const { $1, island } = await boot({
      'POST /api/v1/products/bulk': ok(products),
      [ESTIMATE]: { status: 400, body: { success: false, message: 'Cart validation failed' } },
    });

    expect($1('#min-order-title').textContent).toBe(island.i18n.min_qty_title);
    expect($1('#min-order-text').textContent).toBe(
      island.i18n.min_qty_line.replace('{name}', 'Pure water 19 L').replace('{min}', '3').replace('{remaining}', '1'),
    );
    expect($1('#place-order-btn').disabled).toBe(true);
  });

  it('any other refused quote says the total could not be worked out', async () => {
    const { $1, island } = await boot({ [ESTIMATE]: { status: 500, body: { success: false } } });

    expect($1('#min-order-title').textContent).toBe(island.i18n.quote_failed_title);
    expect($1('#place-order-btn').disabled).toBe(true);
  });

  it('unaccepted terms are said next to the box and nothing is sent; the phone bar places the same order', async () => {
    const { $1, sent, click, island } = await boot({
      [ORDERS]: ok({ order: { id: 55 } }, 201),
    });

    await click('#checkout-bar [data-action="place-order"]');
    expect(sent(ORDERS)).toHaveLength(0);
    expect($1('#agree-terms-error').textContent).toBe(island.i18n.agree_terms);
    expect($1('#agree-terms').getAttribute('aria-invalid')).toBe('true');

    $1('#agree-terms').click();
    expect($1('#agree-terms-error').textContent).toBe('');
    await click('#checkout-bar [data-action="place-order"]');
    expect(sent(ORDERS).map((r) => r.body)).toEqual([{
      items: CART, delivery_address_id: 101, delivery_date: $1('#delivery-date').value, delivery_window_start: null,
      delivery_window_end: null, payment_method: 'cash', delivery_notes: null, source: 'web', expected_total: 36000,
    }]);
  });

  it('an order refused because the total changed redraws the quote, says so, and the next tap sends the new total', async () => {
    const { $1, sent, island, window } = await boot({ [ORDERS]: [PRICE_CHANGED, ok({ order: { id: 55 } }, 201)] });
    $1('#agree-terms').click();

    await click_(window, $1('#place-order-btn'));

    expect(sent(ORDERS)).toHaveLength(1);
    expect($1('#summary-total').textContent).toBe('40 000 UZS');
    expect($1('#checkout-price-changed').hidden).toBe(false);
    expect($1('#checkout-price-changed').textContent).toContain(island.i18n.price_changed);
    expect(window.document.activeElement).toBe($1('#checkout-price-changed'));
    expect($1('#place-order-btn').disabled).toBe(false);
    expect($1('#loading-overlay').style.display).toBe('none');
    expect($1('#toast-container').textContent).toBe('');
    expect(window.location.href).toBe('http://localhost/checkout');

    await click_(window, $1('#place-order-btn'));

    expect(sent(ORDERS)).toHaveLength(2);
    expect(sent(ORDERS)[1].body.expected_total).toBe(40000);
    expect($1('#checkout-price-changed').hidden).toBe(true);
  });

  it('the same refusal on Click redraws and opens nothing', async () => {
    const { $1, sent, click, island, window } = await boot({ [ORDERS]: PRICE_CHANGED });
    $1('.payment-method[data-method="click"] input[type=radio]').click();
    await settle(window);
    $1('#agree-terms').click();

    await click_(window, $1('#place-order-btn'));

    expect(sent(ORDERS)).toHaveLength(1);
    expect(sent(ORDERS)[0].body).toMatchObject({ payment_method: 'click', expected_total: 36000 });
    expect($1('#summary-total').textContent).toBe('40 000 UZS');
    expect($1('#checkout-price-changed').hidden).toBe(false);
    expect($1('#checkout-price-changed').textContent).toContain(island.i18n.price_changed);
    expect($1('#place-order-btn').disabled).toBe(false);
    expect($1('#loading-overlay').style.display).toBe('none');
    expect($1('#checkoutPaymentState').hidden).toBe(true);
    expect(window.location.href).toBe('http://localhost/checkout');
    expect(window.localStorage.getItem('cart')).not.toBeNull();
  });

  it('a delivery window fills the two fields the order carries', async () => {
    const { $1, sent, click, window } = await boot({ [ORDERS]: { status: 400, body: { success: false, message: 'no' } } });

    const radio = $1('.time-slot[data-window-key="morning"] input[type=radio]');
    radio.checked = true;
    radio.dispatchEvent(new window.Event('change', { bubbles: true }));
    expect(radio.getAttribute('aria-label')).toBe(`${$1('.time-slot[data-window-key="morning"] .ae-radio-card__title').textContent} (09:00–12:00)`);
    $1('#agree-terms').click();
    await click('#place-order-btn');

    expect(sent(ORDERS)[0].body).toMatchObject({ delivery_window_start: '09:00', delivery_window_end: '12:00' });
    expect($1('#place-order-btn').disabled).toBe(false);
    expect($1('#loading-overlay').style.display).toBe('none');
  });

  // ---- the day and the window (review checkout-r01/r02/r06): the server publishes them, the quote judges them ----

  const SCHEDULE_REFUSAL = (code) => ({ status: 400, body: { error: 'VALIDATION_ERROR', message: 'delivery window has already passed for today', error_code: code, details: { validation_errors: ['x'] } } });
  const closeMorning = (document) => {
    const island = document.getElementById('page-data');
    const data = JSON.parse(island.textContent);
    data.schedule.windows.find((w) => w.key === 'morning').open_today = false;
    island.textContent = JSON.stringify(data);
  };
  const pick = async (window, $1, key) => {
    const radio = $1(`.time-slot[data-window-key="${key}"] input[type=radio]`);
    radio.checked = true;
    radio.dispatchEvent(new window.Event('change', { bubbles: true }));
    await settle(window);
  };
  const setDay = async (window, $1, value) => {
    $1('#delivery-date').value = value;
    $1('#delivery-date').dispatchEvent(new window.Event('change', { bubbles: true }));
    await settle(window);
  };

  it('draws the windows the server publishes, a window over today offered only for a later day', async () => {
    const { $1, $$, island, window } = await boot({}, closeMorning);

    expect($$('.time-slot').map((tile) => tile.dataset.windowKey)).toEqual(island.schedule.windows.map((w) => w.key));
    expect($1('#delivery-date').value).toBe(island.schedule.today);
    expect($1('.time-slot[data-window-key="morning"] input').disabled).toBe(true);
    expect($1('.time-slot[data-window-key="evening"] input').disabled).toBe(false);

    await setDay(window, $1, '2026-01-02');
    expect($1('.time-slot[data-window-key="morning"] input').disabled).toBe(false);
  });

  it('a window re-quotes with its bounds; a window the quote refuses is said at the window group and holds the order', async () => {
    const { $1, $$, sent, island, window } = await boot({ [ESTIMATE]: [ok(QUOTE), SCHEDULE_REFUSAL('SCHEDULE_WINDOW_PASSED'), ok(QUOTE)] });

    await pick(window, $1, 'morning');
    expect(sent(ESTIMATE)[1].body).toMatchObject({ delivery_window_start: '09:00', delivery_window_end: '12:00' });
    expect($1('#delivery-window-error').textContent).toBe(island.i18n.schedule_window_passed);
    expect($1('#time-slots-container').getAttribute('aria-invalid')).toBe('true');
    expect($1('#min-order-title').textContent).toBe(island.i18n.schedule_title);
    expect($$('[data-action="place-order"]').map((b) => b.disabled)).toEqual([true, true]);

    await pick(window, $1, 'evening');
    expect(sent(ESTIMATE)[2].body).toMatchObject({ delivery_window_start: '18:00', delivery_window_end: '21:00' });
    expect($1('#delivery-window-error').textContent).toBe('');
    expect($1('#time-slots-container').hasAttribute('aria-invalid')).toBe(false);
    expect($1('#place-order-btn').disabled).toBe(false);
  });

  it('a day past the horizon is said at the date field with the last day the server takes', async () => {
    const { $1, island } = await boot({ [ESTIMATE]: SCHEDULE_REFUSAL('SCHEDULE_BEYOND_HORIZON') });

    expect($1('#delivery-date-error').textContent).toBe(island.i18n.schedule_date_far.replace('{date}', island.schedule.max_label));
    expect($1('#delivery-date').getAttribute('aria-invalid')).toBe('true');
    expect($1('#place-order-btn').disabled).toBe(true);
  });

  it('an order refused for its window (it ended after the quote) is said at the field, never as an English toast', async () => {
    const { $1, sent, island, window } = await boot({
      [ORDERS]: { status: 400, body: { success: false, message: 'Validation failed', errors: ['delivery window has already passed for today'], data: { error_code: 'SCHEDULE_WINDOW_PASSED' } } },
    });
    $1('#agree-terms').click();
    await click_(window, $1('#place-order-btn'));

    expect(sent(ORDERS)).toHaveLength(1);
    expect($1('#delivery-window-error').textContent).toBe(island.i18n.schedule_window_passed);
    expect($1('#toast-container').textContent).not.toContain('Validation failed');
    expect($1('#loading-overlay').style.display).toBe('none');
    expect($1('#place-order-btn').disabled).toBe(true);
  });

  it('any other refused order with the generic envelope says the page’s own words', async () => {
    const { $1, island, window } = await boot({
      [ORDERS]: { status: 400, body: { success: false, message: 'Validation failed', errors: ['items: Field required'] } },
    });
    $1('#agree-terms').click();
    await click_(window, $1('#place-order-btn'));

    expect($1('#toast-container').textContent).toContain(island.i18n.place_order_failed);
    expect($1('#toast-container').textContent).not.toContain('Validation failed');
    expect($1('#place-order-btn').disabled).toBe(false);
  });

  it('a cleared day is said at the date field and asks no quote; the cart is not blamed', async () => {
    const { $1, sent, island, window } = await boot();
    expect(sent(ESTIMATE)).toHaveLength(1);

    await setDay(window, $1, '');

    expect(sent(ESTIMATE)).toHaveLength(1);
    expect($1('#delivery-date-error').textContent).toBe(island.i18n.select_delivery_date);
    expect($1('#delivery-date').getAttribute('aria-invalid')).toBe('true');
    expect($1('#min-order-title').textContent).not.toBe(island.i18n.quote_failed_title);
    expect($1('#place-order-btn').disabled).toBe(true);

    await setDay(window, $1, island.schedule.today);
    expect(sent(ESTIMATE)).toHaveLength(2);
    expect($1('#delivery-date-error').textContent).toBe('');
    expect($1('#place-order-btn').disabled).toBe(false);
  });
});
