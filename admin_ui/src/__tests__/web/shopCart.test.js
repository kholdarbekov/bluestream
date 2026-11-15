// /cart steppers, notices and the database cart (website redesign step 3, group "shop", unit "shop-cart", 2026-11-14).
//
// The SHIPPED _page-data.js, base.js and cart.js run in jsdom on the hooks cart.html prints
// (tests/integration/test_web_shop_cart.py asserts the rendered page carries every one of them). Only `fetch` is
// replaced, by a fake backend that answers what the real one does: GET /api/v1/cart/ (base.js's database-cart sync),
// POST /api/v1/products/bulk (each product's charged price and inventory.min_order_quantity) and the cart writes.
// The backend test replays the same writes — PUT /api/v1/cart/items/<product_id> {quantity} — over HTTP.
import fs from 'node:fs';
import path from 'node:path';
import { JSDOM } from 'jsdom';

const REPO = path.resolve(process.cwd(), '..');
// Repo files named in this test (never user input).
// eslint-disable-next-line security/detect-non-literal-fs-filename
const read = (file) => fs.readFileSync(path.join(REPO, file), 'utf8');
const SCRIPTS = [
  'business_app/static/js/pages/_page-data.js',
  'business_app/static/js/pages/base.js',
  'business_app/static/js/pages/cart.js',
];

const NBSP = ' ';
const I18N = {
  units_one: '{n} шт.',
  units_other: '{n} шт.',
  unit_price: '{price} за шт.',
  qty_label: 'Количество: {name}',
  qty_less: 'Меньше',
  qty_more: 'Больше',
  remove: 'Убрать «{name}»',
  removed: '«{name}» убрали из корзины.',
  stock_low: 'Осталось {n} шт.',
  stock_out: 'Нет в наличии',
  min_amount_text: 'Добавьте товаров ещё на {remaining}: заказ оформляется от {minimum}.',
  min_qty_line: '{name}: заказ от {min} шт., добавьте ещё {remaining}.',
  min_qty_hint: 'Заказ от {min} шт.',
  update_error: 'Не удалось сохранить изменение.',
};

// cart.html's hooks, the same ids and data attributes.
const CART_BODY = `
  <div id="cart-undo" hidden><p id="cart-undo-text"></p><button type="button" data-action="undo-remove">Вернуть</button></div>
  <div id="cart-dropped" hidden><p>Некоторых товаров больше нет в продаже.</p></div>
  <div id="cart-error" hidden><button type="button" data-action="retry-load">Попробовать снова</button></div>
  <div id="cart-loading"></div>
  <div id="cart-content" hidden>
    <span id="items-count"></span>
    <ul id="cart-items-container"></ul>
    <button type="button" id="cart-clear-btn" data-action="clear-cart">Очистить корзину</button>
    <div id="cart-clear-confirm" hidden>
      <button type="button" data-action="clear-cart-yes">Да</button><button type="button" data-action="clear-cart-no">Нет</button>
    </div>
    <span id="summary-units"></span><span id="summary-subtotal"></span><span id="summary-total"></span>
    <div id="min-order-warning" class="shop-cart__notice" tabindex="-1" hidden><p id="min-order-text"></p><span id="min-order-progress"></span></div>
    <div id="min-qty-warnings" class="shop-cart__notice" tabindex="-1" hidden><ul id="min-qty-warning-list"></ul></div>
    <div class="shop-cart__actions"><a id="checkout-btn" data-checkout href="/checkout">Оформить заказ</a></div>
  </div>
  <div id="empty-cart" hidden></div>
  <div id="cart-bar" hidden><span id="cart-bar-total"></span><a data-checkout href="/checkout">Оформить заказ</a></div>`;

// POST /products/bulk as the API serializes a product (product_serializers: pricing.current_price, inventory.*).
const product = (id, name, price, min, inventory = {}) => ({
  id,
  name,
  slug: null,
  pricing: { current_price: price },
  inventory: {
    min_order_quantity: min, track_inventory: false, stock_quantity: null, is_in_stock: true, is_low_stock: false, ...inventory,
  },
  media: { images: [] },
});
const CATALOGUE = [product(2, 'Вода 19 л', 18000, 2), product(3, 'Вода 10 л', 10000, 3)];

