// "Add to cart" on /shop and /product/<id> (website redesign step 3, group "shop", 2026-11-14).
//
// Owner decision 2026-11-13: the button adds the smallest order the backend publishes, never one bottle below it.
// The backend prints that number into the page (tests/integration/test_web_shop_catalog.py reads it off the rendered
// HTML): the shop card's button carries it as data-qty (the card's default action, add-to-cart-shop, which base.js
// handles on every page), the product page's stepper starts at #qtyInput data-min-qty.
// Here the SHIPPED _page-data.js, base.js and the page script run in jsdom on that markup; only `fetch` is replaced.
// Signed out, the line lands in the local cart; signed in, base.js addToCart also sends
// POST /api/v1/cart/items {product_id, quantity} with X-CSRF-TOKEN, the request the backend test replays.
import fs from 'node:fs';
import path from 'node:path';
import { JSDOM } from 'jsdom';

const REPO = path.resolve(process.cwd(), '..');
// Repo files named in this test (never user input).
// eslint-disable-next-line security/detect-non-literal-fs-filename
const read = (file) => fs.readFileSync(path.join(REPO, file), 'utf8');
const PAGE_DATA_JS = 'business_app/static/js/pages/_page-data.js';
const BASE_JS = 'business_app/static/js/pages/base.js';
const PRODUCT_JS = 'business_app/static/js/pages/product-detail.js';

// The shared product card's button (partials/product_card.html) as /shop and the product page's related sizes print it:
// the card's default action, which base.js handles.
const card = (id, qty) => `<article class="ae-product shop-block"><button type="button" class="ae-btn ae-btn--primary"
  data-action="add-to-cart-shop" data-product-id="${id}" data-qty="${qty}">Add to cart</button></article>`;

// The product page's stepper, buttons and phone bar (product_detail.html), the smallest order 3, at most 5 (page data
// max_qty), 10 000 a bottle.
const PRODUCT_BODY = `
  <script type="application/json" id="page-data">${JSON.stringify({ max_qty: 5, min_order_quantity: 3, unit_price: 10000, units_label: '{n} шт.' })}</script>
  <div class="shop-pd__qty">
    <button type="button" data-action="change-qty" data-delta="-1" disabled>-</button>
    <input type="text" id="qtyInput" value="3" inputmode="numeric" data-min-qty="3" data-max-qty="5">
    <button type="button" data-action="change-qty" data-delta="1">+</button>
  </div>
  <span id="shop-pd-sum">30 000</span>
  <div id="shop-pd-actions"><button type="button" data-action="add-to-cart-qty" data-product-id="3">Add</button></div>
  <section class="related-section">${card(2, 2)}</section>
  <div id="shop-pd-bar" hidden>
    <span id="shop-pd-bar-qty">3 шт.</span><span id="shop-pd-bar-sum">30 000</span>
    <button type="button" data-action="add-to-cart-qty" data-product-id="3">В корзину</button>
  </div>`;

function boot({ body, script, signedIn = false }) {
  const base = {
    api_base_url: '/api/v1',
    current_user: signedIn ? { id: 44 } : null,
    is_authenticated: signedIn,
    login_url: '/login',
    logout_url: '/logout',
    i18n: { product_added: 'Added' },
  };
  const dom = new JSDOM(
    `<!doctype html><html><head></head><body><script type="application/json" id="base-page-data">${JSON.stringify(base)}</script>${body}</body></html>`,
    { url: 'http://localhost/shop', runScripts: 'outside-only', pretendToBeVisual: true },
  );
  const { window } = dom;
  if (signedIn) window.document.cookie = 'csrf_access_token=csrf-access-value';
  const requests = [];
  window.fetch = async (url, options = {}) => {
    requests.push({ method: (options.method || 'GET').toUpperCase(), url, options });
    return new Response(JSON.stringify({ success: true, data: { cart: {} } }), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    });
  };
  [PAGE_DATA_JS, BASE_JS, script].filter(Boolean).map(read).forEach((source) => window.eval(source));
  return { window, requests };
}

// A visitor taps after the page has loaded: base.js has then replaced the local cart with the database cart (signed
// in; this fake database cart is empty) and published window.cartSyncPromise.
async function loaded(page) {
  await new Promise((resolve) => { setTimeout(resolve, 0); });
  await page.window.cartSyncPromise;
  return page;
}

const click = (window, selector) => window.document.querySelector(selector).click();
const localCart = (window) => JSON.parse(window.localStorage.getItem('cart') || '[]');
const cartPosts = (requests) => requests
  .filter((r) => r.method === 'POST' && r.url === '/api/v1/cart/items')
  .map((r) => ({ body: JSON.parse(r.options.body), csrf: r.options.headers['X-CSRF-TOKEN'] }));
const settle = () => new Promise((resolve) => { setTimeout(resolve, 0); });

describe('/shop card "Add to cart" (base.js, no page script)', () => {
  it('signed out, puts the smallest order the card publishes in the local cart', async () => {
    const { window, requests } = await loaded(boot({ body: card(2, 2) + card(3, 3) }));

    click(window, '[data-product-id="3"]');

    expect(localCart(window)).toEqual([{ product_id: 3, quantity: 3 }]);
    expect(cartPosts(requests)).toEqual([]);
  });

  it('signed in, also sends that quantity to the database cart', async () => {
    const { window, requests } = await loaded(boot({ body: card(2, 2), signedIn: true }));

    click(window, '[data-product-id="2"]');
    await settle();

    expect(localCart(window)).toEqual([{ product_id: 2, quantity: 2 }]);
    expect(cartPosts(requests)).toEqual([{ body: { product_id: 2, quantity: 2 }, csrf: 'csrf-access-value' }]);
  });
});

