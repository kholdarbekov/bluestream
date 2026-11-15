// The home page's subscription builder after the redesign (2026-11-14): the shipped script
// (business_app/static/js/subscription-constructor.js) on the template's own builder markup (index.html, the
// card from the stepper to Back / Next / Create, Jinja tags stripped) with the island the page renders in ru
// (tests/fixtures/web_subscription_builder_island.json, written by test_web_subscription_builder_contract.py).
// What it pins: the inline message next to Back / Next (the only place a problem is said: no toast as well), the
// product card and its quantity control, the water list's placeholders while it loads and its retry after a failure
// (with every control alive meanwhile), the address cards (no raw district key or English city), the shared stepper's
// states and phone line, clicks on a button's icon, and sign-in that comes back to the builder.
import fs from 'node:fs';
import path from 'node:path';

const REPO = path.resolve(process.cwd(), '..');
// Repo files named in this test (never user input).
// eslint-disable-next-line security/detect-non-literal-fs-filename
const read = (file) => fs.readFileSync(path.join(REPO, file), 'utf8');
const SOURCE = read('business_app/static/js/subscription-constructor.js');
const TEMPLATE = read('business_app/templates/frontend/index.html');
const ISLAND = JSON.parse(read('tests/fixtures/web_subscription_builder_island.json')).ru;

// The builder card as the template ships it: each `{{ 'key'|t }}` prints the key's last part, `{% %}` tags go.
const CARD = (() => {
  const start = TEMPLATE.indexOf('<div class="constructor-container sc-card">');
  const end = TEMPLATE.indexOf('<div class="more-btn sc-more">');
  expect(start).toBeGreaterThan(-1);
  expect(end).toBeGreaterThan(start);
  return TEMPLATE.slice(start, end)
    .replace(/\{\{\s*'([\w.]+)'\s*\|\s*t(?:\([^)]*\)|)\s*\}\}/g, (m, key) => key.split('.').pop())
    .replace(/\{\{[^}]*\}\}/g, '1')
    .replace(/\{%-?[^%]*-?%\}/g, '');
})();

const words = (sentence, values) => sentence.replace(/\{(\w+)\}/g, (hole, name) => String(values[name]));
const PRODUCT = { id: 3, product_name: 'Вода 10 л', unit_price_label: '10 000 сум', min_order_quantity: 3, images: [] };

const page = async () => {
  const { JSDOM } = await import('jsdom');
  const dom = new JSDOM(
    `<!doctype html><html lang="ru"><body><section id="subscription-constructor"><div class="ae-container">${CARD}`
    + '</div></section></body></html>',
    { url: 'http://localhost/', runScripts: 'outside-only' },
  );
  const { window } = dom;
  const toasts = [];
  const thrown = [];
  window.addEventListener('error', (e) => thrown.push(e.error || e.message));
  window.CURRENT_USER = null;
  window.API_BASE_URL = '/api/v1';
  window.getPageData = (id) => (id === 'subscription-constructor-data' ? ISLAND : null);
  window.showNotification = (text, type) => toasts.push([type, text]);
  window.apiRequest = async () => ({ ok: true, json: async () => ({ time_slots: [] }) });
  // After DOMContentLoaded, so the script's own start-up (a real constructor fetching the catalog) does not run:
  // the handlers under test are driven on the builder below.
  await new Promise((resolve) => {
    if (window.document.readyState === 'loading') window.document.addEventListener('DOMContentLoaded', resolve);
    else resolve();
  });
  window.eval(`${SOURCE}\n;window.SubscriptionConstructor = SubscriptionConstructor;`);
  const builder = Object.create(window.SubscriptionConstructor.prototype);
  Object.assign(builder, {
    currentStep: 1, totalSteps: 5, selectedProducts: new Map(), selectedFrequency: null, selectedWeekday: null,
    selectedDayOfMonth: null, selectedTimeSlot: null, selectedAddress: null, selectedPaymentMethod: null,
    subscriptionName: '', preview: null, products: [PRODUCT], timeSlots: [], addresses: [], paymentMethods: [],
    isAuthenticated: false, stateKey: 'k',
  });
  builder.setupEventListeners();
  builder.renderProducts();
  const $1 = (selector) => window.document.querySelector(selector);
  const click = (el) => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  return { window, builder, toasts, thrown, $1, click };
};

describe('the builder says what is missing next to Back / Next, and clears it once fixed', () => {
  it('Next with nothing picked shows the message inline only (no toast); picking a product clears it', async () => {
    const { builder, toasts, thrown, $1, click } = await page();
    expect($1('#sc-error').hidden).toBe(true);
    await builder.nextStep();
    await builder.nextStep();
    expect($1('#sc-error').hidden).toBe(false);
    expect($1('#sc-error .sc-error__text').textContent).toBe(ISLAND.i18n.error_products);
    // Said once, where the visitor looks: two misses pile up no toasts over the builder.
    expect(toasts).toEqual([]);
    expect(builder.currentStep).toBe(1);

    click($1('.product-card[data-product-id="3"] .product-card__pick'));
    expect(thrown).toEqual([]);
    expect([...builder.selectedProducts.keys()]).toEqual([3]);
    expect($1('.product-card__pick').getAttribute('aria-pressed')).toBe('true');
    expect($1('#sc-error').hidden).toBe(true);
  });

  it('a click on the arrow icon inside Next is a click on Next', async () => {
    const { $1, click } = await page();
    click($1('.nav-btn-next i'));
    await new Promise((resolve) => { setTimeout(resolve, 0); });
    expect($1('#sc-error').hidden).toBe(false);
    expect($1('#sc-error .sc-error__text').textContent).toBe(ISLAND.i18n.error_products);
  });
});