function boot({
  localCart = [], dbCart = null, refuse = false, minOrderAmount = 20000, catalogue = CATALOGUE, bulkFailures = 0,
}) {
  const signedIn = dbCart !== null;
  const base = {
    api_base_url: '/api/v1',
    current_user: signedIn ? { id: 44 } : null,
    is_authenticated: signedIn,
    login_url: '/login',
    logout_url: '/logout',
    money_template: '{amount} сум',
    i18n: { product_added: 'Добавлено' },
  };
  const pageData = {
    product_detail_url_template: '/product/{id}',
    product_detail_url_slug_template: '/product/__SLUG__',
    min_order_amount: minOrderAmount,
    max_qty: 100,
    delivery_fee: 0,
    current_language: 'ru',
    default_image: '/static/images/product-placeholder.svg',
    i18n: I18N,
  };
  const dom = new JSDOM(
    `<!doctype html><html><head></head><body>
      <script type="application/json" id="base-page-data">${JSON.stringify(base)}</script>
      <script type="application/json" id="page-data">${JSON.stringify(pageData)}</script>${CART_BODY}</body></html>`,
    { url: 'http://localhost/cart', runScripts: 'outside-only', pretendToBeVisual: true },
  );
  const { window } = dom;
  window.localStorage.setItem('cart', JSON.stringify(localCart));
  if (signedIn) window.document.cookie = 'csrf_access_token=csrf-access-value';
  const requests = [];
  const json = (body, status = 200) => new Response(JSON.stringify(body), {
    status, headers: { 'Content-Type': 'application/json' },
  });
  window.fetch = async (url, options = {}) => {
    const method = (options.method || 'GET').toUpperCase();
    requests.push({ method, url, body: options.body ? JSON.parse(options.body) : null, csrf: (options.headers || {})['X-CSRF-TOKEN'] });
    if (url === '/api/v1/cart/' && method === 'GET') {
      return json({ success: true, data: { cart: { cart_items: dbCart || [] } } });
    }
    if (url === '/api/v1/products/bulk') {
      if (requests.filter((r) => r.url === url).length <= bulkFailures) return json({ success: false }, 500);
      const ids = JSON.parse(options.body).product_ids;
      return json({ success: true, data: { products: catalogue.filter((p) => ids.includes(p.id)) } });
    }
    return refuse ? json({ success: false, message: 'refused' }, 400) : json({ success: true, data: { cart: {} } });
  };
  SCRIPTS.map(read).forEach((source) => window.eval(source));
  return { window, requests };
}

const settle = async () => {
  for (let i = 0; i < 6; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    await new Promise((resolve) => { setTimeout(resolve, 0); });
  }
};
const $ = (window, selector) => window.document.querySelector(selector);
const step = (window, id, which) => $(window, `.shop-cart__item[data-product-id="${id}"] [data-step="${which}"]`);
const shown = (window, id) => !$(window, `#${id}`).hidden;
const localCart = (window) => JSON.parse(window.localStorage.getItem('cart') || '[]');
const writes = (requests) => requests
  .filter((r) => r.method !== 'GET' && r.url.startsWith('/api/v1/cart/'))
  .map((r) => ({ method: r.method, url: r.url, body: r.body, csrf: r.csrf }));

describe('/cart signed in: the steppers save to the database cart', () => {
  it('steps up and back down to the minimum, and never sends a quantity below it', async () => {
    const { window, requests } = boot({ dbCart: [{ product_id: 2, quantity: 2 }] });
    await settle();

    expect(shown(window, 'cart-content')).toBe(true);
    expect(step(window, 2, 'less').disabled).toBe(true);
    step(window, 2, 'more').click();
    await settle();
    expect(step(window, 2, 'less').disabled).toBe(false);
    step(window, 2, 'less').click();
    await settle();
    expect(step(window, 2, 'less').disabled).toBe(true);
    step(window, 2, 'less').disabled = false; // even a button forced back on cannot go below the minimum
    step(window, 2, 'less').click();
    await settle();

    expect(writes(requests)).toEqual([
      { method: 'PUT', url: '/api/v1/cart/items/2', body: { quantity: 3 }, csrf: 'csrf-access-value' },
      { method: 'PUT', url: '/api/v1/cart/items/2', body: { quantity: 2 }, csrf: 'csrf-access-value' },
    ]);
    expect(localCart(window)).toEqual([{ product_id: 2, quantity: 2 }]);
    expect($(window, '#summary-total').textContent).toBe(`36${NBSP}000 сум`);
  });

  it('puts the quantity back and says so when the server refuses the change', async () => {
    const { window } = boot({ dbCart: [{ product_id: 2, quantity: 2 }], refuse: true });
    await settle();

    step(window, 2, 'more').click();
    await settle();

    expect(localCart(window)).toEqual([{ product_id: 2, quantity: 2 }]);
    expect($(window, '.shop-cart__item[data-product-id="2"] .ae-qty__input').value).toBe('2');
    expect($(window, '#toast-container').textContent).toContain(I18N.update_error);
  });

  it('removes a line, undoes it and clears the cart with the requests the API serves', async () => {
    const { window, requests } = boot({ dbCart: [{ product_id: 2, quantity: 2 }, { product_id: 3, quantity: 3 }] });
    await settle();

    $(window, '.shop-cart__item[data-product-id="3"] [data-action="remove-item"]').click();
    await settle();
    $(window, '[data-action="undo-remove"]').click();
    await settle();
    $(window, '[data-action="clear-cart"]').click();
    $(window, '[data-action="clear-cart-yes"]').click();
    await settle();

    expect(writes(requests).map(({ method, url, body }) => ({ method, url, body }))).toEqual([
      { method: 'DELETE', url: '/api/v1/cart/items/3', body: null },
      { method: 'POST', url: '/api/v1/cart/items', body: { product_id: 3, quantity: 3 } },
      { method: 'POST', url: '/api/v1/cart/clear', body: null },
    ]);
    expect(shown(window, 'empty-cart')).toBe(true);
    expect(localCart(window)).toEqual([]);
  });
});