// The card's DEFAULT action (partials/product_card.html action='add-to-cart-shop') is base.js's own handler, on every
// page that loads base.js: it too adds the smallest order the card publishes, never one bottle below it.
describe('product card default action (base.js add-to-cart-shop)', () => {
  const defaultCard = (id, qty) => `<article class="ae-product shop-block"><button type="button" class="ae-btn ae-btn--primary"
    data-action="add-to-cart-shop" data-product-id="${id}"${qty === undefined ? '' : ` data-qty="${qty}"`}>Add to cart</button></article>`;

  it('signed in, sends the published smallest order to the database cart', async () => {
    const { window, requests } = await loaded(boot({ body: defaultCard(2, 3), signedIn: true }));

    click(window, '[data-product-id="2"]');
    await settle();

    expect(localCart(window)).toEqual([{ product_id: 2, quantity: 3 }]);
    expect(cartPosts(requests)).toEqual([{ body: { product_id: 2, quantity: 3 }, csrf: 'csrf-access-value' }]);
  });

  it('a card without a published smallest order adds one', async () => {
    const { window } = await loaded(boot({ body: defaultCard(5) }));

    click(window, '[data-product-id="5"]');

    expect(localCart(window)).toEqual([{ product_id: 5, quantity: 1 }]);
  });
});

describe('/product/<id> stepper and "Add to cart"', () => {
  it('starts at the smallest order and never goes below it or above the cap', () => {
    const { window } = boot({ body: PRODUCT_BODY, script: PRODUCT_JS });
    const input = window.document.getElementById('qtyInput');
    const less = window.document.querySelector('[data-delta="-1"]');
    const more = window.document.querySelector('[data-delta="1"]');

    expect(input.value).toBe('3');
    expect(less.disabled).toBe(true);
    less.disabled = false; // even a forced tap cannot go below the smallest order
    less.click();
    expect(input.value).toBe('3');
    more.click();
    more.click();
    more.click();
    expect(input.value).toBe('5');
    expect(more.disabled).toBe(true);
    less.click();
    expect(input.value).toBe('4');
  });

  it('adds the chosen quantity, and a related card adds its own smallest order, once each', async () => {
    const { window, requests } = await loaded(boot({ body: PRODUCT_BODY, script: PRODUCT_JS, signedIn: true }));

    click(window, '#shop-pd-actions [data-action="add-to-cart-qty"]');
    click(window, '.related-section [data-action="add-to-cart-shop"]');
    await settle();

    expect(localCart(window)).toEqual([{ product_id: 3, quantity: 3 }, { product_id: 2, quantity: 2 }]);
    expect(cartPosts(requests).map((p) => p.body)).toEqual([
      { product_id: 3, quantity: 3 },
      { product_id: 2, quantity: 2 },
    ]);
  });

  it('the phone bar prints the quantity and the sum its button adds', async () => {
    const { window, requests } = await loaded(boot({ body: PRODUCT_BODY, script: PRODUCT_JS, signedIn: true }));
    const text = (id) => window.document.getElementById(id).textContent.replace(/\s/g, ' ');

    expect(text('shop-pd-bar-qty')).toBe('3 шт.');
    click(window, '[data-delta="1"]');

    expect(text('shop-pd-bar-qty')).toBe('4 шт.');
    expect(text('shop-pd-bar-sum')).toBe(text('shop-pd-sum'));
    expect(text('shop-pd-bar-sum')).toMatch(/^40 000/);
    click(window, '#shop-pd-bar [data-action="add-to-cart-qty"]');
    await settle();
    expect(cartPosts(requests).map((p) => p.body)).toEqual([{ product_id: 3, quantity: 4 }]);
  });

  it('a typed quantity is held between the smallest order and the cap, and is what gets added', async () => {
    const { window, requests } = await loaded(boot({ body: PRODUCT_BODY, script: PRODUCT_JS, signedIn: true }));
    const input = window.document.getElementById('qtyInput');
    const type = (value) => {
      input.focus();
      input.value = value;
      input.dispatchEvent(new window.Event('input', { bubbles: true }));
    };
    const leave = () => input.dispatchEvent(new window.Event('change', { bubbles: true }));

    type('4x');
    expect(input.value).toBe('4'); // digits only while typing
    leave();
    expect(input.value).toBe('4');
    expect(window.document.querySelector('[data-delta="-1"]').disabled).toBe(false);

    type('1');
    leave();
    expect(input.value).toBe('3'); // never below the smallest order

    type('50');
    leave();
    expect(input.value).toBe('5'); // never above the cap
    expect(window.document.querySelector('[data-delta="1"]').disabled).toBe(true);

    // Typed and tapped "Add" without leaving the field first: the typed number counts, held to the range.
    type('4');
    click(window, '#shop-pd-actions [data-action="add-to-cart-qty"]');
    await settle();
    expect(cartPosts(requests).map((p) => p.body)).toEqual([{ product_id: 3, quantity: 4 }]);
  });
});
