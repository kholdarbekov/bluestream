// B1 (final fix wave): the order list and the order modal print a courtesy line as the split the API publishes
// ("2 × 18 000 (kept for you) + 2 × 20 000"), never "4 × 20 000" beside the net total. The SHIPPED _page-data.js,
// base.js and the page scripts run in jsdom; only `fetch` is replaced, answering with the order shape
// serialize_order_item publishes (tests/integration/test_price_courtesy_order_create.py pins those fields over HTTP).
import fs from 'node:fs';
import path from 'node:path';
import { JSDOM } from 'jsdom';

const REPO = path.resolve(process.cwd(), '..');
// Repo files named in this test (never user input).
// eslint-disable-next-line security/detect-non-literal-fs-filename
const read = (file) => fs.readFileSync(path.join(REPO, file), 'utf8');
const BASE = ['_page-data.js', 'base.js'].map((name) => read(`business_app/static/js/pages/${name}`));
const ORDERS = read('business_app/static/js/pages/orders.js');
const MODAL = read('business_app/static/js/pages/order-detail-modal.js');

const I18N = {
  qty_price: '{quantity} × {price}',
  reward: 'Reward',
  courtesy_part: '{qty} × {price} (your price, kept for you)',
  order_number: 'Order {number}',
  status_labels: {},
};
const COURTESY_LINE = {
  id: 7, product_display_name: 'Water 18.9 L', quantity: 4, unit_price: 20000, courtesy_quantity: 2,
  courtesy_unit_price: 18000, regular_quantity: 2, discount_amount: 4000, total_price: 76000, is_reward: false,
};
const PLAIN_LINE = { ...COURTESY_LINE, id: 8, courtesy_quantity: null, courtesy_unit_price: null, regular_quantity: null, discount_amount: 0, total_price: 80000 };
const squash = (value) => value.replace(/\s+/g, ' ').trim(); // JS \s already matches U+00A0 and U+202F

function boot(body, island, scripts, fetchAnswer) {
  const dom = new JSDOM(
    `<!doctype html><html lang="en"><body>` +
    `<script type="application/json" id="base-page-data">${JSON.stringify({ api_base_url: '/api/v1', current_user: null, is_authenticated: false, login_url: '/login', i18n: {} })}</script>` +
    `${body}<script type="application/json" id="page-data">${JSON.stringify(island)}</script></body></html>`,
    { url: 'http://localhost/my-orders', runScripts: 'outside-only', pretendToBeVisual: true },
  );
  const { window } = dom;
  window.fetch = async (url) => ({ ok: true, status: 200, json: async () => fetchAnswer(url) });
  window.OrderActions = { render() {} };
  window.$ = () => ({ on() {}, hasClass: () => false, modal() {} });
  BASE.concat(scripts).forEach((source) => window.eval(source));
  window.document.dispatchEvent(new window.Event('DOMContentLoaded'));
  return window;
}

const settle = (window) => new Promise((resolve) => window.setTimeout(resolve, 20));

test('the order list prints a courtesy line as its split and a plain line as quantity x price', async () => {
  const order = {
    id: 1, order_number: 'W1', created_at: '2026-11-16T10:00:00Z', total_amount: 76000, display_status: 'pending',
    order_items: [COURTESY_LINE, PLAIN_LINE], customer_actions: {},
  };
  const window = boot(
    '<ul id="ordersList"></ul><div id="loadingOrders"></div>',
    { i18n: I18N },
    [ORDERS],
    () => ({ success: true, data: { orders: [order], pagination: { pages: 1, page: 1, total: 1 } } }),
  );
  await settle(window);

  const metas = [...window.document.querySelectorAll('.ae-line__meta')].map((node) => squash(node.textContent));
  expect(metas).toHaveLength(2);
  expect(metas[0]).toMatch(/^2 × 18[\s\S]*000[\s\S]*\(your price, kept for you\) \+ 2 × 20[\s\S]*000/);
  expect(metas[0]).not.toMatch(/4 × 20/);
  expect(metas[1]).toMatch(/^4 × 20/);
});

test('the order modal prints a courtesy line as its split', () => {
  const window = boot(
    '<div id="orderDetailContent"></div><button id="reorderBtn"><span data-reorder-label></span></button>',
    {},
    [],
    () => ({}),
  );
  window.document.body.insertAdjacentHTML(
    'beforeend',
    `<script type="application/json" id="order-detail-modal-data">${JSON.stringify({ i18n: I18N })}</script>`,
  );
  window.eval(MODAL);

  window.displayOrderDetail(
    { id: 1, order_number: 'W1', created_at: '2026-11-16T10:00:00Z', display_status: 'pending', order_items: [COURTESY_LINE], customer_actions: {} },
    { items: { 7: '76 000 UZS' }, total: '76 000 UZS' },
  );

  const meta = squash(window.document.querySelector('.modal-order-item .ae-line__meta').textContent);
  expect(meta).toMatch(/^2 × 18[\s\S]*000[\s\S]*\(your price, kept for you\) \+ 2 × 20[\s\S]*000/);
  expect(meta).not.toMatch(/4 × 20/);
});