describe('/cart: a line below its minimum', () => {
  it('says so on the line, steps straight up to the minimum and holds checkout back meanwhile', async () => {
    const { window, requests } = boot({ localCart: [{ product_id: 3, quantity: 1 }] });
    await settle();

    expect($(window, '.shop-cart__item[data-product-id="3"] .shop-cart__hint').textContent).toBe('Заказ от 3 шт.');
    expect(step(window, 3, 'less').disabled).toBe(true);
    expect(step(window, 3, 'more').getAttribute('data-qty')).toBe('3');
    // One notice at a time: the line minimum, not also the order floor (10 000 is below 20 000 as well).
    expect(shown(window, 'min-qty-warnings')).toBe(true);
    expect(shown(window, 'min-order-warning')).toBe(false);
    expect($(window, '#min-qty-warning-list').textContent).toBe('Вода 10 л: заказ от 3 шт., добавьте ещё 2.');
    const checkout = $(window, '#checkout-btn');
    expect(checkout.getAttribute('aria-disabled')).toBe('true');
    const tap = new window.MouseEvent('click', { bubbles: true, cancelable: true });
    checkout.dispatchEvent(tap);
    expect(tap.defaultPrevented).toBe(true);

    step(window, 3, 'more').click();
    await settle();

    expect(localCart(window)).toEqual([{ product_id: 3, quantity: 3 }]);
    expect($(window, '.shop-cart__hint')).toBeNull();
    expect(shown(window, 'min-qty-warnings')).toBe(false);
    expect(shown(window, 'min-order-warning')).toBe(false);
    expect(checkout.hasAttribute('aria-disabled')).toBe(false);
    // Signed out: nothing is sent to a database cart.
    expect(writes(requests)).toEqual([]);
  });

  it('shows the order floor once every line meets its minimum', async () => {
    const { window } = boot({ localCart: [{ product_id: 3, quantity: 3 }], minOrderAmount: 40000 });
    await settle();

    expect(shown(window, 'min-qty-warnings')).toBe(false);
    expect(shown(window, 'min-order-warning')).toBe(true);
    expect($(window, '#min-order-text').textContent)
      .toBe(`Добавьте товаров ещё на 10${NBSP}000 сум: заказ оформляется от 40${NBSP}000 сум.`);
    expect($(window, '#checkout-btn').getAttribute('aria-disabled')).toBe('true');
  });
});

describe('/cart: a product the catalogue no longer sells', () => {
  it('signed out, drops the line from the local cart and the header count, and says so', async () => {
    const { window, requests } = boot({ localCart: [{ product_id: 2, quantity: 2 }, { product_id: 9999, quantity: 5 }] });
    await settle();

    expect(localCart(window)).toEqual([{ product_id: 2, quantity: 2 }]);
    expect(window.cart).toEqual([{ product_id: 2, quantity: 2 }]);
    expect(shown(window, 'cart-dropped')).toBe(true);
    expect(window.document.querySelectorAll('.shop-cart__item').length).toBe(1);
    expect(writes(requests)).toEqual([]);
  });

  it('a cart of nothing but such lines ends empty, with nothing left behind', async () => {
    const { window } = boot({ localCart: [{ product_id: 9999, quantity: 5 }] });
    await settle();

    expect(shown(window, 'empty-cart')).toBe(true);
    expect(shown(window, 'cart-dropped')).toBe(true);
    expect(localCart(window)).toEqual([]);
  });

  it('signed in, also deletes it from the database cart', async () => {
    const { window, requests } = boot({ dbCart: [{ product_id: 3, quantity: 3 }, { product_id: 9999, quantity: 5 }] });
    await settle();

    expect(writes(requests)).toEqual([
      { method: 'DELETE', url: '/api/v1/cart/items/9999', body: null, csrf: 'csrf-access-value' },
    ]);
    expect(localCart(window)).toEqual([{ product_id: 3, quantity: 3 }]);
  });
});

