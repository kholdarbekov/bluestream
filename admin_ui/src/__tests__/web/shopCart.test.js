// /cart steppers, notices and the database cart (website redesign step 3, group "shop", unit "shop-cart", 2026-11-14).
//
// The SHIPPED _page-data.js, base.js and cart.js run in jsdom on the hooks cart.html prints
// (tests/integration/test_web_shop_cart.py asserts the rendered page carries every one of them). Only `fetch` is
// replaced, by a fake backend that answers what the real one does: POST /api/v1/products/bulk (each product's card:
// the public price, pricing.upcoming_price and inventory.min_order_quantity), the cart writes, and GET /api/v1/cart/
// (base.js's database-cart sync, and signed in the money cart.js draws): the database cart kept as state, each line
// priced as CartService.get_cart_details publishes it (at the card price unless `serverLine` says otherwise).
// The backend test replays the same writes — PUT /api/v1/cart/items/<product_id> {quantity} — over HTTP, and reads
// the courtesy fields cart.js draws off the real GET /api/v1/cart/.
import fs from 'node:fs';
import path from 'node:path';
import { JSDOM } from 'jsdom';

const REPO = path.resolve(process.cwd(), '..');
// Repo files named in this test (never user input).
// eslint-disable-next-line security/detect-non-literal-fs-filename
const read = (file) => fs.readFileSync(path.join(REPO, file), 'utf8');
// The month names the real base.html published (date_words), as test_my_subscriptions_page.py rendered it.
const SHELL = JSON.parse(read('tests/fixtures/web_subscription_management/shell.json'));
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
  calculating: 'Считаем…',
  courtesy_part: '{qty} × {price} (ваша цена, сохранили для вас)',
  courtesy_thanks: 'Спасибо, что вы с нами: для этого заказа мы сохранили вашу прежнюю цену.',
  upcoming_price: 'С {date}: {price}.',
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
    <p id="cart-courtesy" hidden></p>
    <div id="min-order-warning" class="shop-cart__notice" tabindex="-1" hidden><p id="min-order-text"></p><span id="min-order-progress"></span></div>
    <div id="min-qty-warnings" class="shop-cart__notice" tabindex="-1" hidden><ul id="min-qty-warning-list"></ul></div>
    <div class="shop-cart__actions"><a id="checkout-btn" data-checkout href="/checkout">Оформить заказ</a></div>
  </div>
  <div id="empty-cart" hidden></div>
  <div id="cart-bar" hidden><span id="cart-bar-total"></span><a data-checkout href="/checkout">Оформить заказ</a></div>`;

// POST /products/bulk as the API serializes a product (product_serializers: pricing.current_price and
// pricing.upcoming_price, inventory.*).
const product = (id, name, price, min, inventory = {}, pricing = {}) => ({
  id,
  name,
  slug: null,
  pricing: { current_price: price, upcoming_price: null, ...pricing },
  inventory: {
    min_order_quantity: min, track_inventory: false, stock_quantity: null, is_in_stock: true, is_low_stock: false, ...inventory,
  },
  media: { images: [] },
});
const CATALOGUE = [product(2, 'Вода 19 л', 18000, 2), product(3, 'Вода 10 л', 10000, 3)];

// GET /api/v1/cart/'s cart as CartService.get_cart_details publishes it: a line of a product on sale carries its money
// (at the card price, no courtesy, unless `serverLine` names that product at that quantity), a line of a product off
// sale is listed unpriced, and the cart carries the subtotal, the total (delivery is free here) and the courtesy block.
function pricedCart(lines, catalogue, serverLine) {
  const priced = lines.map((line) => {
    const card = catalogue.find((p) => p.id === line.product_id);
    if (!card) return { ...line };
    const own = serverLine && serverLine.product_id === line.product_id && serverLine.quantity === line.quantity
      ? serverLine : {};
    return {
      ...line,
      unit_price: card.pricing.current_price,
      total_price: card.pricing.current_price * line.quantity,
      discount_amount: 0,
      courtesy_quantity: null,
      courtesy_unit_price: null,
      regular_quantity: null,
      upcoming_price: null,
      ...own,
    };
  });
  const subtotal = priced.reduce((sum, line) => sum + (line.total_price || 0), 0);
  const courtesy = priced.some((line) => line.courtesy_quantity);
  return {
    cart_items: priced,
    subtotal,
    estimated_total: subtotal,
    price_courtesy: courtesy ? { applied: true, expires_on: '2027-02-14' } : null,
  };
}

function boot({
  localCart = [], dbCart = null, refuse = false, minOrderAmount = 20000, catalogue = CATALOGUE, bulkFailures = 0,
  serverLine = null,
}) {
  const signedIn = dbCart !== null;
  const base = {
    api_base_url: '/api/v1',
    current_user: signedIn ? { id: 44 } : null,
    is_authenticated: signedIn,
    login_url: '/login',
    logout_url: '/logout',
    language: 'ru',
    money_template: '{amount} сум',
    date_words: SHELL.ru.base.date_words,
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
    `<!doctype html><html lang="ru"><head></head><body>
      <script type="application/json" id="base-page-data">${JSON.stringify(base)}</script>
      <script type="application/json" id="page-data">${JSON.stringify(pageData)}</script>${CART_BODY}</body></html>`,
    { url: 'http://localhost/cart', runScripts: 'outside-only', pretendToBeVisual: true },
  );
  const { window } = dom;
  window.localStorage.setItem('cart', JSON.stringify(localCart));
  if (signedIn) window.document.cookie = 'csrf_access_token=csrf-access-value';
  const requests = [];
  let stored = (dbCart || []).map((line) => ({ ...line }));
  // backend.hold() keeps GET /api/v1/cart/ waiting until backend.release(): the moment before the server answers.
  let held = null;
  const backend = {
    hold() {
      let open;
      held = new Promise((resolve) => { open = resolve; });
      backend.release = () => { held = null; open(); };
    },
  };
  const json = (body, status = 200) => new Response(JSON.stringify(body), {
    status, headers: { 'Content-Type': 'application/json' },
  });
  const write = (method, url, body) => {
    const id = Number(url.split('/').pop());
    if (method === 'PUT') stored = stored.map((line) => (line.product_id === id ? { ...line, quantity: body.quantity } : line));
    if (method === 'DELETE') stored = stored.filter((line) => line.product_id !== id);
    if (method === 'POST' && url === '/api/v1/cart/items') stored = [...stored, { product_id: body.product_id, quantity: body.quantity }];
    if (method === 'POST' && url === '/api/v1/cart/clear') stored = [];
  };
  window.fetch = async (url, options = {}) => {
    const method = (options.method || 'GET').toUpperCase();
    const body = options.body ? JSON.parse(options.body) : null;
    requests.push({ method, url, body, csrf: (options.headers || {})['X-CSRF-TOKEN'] });
    if (url === '/api/v1/cart/' && method === 'GET') {
      if (held) await held;
      return json({ success: true, data: { cart: pricedCart(stored, catalogue, serverLine) } });
    }
    if (url === '/api/v1/products/bulk') {
      if (requests.filter((r) => r.url === url).length <= bulkFailures) return json({ success: false }, 500);
      const ids = body.product_ids;
      return json({ success: true, data: { products: catalogue.filter((p) => ids.includes(p.id)) } });
    }
    if (refuse) return json({ success: false, message: 'refused' }, 400);
    write(method, url, body);
    return json({ success: true, data: { cart: {} } });
  };
  SCRIPTS.map(read).forEach((source) => window.eval(source));
  return { window, requests, backend };
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

// Spec 2026-11-16 §5.8: signed in, the money on /cart is the server's (GET /api/v1/cart/), never the card price
// multiplied in the browser, so a customer's kept price shows before checkout.
describe('/cart signed in: the money is the server cart\'s', () => {
  const LINE = '.shop-cart__item[data-product-id="2"]';
  const MONEY = [`${LINE} .shop-cart__line`, '#summary-subtotal', '#summary-total', '#cart-bar-total'];
  const text = (window, selector) => $(window, selector).textContent;

  it('/cart signed in: draws each line and the totals from GET /cart/, never from the card price', async () => {
    const serverLine = {
      product_id: 2,
      quantity: 4,
      unit_price: 20000,
      courtesy_quantity: 2,
      courtesy_unit_price: 18000,
      regular_quantity: 2,
      discount_amount: 4000,
      total_price: 76000,
    };
    const { window } = boot({ dbCart: [{ product_id: 2, quantity: 4 }], serverLine });
    await settle();

    // The card says 18 000 a bottle (4 × 18 000 = 72 000); the server charges 76 000 and names the kept units.
    expect(text(window, `${LINE} .shop-cart__unit`))
      .toBe(`2 × 18${NBSP}000 сум (ваша цена, сохранили для вас) + 2 × 20${NBSP}000 сум`);
    MONEY.forEach((selector) => expect(text(window, selector)).toBe(`76${NBSP}000 сум`));
    expect(shown(window, 'cart-courtesy')).toBe(true);
    expect(text(window, '#cart-courtesy')).toBe(I18N.courtesy_thanks);
  });

  it('/cart signed in: a step says "calculating" until the server answers, then draws its figures', async () => {
    const serverLine = { product_id: 2, quantity: 3, unit_price: 20000, total_price: 60000 };
    const { window, requests, backend } = boot({ dbCart: [{ product_id: 2, quantity: 2 }], serverLine });
    await settle();
    MONEY.forEach((selector) => expect(text(window, selector)).toBe(`36${NBSP}000 сум`));
    expect(text(window, `${LINE} .shop-cart__unit`)).toBe(`2 × 18${NBSP}000 сум`);
    const loaded = requests.length;

    backend.hold();
    step(window, 2, 'more').click();
    await settle();

    // Nothing is multiplied in the meantime (3 × the card's 18 000 would read 54 000).
    MONEY.forEach((selector) => expect(text(window, selector)).toBe(I18N.calculating));
    expect(text(window, `${LINE} .shop-cart__unit`)).toBe(I18N.calculating);

    backend.release();
    await settle();

    MONEY.forEach((selector) => expect(text(window, selector)).toBe(`60${NBSP}000 сум`));
    expect(text(window, `${LINE} .shop-cart__unit`)).toBe(`3 × 20${NBSP}000 сум`);
    expect(shown(window, 'cart-courtesy')).toBe(false);
    expect(window.document.activeElement).toBe(step(window, 2, 'more'));
    expect(requests.slice(loaded).map(({ method, url }) => ({ method, url }))).toEqual([
      { method: 'PUT', url: '/api/v1/cart/items/2' },
      { method: 'GET', url: '/api/v1/cart/' },
    ]);
  });

  it('/cart signed in: the order floor reads the server subtotal', async () => {
    // The card would say 36 000 (14 000 short of 50 000); the server says 40 000.
    const serverLine = { product_id: 2, quantity: 2, unit_price: 20000, total_price: 40000 };
    const { window, backend } = boot({ dbCart: [{ product_id: 2, quantity: 2 }], serverLine, minOrderAmount: 50000 });
    await settle();
    const short = `Добавьте товаров ещё на 10${NBSP}000 сум: заказ оформляется от 50${NBSP}000 сум.`;

    expect(shown(window, 'min-order-warning')).toBe(true);
    expect(text(window, '#min-order-text')).toBe(short);
    expect($(window, '#min-order-progress').style.width).toBe('80%');
    expect($(window, '#checkout-btn').getAttribute('aria-disabled')).toBe('true');

    // A step is not judged until the server answers (3 × the card's 18 000 = 54 000 would clear the floor).
    backend.hold();
    step(window, 2, 'more').click();
    await settle();
    expect(shown(window, 'min-order-warning')).toBe(true);
    expect(text(window, '#min-order-text')).toBe(short);

    backend.release();
    await settle();
    expect(shown(window, 'min-order-warning')).toBe(false);
    expect($(window, '#checkout-btn').hasAttribute('aria-disabled')).toBe(false);
  });
});

describe('/cart states the coming price', () => {
  const COMING = { new_price: 20000, effective_date: '2027-02-01' };
  const dated = (window, id) => {
    const node = $(window, `.shop-cart__item[data-product-id="${id}"] .shop-cart__upcoming`);
    return node ? { text: node.textContent, hook: node.getAttribute('data-upcoming-price') } : null;
  };

  it('/cart states the coming price: signed in from the server line, signed out from the card', async () => {
    // Signed in: the server line states it for product 2 (its card does not), and none for product 3 (its card
    // does: a contract-priced line is never told the catalogue's coming price).
    const catalogue = [
      product(2, 'Вода 19 л', 18000, 2),
      product(3, 'Вода 10 л', 10000, 3, {}, { upcoming_price: { new_price: 12000, effective_date: '2027-02-01' } }),
    ];
    const serverLine = { product_id: 2, quantity: 2, upcoming_price: COMING };
    const signedIn = boot({ dbCart: [{ product_id: 2, quantity: 2 }, { product_id: 3, quantity: 3 }], catalogue, serverLine });
    await settle();

    expect(dated(signedIn.window, 2)).toEqual({ text: `С 1 февраля: 20${NBSP}000 сум.`, hook: '2' });
    expect(dated(signedIn.window, 3)).toBeNull();

    // Signed out: the card's block (a guest is charged the public price).
    const signedOut = boot({
      localCart: [{ product_id: 2, quantity: 2 }],
      catalogue: [product(2, 'Вода 19 л', 18000, 2, {}, { upcoming_price: COMING })],
    });
    await settle();

    expect(dated(signedOut.window, 2)).toEqual({ text: `С 1 февраля: 20${NBSP}000 сум.`, hook: '2' });
    expect(shown(signedOut.window, 'cart-courtesy')).toBe(false);
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

  it('signed in, the server\'s answer repaints only the money: a number being typed in another line is saved when it is left', async () => {
    // A browser fires `change` on leaving a field only for what was typed into that very field: a field redrawn with
    // the typed text written back by script never fires it, so the number would show but never be saved. jsdom does
    // not model that, so the test pins what the browser relies on: the field (and the steppers a tap may be landing
    // on) are the same nodes after the server answers, and only the money around them is repainted.
    const COMING = { new_price: 20000, effective_date: '2027-02-01' };
    const serverLine = { product_id: 2, quantity: 5, unit_price: 20000, total_price: 100000, upcoming_price: COMING };
    const { window, requests, backend } = boot({
      dbCart: [{ product_id: 2, quantity: 2 }, { product_id: 3, quantity: 3 }], serverLine,
    });
    await settle();
    const LINE2 = '.shop-cart__item[data-product-id="2"]';
    const text = (selector) => $(window, selector).textContent;
    const dated = () => [...window.document.querySelectorAll(`${LINE2} .shop-cart__upcoming`)].map((node) => node.textContent);
    expect(dated()).toEqual([]);

    // Line 2 is typed and left: the PUT is accepted and GET /cart/ is on its way.
    backend.hold();
    typeAndLeave(window, 2, '5');
    await settle();
    expect(text(`${LINE2} .shop-cart__line`)).toBe(I18N.calculating);

    // Meanwhile line 3's field is being typed into.
    const typing = field(window, 3);
    const more = step(window, 3, 'more');
    typing.focus();
    typing.value = '7';
    typing.dispatchEvent(new window.Event('input', { bubbles: true }));

    backend.release();
    await settle();

    expect(field(window, 3)).toBe(typing);
    expect(step(window, 3, 'more')).toBe(more);
    expect(typing.value).toBe('7');
    expect(window.document.activeElement).toBe(typing);
    // The money around it is the server's answer for line 2 at 5.
    expect(text(`${LINE2} .shop-cart__unit`)).toBe(`5 × 20${NBSP}000 сум`);
    expect(text(`${LINE2} .shop-cart__line`)).toBe(`100${NBSP}000 сум`);
    expect(dated()).toEqual([`С 1 февраля: 20${NBSP}000 сум.`]);
    ['#summary-subtotal', '#summary-total', '#cart-bar-total'].forEach((selector) => {
      expect(text(selector)).toBe(`130${NBSP}000 сум`);
    });

    // Leaving line 3 fires its change: the 7 is saved and priced.
    typing.dispatchEvent(new window.Event('change', { bubbles: true }));
    await settle();

    expect(writes(requests).map(({ method, url, body }) => ({ method, url, body }))).toEqual([
      { method: 'PUT', url: '/api/v1/cart/items/2', body: { quantity: 5 } },
      { method: 'PUT', url: '/api/v1/cart/items/3', body: { quantity: 7 } },
    ]);
    expect(localCart(window)).toEqual([{ product_id: 2, quantity: 5 }, { product_id: 3, quantity: 7 }]);
    expect(text('.shop-cart__item[data-product-id="3"] .shop-cart__line')).toBe(`70${NBSP}000 сум`);
    expect(text('#summary-total')).toBe(`170${NBSP}000 сум`);
    expect(dated()).toEqual([`С 1 февраля: 20${NBSP}000 сум.`]);
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