describe('the product card and its quantity control', () => {
  it('starts at the smallest order, says so, and works only once the product is picked', async () => {
    const { builder, $1, click } = await page();
    const card = $1('.product-card[data-product-id="3"]');
    expect(card.querySelector('.quantity-display').textContent).toBe('3');
    expect(card.querySelector('.ae-qty__hint').textContent).toBe(words(ISLAND.ui.min_quantity, { count: 3 }));
    expect(card.querySelector('[data-action="increase"]').disabled).toBe(true);
    click(card.querySelector('.quantity-display'));  // the control is not the card: nothing is picked
    expect(builder.selectedProducts.size).toBe(0);

    click(card.querySelector('.product-card__pick'));
    const less = card.querySelector('[data-action="decrease"]');
    const more = card.querySelector('[data-action="increase"]');
    expect([less.disabled, more.disabled]).toEqual([true, false]);
    click(more);
    expect(card.querySelector('.quantity-display').textContent).toBe('4');
    expect(builder.selectedProducts.get(3).quantity).toBe(4);
    expect(less.disabled).toBe(false);
    click(less);
    expect(builder.selectedProducts.get(3).quantity).toBe(3);
    expect(less.disabled).toBe(true);
    click(less);
    expect(builder.selectedProducts.get(3).quantity).toBe(3);
  });
});

describe('the stepper', () => {
  it('marks the steps done and current, and the phone line names the step with its progress', async () => {
    const { builder, $1, window } = await page();
    builder.currentStep = 3;
    builder.updateStepIndicators();
    const steps = [...window.document.querySelectorAll('.constructor-step')];
    expect(steps.map((s) => [s.classList.contains('is-done'), s.classList.contains('completed')]))
      .toEqual([[true, true], [true, true], [false, false], [false, false], [false, false]]);
    expect(steps.map((s) => s.classList.contains('is-current') && s.classList.contains('active')))
      .toEqual([false, false, true, false, false]);
    expect(steps.map((s) => s.getAttribute('aria-current'))).toEqual([null, null, 'step', null, null]);
    const label = steps[2].querySelector('.step-label').textContent.trim();
    expect($1('.sc-compact').textContent).toBe(`${words(ISLAND.ui.step_of, { current: 3, total: 5 })}: ${label}`);
    expect($1('.sc-progress').style.width).toBe('60%');
  });

  it('shows Back from step 2, Next until the review and Create on it, leaving the display to the stylesheet', async () => {
    const { builder, $1 } = await page();
    const shown = () => ['.nav-btn-prev', '.nav-btn-next', '.nav-btn-submit'].map((s) => $1(s).style.display);
    builder.currentStep = 1; builder.updateNavigation();
    expect(shown()).toEqual(['none', '', 'none']);
    builder.currentStep = 3; builder.updateNavigation();
    expect(shown()).toEqual(['', '', 'none']);
    builder.currentStep = 5; builder.updateNavigation();
    expect(shown()).toEqual(['', 'none', '']);
  });
});

describe('choices picked with the keyboard keep the focus', () => {
  it('an address re-rendered as picked is the element that has the focus', async () => {
    const { builder, $1, click, window } = await page();
    builder.addresses = [{ id: 11, title: 'Дом', full_address: 'Амир Темур 1' }, { id: 12, title: 'Офис', full_address: 'Навои 2' }];
    builder.renderAddresses();
    const card = $1('.address-card[data-address-id="12"]');
    card.focus();
    click(card);
    const now = window.document.activeElement;
    expect(now.dataset.addressId).toBe('12');
    expect(now.getAttribute('aria-pressed')).toBe('true');
    expect(builder.selectedAddress.id).toBe(12);
  });
});

describe('the address cards', () => {
  it('name the address as checkout does, never the API\'s district key or its English city', async () => {
    const { builder, window } = await page();
    // GET /auth/addresses as dev customer #44 gets it: district is a key, city is stored in English.
    builder.addresses = [{
      id: 21, title: 'Uy', full_address: 'Toshkent, Shayxontohur tumani, Aloqa dahasi, 7Б', apartment_number: '12',
      city: 'Tashkent', district: 'shaykhontohur',
    }];
    builder.renderAddresses();
    const card = window.document.querySelector('.address-card[data-address-id="21"]');
    const lines = [...card.querySelectorAll('.address-line')].map((el) => el.textContent.trim());
    expect(card.querySelector('.address-type').textContent).toBe('Uy');
    expect(lines).toEqual(['Toshkent, Shayxontohur tumani, Aloqa dahasi, 7Б', words(ISLAND.i18n.address_apartment, { number: '12' })]);
    expect(card.textContent).not.toContain('shaykhontohur');
    expect(card.textContent).not.toContain('Tashkent');
  });
});