describe('/cart: stock tags are the API\'s answers', () => {
  const tag = (window, id) => {
    const pill = $(window, `.shop-cart__item[data-product-id="${id}"] .ae-status`);
    return pill ? pill.textContent : null;
  };

  it('low and out of stock as inventory.is_low_stock / is_in_stock say, never a number of its own', async () => {
    const catalogue = [
      product(2, 'Вода 19 л', 18000, 2, { track_inventory: true, stock_quantity: 8, is_low_stock: false }),
      product(3, 'Вода 10 л', 10000, 3, { track_inventory: true, stock_quantity: 40, is_low_stock: true }),
      product(4, 'Кулер', 50000, 1, { track_inventory: true, stock_quantity: 0, is_in_stock: false, is_low_stock: true }),
    ];
    const { window } = boot({
      localCart: [{ product_id: 2, quantity: 2 }, { product_id: 3, quantity: 3 }, { product_id: 4, quantity: 1 }], catalogue,
    });
    await settle();

    expect(tag(window, 2)).toBeNull(); // 8 left, but the API does not call it low
    expect(tag(window, 3)).toBe('Осталось 40 шт.');
    expect(tag(window, 4)).toBe('Нет в наличии');
  });

  it('a line asking for more than is left names the number left (the cap checkout holds it to)', async () => {
    const catalogue = [product(2, 'Вода 19 л', 18000, 2, { track_inventory: true, stock_quantity: 3 })];
    const { window } = boot({ localCart: [{ product_id: 2, quantity: 5 }], catalogue });
    await settle();

    expect(tag(window, 2)).toBe('Осталось 3 шт.');
  });
});

describe('/cart: a typed quantity', () => {
  const field = (window, id) => $(window, `.shop-cart__item[data-product-id="${id}"] [data-qty-input]`);
  const typeAndLeave = (window, id, value) => {
    const input = field(window, id);
    input.value = value;
    input.dispatchEvent(new window.Event('input', { bubbles: true }));
    input.dispatchEvent(new window.Event('change', { bubbles: true }));
  };

  it('is saved like a step, held between the minimum and the cap', async () => {
    const { window, requests } = boot({ dbCart: [{ product_id: 2, quantity: 2 }] });
    await settle();

    typeAndLeave(window, 2, '20');
    await settle();
    typeAndLeave(window, 2, '1'); // below the minimum: held at it
    await settle();
    typeAndLeave(window, 2, '500'); // above MAX_QUANTITY_PER_ITEM (page data max_qty 100)
    await settle();

    expect(writes(requests).map(({ method, url, body }) => ({ method, url, body }))).toEqual([
      { method: 'PUT', url: '/api/v1/cart/items/2', body: { quantity: 20 } },
      { method: 'PUT', url: '/api/v1/cart/items/2', body: { quantity: 2 } },
      { method: 'PUT', url: '/api/v1/cart/items/2', body: { quantity: 100 } },
    ]);
    expect(field(window, 2).value).toBe('100');
    expect(localCart(window)).toEqual([{ product_id: 2, quantity: 100 }]);
  });

  it('Enter saves it; the same number sends nothing', async () => {
    const { window, requests } = boot({ dbCart: [{ product_id: 2, quantity: 2 }] });
    await settle();

    const input = field(window, 2);
    input.value = '2';
    input.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    await settle();
    expect(writes(requests)).toEqual([]);
    field(window, 2).value = '6';
    field(window, 2).dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    await settle();

    expect(writes(requests).map((r) => r.body)).toEqual([{ quantity: 6 }]);
    expect(window.document.activeElement).toBe(field(window, 2));
  });
});

describe('/cart: the load error', () => {
  it('offers to try again, and trying again loads the cart', async () => {
    const { window } = boot({ localCart: [{ product_id: 2, quantity: 2 }], bulkFailures: 1 });
    await settle();
    expect(shown(window, 'cart-error')).toBe(true);
    expect(shown(window, 'cart-content')).toBe(false);

    $(window, '[data-action="retry-load"]').click();
    await settle();

    expect(shown(window, 'cart-error')).toBe(false);
    expect(shown(window, 'cart-content')).toBe(true);
    expect(localCart(window)).toEqual([{ product_id: 2, quantity: 2 }]); // a failed load drops nothing
  });
});

describe('/cart: checkout held back', () => {
  it('scrolls to the reason without animation when the visitor asks for reduced motion', async () => {
    const { window } = boot({ localCart: [{ product_id: 3, quantity: 1 }] });
    await settle();
    window.matchMedia = (query) => ({ matches: query === '(prefers-reduced-motion: reduce)' });
    const scrolls = [];
    window.HTMLElement.prototype.scrollIntoView = function scrollIntoView(options) { scrolls.push(options); };

    $(window, '#checkout-btn').click();

    expect(scrolls).toEqual([{ block: 'center', behavior: 'auto' }]);
  });
});