// The real constructor and its init(), on the template's card, with the catalog answering as the test says.
const startedPage = async (catalog) => {
  const { JSDOM } = await import('jsdom');
  const dom = new JSDOM(
    `<!doctype html><html lang="ru"><body><section id="subscription-constructor"><div class="ae-container">${CARD}`
    + '</div></section></body></html>',
    { url: 'http://localhost/', runScripts: 'outside-only' },
  );
  const { window } = dom;
  const toasts = [];
  const asked = [];
  window.CURRENT_USER = null;
  window.API_BASE_URL = '/api/v1';
  window.getPageData = (id) => (id === 'subscription-constructor-data' ? ISLAND : null);
  window.showNotification = (text, type) => toasts.push([type, text]);
  window.apiRequest = async () => ({ ok: true, json: async () => ({ time_slots: [] }) });
  window.fetch = (url, options) => { asked.push([url, options]); return catalog(asked.length); };
  await new Promise((resolve) => {
    if (window.document.readyState === 'loading') window.document.addEventListener('DOMContentLoaded', resolve);
    else resolve();
  });
  window.eval(`${SOURCE}\n;window.SubscriptionConstructor = SubscriptionConstructor;`);
  const { init } = window.SubscriptionConstructor.prototype;
  let started;
  window.SubscriptionConstructor.prototype.init = function initAndKeep() { started = init.call(this); return started; };
  const builder = new window.SubscriptionConstructor();
  const $1 = (selector) => window.document.querySelector(selector);
  const click = (el) => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  return { window, builder, toasts, asked, $1, click, started: () => started };
};
const CATALOG_OK = { ok: true, json: async () => ({ success: true, data: { items: [PRODUCT] } }) };
const CATALOG_500 = { ok: false, status: 500, json: async () => ({ success: false }) };

describe('the water list while it loads, and after it failed', () => {
  it('shows two placeholder cards while the catalog is asked, and Next already answers', async () => {
    let answer;
    const { builder, $1, click, started } = await startedPage(() => new Promise((resolve) => { answer = resolve; }));
    expect($1('#products-grid').getAttribute('aria-busy')).toBe('true');
    expect($1('#products-grid').querySelectorAll('.sc-product-skeleton')).toHaveLength(2);
    expect($1('#products-grid .product-card')).toBeNull();
    click($1('.nav-btn-next'));
    await new Promise((resolve) => { setTimeout(resolve, 0); });
    expect($1('#sc-error .sc-error__text').textContent).toBe(ISLAND.i18n.error_products);

    answer(CATALOG_OK);
    await started();
    expect($1('#products-grid').hasAttribute('aria-busy')).toBe(false);
    expect($1('#products-grid .sc-product-skeleton')).toBeNull();
    expect($1('.product-card[data-product-id="3"]')).not.toBeNull();
    expect(builder.currentStep).toBe(1);
  });

  it('a failed catalog says so with "Try again"; the controls still work, and the retry lists the water', async () => {
    const { window, builder, asked, toasts, $1, click, started } = await startedPage((n) => Promise.resolve(n === 1 ? CATALOG_500 : CATALOG_OK));
    await started();
    expect($1('.sc-catalog-failed').textContent).toContain(ISLAND.ui.catalog_failed);
    expect($1('.sc-catalog-retry').textContent.trim()).toBe(ISLAND.ui.retry);
    expect(builder.currentStep).toBe(1);

    // Next is alive (the listeners came before the catalog): with no list to pick from, it takes the visitor to
    // "Try again" instead of asking for a product, and stays on step 1.
    click($1('.nav-btn-next'));
    await new Promise((resolve) => { setTimeout(resolve, 0); });
    expect(window.document.activeElement).toBe($1('.sc-catalog-retry'));
    expect($1('#sc-error').hidden).toBe(true);
    expect(builder.currentStep).toBe(1);

    click($1('.sc-catalog-retry'));
    await new Promise((resolve) => { setTimeout(resolve, 0); });
    await new Promise((resolve) => { setTimeout(resolve, 0); });
    expect(asked.map(([url]) => url)).toEqual(['/api/v1/subscriptions/products', '/api/v1/subscriptions/products']);
    expect($1('.sc-catalog-failed')).toBeNull();
    expect($1('#sc-error').hidden).toBe(true);
    click($1('.product-card[data-product-id="3"] .product-card__pick'));
    expect([...builder.selectedProducts.keys()]).toEqual([3]);
    expect(toasts).toEqual([]);
  });
});

describe('a signed-out visitor signs in and comes back to the builder', () => {
  it('the script signs in through the page\'s URL, the one /login reads (`next`)', async () => {
    const { builder } = await page();
    expect(ISLAND.sign_in_url).toBe('/login?next=/%23subscription-constructor');
    expect(builder.signInUrl()).toBe(ISLAND.sign_in_url);
    expect(SOURCE).not.toContain('?redirect=');
  });
});
