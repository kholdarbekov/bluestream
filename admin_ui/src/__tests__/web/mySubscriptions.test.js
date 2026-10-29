// /my-subscriptions (plan 2026-10-26 Tasks 4a and 4b; spec 2026-10-26 §4, §12; UX brief state matrix and
// interaction rules).
//
// The SHIPPED scripts run in jsdom exactly as the page loads them: jquery.js, popper and bootstrap.min.js (the
// modals), _page-data.js, base.js (its real apiRequest: cookie credentials, X-Platform, the X-CSRF-TOKEN it
// copies from the readable csrf cookie) and my-subscriptions.js, over the shell the server renders
// (tests/fixtures/web_subscription_management/shell.json). Only `fetch` is replaced, in one of two modes:
// - read cases answer each request by its key from the case fixture, which
//   tests/integration/test_my_subscriptions_page.py captured from the real endpoints (and keeps fresh);
// - write cases (`write_<scenario>.json`) are a strict SEQUENCE: the script must send exactly the requests
//   tests/integration/test_web_subscription_management_contract.py replays through the real endpoints, in that
//   order and with those bodies, and each is answered with what the backend answered there.
// An unexpected request fails the case. The shared order modal's entry point, window.showOrderDetail, is a spy
// unless a case loads the modal's own script.
import fs from 'node:fs';
import path from 'node:path';
import { JSDOM } from 'jsdom';

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
  // The one renderer of an order's Pay, Skip/Cancel/Decline and Confirm (the modal's, and the hero's Pay).
  'business_app/static/js/pages/order-actions.js',
].map(read);
const MODAL_SCRIPT = read('business_app/static/js/pages/order-detail-modal.js');
const PAGE_SCRIPT = read('business_app/static/js/pages/my-subscriptions.js');
const CSS = read('business_app/static/css/pages/my-subscriptions.css');
const FIXTURES = path.join(REPO, 'tests/fixtures/web_subscription_management');
const SHELL = JSON.parse(fs.readFileSync(path.join(FIXTURES, 'shell.json'), 'utf8'));
// eslint-disable-next-line security/detect-non-literal-fs-filename
const loadCase = (name) => JSON.parse(fs.readFileSync(path.join(FIXTURES, `${name}.json`), 'utf8'));
const CSRF = 'csrf-cookie-value';

const LIST = 'GET /api/v1/subscriptions/?per_page=50';
const history = (id, page = 1) => `GET /api/v1/subscriptions/${id}/history?page=${page}&per_page=5`;
const methods = (address = 1) => `GET /api/v1/payments/methods?context=subscription&delivery_address_id=${address}`;
const NBSP = '\u00a0';
const money = (amount) => `${amount.replace(' ', NBSP)}${NBSP}so'm`;

const squash = (value) => (value || '').replace(/[ \t\n\r]+/g, ' ').trim();
// The words a sighted reader sees, element by element (adjacent elements have no space between them in
// textContent), leaving out the screen-reader-only text.
const seen = (el) => {
  const parts = [];
  const walk = (node) => {
    if (node.nodeType === 3) {
      if (node.textContent.trim()) parts.push(node.textContent.trim());
    } else if (node.nodeType === 1 && !node.classList.contains('sr-only')) {
      node.childNodes.forEach(walk);
    }
  };
  walk(el);
  return parts.join(' ');
};

async function settle(window, rounds = 30) {
  for (let i = 0; i < rounds; i += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, 0));
  }
}

const parseBody = (body) => (body === undefined || body === null ? null : JSON.parse(body));
// A JSON value with its object keys sorted, so two bodies compare by content (the fixture is written sorted).
const canonical = (value) => JSON.stringify(value, (key, item) => (
  item && typeof item === 'object' && !Array.isArray(item)
    ? Object.keys(item).sort().reduce((sorted, name) => ({ ...sorted, [name]: item[name] }), {})
    : item
));

async function boot(name, { search, answers = {}, modal = false } = {}) {
  const fixture = loadCase(name);
  const shell = SHELL[fixture.language];
  const base = { api_base_url: '/api/v1', current_user: null, is_authenticated: false, login_url: '/login', i18n: {} };
  const island = JSON.stringify(shell.island).replace(/</g, '\\u003c');
  const modalMarkup = modal
    ? `${shell.modal.html}<script type="application/json" id="order-detail-modal-data">` +
      `${JSON.stringify(shell.modal.island).replace(/</g, '\\u003c')}</script>`
    : '';
  const dom = new JSDOM(
    `<!doctype html><html lang="${fixture.language}"><head><style>${CSS}</style></head><body>` +
    `<script type="application/json" id="base-page-data">${JSON.stringify(base)}</script>` +
    `${shell.html}<script type="application/json" id="page-data">${island}</script>${modalMarkup}</body></html>`,
    {
      url: `http://localhost/my-subscriptions${search === undefined ? fixture.search || '' : search}`,
      runScripts: 'outside-only',
      pretendToBeVisual: true,
    },
  );
  const { window } = dom;
  window.document.cookie = `csrf_access_token=${CSRF}`;
  const requests = [];
  const mismatches = [];
  const sequence = fixture.exchanges || null;
  window.fetch = async (url, options = {}) => {
    const method = (options.method || 'GET').toUpperCase();
    const key = `${method} ${url}`;
    const body = parseBody(options.body);
    requests.push({ key, method, url, body, options });
    let answer = answers[key];
    if (!answer && sequence) {
      const expected = sequence[requests.length - 1];
      const same = expected && expected.method === method && expected.path === url &&
        canonical(expected.body) === canonical(body);
      if (same) answer = { status: expected.status, body: expected.response };
      else mismatches.push({ sent: { method, url, body }, expected: expected && { method: expected.method, url: expected.path, body: expected.body } });
    } else if (!answer && fixture.requests) {
      answer = fixture.requests[key];
    }
    if (!answer) {
      return { ok: false, status: 599, json: async () => ({ success: false, message: `unexpected ${key}` }) };
    }
    return { ok: answer.status < 400, status: answer.status, json: async () => JSON.parse(JSON.stringify(answer.body)) };
  };
  window.L = { map: () => ({ setView: () => ({}), remove() {}, invalidateSize() {} }), tileLayer: () => ({ addTo() {} }), marker: () => ({ addTo: () => ({ bindPopup: () => ({ openPopup() {} }) }) }) };
  if (!modal) window.showOrderDetail = vi.fn();
  window.confirm = vi.fn(() => true);
  BASE_SCRIPTS.forEach((source) => window.eval(source));
  if (modal) window.eval(MODAL_SCRIPT);
  window.eval(PAGE_SCRIPT);
  await settle(window);
  const document = window.document;
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const $1 = (selector, root = document) => root.querySelector(selector);
  const first = sequence ? sequence[0].response : fixture.requests[LIST].body;
  const listed = first.data.items;
  return { window, document, requests, mismatches, fixture, listed, shell, $$, $1, words: shell.island.i18n };
}

const keys = (requests) => requests.map((r) => r.key);
const hero = (page) => page.$1('#subscription .subs-hero');
const primaries = (page) => page.$$('.subs-btn--primary', hero(page));
const heroButtons = (page) => page.$$('.subs-hero__actions button.subs-btn', hero(page)).map((b) => b.textContent);
const sectionText = (page, name) => squash(page.$1(`#subs-sec-${name} .subs-sec__value`).textContent);
const changeButton = (page, name) => page.$1(`#subs-sec-${name} [data-action="change"]`);

// ------------------------------------------------------------------ the requests

describe('what the page asks the backend', () => {
  it('reads the list once, then the selected subscription\'s first five history rows and its payment menu', async () => {
    const page = await boot('needs_payment');
    expect(keys(page.requests)).toEqual([LIST, history(1), methods(1)]);
    page.requests.forEach(({ options }) => {
      expect(options.credentials).toBe('include');
      expect(options.headers['X-Platform']).toBe('web');
      expect(options.method || 'GET').toBe('GET');
    });
  });

  it('asks nothing more when there is no subscription', async () => {
    const page = await boot('empty');
    expect(keys(page.requests)).toEqual([LIST]);
  });
});

// ------------------------------------------------------------------ active, unpaid Click order (needs payment)

describe('active with an unpaid Click order', () => {
  it('shows the next delivery, its order and Pay as the one primary action', async () => {
    const page = await boot('needs_payment');
    const card = hero(page);
    expect(page.$1('.subs-hero__label', card).textContent).toBe('Next delivery');
    expect(page.$1('.subs-hero__date', card).textContent).toBe('Thursday, 29 October');
    expect(page.listed[0].upcoming_delivery.time_label).toBe('10:00–12:00');
    expect(page.$1('.subs-hero__win', card).textContent).toBe('10:00–12:00, tomorrow');
    expect(page.$1('.subs-hero__order [data-action="open-order"]', card).textContent).toBe('Order TG_000001_26');
    expect(page.$1('.subs-hero__order .subs-money', card).textContent).toBe(money('30 000'));
    const chip = page.$1('.subs-hero__order .subs-chip', card);
    expect(chip.textContent).toBe('Waiting for payment');
    expect(chip.className).toContain('subs-chip--warning');
    expect(chip.querySelector('i[aria-hidden="true"]')).not.toBeNull();

    const [pay] = primaries(page);
    expect(primaries(page)).toHaveLength(1);
    expect(pay.textContent).toBe(`Pay ${money('30 000')}`);
    expect(pay.dataset).toMatchObject({ action: 'pay', orderId: '1' });
    expect(page.$$('.subs-hint', card).map((p) => p.textContent)).toEqual(['Pay by 12:00 tomorrow so we can bring it.']);
    expect(heroButtons(page)).toEqual([`Pay ${money('30 000')}`, 'Skip this delivery', 'Pause']);
    // Cancel never sits in the hero.
    expect(page.$1('[data-action="cancel"]', card)).toBeNull();
  });

  it('draws the waterline: the placed order filled, the next three planned, each read out in full', async () => {
    const page = await boot('needs_payment');
    const items = page.$$('.subs-wl > li', hero(page));
    expect(page.$1('ol.subs-wl', hero(page)).getAttribute('aria-label')).toBe('Next deliveries');
    expect(items.map((li) => li.querySelector('.subs-wl__date').textContent)).toEqual(
      ['Thu 29 Oct', 'Thu 5 Nov', 'Thu 12 Nov', 'Thu 19 Nov'],
    );
    expect(items.map((li) => li.querySelector('svg').getAttribute('class'))).toEqual([
      'subs-bottle subs-bottle--placed', 'subs-bottle subs-bottle--planned',
      'subs-bottle subs-bottle--planned', 'subs-bottle subs-bottle--planned',
    ]);
    expect(items.every((li) => li.querySelector('svg').getAttribute('aria-hidden') === 'true')).toBe(true);
    expect(items.map((li) => li.querySelector('.sr-only').textContent)).toEqual([
      'Thursday, 29 October, order placed', 'Thursday, 5 November, planned',
      'Thursday, 12 November, planned', 'Thursday, 19 November, planned',
    ]);
    expect(seen(page.$1('.subs-wl__legend', hero(page)))).toBe('Order placed Planned');
  });

  it('shows the strip, the body header and the jump links', async () => {
    const page = await boot('needs_payment');
    const cards = page.$$('#subs-strip a.subs-card');
    expect(cards.map((a) => a.getAttribute('href'))).toEqual(['?sub=1#subscription', '/#subscription-constructor']);
    expect(cards[0].getAttribute('aria-current')).toBe('true');
    expect(seen(cards[0])).toBe('Home water Waiting for payment');
    expect(cards[1].textContent).toBe('New subscription');
    expect(page.$1('#subs-strip-nav').hidden).toBe(false);
    expect(page.$1('#subs-strip-nav').getAttribute('aria-label')).toBe('Your subscriptions');

    expect(page.$1('#sub-h').textContent).toBe('Home water');
    expect(page.$1('#sub-h').getAttribute('tabindex')).toBe('-1');
    expect(page.$1('.subs-bh .subs-chip')).toBeNull(); // no "Active" chip
    expect(page.$1('.subs-bh p').textContent).toBe('Every Thursday, 10:00–12:00. Since 26 October.');
    expect(page.$$('.subs-jump a').map((a) => [a.textContent, a.getAttribute('href')])).toEqual([
      ['Items', '#subs-sec-items'], ['Schedule', '#subs-sec-schedule'], ['Delivery address', '#subs-sec-address'],
      ['Payment', '#subs-sec-payment'], ['Orders', '#subs-sec-orders'],
    ]);
    expect(page.$1('#subscription').getAttribute('aria-busy')).toBe('false');
  });

  it('shows Items, Schedule and Payment in read mode, each with its own Change', async () => {
    const page = await boot('needs_payment');
    const [row] = page.$$('#subs-sec-items .subs-item');
    expect(row.querySelector('b').textContent).toBe('Pure Water 19L');
    expect(row.querySelector('small').textContent).toBe(`2 × ${money('15 000')}`);
    expect(row.querySelector('.subs-money').textContent).toBe(money('30 000'));
    expect(page.$$('#subs-sec-items .subs-totals > *').map((el) => el.textContent)).toEqual([
      'Per delivery', money('30 000'), 'About a month', money('130 000'),
    ]);
    const benefit = page.$1('#subs-sec-items .subs-benefit');
    expect(benefit.textContent).toBe("Delivery is always free, and every order earns AquaCoins at your tier's rate.");
    expect(benefit.querySelector('b').textContent).toBe('Delivery is always free');

    expect(sectionText(page, 'schedule')).toBe('Every Thursday, 10:00–12:00');
    expect(page.$1('#subs-sec-payment .subs-val').firstChild.textContent).toBe('Click');
    expect(page.$1('#subs-sec-payment .subs-val small').textContent)
      .toBe("Pay online for each order. We'll remind you before the deadline.");

    ['items', 'schedule', 'payment'].forEach((name) => {
      const button = changeButton(page, name);
      expect(button.tagName).toBe('BUTTON');
      expect(button.getAttribute('aria-expanded')).toBe('false');
      expect(button.querySelector('[aria-hidden="true"]').textContent).toBe('Change');
    });
    expect(changeButton(page, 'items').querySelector('.sr-only').textContent).toBe('Change items');
    expect(changeButton(page, 'schedule').querySelector('.sr-only').textContent).toBe('Change schedule');
    expect(changeButton(page, 'payment').querySelector('.sr-only').textContent).toBe('Change payment method');
    expect(page.$1('#subs-sec-orders [data-action="change"]')).toBeNull();
  });

  it('lists the order in Orders: the row opens the order modal, Pay is a sibling button', async () => {
    const page = await boot('needs_payment');
    expect(page.$1('#subs-sec-orders h3').textContent).toBe('Orders from this subscription');
    const [row] = page.$$('#subs-orders > li');
    const open = row.querySelector('.subs-orow__open');
    expect(open.tagName).toBe('BUTTON');
    expect(seen(open)).toBe(`Thu 29 Oct Waiting for payment ${money('30 000')}`);
    const pay = row.querySelector('.subs-orow__pay');
    expect(pay.parentElement).toBe(row);
    expect(open.contains(pay)).toBe(false);
    expect(pay.dataset).toMatchObject({ action: 'pay', orderId: '1' });
    expect(pay.querySelector('.sr-only').textContent).toBe('Pay for order TG_000001_26');
    expect(page.$1('#subs-orders').getAttribute('aria-busy')).toBe('false');
    expect(page.$1('#subs-orders-more').hidden).toBe(true);

    open.click();
    page.$1('.subs-hero__order [data-action="open-order"]').click();
    expect(page.window.showOrderDetail.mock.calls).toEqual([[1], [1]]);
  });

  it('ends with Cancel and the Telegram help line', async () => {
    const page = await boot('needs_payment');
    const foot = page.$1('#subscription .subs-foot');
    expect(foot.querySelector('button.subs-danger').textContent).toBe('Cancel subscription');
    expect(foot.querySelector('button.subs-danger').dataset.action).toBe('cancel');
    expect(squash(foot.querySelector('.subs-help').textContent)).toBe('Questions? Message us in the Telegram bot.');
    const link = foot.querySelector('.subs-help a');
    expect(link.getAttribute('href')).toBe(page.shell.island.help_url);
    expect(link.getAttribute('rel')).toBe('noopener noreferrer');
  });
});

// ------------------------------------------------------------------ the other active states

describe('the other active states', () => {
  it('paid: the Paid chip and note, and no Pay anywhere', async () => {
    const page = await boot('paid');
    const chip = page.$1('.subs-hero__order .subs-chip', hero(page));
    expect([chip.textContent, chip.className]).toEqual(['Paid', 'subs-chip subs-chip--success']);
    expect(primaries(page)).toHaveLength(0);
    expect(heroButtons(page)).toEqual(['Skip this delivery', 'Pause']);
    expect(page.$$('.subs-hint', hero(page)).map((p) => p.textContent)).toEqual(['Paid. This delivery will arrive as planned.']);
    expect(page.$1('#subs-orders .subs-orow__pay')).toBeNull();
  });

  it('cash: pay on delivery, at the door', async () => {
    const page = await boot('cash');
    const chip = page.$1('.subs-hero__order .subs-chip', hero(page));
    expect([chip.textContent, chip.className]).toEqual(['Pay on delivery', 'subs-chip subs-chip--info']);
    expect(primaries(page)).toHaveLength(0);
    expect(page.$$('.subs-hint', hero(page)).map((p) => p.textContent)).toEqual(['Pay the driver at the door.']);
    expect(page.$1('#subs-sec-payment .subs-val').firstChild.textContent).toBe('Cash');
    expect(page.$1('#subs-sec-payment .subs-val small').textContent).toBe('Pay the driver at the door.');
  });

  it('with the driver: "On the way" on the card and the first bottle, Skip still offered', async () => {
    const page = await boot('on_the_way');
    const chips = page.$$('.subs-hero__order .subs-chip', hero(page)).map((c) => [c.textContent, c.className]);
    expect(chips).toEqual([['On the way', 'subs-chip subs-chip--info'], ['Waiting for payment', 'subs-chip subs-chip--warning']]);
    expect(page.$$('.subs-hero__note', hero(page)).map((p) => p.textContent))
      .toContain('Your driver has this order and is on the way.');
    const [first] = page.$$('.subs-wl > li', hero(page));
    expect(first.className).toBe('subs-wl__item subs-wl__item--on_the_way');
    expect(first.querySelector('svg').getAttribute('class')).toBe('subs-bottle subs-bottle--placed');
    expect(first.querySelector('.sr-only').textContent).toBe('Thursday, 29 October, on the way');
    expect(heroButtons(page)).toContain('Skip this delivery');
    expect(seen(page.$1('.subs-wl__legend', hero(page)))).toBe('On the way Planned');
  });

  it('the last Click attempt failed: Pay is still the primary action, with the retry sentence (Review Focus 3)', async () => {
    const page = await boot('payment_failed');
    const chip = page.$1('.subs-hero__order .subs-chip', hero(page));
    expect([chip.textContent, chip.className]).toEqual(['Payment failed', 'subs-chip subs-chip--danger']);
    expect(primaries(page).map((b) => b.textContent)).toEqual([`Pay ${money('30 000')}`]);
    expect(page.$$('.subs-hint', hero(page)).map((p) => p.textContent)).toEqual([
      "That payment didn't go through. Your order is still waiting, and you can try again until 12:00 tomorrow.",
    ]);
    expect(page.$1('#subs-orders .subs-orow__pay')).not.toBeNull();
  });

  // Final review Minor 12: a date already past has no "in N days"; the line is the window alone, no comma.
  it('a next delivery whose date has passed shows its window without a dangling comma', async () => {
    const page = await boot('past_dated');
    expect(page.$1('.subs-hero__date', hero(page)).textContent).toBe('Thursday, 29 October');
    expect(page.$1('.subs-hero__win', hero(page)).textContent).toBe('10:00–12:00');
  });

  it('no order yet: when it will be placed, four planned bottles, and the empty Orders line', async () => {
    const page = await boot('no_order_yet');
    expect(page.$1('.subs-hero__date', hero(page)).textContent).toBe('Thursday, 29 October');
    expect(page.$1('.subs-hero__win', hero(page)).textContent).toBe('10:00–12:00, in 3 days');
    expect(page.$1('.subs-hero__order', hero(page))).toBeNull();
    expect(page.$$('.subs-hero__note', hero(page)).map((p) => p.textContent))
      .toEqual(["We'll place this order on Wednesday, 28 October."]);
    expect(primaries(page)).toHaveLength(0);
    expect(page.$$('.subs-wl svg', hero(page)).map((svg) => svg.getAttribute('class')))
      .toEqual(Array(4).fill('subs-bottle subs-bottle--planned'));
    expect(page.$$('#subs-orders > li')).toHaveLength(0);
    const empty = page.$1('#subs-orders-empty');
    expect([empty.hidden, empty.textContent])
      .toEqual([false, 'Your first order will show up here once we place it on Wednesday, 28 October.']);
    expect(seen(page.$1('#subs-strip a.subs-card'))).toBe('Home water Next: Thu 29 Oct');
  });
});

// ------------------------------------------------------------------ paused and cancelled

describe('paused', () => {
  it('turns the card into "Paused since", with Resume and the one bottle a resume today gives', async () => {
    const page = await boot('paused');
    const card = hero(page);
    expect(card.className).toBe('subs-hero subs-hero--paused');
    expect(page.$1('.subs-hero__date', card).textContent).toBe('Paused since 26 October');
    expect(page.$1('.subs-hero__note', card).textContent)
      .toBe('No new orders until you resume. Your items and schedule are saved.');
    expect(heroButtons(page)).toEqual(['Resume deliveries']);
    expect(primaries(page)[0].dataset.action).toBe('resume');
    // What a resume today gives is said once, by the one-bottle waterline.
    expect(page.$$('.subs-hint', card)).toHaveLength(0);
    const bottles = page.$$('.subs-wl > li', card);
    expect(bottles).toHaveLength(1);
    expect(bottles[0].querySelector('.sr-only').textContent).toBe('Thursday, 29 October, planned');
    expect(page.$1('.subs-wl__caption', card).textContent).toBe('If you resume today: Thu 29 Oct');
    // Items, schedule and payment stay editable; cancel stays offered.
    ['items', 'schedule', 'payment'].forEach((name) => expect(changeButton(page, name)).not.toBeNull());
    expect(page.$1('.subs-foot [data-action="cancel"]')).not.toBeNull();
    const chip = page.$1('.subs-bh .subs-chip');
    expect([chip.textContent, chip.className]).toEqual(['Paused', 'subs-chip subs-chip--warning']);
    expect(seen(page.$1('#subs-strip a.subs-card'))).toBe('Home water Paused');
    expect(page.$1('#subs-strip a.subs-card svg').getAttribute('class')).toBe('subs-bottle subs-bottle--muted');
  });

  it('still shows a kept paid order and when it arrives (Review Focus 4)', async () => {
    const page = await boot('paused_kept_paid');
    const card = hero(page);
    expect(page.$1('.subs-hero__date', card).textContent).toBe('Paused since 28 October');
    expect(page.$$('.subs-hero__note', card).map((p) => p.textContent)).toEqual([
      'No new orders until you resume. Your items and schedule are saved.',
      'Your paid order for Thursday, 29 October, 10:00–12:00 will still be delivered.',
    ]);
    expect(seen(page.$1('.subs-hero__order', card))).toBe(`Order TG_000001_26 ${money('30 000')} Paid`);
    expect(heroButtons(page)).toEqual(['Resume deliveries']);
  });

  it('keeps the order the driver has "On the way", with Pay first and Resume still reachable (spec §12)', async () => {
    const page = await boot('paused_on_the_way');
    const card = hero(page);
    expect(card.className).toBe('subs-hero subs-hero--paused');
    expect(page.$1('.subs-hero__date', card).textContent).toBe('Paused since 28 October');
    expect(page.$$('.subs-hero__note', card).map((p) => p.textContent)).toEqual([
      'No new orders until you resume. Your items and schedule are saved.',
      'Your order for Thursday, 29 October, 10:00–12:00 is on the way.',
    ]);
    expect(seen(page.$1('.subs-hero__order', card)))
      .toBe(`Order TG_000001_26 ${money('30 000')} On the way Waiting for payment`);
    expect(page.$1('.subs-hero__order [data-action="open-order"]', card).dataset.orderId).toBe('1');
    // One primary (Pay), and Resume beside it as a secondary, never dropped.
    expect(heroButtons(page)).toEqual([`Pay ${money('30 000')}`, 'Resume deliveries']);
    expect(primaries(page).map((b) => [b.dataset.action, b.dataset.orderId])).toEqual([['pay', '1']]);
    const resume = page.$1('.subs-hero__row [data-action="resume"]', card);
    expect([resume.className, resume.textContent]).toEqual(['subs-btn subs-btn--secondary', 'Resume deliveries']);
    // The driver has it, so Pay has no deadline (spec 2026-10-27 §4.4).
    expect(page.$$('.subs-hint', card).map((p) => p.textContent)).toEqual(["Pay online whenever you're ready."]);
    expect(page.$1('.subs-wl__caption', card).textContent).toBe('If you resume today: Thu 29 Oct');
    expect(seen(page.$1('#subs-strip a.subs-card'))).toBe('Home water Paused');
  });

  it('keeps a paid order the driver has "On the way", with Resume as the primary', async () => {
    const page = await boot('paused_on_the_way_paid');
    const card = hero(page);
    expect(page.$$('.subs-hero__note', card).map((p) => p.textContent)).toEqual([
      'No new orders until you resume. Your items and schedule are saved.',
      'Your order for Thursday, 29 October, 10:00–12:00 is on the way.',
    ]);
    expect(seen(page.$1('.subs-hero__order', card))).toBe(`Order TG_000001_26 ${money('30 000')} On the way Paid`);
    expect(heroButtons(page)).toEqual(['Resume deliveries']);
    expect(primaries(page)[0].dataset.action).toBe('resume');
    // The kept order's sentence and chips say how it stands; no payment line sits under Resume.
    expect(page.$$('.subs-hint', card)).toHaveLength(0);
  });
});

describe('cancelled', () => {
  it('is read-only: no actions, no waterline, no Change, no Cancel; history stays', async () => {
    const page = await boot('cancelled');
    const card = hero(page);
    expect(card.className).toBe('subs-hero subs-hero--cancelled');
    expect(page.$1('.subs-hero__date', card).textContent).toBe('Cancelled on 26 October');
    expect(page.$1('.subs-hero__note', card).textContent).toBe('Your past orders are below.');
    expect(page.$1('.subs-hero__actions', card)).toBeNull();
    expect(page.$1('.subs-wl-band', card)).toBeNull();
    expect(page.$$('#subscription [data-action="change"]')).toHaveLength(0);
    expect(page.$1('#subscription [data-action="cancel"]')).toBeNull();
    expect(page.$1('#subscription').classList.contains('is-readonly')).toBe(true);
    const chip = page.$1('.subs-bh .subs-chip');
    expect([chip.textContent, chip.className]).toEqual(['Cancelled', 'subs-chip subs-chip--neutral']);

    const [row] = page.$$('#subs-orders > li');
    expect(row.className).toBe('subs-orow subs-orow--skipped');
    expect(row.querySelector('.subs-orow__open').tagName).toBe('DIV'); // no order to open
    expect(row.querySelector('.subs-chip').textContent).toBe('Skipped');
    expect(row.querySelector('.subs-orow__why').textContent).toBe("I'll be away");
    expect(row.querySelector('.subs-orow__amt').textContent).toBe('');
  });

  it('still shows a kept paid order (Review Focus 4)', async () => {
    const page = await boot('cancelled_kept_paid');
    const card = hero(page);
    expect(page.$$('.subs-hero__note', card).map((p) => p.textContent)).toEqual([
      'Your paid order for Thursday, 29 October, 10:00–12:00 will still be delivered.',
      'Your past orders are below.',
    ]);
    expect(page.$1('.subs-hero__order [data-action="open-order"]', card).dataset.orderId).toBe('1');
  });

  it('keeps the order the driver has "On the way", and only Pay for it (spec §12)', async () => {
    const page = await boot('cancelled_on_the_way');
    const card = hero(page);
    expect(card.className).toBe('subs-hero subs-hero--cancelled');
    expect(page.$1('.subs-hero__date', card).textContent).toBe('Cancelled on 28 October');
    expect(page.$$('.subs-hero__note', card).map((p) => p.textContent)).toEqual([
      'Your order for Thursday, 29 October, 10:00–12:00 is on the way.',
      'Your past orders are below.',
    ]);
    expect(seen(page.$1('.subs-hero__order', card)))
      .toBe(`Order TG_000001_26 ${money('30 000')} On the way Waiting for payment`);
    expect(heroButtons(page)).toEqual([`Pay ${money('30 000')}`]);
    expect(primaries(page).map((b) => [b.dataset.action, b.dataset.orderId])).toEqual([['pay', '1']]);
    // The driver has it, so Pay has no deadline (spec 2026-10-27 §4.4).
    expect(page.$$('.subs-hint', card).map((p) => p.textContent)).toEqual(["Pay online whenever you're ready."]);
    expect(page.$1('.subs-wl-band', card)).toBeNull();
    expect(page.$$('#subscription [data-action="change"]')).toHaveLength(0);
    expect(page.$1('#subscription [data-action="cancel"]')).toBeNull();
  });
});

// ------------------------------------------------------------------ a discontinued product (Review Focus 2)

describe('a discontinued product', () => {
  it('another product still delivers: the row is greyed with a note, the card names what is not included', async () => {
    const page = await boot('discontinued_some');
    const rows = page.$$('#subs-sec-items .subs-item');
    expect(rows.map((row) => row.className)).toEqual(['subs-item', 'subs-item is-unavailable']);
    const gone = rows[1];
    expect(gone.querySelector('b').textContent).toBe('Spring water 5L');
    expect(gone.querySelector('.subs-item__warn').textContent)
      .toBe("No longer available. We'll deliver the rest of your order without it.");
    expect(gone.querySelector('.subs-money').textContent).toBe('Not included');
    expect(page.$1('.subs-warning', hero(page)).textContent).toBe('Not included: Spring water 5L (no longer available).');
  });

  it('the only product: the card says the delivery cannot be placed, the skipped bottle is struck through', async () => {
    const page = await boot('discontinued_all');
    expect(page.$1('.subs-warning', hero(page)).textContent).toBe(
      "None of these products are available any more, so we can't place your next order. " +
      'Change your items to keep your deliveries coming.',
    );
    // Nothing can be delivered, so the card does not also promise to place the order.
    expect(page.$$('.subs-hero__note', hero(page))).toHaveLength(0);
    const [row] = page.$$('#subs-sec-items .subs-item');
    expect(row.className).toBe('subs-item is-unavailable');
    expect(row.querySelector('.subs-item__warn').textContent).toBe('No longer available.');
    const [skipped] = page.$$('.subs-wl > li', hero(page));
    expect(skipped.className).toBe('subs-wl__item subs-wl__item--skipped');
    expect(skipped.querySelector('svg').getAttribute('class')).toBe('subs-bottle subs-bottle--skipped');
    expect(skipped.querySelector('.subs-wl__note').textContent).toBe('Skipped');
    expect(skipped.querySelector('.sr-only').textContent).toBe('Thursday, 29 October, skipped');
    expect(page.window.getComputedStyle(skipped.querySelector('.subs-wl__date')).textDecoration).toContain('line-through');
    expect(page.$1('#subs-orders .subs-orow__why').textContent).toBe('a product was out of stock');
  });
});

// ------------------------------------------------------------------ Orders paging

describe('Show more orders', () => {
  it('shows five rows, then appends ten, keeping focus on the button, then the rest', async () => {
    const page = await boot('history_paging');
    expect(page.$$('#subs-orders > li')).toHaveLength(5);
    const more = page.$1('#subs-orders-more');
    expect([more.hidden, more.textContent]).toEqual([false, 'Show more orders']);

    more.focus();
    more.click();
    await settle(page.window);
    expect(keys(page.requests).slice(3)).toEqual([history(1, 2), history(1, 3)]);
    expect(page.$$('#subs-orders > li')).toHaveLength(15);
    expect(page.$1('#subs-status').textContent).toBe('Showing 15 of 17 orders');
    expect(page.document.activeElement).toBe(more);
    expect(more.hidden).toBe(false);

    more.click();
    await settle(page.window);
    expect(keys(page.requests).slice(5)).toEqual([history(1, 4)]);
    expect(page.$$('#subs-orders > li')).toHaveLength(17);
    expect(page.$1('#subs-status').textContent).toBe('Showing 17 of 17 orders');
    expect(more.hidden).toBe(true);
    expect(page.document.activeElement).toBe(page.$1('#subs-orders'));
    // Newest first, as the backend merged them.
    expect(page.$$('#subs-orders .subs-orow__when').map((el) => el.textContent).slice(0, 2)).toEqual(['Thu 18 Feb', 'Thu 11 Feb']);
  });

  it('says so when Orders cannot load, and never claims the list is empty', async () => {
    const page = await boot('needs_payment', {
      answers: { [history(1)]: { status: 500, body: { success: false, message: 'boom' } } },
    });
    expect(page.$$('#subs-orders > li')).toHaveLength(0);
    expect(page.$1('#subs-orders-empty').textContent).toBe("We couldn't load your orders just now.");
  });
});

// ------------------------------------------------------------------ the strip and switching

describe('the strip', () => {
  it('orders needs-payment, active, paused, and keeps cancelled behind "Show cancelled (n)"', async () => {
    const page = await boot('mixed');
    const names = page.$$('#subs-strip > li').map((li) => [seen(li), li.hidden]);
    expect(names).toEqual([
      ['Home water Waiting for payment', false],
      ['Office cooler set Next: Thu 29 Oct', false],
      ['Dacha 10 L Paused', false],
      ['Show cancelled (1)', false],
      ['Old plan Cancelled 26 October', true],
      ['New subscription', false],
    ]);
    // The backend listed them newest first: the page did the ordering.
    expect(page.listed.map((s) => s.name)).toEqual(['Old plan', 'Dacha 10 L', 'Office cooler set', 'Home water']);
    expect(page.$1('#subs-strip [aria-current="true"]').dataset.subId).toBe('1');
    const toggle = page.$1('#subs-strip [data-action="toggle-cancelled"]');
    expect(toggle.getAttribute('aria-expanded')).toBe('false');

    toggle.click();
    const reopened = page.$1('#subs-strip [data-action="toggle-cancelled"]');
    expect([reopened.getAttribute('aria-expanded'), reopened.textContent]).toEqual(['true', 'Hide cancelled']);
    expect(page.$1('#subs-strip .subs-strip__item--cancelled').hidden).toBe(false);
    expect(page.document.activeElement).toBe(reopened);
  });

  it('switches in place: the URL, aria-current, the body and its history, focus on the name', async () => {
    const page = await boot('mixed');
    const office = page.$1('#subs-strip a[data-sub-id="2"]');
    expect(office.getAttribute('href')).toBe('?sub=2#subscription');
    office.click();
    await settle(page.window);

    expect(page.window.location.search).toBe('?sub=2');
    expect(page.window.location.hash).toBe('#subscription');
    expect(page.$1('#subs-strip [aria-current="true"]').dataset.subId).toBe('2');
    expect(page.$$('#subs-strip [aria-current]')).toHaveLength(1);
    expect(page.$1('#sub-h').textContent).toBe('Office cooler set');
    expect(page.document.activeElement).toBe(page.$1('#sub-h'));
    expect(keys(page.requests)).toEqual([LIST, history(1), methods(1), history(2), methods(1)]);

    page.window.history.back();
    await settle(page.window);
    expect(page.$1('#sub-h').textContent).toBe('Home water');
  });

  it('opens the deep-linked subscription without moving focus', async () => {
    const page = await boot('mixed', { search: '?sub=3' });
    expect(page.$1('#sub-h').textContent).toBe('Dacha 10 L');
    expect(page.document.activeElement).toBe(page.document.body);
    expect(keys(page.requests)).toEqual([LIST, history(3), methods(1)]);
  });

  it('reveals the cancelled group when the link names a cancelled subscription', async () => {
    const page = await boot('mixed', { search: '?sub=4' });
    expect(page.$1('#sub-h').textContent).toBe('Old plan');
    expect(page.$1('#subs-strip [data-action="toggle-cancelled"]').getAttribute('aria-expanded')).toBe('true');
    expect(page.$1('#subs-strip a[data-sub-id="4"]').closest('li').hidden).toBe(false);
  });

  it('falls back to the needs-payment subscription for an unknown ?sub', async () => {
    const page = await boot('mixed', { search: '?sub=999' });
    expect(page.$1('#sub-h').textContent).toBe('Home water');
  });

  it('shows cancelled subscriptions directly when every one is cancelled, the newest selected', async () => {
    const page = await boot('all_cancelled');
    expect(page.$1('#subs-strip [data-action="toggle-cancelled"]')).toBeNull();
    expect(page.$$('#subs-strip > li').map((li) => [seen(li), li.hidden])).toEqual([
      ['Summer plan Cancelled 26 October', false],
      ['Old plan Cancelled 26 October', false],
      ['New subscription', false],
    ]);
    expect(page.$1('#sub-h').textContent).toBe('Summer plan');
  });
});

describe('no subscriptions', () => {
  it('shows the empty state with the way to the builder', async () => {
    const page = await boot('empty');
    expect(page.$1('#subs-strip-nav').hidden).toBe(true);
    expect(page.$1('#sub-h').textContent).toBe('Water that arrives on its own');
    expect(page.$1('.subs-empty p').textContent).toBe(
      "Pick your bottles and a day, and we'll place the order for you every time, so you never forget and never run out. " +
      'Delivery is always free.',
    );
    const cta = page.$1('.subs-empty a.subs-btn--primary');
    expect([cta.textContent, cta.getAttribute('href')]).toEqual(['Set up a subscription', '/#subscription-constructor']);
    // A link styled as the primary button keeps white text on the aqua-deep fill: its rule must outrank the
    // page's link colour `.subs a` (0,1,1). jsdom's cascade ignores specificity, so the rule itself is checked.
    expect(CSS).toMatch(/\.subs \.subs-btn--primary \{[^}]*color: #fff;/);
    expect(CSS).toMatch(/\.subs a \{ color: var\(--subs-aqua-deep\)/);
  });

  it('offers a retry when the list cannot load', async () => {
    const page = await boot('empty', { answers: { [LIST]: { status: 500, body: { success: false } } } });
    const error = page.$1('#subscription .subs-error');
    expect(error.getAttribute('role')).toBe('alert');
    expect(seen(error)).toBe("We couldn't load your subscriptions just now. Please try again. Try again");
  });
});

// ------------------------------------------------------------------ languages and long labels

describe.each([
  ['needs_payment_uz', 'uz'],
  ['needs_payment_ru', 'ru'],
  ['paused_ru', 'ru'],
])('%s: every word in the page language', (name, language) => {
  it('renders no English page word, no key and no unfilled placeholder', async () => {
    const page = await boot(name);
    expect(page.fixture.language).toBe(language);
    const rendered = page.$1('#subs').textContent;
    const english = SHELL.en.island.i18n;
    Object.entries(page.words).forEach(([key, value]) => {
      const en = english[key];
      const words = en.replace(/\{\w+\}/g, '|').split('|').map((part) => part.trim()).filter((part) => part.length >= 4);
      if (en === value) return;
      words.forEach((part) => expect(rendered, `${key}: "${part}"`).not.toContain(part));
    });
    expect(rendered).not.toMatch(/cabinet\.subs\.|\{\w+\}/);
    expect(rendered).not.toMatch(/Next delivery|Waiting for payment|Skip this delivery|Orders from this subscription/);
  });

  it('labels the sections from the island and the card\'s buttons with the published labels', async () => {
    const page = await boot(name);
    const section = (key) => page.words[key];
    const actions = page.listed[0].allowed_actions;
    expect(page.$1('#subs-sec-items h3').textContent).toBe(section('section.items'));
    expect(page.$1('#subs-sec-orders h3').textContent).toBe(section('section.orders'));
    if (name.startsWith('needs_payment')) {
      expect(page.$1('.subs-hero__label').textContent).toBe(section('hero.label'));
      expect(primaries(page)[0].textContent).toBe(actions.pay.label);
    } else {
      expect(primaries(page)[0].textContent).toBe(actions.resume.label);
    }
    expect(page.$1('.subs-foot [data-action="cancel"]').textContent).toBe(actions.cancel.label);
  });
});

describe('long Russian and Uzbek labels at 360px (Review Focus 5)', () => {
  it.each(['needs_payment_ru', 'needs_payment_uz'])('%s: button rows wrap and nothing is set to nowrap', async (name) => {
    const page = await boot(name);
    const style = (el) => page.window.getComputedStyle(el);
    const row = page.$1('.subs-hero__row');
    expect(style(row).display).toBe('flex');
    expect(style(row).flexWrap).toBe('wrap');
    page.$$('#subscription .subs-btn, #subscription .subs-change, #subscription .subs-danger').forEach((button) => {
      expect(style(button).whiteSpace).not.toBe('nowrap');
    });
    expect(style(page.$1('.subs-hero__order')).flexWrap).toBe('wrap');
    expect(style(page.$1('.subs-foot')).flexWrap).toBe('wrap');
  });

  it('the stylesheet sets nowrap nowhere', () => {
    expect(CSS).not.toMatch(/white-space\s*:\s*nowrap/);
  });
});

// =================================================================== the write side (plan Task 4b)
//
// Each `write_<scenario>` case clicks through what a customer does and asserts the script sent exactly the
// fixture's requests, in order (method, path, body), each with X-CSRF-TOKEN and X-Platform as base.js sends
// them. The pytest replay sends the same requests through the real endpoints and asserts the rows they leave.

// Focus checks compare identity and name the focused element (jsdom elements from the page's realm do not
// pretty-print in a failure).
const isFocused = (page, el) => {
  const active = page.document.activeElement;
  expect(active === el, `focus is on ${active ? active.outerHTML.slice(0, 160) : 'nothing'}`).toBe(true);
};
const wait = (window, ms) => new Promise((resolve) => window.setTimeout(resolve, ms));
// The script previews a drafted change 300ms after the last edit (UX brief "PREVIEWS COME FROM THE BACKEND").
const afterPreview = async (page) => {
  await wait(page.window, 340);
  await settle(page.window);
};
const press = async (page, el) => {
  expect(el, 'the control to press').not.toBeNull();
  el.click();
  await settle(page.window);
};
// A tap on a tile focuses its radio, as a browser does, then checks it.
const pick = async (page, input) => {
  expect(input, 'the option to pick').not.toBeNull();
  input.focus();
  input.click();
  await settle(page.window);
};
const choose = async (page, select, value) => {
  select.value = value;
  select.dispatchEvent(new page.window.Event('change', { bubbles: true }));
  await settle(page.window);
};
const type = async (page, input, value) => {
  input.value = value;
  input.dispatchEvent(new page.window.Event('input', { bubbles: true }));
  await settle(page.window);
};
const writes = (page) => page.requests.filter((r) => r.method !== 'GET');
const exchanges = (page) => page.fixture.exchanges.map((e) => `${e.method} ${e.path}`);

// Every request in the fixture was sent, in order, with its body; nothing else was sent; every write carried
// the CSRF header and every request the platform header.
function sentExactlyTheContract(page) {
  expect(page.mismatches).toEqual([]);
  expect(keys(page.requests)).toEqual(exchanges(page));
  page.requests.forEach((request, index) => {
    expect(request.body).toEqual(page.fixture.exchanges[index].body);
    expect(request.options.headers['X-Platform']).toBe('web');
    expect(request.options.credentials).toBe('include');
    if (request.method !== 'GET') expect(request.options.headers['X-CSRF-TOKEN']).toBe(CSRF);
  });
}

const heroStatus = (page) => page.$1('#subs-hero-status');
const confirmPanel = (page) => page.$1('#subs-confirm');
const confirmButtons = (page) => page.$$('#subs-confirm .subs-confirm__row button').map((b) => b.textContent);
const reasonChipLabels = (page) => page.$$('#subs-confirm [data-action="skip-reason"]').map((c) => c.textContent);
const savebar = (page) => page.$1('#subs-savebar');
const saveButton = (page) => page.$1('#subs-savebar [data-action="save"]');

// ------------------------------------------------------------------ Pay

describe('Pay', () => {
  it('the card\'s Pay asks the retry route once, says it is opening, then hands the customer to Click', async () => {
    const page = await boot('write_pay');
    const pay = primaries(page)[0];
    pay.click();
    // While the request is in flight the button is busy and refuses a second tap.
    expect(pay.getAttribute('aria-disabled')).toBe('true');
    expect(pay.textContent).toBe('Opening payment…');
    expect(pay.querySelector('.spinner-border-sm[aria-hidden="true"]')).not.toBeNull();
    pay.click();
    await settle(page.window);
    sentExactlyTheContract(page);
    expect(page.window.location.hash).toBe('#click-checkout');
  });

  it('an Orders row\'s Pay is the same function and the same request', async () => {
    const page = await boot('write_pay');
    await press(page, page.$1('#subs-orders .subs-orow__pay'));
    sentExactlyTheContract(page);
    expect(page.window.location.hash).toBe('#click-checkout');
  });

  it('still pays when the last Click attempt failed (Review Focus 3)', async () => {
    const page = await boot('write_pay_after_failed_attempt');
    expect(page.$1('.subs-hero__order .subs-chip').textContent).toBe('Payment failed');
    await press(page, primaries(page)[0]);
    sentExactlyTheContract(page);
    expect(page.window.location.hash).toBe('#click-checkout');
  });

  it('a failure shows an inline error next to Pay, restores the button and goes nowhere', async () => {
    const page = await boot('needs_payment', {
      answers: { 'POST /api/v1/orders/1/retry-payment': { status: 500, body: { success: false } } },
    });
    const pay = primaries(page)[0];
    await press(page, pay);
    expect(heroStatus(page).textContent).toBe("We couldn't open the payment just now. Please try again.");
    expect(heroStatus(page).getAttribute('role')).toBe('alert');
    expect(pay.getAttribute('aria-disabled')).toBeNull();
    expect(pay.textContent).toBe(`Pay ${money('30 000')}`);
    expect(page.window.location.hash).toBe('');
  });

  it('a failure on an Orders row shows its error in that row', async () => {
    const page = await boot('needs_payment', {
      answers: { 'POST /api/v1/orders/1/retry-payment': { status: 500, body: { success: false } } },
    });
    await press(page, page.$1('#subs-orders .subs-orow__pay'));
    expect(page.$1('#subs-orders .subs-orow__error').textContent).toBe("We couldn't open the payment just now. Please try again.");
  });
});

// ------------------------------------------------------------------ Skip

describe('Skip, the inline two-step', () => {
  it('unpaid order: names the date and the consequence, takes an optional reason, then skips', async () => {
    const page = await boot('write_skip_unpaid');
    const skip = page.$1('.subs-hero__row [data-action="skip"]');
    expect([skip.getAttribute('aria-expanded'), skip.getAttribute('aria-controls')]).toEqual(['false', 'subs-confirm']);
    expect(confirmPanel(page).hidden).toBe(true);
    await press(page, skip);
    expect(skip.getAttribute('aria-expanded')).toBe('true');
    expect(confirmPanel(page).hidden).toBe(false);
    isFocused(page, confirmPanel(page));
    expect(page.$1('#subs-confirm .subs-confirm__text').textContent).toBe(
      "Skip Thursday, 29 October? We'll cancel that order. Your next delivery will be Thursday, 5 November.",
    );
    expect(page.$1('#subs-confirm legend').textContent).toBe('Want to tell us why? (optional)');
    const chips = page.$$('#subs-confirm [data-action="skip-reason"]');
    expect(chips.map((c) => [c.textContent, c.getAttribute('aria-pressed')])).toEqual([
      ['I still have water', 'false'], ["I'll be away", 'false'], ['Other', 'false'],
    ]);
    expect(confirmButtons(page)).toEqual(['Skip 29 October', 'Keep this delivery']);
    expect(writes(page)).toEqual([]);  // nothing is sent before the second step

    await press(page, chips[0]);
    expect(page.$1('#subs-confirm [data-reason="still_have_water"]').getAttribute('aria-pressed')).toBe('true');
    await press(page, page.$1('#subs-confirm [data-action="skip-confirm"]'));
    sentExactlyTheContract(page);

    // The panel closed; the card, its waterline and the status line are the backend's fresh answer.
    expect(confirmPanel(page).hidden).toBe(true);
    expect(heroStatus(page).textContent).toBe('Skipped. Your next delivery is Thursday, 5 November, 10:00–12:00.');
    expect(page.$1('#subs-status').textContent).toBe(heroStatus(page).textContent);
    expect(page.$1('.subs-hero__date').textContent).toBe('Thursday, 5 November');
    const [first] = page.$$('.subs-wl > li', hero(page));
    expect(first.className).toBe('subs-wl__item subs-wl__item--skipped');
    expect(page.document.activeElement.closest('.subs-hero__actions')).not.toBeNull();
  });

  it('paid order: it still comes; the one after it is skipped, with the reason in the customer\'s words', async () => {
    const page = await boot('write_skip_after_paid');
    await press(page, page.$1('[data-action="skip"]'));
    expect(page.$1('#subs-confirm .subs-confirm__text').textContent).toBe(
      "This order is paid, so it will still arrive on 29 October. We'll skip the one after it, on 5 November.",
    );
    expect(confirmButtons(page)).toEqual(['Skip 5 November', 'Keep both']);
    await press(page, page.$1('#subs-confirm [data-reason="other"]'));
    const other = page.$1('#subs-skip-other');
    isFocused(page, other);
    expect(page.$1('label[for="subs-skip-other"]').textContent).toBe('In a few words (optional)');
    await type(page, other, '  Visiting family ');
    await press(page, page.$1('#subs-confirm [data-action="skip-confirm"]'));
    sentExactlyTheContract(page);
    expect(heroStatus(page).textContent).toBe('Skipped. Your next delivery is Thursday, 29 October, 10:00–12:00.');
  });

  it('no order yet, no reason: just the date', async () => {
    const page = await boot('write_skip_no_order');
    await press(page, page.$1('[data-action="skip"]'));
    expect(page.$1('#subs-confirm .subs-confirm__text').textContent)
      .toBe('Skip Thursday, 29 October? Your next delivery will be Thursday, 5 November.');
    // A chip pressed twice is no reason again.
    await press(page, page.$1('#subs-confirm [data-reason="away"]'));
    await press(page, page.$1('#subs-confirm [data-reason="away"]'));
    await press(page, page.$1('#subs-confirm [data-action="skip-confirm"]'));
    sentExactlyTheContract(page);
  });

  it('with the driver: the confirm says the order on the way still comes and the skip is the one after it', async () => {
    const page = await boot('on_the_way');
    await press(page, page.$1('[data-action="skip"]'));
    expect(page.$1('#subs-confirm .subs-confirm__text').textContent).toBe(
      "Your driver already has your order for Thursday, 29 October, so it will still arrive. We'll skip the one after it, on 5 November.",
    );
    expect(confirmButtons(page)).toEqual(['Skip 5 November', 'Keep both']);
  });

  it('Keep this delivery closes it, sends nothing and gives focus back to Skip', async () => {
    const page = await boot('needs_payment');
    const skip = page.$1('[data-action="skip"]');
    await press(page, skip);
    await press(page, page.$1('#subs-confirm [data-action="confirm-keep"]'));
    expect(confirmPanel(page).hidden).toBe(true);
    expect(skip.getAttribute('aria-expanded')).toBe('false');
    isFocused(page, page.$1('[data-action="skip"]'));
    expect(writes(page)).toEqual([]);
  });

  it('a stale date is a 409: the message shows, nothing is skipped, and the page reads the latest (Review Focus 1)', async () => {
    const page = await boot('write_skip_stale');
    await press(page, page.$1('[data-action="skip"]'));
    await press(page, page.$1('#subs-confirm [data-action="skip-confirm"]'));
    sentExactlyTheContract(page);  // the 409, then the list, its Orders and its menu again
    expect(heroStatus(page).textContent).toBe('This changed. Refresh to see the latest.');
    expect(heroStatus(page).getAttribute('role')).toBe('alert');
    // The fresh answer: the order was paid meanwhile, so there is nothing to pay and Skip now names 5 November.
    expect(page.$1('.subs-hero__order .subs-chip').textContent).toBe('Paid');
    expect(primaries(page)).toHaveLength(0);
    await press(page, page.$1('[data-action="skip"]'));
    expect(confirmButtons(page)).toEqual(['Skip 5 November', 'Keep both']);
  });

  it('any other failure keeps the panel open with an inline error and never says Skipped', async () => {
    const page = await boot('needs_payment', {
      answers: { 'POST /api/v1/subscriptions/1/skip-next-delivery': { status: 500, body: { success: false } } },
    });
    await press(page, page.$1('[data-action="skip"]'));
    const confirm = page.$1('#subs-confirm [data-action="skip-confirm"]');
    await press(page, confirm);
    expect(confirmPanel(page).hidden).toBe(false);
    expect(page.$1('#subs-confirm .subs-error-line').textContent).toBe("We couldn't do that just now. Please try again.");
    expect(page.$1('#subs-confirm .subs-error-line').getAttribute('role')).toBe('alert');
    expect(page.$1('#subs-confirm [data-action="skip-confirm"]').getAttribute('aria-disabled')).toBeNull();
    expect(page.$1('#subs').textContent).not.toContain('Skipped.');
  });
});

// ------------------------------------------------------------------ Pause and Resume

describe('Pause and Resume', () => {
  it('pausing that cancels the unpaid order asks inline first, then pauses', async () => {
    const page = await boot('write_pause_cancels_order');
    const pause = page.$1('[data-action="pause"]');
    await press(page, pause);
    expect(pause.getAttribute('aria-expanded')).toBe('true');
    expect(page.$$('#subs-confirm .subs-confirm__text').map((p) => p.textContent)).toEqual([
      "Pause deliveries? We won't place new orders until you resume.",
      "We'll cancel your order for 29 October.",
    ]);
    expect(confirmButtons(page)).toEqual(['Pause subscription', 'Keep delivering']);
    expect(writes(page)).toEqual([]);
    await press(page, page.$1('#subs-confirm [data-action="pause-confirm"]'));
    sentExactlyTheContract(page);
    expect(hero(page).className).toBe('subs-hero subs-hero--paused');
    expect(primaries(page).map((b) => b.dataset.action)).toEqual(['resume']);
    expect(heroStatus(page).textContent).toBe("Paused. Tap Resume whenever you're ready.");
    isFocused(page, primaries(page)[0]);
  });

  it('pausing with no order placed pauses at once, sending only the state its card was drawn from', async () => {
    const page = await boot('write_pause_now');
    await press(page, page.$1('[data-action="pause"]'));
    sentExactlyTheContract(page);
    // Spec 2026-10-27 §16 M2: the token the backend published with the card, echoed as it is.
    expect(writes(page).map((w) => w.body)).toEqual([{ expected_state: page.listed[0].allowed_actions.expected_state }]);
    expect(heroStatus(page).textContent).toBe("Paused. Tap Resume whenever you're ready.");
  });

  it('Resume never asks, sends {}, and says when the next delivery is', async () => {
    const page = await boot('write_resume');
    await press(page, primaries(page)[0]);
    sentExactlyTheContract(page);
    expect(writes(page).map((w) => w.options.body)).toEqual(['{}']);
    expect(heroStatus(page).textContent).toBe('Welcome back. Your next delivery is Thursday, 29 October, 10:00–12:00.');
    expect(hero(page).className).toBe('subs-hero');
  });
});

// ------------------------------------------------------------------ Cancel

const openCancel = async (page) => {
  await press(page, page.$1('.subs-foot [data-action="cancel"]'));
  await settle(page.window);
  return page.$1('#subs-cancel-modal');
};

describe('the Cancel modal', () => {
  it('lists what cancelling does, offers Pause instead and Deliver less often, and starts on Keep', async () => {
    const page = await boot('needs_payment');
    const trigger = page.$1('.subs-foot [data-action="cancel"]');
    expect([trigger.dataset.toggle, trigger.dataset.target]).toEqual(['modal', '#subs-cancel-modal']);
    const modal = await openCancel(page);
    expect(modal.classList.contains('show')).toBe(true);
    expect(page.$1('#subs-cancel-title').textContent).toBe('Cancel “Home water”?');
    expect(page.$$('.subs-modal__list li', modal).map((li) => li.textContent)).toEqual([
      "We won't place any more automatic orders.",
      "We'll cancel your order for 29 October.",
      'Your AquaCoins stay with you.',
    ]);
    expect(seen(page.$1('.subs-modal__alt', modal))).toBe('Running low on space or still have water? Pause instead Deliver less often');
    expect(page.$1('label[for="subs-cancel-reason"]').textContent).toBe('Anything we could do better? (optional)');
    expect(page.$$('.modal-footer button', modal).map((b) => [b.textContent, b.className])).toEqual([
      ['Keep subscription', 'subs-btn subs-btn--secondary'],
      ['Cancel subscription', 'subs-btn subs-btn--danger'],
    ]);
    isFocused(page, page.$1('[data-action="cancel-keep"]', modal));
    expect(page.window.confirm).not.toHaveBeenCalled();

    await press(page, page.$1('[data-action="cancel-keep"]', modal));
    await settle(page.window);
    expect(modal.classList.contains('show')).toBe(false);
    expect(writes(page)).toEqual([]);
    isFocused(page, page.$1('.subs-foot [data-action="cancel"]'));
  });

  it('cancels with the optional words, then shows it read-only among the cancelled', async () => {
    const page = await boot('write_cancel_with_reason');
    const modal = await openCancel(page);
    await type(page, page.$1('#subs-cancel-reason'), 'Moving to another city');
    await press(page, page.$1('[data-action="cancel-confirm"]', modal));
    await settle(page.window);
    sentExactlyTheContract(page);
    expect(modal.classList.contains('show')).toBe(false);
    expect(hero(page).className).toBe('subs-hero subs-hero--cancelled');
    expect(heroStatus(page).textContent).toBe('Subscription cancelled.');
    expect(page.$1('#subscription').classList.contains('is-readonly')).toBe(true);
    expect(page.$$('#subscription [data-action="change"]')).toHaveLength(0);
    expect(page.$1('#subs-strip [aria-current="true"]').closest('li').className).toContain('subs-strip__item--cancelled');
    isFocused(page, page.$1('#sub-h'));
  });

  it('a paid order stays: the modal says when it comes, and no words send only the card\'s state', async () => {
    const page = await boot('write_cancel_paid_kept');
    const modal = await openCancel(page);
    expect(page.$$('.subs-modal__list li', modal).map((li) => li.textContent)).toEqual([
      "We won't place any more automatic orders.",
      'Your paid order for Thursday, 29 October, 10:00–12:00 will still be delivered.',
      'Your AquaCoins stay with you.',
    ]);
    await press(page, page.$1('[data-action="cancel-confirm"]', modal));
    await settle(page.window);
    sentExactlyTheContract(page);
    expect(writes(page).map((w) => w.body)).toEqual([{ expected_state: page.listed[0].allowed_actions.expected_state }]);
  });

  // Final review Minor 2: each kept order is worded by the reason the backend published for keeping it. The
  // backend checks AWAITING_NEW_DATE before PAID, so an order waiting for a new date may be paid or unpaid: its
  // line names no payment either way (this fixture's order is unpaid, so it must never be called "paid").
  it('an order waiting for a new date is kept, and the modal never calls it paid', async () => {
    const page = await boot('awaiting_new_date');
    const modal = await openCancel(page);
    expect(page.$$('.subs-modal__list li', modal).map((li) => li.textContent)).toEqual([
      "We won't place any more automatic orders.",
      "Your order TG_000001_26 stays: we'll deliver it on its new date.",
      'Your AquaCoins stay with you.',
    ]);
    expect(modal.textContent).not.toMatch(/paid/i);
  });

  // Re-review G8: the leftover NOT_CANCELLABLE (an unpaid order past PENDING over a returned delivery row) has its
  // own line, `cancel.kept_other`, and is never worded as a paid one (the original Minor 2 bug).
  it('an order kept for another reason stays as it is, and the modal never calls it paid', async () => {
    const page = await boot('not_cancellable');
    const modal = await openCancel(page);
    expect(page.$$('.subs-modal__list li', modal).map((li) => li.textContent)).toEqual([
      "We won't place any more automatic orders.",
      'Your order TG_000001_26 stays as it is.',
      'Your AquaCoins stay with you.',
    ]);
    expect(modal.textContent).not.toMatch(/paid/i);
  });

  it('the order the driver has is kept as on the way, not as paid', async () => {
    const page = await boot('on_the_way');
    const modal = await openCancel(page);
    expect(page.$$('.subs-modal__list li', modal).map((li) => li.textContent)).toEqual([
      "We won't place any more automatic orders.",
      'Your order for Thursday, 29 October, 10:00–12:00 is on the way.',
      'Your AquaCoins stay with you.',
    ]);
  });

  it('Pause instead closes the modal and runs the pause, with its inline confirm', async () => {
    const page = await boot('write_pause_cancels_order');
    const modal = await openCancel(page);
    await press(page, page.$1('[data-action="cancel-pause"]', modal));
    await settle(page.window);
    expect(modal.classList.contains('show')).toBe(false);
    expect(confirmPanel(page).hidden).toBe(false);
    await press(page, page.$1('#subs-confirm [data-action="pause-confirm"]'));
    sentExactlyTheContract(page);
  });

  it('Deliver less often closes the modal and opens Schedule on "How often"', async () => {
    const slots = loadCase('write_schedule_saturday').exchanges[3];
    const page = await boot('needs_payment', { answers: { [`GET ${slots.path}`]: { status: 200, body: slots.response } } });
    const modal = await openCancel(page);
    await press(page, page.$1('[data-action="cancel-less-often"]', modal));
    await settle(page.window);
    expect(modal.classList.contains('show')).toBe(false);
    expect(page.$1('#subs-edit-schedule')).not.toBeNull();
    isFocused(page, page.$1('#subs-edit-schedule input[name="subs-frequency"][value="weekly"]'));
    expect(page.$1('#subs-edit-schedule legend').textContent).toBe('How often');
    expect(writes(page)).toEqual([]);
  });

  it('a refused cancel keeps the modal open with the reason', async () => {
    const page = await boot('needs_payment', {
      answers: { 'POST /api/v1/subscriptions/1/cancel': { status: 400, body: { success: false, message: 'Not now.' } } },
    });
    const modal = await openCancel(page);
    await press(page, page.$1('[data-action="cancel-confirm"]', modal));
    expect(modal.classList.contains('show')).toBe(true);
    expect(page.$1('#subs-cancel-error').hidden).toBe(false);
    expect(page.$1('#subs-cancel-error').textContent).toBe('Not now.');
  });
});

// ------------------------------------------------------------------ Items

const openSection = async (page, name) => {
  await press(page, changeButton(page, name));
  return page.$1(`#subs-edit-${name}`);
};
const row = (page, productId) => page.$1(`#subs-edit-rows > li[data-product-id="${productId}"]`);

describe('Items, read then edit', () => {
  it('a stepper, a preview, the picker, one atomic save; only then "Saved" with the new total', async () => {
    const page = await boot('write_items_save');
    const form = await openSection(page, 'items');
    expect(changeButton(page, 'items').getAttribute('aria-expanded')).toBe('true');
    expect(changeButton(page, 'items').getAttribute('aria-controls')).toBe('subs-edit-items');
    const water = row(page, 1);
    const input = water.querySelector('.subs-stepper__input');
    isFocused(page, input);
    expect([input.value, input.getAttribute('inputmode'), input.getAttribute('pattern'), input.type])
      .toEqual(['2', 'numeric', '[0-9]*', 'text']);
    expect(water.querySelector('[data-action="dec"]').getAttribute('aria-label')).toBe('Fewer: Pure Water 19L');
    expect(water.querySelector('[data-action="inc"]').getAttribute('aria-label')).toBe('More: Pure Water 19L');
    // The only product cannot be removed: shown, blocked, its reason beside it.
    const remove = water.querySelector('[data-action="remove-item"]');
    expect([remove.getAttribute('aria-disabled'), remove.getAttribute('aria-describedby')]).toEqual(['true', 'subs-last-item']);
    expect(page.$1('#subs-last-item').textContent).toBe('A subscription needs at least one product. Need a break instead? Pause.');
    await press(page, remove);
    expect(row(page, 1)).not.toBeNull();
    expect(savebar(page)).toBeNull();  // nothing changed yet

    await press(page, water.querySelector('[data-action="inc"]'));
    expect(row(page, 1).querySelector('.subs-stepper__input').value).toBe('3');
    expect(saveButton(page).getAttribute('aria-disabled')).toBe('true');  // waits for the preview
    expect(page.$1('.subs-savebar__figure').textContent).toBe('Checking your changes…');
    expect(page.document.body.classList.contains('subs-editing')).toBe(true);
    await afterPreview(page);
    expect(row(page, 1).querySelector('.subs-item__line').textContent).toBe(`3 × ${money('15 000')}`);
    expect(row(page, 1).querySelector('.subs-item__total').textContent).toBe(money('45 000'));
    expect(row(page, 1).querySelector('.subs-item__total').getAttribute('aria-live')).toBe('off');
    expect(page.$$('#subs-edit-totals dd').map((dd) => dd.textContent)).toEqual([money('45 000'), money('195 000')]);
    expect(page.$1('#subs-edit-note').textContent)
      .toBe('Changes start with your next new order. Your order for 29 October stays as it is.');
    expect(page.$1('.subs-savebar__title').textContent).toBe('Unsaved changes');
    expect(page.$1('.subs-savebar__figure').textContent).toBe(`New total ${money('45 000')} per delivery`);
    expect(saveButton(page).getAttribute('aria-disabled')).toBeNull();

    // The picker lists only what is not in the subscription yet, with a stepper and a button naming the result.
    await press(page, page.$1('[data-action="open-picker"]'));
    await settle(page.window);
    const picker = page.$1('#subs-picker-modal');
    expect(picker.classList.contains('show')).toBe(true);
    expect(page.$1('#subs-picker-title').textContent).toBe('Add to your subscription');
    expect(page.$$('.subs-pick b', picker).map((b) => b.textContent)).toEqual(['Spring water 5L']);
    // Each product at its subscription price, the label the backend publishes (final review Minor 10).
    expect(page.$$('.subs-pick .subs-pick__price', picker).map((el) => el.textContent)).toEqual([money('5 000')]);
    const add = page.$1('[data-action="picker-add"]', picker);
    expect(add.textContent).toBe('Add 1 × Spring water 5L');
    isFocused(page, add);
    await press(page, add);
    await settle(page.window);
    expect(picker.classList.contains('show')).toBe(false);
    expect(row(page, 2).querySelector('b').textContent).toBe('Spring water 5L');
    isFocused(page, row(page, 2).querySelector('.subs-stepper__input'));
    // Two products now: either can be removed.
    expect(page.$$('#subs-edit-rows [data-action="remove-item"][aria-disabled]')).toHaveLength(0);
    await afterPreview(page);
    expect(row(page, 2).querySelector('.subs-item__total').textContent).toBe(money('5 000'));

    await press(page, saveButton(page));
    sentExactlyTheContract(page);
    expect(page.$1('#subs-edit-items')).toBeNull();
    expect(page.$1('#subs-sec-items-status').textContent).toBe(`Saved. Each delivery is now ${money('50 000')}.`);
    expect(page.$1('#subs-status').textContent).toBe(`Saved. Each delivery is now ${money('50 000')}.`);
    expect(page.$$('#subs-sec-items .subs-item b').map((b) => b.textContent)).toEqual(['Pure Water 19L', 'Spring water 5L']);
    expect(page.document.body.classList.contains('subs-editing')).toBe(false);
    isFocused(page, changeButton(page, 'items'));
    expect(form.isConnected).toBe(false);
  });

  it('removes the product that is no longer available, previewed, then saved', async () => {
    const page = await boot('write_items_remove_discontinued');
    await openSection(page, 'items');
    expect(row(page, 2).className).toContain('is-unavailable');
    await press(page, row(page, 2).querySelector('[data-action="remove-item"]'));
    expect(row(page, 2)).toBeNull();
    isFocused(page, page.$1('[data-action="open-picker"]'));
    await afterPreview(page);
    await press(page, saveButton(page));
    sentExactlyTheContract(page);
  });

  it('− stops at the product\'s minimum, a typed quantity is previewed, and only digits are kept', async () => {
    const page = await boot('needs_payment', {
      answers: { 'POST /api/v1/subscriptions/1/preview': { status: 200, body: loadCase('write_items_save').exchanges[3].response } },
    });
    await openSection(page, 'items');
    const dec = () => row(page, 1).querySelector('[data-action="dec"]');
    await press(page, dec());
    expect(row(page, 1).querySelector('.subs-stepper__input').value).toBe('1');
    expect(dec().getAttribute('aria-disabled')).toBe('true');
    await press(page, dec());
    expect(row(page, 1).querySelector('.subs-stepper__input').value).toBe('1');
    const input = row(page, 1).querySelector('.subs-stepper__input');
    input.focus();
    await type(page, input, '5a');
    expect(input.value).toBe('5');
    await afterPreview(page);
    expect(writes(page).map((w) => w.body)).toEqual([{ items: [{ product_id: 1, quantity: 5 }] }]);
  });

  it('a refused preview is shown and keeps Save blocked; a failed save keeps the draft and never says Saved', async () => {
    const page = await boot('needs_payment', {
      answers: {
        'POST /api/v1/subscriptions/1/preview': { status: 400, body: { success: false, message: 'Order at least 2.' } },
      },
    });
    await openSection(page, 'items');
    await press(page, row(page, 1).querySelector('[data-action="dec"]'));
    await afterPreview(page);
    expect(page.$1('#subs-savebar .subs-error-line').textContent).toBe('Order at least 2.');
    expect(saveButton(page).getAttribute('aria-disabled')).toBe('true');
    await press(page, saveButton(page));
    expect(writes(page).map((w) => w.key)).toEqual(['POST /api/v1/subscriptions/1/preview']);

    const failing = await boot('needs_payment', {
      answers: {
        'POST /api/v1/subscriptions/1/preview': { status: 200, body: loadCase('write_items_save').exchanges[3].response },
        'PUT /api/v1/subscriptions/1/items': { status: 500, body: { success: false } },
      },
    });
    await openSection(failing, 'items');
    await press(failing, row(failing, 1).querySelector('[data-action="inc"]'));
    await afterPreview(failing);
    await press(failing, saveButton(failing));
    expect(failing.$1('#subs-savebar .subs-error-line').textContent)
      .toBe("We couldn't save just now. Your changes are still here, so try again.");
    expect(row(failing, 1).querySelector('.subs-stepper__input').value).toBe('3');
    expect(saveButton(failing).getAttribute('aria-disabled')).toBeNull();
    expect(failing.$1('#subs').textContent).not.toContain('Saved.');
  });

  it('Discard restores the summary, sends nothing and gives focus back to Change', async () => {
    const page = await boot('needs_payment', {
      answers: { 'POST /api/v1/subscriptions/1/preview': { status: 200, body: loadCase('write_items_save').exchanges[3].response } },
    });
    await openSection(page, 'items');
    await press(page, row(page, 1).querySelector('[data-action="inc"]'));
    await press(page, page.$1('#subs-savebar [data-action="discard"]'));
    expect(page.$1('#subs-edit-items')).toBeNull();
    expect(page.$1('#subs-sec-items .subs-item small').textContent).toBe(`2 × ${money('15 000')}`);
    expect(changeButton(page, 'items').getAttribute('aria-expanded')).toBe('false');
    isFocused(page, changeButton(page, 'items'));
    expect(page.document.body.classList.contains('subs-editing')).toBe(false);
    await afterPreview(page);
    expect(writes(page)).toEqual([]);
  });
});

describe('one section at a time', () => {
  const dirtyItems = async (name = 'needs_payment', extra = {}) => {
    const page = await boot(name, {
      answers: { 'POST /api/v1/subscriptions/1/preview': { status: 200, body: loadCase('write_items_save').exchanges[3].response }, ...extra },
    });
    await openSection(page, 'items');
    await press(page, row(page, 1).querySelector('[data-action="inc"]'));
    return page;
  };

  it('another Change while one is dirty points to its save bar instead', async () => {
    const page = await dirtyItems();
    await press(page, changeButton(page, 'schedule'));
    expect(page.$1('#subs-edit-schedule')).toBeNull();
    expect(page.$1('#subs-edit-items')).not.toBeNull();
    expect(page.$1('.subs-savebar__guard').textContent).toBe('Save or discard these changes first.');
    isFocused(page, savebar(page));
  });

  it('a strip link while dirty stays on this subscription', async () => {
    const page = await dirtyItems('mixed');
    await press(page, page.$1('#subs-strip a[data-sub-id="2"]'));
    expect(page.$1('#sub-h').textContent).toBe('Home water');
    expect(page.window.location.search).toBe('');
    isFocused(page, savebar(page));
  });

  it('a card action while dirty points to the save bar too', async () => {
    const page = await dirtyItems();
    await press(page, page.$1('[data-action="skip"]'));
    expect(confirmPanel(page).hidden).toBe(true);
    isFocused(page, savebar(page));
  });

  it('a clean form simply gives way to the next one', async () => {
    const slots = loadCase('write_schedule_saturday').exchanges[3];
    const page = await boot('needs_payment', { answers: { [`GET ${slots.path}`]: { status: 200, body: slots.response } } });
    await openSection(page, 'items');
    await openSection(page, 'schedule');
    expect(page.$1('#subs-edit-items')).toBeNull();
    expect(page.$1('#subs-edit-schedule')).not.toBeNull();
    expect(changeButton(page, 'items').getAttribute('aria-expanded')).toBe('false');
  });

  it('asks before leaving the page only while something is unsaved', async () => {
    const clean = await boot('needs_payment');
    const before = new clean.window.Event('beforeunload', { cancelable: true });
    clean.window.dispatchEvent(before);
    expect(before.defaultPrevented).toBe(false);

    const page = await dirtyItems();
    const leaving = new page.window.Event('beforeunload', { cancelable: true });
    page.window.dispatchEvent(leaving);
    expect(leaving.defaultPrevented).toBe(true);
  });

  it('the save bar is sticky inside its section, never fixed, and clears the phone\'s safe area', () => {
    const rule = CSS.match(/\.subs-savebar \{[^}]*\}/)[0];
    expect(rule).toMatch(/position: sticky;/);
    expect(rule).toMatch(/bottom: 0;/);
    expect(rule).toMatch(/env\(safe-area-inset-bottom\)/);
    expect(CSS).not.toMatch(/position:\s*fixed/);
    expect(CSS).toMatch(/body\.subs-editing \.social-float-links,\s*body\.subs-editing \.scroll-top \{ display: none !important; \}/);
  });

  // jsdom lays nothing out, so this pins the two conditions sticky needs; the fix-round report scrolls the
  // real page in headless Chrome and measures the bar at the bottom of the viewport.
  it('the save bar can actually stick: no box of its own size around it, and no theme scrollport above it', async () => {
    const page = await dirtyItems();
    const bar = page.$1('#subs-savebar');
    const form = bar.closest('.subs-edit');
    expect(form).not.toBeNull();
    // Every element between the bar and its open form generates no box, so the form is its containing block.
    for (let el = bar.parentElement; el !== form; el = el.parentElement) {
      expect(page.window.getComputedStyle(el).display).toBe('contents');
    }
    // The theme's .boxed_wrapper{overflow:hidden} would be the bar's scrollport; clip clips without being one.
    expect(CSS).toMatch(/\.boxed_wrapper:has\(\.subs\) \{ overflow: clip !important; \}/);
  });
});

// ------------------------------------------------------------------ Schedule

const slotTiles = (page) => page.$$('#subs-edit-slots .subs-tile').map((t) => [seen(t), t.querySelector('input').checked]);

describe('Schedule, read then edit', () => {
  it('frequency, day and slot tiles; the slots of the chosen day; a backend preview; then the save', async () => {
    const page = await boot('write_schedule_saturday');
    const form = await openSection(page, 'schedule');
    expect(page.$$('legend', form).map((l) => l.textContent)).toEqual(['How often', 'Which day', 'Delivery time']);
    expect(page.$$('#subs-edit-freq .subs-tile', form).map((t) => [seen(t), t.querySelector('input').checked])).toEqual([
      ['Daily about 30 a month', false], ['Weekly about 4 a month', true],
      ['Every 2 weeks about 2 a month', false], ['Monthly once a month', false],
    ]);
    isFocused(page, page.$1('input[name="subs-frequency"][value="weekly"]'));
    const days = page.$$('.subs-day', form);
    expect(days.map((d) => seen(d))).toEqual(['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']);
    expect(days.map((d) => d.querySelector('.sr-only').textContent)).toEqual(
      ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'],
    );
    expect(page.$1('input[name="subs-weekday"]:checked').value).toBe('3');
    expect(slotTiles(page)).toEqual([['Any time', false], ['10:00–12:00', true]]);
    expect(savebar(page)).toBeNull();

    await pick(page, page.$1('input[name="subs-weekday"][value="5"]'));
    isFocused(page, page.$1('input[name="subs-weekday"][value="5"]'));
    expect(slotTiles(page)).toEqual([['Any time', false], ['10:00–12:00', true]]);  // the usual time is offered on Saturday
    await afterPreview(page);
    const preview = page.$1('#subs-edit-preview .subs-preview');
    expect(page.$$('.subs-wl svg', preview).map((svg) => svg.getAttribute('class'))).toEqual([
      'subs-bottle subs-bottle--placed', 'subs-bottle subs-bottle--draft',
      'subs-bottle subs-bottle--draft', 'subs-bottle subs-bottle--draft',
    ]);
    expect(page.$1('.subs-preview__sentence', preview).textContent).toBe(
      'Your first delivery with these changes: Saturday, 7 November, 10:00–12:00. Your order for 29 October stays as it is. ' +
      `About 4 deliveries a month, about ${money('130 000')}.`,
    );
    await press(page, saveButton(page));
    sentExactlyTheContract(page);
    expect(page.$1('#subs-sec-schedule-status').textContent)
      .toBe('Saved. Your next delivery is Thursday, 29 October, 10:00–12:00.');
    expect(sectionText(page, 'schedule')).toContain('Every Saturday, 10:00–12:00');
  });

  it('a time not offered on the new day is cleared with a message, and Save waits for another', async () => {
    const page = await boot('write_schedule_slot_missing');
    await openSection(page, 'schedule');
    expect(slotTiles(page)).toEqual([['Any time', false], ['09:00–18:00', true], ['10:00–12:00', false]]);
    await pick(page, page.$1('input[name="subs-weekday"][value="5"]'));
    expect(slotTiles(page)).toEqual([['Any time', false], ['10:00–12:00', false]]);
    expect(page.$1('#subs-edit-slots .subs-edit__msg').textContent)
      .toBe("Your usual time isn't available on Saturday. Choose another time or Any time.");
    expect(saveButton(page).getAttribute('aria-disabled')).toBe('true');
    await afterPreview(page);
    expect(writes(page)).toEqual([]);  // nothing to check until a time is picked
    await pick(page, page.$1('input[name="subs-slot"][value="1"]'));
    expect(page.$1('#subs-edit-slots .subs-edit__msg')).toBeNull();
    await afterPreview(page);
    await press(page, saveButton(page));
    sentExactlyTheContract(page);
  });

  it('monthly: the day of the month is a plain select; the backend refuses until one is chosen', async () => {
    const page = await boot('write_schedule_monthly');
    await openSection(page, 'schedule');
    await pick(page, page.$1('input[name="subs-frequency"][value="monthly"]'));
    expect(page.$$('.subs-day')).toHaveLength(0);
    const select = page.$1('#subs-dom');
    expect([select.className, select.value]).toEqual(['form-control ignore subs-select', '']);
    expect(page.$1('label[for="subs-dom"]').textContent).toBe('Day of the month');
    expect(select.options).toHaveLength(32);
    expect(page.$1('#subs-dom-note').textContent)
      .toBe("On the 29th to the 31st, in shorter months we'll deliver on the last day of the month.");
    await afterPreview(page);
    expect(page.$1('#subs-savebar .subs-error-line').textContent).toBe('Choose the day of the month for your deliveries.');
    expect(saveButton(page).getAttribute('aria-disabled')).toBe('true');
    await choose(page, select, '15');
    await afterPreview(page);
    expect(page.$1('#subs-savebar .subs-error-line')).toBeNull();
    await press(page, saveButton(page));
    sentExactlyTheContract(page);
  });

  it('a refused save stays inline with the backend\'s reason, and nothing is reported saved', async () => {
    const page = await boot('write_schedule_save_refused');
    await openSection(page, 'schedule');
    await pick(page, page.$1('input[name="subs-weekday"][value="5"]'));
    await afterPreview(page);
    await press(page, saveButton(page));
    sentExactlyTheContract(page);
    expect(page.$1('#subs-edit-schedule')).not.toBeNull();
    expect(page.$1('#subs-savebar .subs-error-line').textContent).toBe('Invalid or inactive delivery time slot');
    expect(page.$1('#subs-sec-schedule-status').textContent).toBe('');
  });
});

describe('every schedule the editor can build (frequency × day × slot)', () => {
  const { bodies } = loadCase('schedule_bodies');
  const everyDaySlot = loadCase('write_schedule_saturday').exchanges[3].response;
  const slotAnswers = {};
  ['any', 0, 1, 2, 3, 4, 5, 6].forEach((weekday) => {
    ['daily', 'weekly', 'biweekly', 'monthly'].forEach((frequency) => {
      slotAnswers[`GET /api/v1/delivery/time-slots?weekday=${weekday}&frequency=${frequency}`] = { status: 200, body: everyDaySlot };
    });
  });
  const okPreview = loadCase('write_schedule_saturday').exchanges[5].response;
  const okSave = loadCase('write_schedule_saturday').exchanges[6].response;

  it.each(bodies.map((entry) => [entry.name, entry.body]))('%s', async (name, body) => {
    const page = await boot('no_order_yet', {
      answers: {
        ...slotAnswers,
        'POST /api/v1/subscriptions/1/preview': { status: 200, body: okPreview },
        'PUT /api/v1/subscriptions/1': { status: 200, body: okSave },
      },
    });
    await openSection(page, 'schedule');
    if (body.frequency !== 'weekly') await pick(page, page.$1(`input[name="subs-frequency"][value="${body.frequency}"]`));
    if ('delivery_day_of_week' in body) await pick(page, page.$1(`input[name="subs-weekday"][value="${body.delivery_day_of_week}"]`));
    if ('delivery_day_of_month' in body) await choose(page, page.$1('#subs-dom'), String(body.delivery_day_of_month));
    // The Any time tile is the one that sends an explicit null.
    if (body.delivery_time_slot_id === null) await pick(page, page.$1('input[name="subs-slot"][value="any"]'));
    await afterPreview(page);
    // The slots asked for are the chosen weekday's (or any day's), for the chosen frequency.
    const weekday = 'delivery_day_of_week' in body ? body.delivery_day_of_week : 'any';
    expect(keys(page.requests)).toContain(`GET /api/v1/delivery/time-slots?weekday=${weekday}&frequency=${body.frequency}`);
    if (name === 'weekly-3') {
      // The saved schedule itself: nothing to preview or save.
      expect(writes(page)).toEqual([]);
      expect(savebar(page)).toBeNull();
      return;
    }
    const previews = writes(page).filter((w) => w.key.endsWith('/preview'));
    expect(previews[previews.length - 1].body).toEqual(body);
    await press(page, saveButton(page));
    const saved = writes(page).filter((w) => w.key === 'PUT /api/v1/subscriptions/1');
    // The save carries the state its preview was drawn from (spec 2026-10-27 §16 M2).
    expect(saved.map((w) => w.body)).toEqual([{ ...body, expected_state: okPreview.data.preview.expected_state }]);
  });
});

const SLOTS_THURSDAY = 'GET /api/v1/delivery/time-slots?weekday=3&frequency=weekly';
const slotsAnswer = (overrides) => ({
  [SLOTS_THURSDAY]: {
    status: 200,
    body: { ...loadCase('write_schedule_saturday').exchanges[3].response, ...overrides },
  },
});

describe('Schedule: Any time', () => {
  it('is a tile next to the slots; choosing it previews and saves an explicit null', async () => {
    const page = await boot('write_schedule_to_any_time');
    await openSection(page, 'schedule');
    expect(slotTiles(page)).toEqual([['Any time', false], ['10:00–12:00', true]]);
    await pick(page, page.$1('input[name="subs-slot"][value="any"]'));
    isFocused(page, page.$1('input[name="subs-slot"][value="any"]'));
    expect(slotTiles(page)).toEqual([['Any time', true], ['10:00–12:00', false]]);
    await afterPreview(page);
    expect(writes(page).map((w) => w.body)).toEqual([
      { frequency: 'weekly', delivery_day_of_week: 3, delivery_time_slot_id: null },
    ]);
    expect(saveButton(page).getAttribute('aria-disabled')).toBeNull();
    await press(page, saveButton(page));
    sentExactlyTheContract(page);
    expect(page.$1('#subs-sec-schedule .subs-val').textContent).toBe('Every Thursday, any time');
    expect(page.$1('#subs-sec-schedule-status').textContent).toContain('Saved.');
  });

  it('a subscription on Any time opens on it and moves to Saturday without picking a time', async () => {
    const page = await boot('write_schedule_any_time_new_day');
    await openSection(page, 'schedule');
    expect(slotTiles(page)).toEqual([['Any time', true], ['10:00–12:00', false]]);
    await pick(page, page.$1('input[name="subs-weekday"][value="5"]'));
    expect(slotTiles(page)).toEqual([['Any time', true], ['10:00–12:00', false]]);
    expect(page.$1('#subs-edit-slots .subs-edit__msg')).toBeNull();
    await afterPreview(page);
    expect(saveButton(page).getAttribute('aria-disabled')).toBeNull();
    await press(page, saveButton(page));
    sentExactlyTheContract(page);
    expect(page.$1('#subs-sec-schedule .subs-val').textContent).toBe('Every Saturday, any time');
  });

  it('a dropped time asks for another time or Any time; Save waits; Any time then saves', async () => {
    const page = await boot('write_schedule_slot_missing_any_time');
    await openSection(page, 'schedule');
    await pick(page, page.$1('input[name="subs-weekday"][value="5"]'));
    expect(slotTiles(page)).toEqual([['Any time', false], ['10:00–12:00', false]]);
    expect(page.$1('#subs-edit-slots .subs-edit__msg').textContent)
      .toBe("Your usual time isn't available on Saturday. Choose another time or Any time.");
    expect(saveButton(page).getAttribute('aria-disabled')).toBe('true');
    await afterPreview(page);
    expect(writes(page)).toEqual([]);  // nothing is checked, and nothing is chosen for the customer
    await pick(page, page.$1('input[name="subs-slot"][value="any"]'));
    expect(page.$1('#subs-edit-slots .subs-edit__msg')).toBeNull();
    await afterPreview(page);
    expect(saveButton(page).getAttribute('aria-disabled')).toBeNull();
    await press(page, saveButton(page));
    sentExactlyTheContract(page);
  });

  it('with no time offered, Any time is the only tile, already chosen', async () => {
    const page = await boot('any_time', { answers: slotsAnswer({ time_slots: [] }) });
    await openSection(page, 'schedule');
    expect(slotTiles(page)).toEqual([['Any time', true]]);
    expect(page.$1('#subs-edit-slots .subs-edit__msg')).toBeNull();
    expect(savebar(page)).toBeNull();
  });

  it('a chosen time with none offered is dropped, never replaced by Any time on the customer\'s behalf', async () => {
    const page = await boot('needs_payment', { answers: slotsAnswer({ time_slots: [] }) });
    await openSection(page, 'schedule');
    expect(slotTiles(page)).toEqual([['Any time', false]]);
    expect(page.$1('#subs-edit-slots .subs-edit__msg').textContent)
      .toBe("Your usual time isn't available on Thursday. Choose another time or Any time.");
    expect(saveButton(page).getAttribute('aria-disabled')).toBe('true');
    await afterPreview(page);
    expect(writes(page)).toEqual([]);
  });

  it('the tile and the card name Any time in the same words', async () => {
    const page = await boot('any_time', { answers: slotsAnswer({}) });
    const { time_label: label } = page.listed[0].upcoming_delivery;
    expect(label).toBe(page.words['slot.any']);
    expect(page.$1('.subs-hero__win', hero(page)).textContent).toContain(label);
    await openSection(page, 'schedule');
    expect(seen(page.$1('input[name="subs-slot"][value="any"]').closest('.subs-tile'))).toBe(label);
  });

  it('the day tiles offer only delivery days', async () => {
    const page = await boot('needs_payment', { answers: slotsAnswer({ delivery_weekdays: [0, 1, 2, 3, 4] }) });
    const form = await openSection(page, 'schedule');
    expect(page.$$('.subs-day', form).map((d) => d.querySelector('.sr-only').textContent))
      .toEqual(['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']);
    expect(page.$1('input[name="subs-weekday"]:checked').value).toBe('3');
  });
});

// ------------------------------------------------------------------ Delivery address

const addressTiles = (page) => page.$$('#subs-edit-addresses .subs-tile')
  .map((t) => [seen(t), t.querySelector('input').checked]);
const consequenceLines = (page) => page.$$('#subs-edit-address-preview .subs-preview__list li').map((li) => li.textContent);
const previewedConsequences = (page) => page.fixture.exchanges
  .filter((e) => e.path.endsWith('/preview') && e.status === 200)
  .map((e) => e.response.data.preview.consequences);

describe('Delivery address', () => {
  it('reads delivery_address_label; Change lists the saved addresses with the current one chosen', async () => {
    const page = await boot('write_address_move');
    expect(page.$1('#subs-sec-address h3').textContent).toBe('Delivery address');
    expect(sectionText(page, 'address')).toBe(page.listed[0].delivery_address_label);
    expect(changeButton(page, 'address').querySelector('.sr-only').textContent).toBe('Change the delivery address');
    expect(changeButton(page, 'address').getAttribute('aria-controls')).toBe('subs-edit-address');

    const form = await openSection(page, 'address');
    expect(page.$1('legend', form).textContent).toBe('Where we deliver');
    expect(addressTiles(page)).toEqual([['1 Test St, Tashkent', true], ['Office 2 Work St, Tashkent', false]]);
    const add = page.$1('#subs-edit-addresses a.subs-add');
    expect([add.textContent, add.getAttribute('href')]).toEqual(['Add a new address', page.shell.island.addresses_url]);
    expect(page.$1('input[name="subs-address"]:checked').value).toBe('1');
    isFocused(page, page.$1('input[name="subs-address"][value="1"]'));
    expect(savebar(page)).toBeNull();
  });

  it('moving previews both consequences, then saves with the state it previewed', async () => {
    const page = await boot('write_address_move');
    await openSection(page, 'address');
    await pick(page, page.$1('input[name="subs-address"][value="2"]'));
    expect(saveButton(page).getAttribute('aria-disabled')).toBe('true');
    await afterPreview(page);
    expect(consequenceLines(page)).toEqual(previewedConsequences(page)[0]);
    expect(consequenceLines(page)).toHaveLength(2);
    expect(page.$1('#subs-edit-address-methods').children).toHaveLength(0);
    expect(saveButton(page).getAttribute('aria-disabled')).toBeNull();
    await press(page, saveButton(page));
    sentExactlyTheContract(page);
    expect(page.$1('#subs-sec-address-status').textContent).toBe("Saved. We'll deliver to Office.");
    expect(page.$1('#subs-sec-address .subs-val').textContent).toBe('Office');
  });

  it('when the driver already has the next order, the preview says so and the save still moves the subscription', async () => {
    const page = await boot('write_address_driver_has_it');
    await openSection(page, 'address');
    await pick(page, page.$1('input[name="subs-address"][value="2"]'));
    await afterPreview(page);
    const lines = consequenceLines(page);
    expect(lines).toEqual(previewedConsequences(page)[0]);
    expect(lines).toHaveLength(2);
    expect(lines[1]).toMatch(/still goes to 1 Test St, Tashkent\.$/);
    await press(page, saveButton(page));
    sentExactlyTheContract(page);
    expect(page.$1('#subs-sec-address .subs-val').textContent).toBe('Office');
  });

  it("a way to pay the new address does not offer is asked for from the preview's payment_methods before Save", async () => {
    const page = await boot('write_address_repick_method');
    await openSection(page, 'address');
    await pick(page, page.$1('input[name="subs-address"][value="2"]'));
    await afterPreview(page);
    expect(page.$1('#subs-edit-address-methods legend').textContent)
      .toBe("Your usual way to pay isn't offered at this address. Choose how you'll pay there.");
    expect(page.$$('#subs-edit-address-methods .subs-tile').map((t) => [seen(t), t.querySelector('input').checked]))
      .toEqual([["Click Pay online for each order. We'll remind you before the deadline.", false]]);
    expect(consequenceLines(page)).toEqual(previewedConsequences(page)[0]);
    expect(saveButton(page).getAttribute('aria-disabled')).toBe('true');  // a method is still to be chosen
    await pick(page, page.$1('input[name="subs-address-method"][value="click"]'));
    await afterPreview(page);
    // The menu stays up once asked for, now with Click chosen; the second preview no longer asks.
    expect(page.$$('#subs-edit-address-methods .subs-tile').map((t) => t.querySelector('input').checked)).toEqual([true]);
    expect(consequenceLines(page)).toEqual(previewedConsequences(page)[1]);
    expect(saveButton(page).getAttribute('aria-disabled')).toBeNull();
    await press(page, saveButton(page));
    sentExactlyTheContract(page);
    expect(writes(page).map((w) => w.body.payment_method)).toEqual([undefined, 'click', 'click']);
  });

  it('with one saved address: only the current one and the add-address link; Save never enabled', async () => {
    const [saved] = loadCase('write_address_move').exchanges.filter((e) => e.path.endsWith('/auth/addresses'));
    const one = { status: 200, body: { ...saved.response, data: { addresses: [saved.response.data.addresses[0]] } } };
    const page = await boot('needs_payment', { answers: { 'GET /api/v1/auth/addresses': one } });
    await openSection(page, 'address');
    expect(addressTiles(page)).toEqual([['1 Test St, Tashkent', true]]);
    expect(page.$1('#subs-edit-addresses a.subs-add').textContent).toBe('Add a new address');
    await pick(page, page.$1('input[name="subs-address"][value="1"]'));
    await afterPreview(page);
    expect(savebar(page)).toBeNull();
    expect(writes(page)).toEqual([]);
  });

  it('says so when the saved addresses cannot be read', async () => {
    const page = await boot('needs_payment', { answers: { 'GET /api/v1/auth/addresses': { status: 500, body: { success: false } } } });
    await openSection(page, 'address');
    expect(page.$1('#subs-edit-addresses .subs-error-line').textContent).toBe("We couldn't load your addresses. Please try again.");
  });

  it('a cancelled subscription has no address Change', async () => {
    const page = await boot('cancelled');
    expect(page.$1('#subs-sec-address')).not.toBeNull();
    expect(sectionText(page, 'address')).toBe(page.listed[0].delivery_address_label);
    expect(changeButton(page, 'address')).toBeNull();
  });
});


// ------------------------------------------------------------------ Payment

describe('Payment, read then edit', () => {
  it('offers only the methods the backend offers, each with its consequence, and saves through its own route', async () => {
    const page = await boot('write_payment_cash');
    const form = await openSection(page, 'payment');
    expect(page.$1('legend', form).textContent).toBe('How you pay');
    expect(page.$$('.subs-tile', form).map((t) => [seen(t), t.querySelector('input').checked])).toEqual([
      ['Cash Pay the driver at the door.', false],
      ["Click Pay online for each order. We'll remind you before the deadline.", true],
    ]);
    isFocused(page, page.$1('input[name="subs-method"][value="click"]'));
    // Before the save: the order already placed for 29 October keeps the method it was placed with (the
    // backend's kept_order_note; UX brief "SCHEDULE AND PAYMENT SAVE").
    expect(page.listed[0].kept_order_note).toBe('Your order for 29 October stays as it is.');
    expect(page.$1('#subs-edit-note', form).textContent).toBe('Your order for 29 October stays as it is.');
    await pick(page, page.$1('input[name="subs-method"][value="cash"]'));
    isFocused(page, page.$1('input[name="subs-method"][value="cash"]'));
    expect(page.$1('#subs-edit-note', form).textContent).toBe('Your order for 29 October stays as it is.');
    expect(saveButton(page).getAttribute('aria-disabled')).toBeNull();  // no preview for a payment method
    await press(page, saveButton(page));
    sentExactlyTheContract(page);
    expect(page.$1('#subs-sec-payment-status').textContent).toBe(
      "Saved. You'll pay by Cash. Your order for 29 October stays as it is.",
    );
    expect(page.$1('#subs-sec-payment .subs-val').firstChild.textContent).toBe('Cash');
    // The order itself still waits for its Click payment, and the card still offers Pay for it.
    expect(page.$$('.subs-btn--primary', hero(page)).map((b) => b.textContent)).toEqual([`Pay ${money('30 000')}`]);
  });

  it('says nothing about a kept order when none is placed yet', async () => {
    const page = await boot('no_order_yet');
    expect(page.listed[0].kept_order_note).toBeNull();
    const form = await openSection(page, 'payment');
    expect(page.$1('#subs-edit-note', form)).toBeNull();
  });

  const menu = (list) => {
    const answer = loadCase('needs_payment').requests[methods(1)];
    const body = JSON.parse(JSON.stringify(answer.body));
    body.data.available_methods = body.data.available_methods.filter((m) => list.includes(m.method));
    return { [methods(1)]: { status: 200, body } };
  };

  it('hides Change when the current method is the only one offered', async () => {
    const page = await boot('needs_payment', { answers: menu(['click']) });
    expect(changeButton(page, 'payment')).toBeNull();
    expect(changeButton(page, 'items')).not.toBeNull();
  });

  // Spec 2026-10-27 §7 (B8): the backend publishes the warning, gated on the same edit guard the save uses; the
  // page shows it as it is and stresses Change, and never works out from the menu that the method is gone.
  it('the method warning comes from payment_method_warning', async () => {
    const page = await boot('method_not_offered', { search: '?sub=1' });
    const cash = page.listed.find((sub) => sub.id === 1);
    expect(cash.payment_method).toBe('cash');
    const chip = page.$1('#subs-sec-payment .subs-val__chip .subs-chip');
    expect([chip.textContent, chip.className]).toEqual([cash.payment_method_warning, 'subs-chip subs-chip--warning']);
    expect(cash.payment_method_warning).toBe('Not available right now. Choose another method.');
    expect(changeButton(page, 'payment').className).toBe('subs-change subs-change--emphasis');

    const other = await boot('method_not_offered', { search: '?sub=2' });
    expect(other.listed.find((sub) => sub.id === 2).payment_method_warning).toBeNull();
    expect(other.$1('#subs-sec-payment .subs-val__chip')).toBeNull();
    // Its own method is the only one this place offers, so there is nothing to change to.
    expect(changeButton(other, 'payment')).toBeNull();
  });
});

// =================================================================== the published answers (spec 2026-10-27 §5.3, §8, M2, M3)

const STALE = 'This changed. Refresh to see the latest.';
// The list answers in a write case, oldest first: the screen the customer acted on, then the latest.
const listAnswers = (page) => page.fixture.exchanges
  .filter((e) => `${e.method} ${e.path}` === LIST).map((e) => e.response.data.items[0]);
const historyAnswers = (page) => page.fixture.exchanges
  .filter((e) => `${e.method} ${e.path}` === history(1)).map((e) => e.response.data.items);
const writeAnswer = (page) => page.fixture.exchanges.find((e) => e.method !== 'GET').response;

describe("the confirms read the backend's sentences", () => {
  it('every card button and the footer Cancel are the published labels', async () => {
    const page = await boot('needs_payment');
    const actions = page.listed[0].allowed_actions;
    expect(heroButtons(page)).toEqual([actions.pay.label, actions.skip.label, actions.pause.label]);
    expect(page.$1('.subs-foot [data-action="cancel"]').textContent).toBe(actions.cancel.label);
    const paused = await boot('paused_on_the_way');
    const kept = paused.listed[0].allowed_actions;
    expect(heroButtons(paused)).toEqual([kept.pay.label, kept.resume.label]);
  });

  it('Skip shows the published sentence, both buttons and the published reasons', async () => {
    const page = await boot('write_skip_unpaid');
    const plan = page.listed[0].allowed_actions.skip;
    await press(page, page.$1('[data-action="skip"]'));
    expect(page.$1('#subs-confirm .subs-confirm__text').textContent).toBe(plan.confirm_text);
    expect(confirmButtons(page)).toEqual([plan.confirm_label, plan.keep_label]);
    expect([plan.confirm_label, plan.keep_label]).toEqual(['Skip 29 October', 'Keep this delivery']);
    expect(reasonChipLabels(page)).toEqual(plan.reasons.map((r) => r.label));
    expect(page.$$('#subs-confirm [data-action="skip-reason"]').map((c) => c.dataset.reason))
      .toEqual(plan.reasons.map((r) => r.code));
  });

  it('Pause asks only when needs_confirm, and lists confirm_text, kept_lines, cancel_lines', async () => {
    const asking = await boot('write_pause_cancels_order');
    const plan = asking.listed[0].allowed_actions.pause;
    expect(plan.needs_confirm).toBe(true);
    await press(asking, asking.$1('[data-action="pause"]'));
    expect(asking.$$('#subs-confirm .subs-confirm__text').map((p) => p.textContent))
      .toEqual([plan.confirm_text, ...plan.kept_lines, ...plan.cancel_lines]);
    expect(confirmButtons(asking)).toEqual([plan.confirm_label, plan.keep_label]);
    expect(writes(asking)).toEqual([]);

    const now = await boot('write_pause_now');
    expect(now.listed[0].allowed_actions.pause.needs_confirm).toBe(false);
    await press(now, now.$1('[data-action="pause"]'));
    sentExactlyTheContract(now);
    expect(confirmPanel(now).hidden).toBe(true);
  });

  it.each(['write_cancel_with_reason', 'write_cancel_paid_kept'])(
    '%s: the Cancel modal lists confirm_text, kept_lines, cancel_lines, coins_line',
    async (name) => {
      const page = await boot(name);
      const plan = page.listed[0].allowed_actions.cancel;
      const modal = await openCancel(page);
      expect(page.$$('.subs-modal__list li', modal).map((li) => li.textContent))
        .toEqual([plan.confirm_text, ...plan.kept_lines, ...plan.cancel_lines, plan.coins_line]);
      expect(page.$$('.modal-footer button', modal).map((b) => b.textContent)).toEqual([plan.keep_label, plan.confirm_label]);
    },
  );
});

// What a customer does on each changed screen, and how the latest answer then reads.
const STALE_CARD = {
  write_pay_stale: {
    act: (page) => press(page, primaries(page)[0]),
    latest: (page) => {
      const [, latest] = listAnswers(page);
      expect(primaries(page).map((b) => [b.textContent, b.dataset.amount]))
        .toEqual([[latest.allowed_actions.pay.label, latest.allowed_actions.pay.expected_amount]]);
      expect(latest.allowed_actions.pay.label).toBe(`Pay ${money('32 000')}`);
    },
  },
  write_pause_stale: {
    act: async (page) => {
      await press(page, page.$1('[data-action="pause"]'));
      await press(page, page.$1('#subs-confirm [data-action="pause-confirm"]'));
    },
    latest: (page) => {
      expect(page.$1('.subs-hero__order .subs-chip').textContent).toBe('Paid');
      expect(primaries(page)).toHaveLength(0);
    },
  },
  write_cancel_stale: {
    act: async (page) => {
      const modal = await openCancel(page);
      await press(page, page.$1('[data-action="cancel-confirm"]', modal));
      await settle(page.window);
    },
    latest: (page) => {
      expect(page.$1('#subs-cancel-modal').classList.contains('show')).toBe(false);
      expect(hero(page).className).toBe('subs-hero');
      expect(page.$1('.subs-hero__order .subs-chip').textContent).toBe('Paid');
    },
  },
};

describe('a changed screen', () => {
  it.each(Object.keys(STALE_CARD))('%s: the card says it changed and shows the latest', async (name) => {
    const page = await boot(name);
    await STALE_CARD[name].act(page);
    sentExactlyTheContract(page);  // the write, its 409, then the list, its Orders and its menu again
    expect(writeAnswer(page).message).toBe(STALE);
    expect(heroStatus(page).textContent).toBe(STALE);
    expect(heroStatus(page).getAttribute('role')).toBe('alert');
    expect(page.window.location.hash).toBe('');
    expect(confirmPanel(page).hidden).toBe(true);
    STALE_CARD[name].latest(page);
    expect(page.document.activeElement.closest('.subs-hero__actions')).not.toBeNull();
  });

  it.each([['write_schedule_stale', 'schedule'], ['write_payment_stale', 'payment']])(
    '%s: the section says it changed; the form closed on the latest',
    async (name, section) => {
      const page = await boot(name);
      await openSection(page, section);
      if (section === 'schedule') {
        await pick(page, page.$1('input[name="subs-weekday"][value="5"]'));
        await afterPreview(page);
      } else {
        await pick(page, page.$1('input[name="subs-method"][value="cash"]'));
      }
      await press(page, saveButton(page));
      sentExactlyTheContract(page);
      const status = page.$1(`#subs-sec-${section}-status`);
      expect([status.textContent, status.getAttribute('role')]).toEqual([STALE, 'alert']);
      expect(page.$1(`#subs-edit-${section}`)).toBeNull();
      expect(page.document.body.classList.contains('subs-editing')).toBe(false);
      expect(page.$1('#subs').textContent).not.toContain('Saved.');
      // The latest: the other tab's change, and nothing of this one.
      if (section === 'schedule') {
        expect(page.$1('#subs-sec-schedule .subs-val').textContent).toBe('Every Thursday, 10:00–12:00');
        expect(page.$1('#subs-sec-items .subs-item small').textContent).toBe(`3 × ${money('15 000')}`);
      } else {
        expect(page.$1('#subs-sec-payment .subs-val').firstChild.textContent).toBe('Click');
        expect(page.$1('#subs-sec-schedule .subs-val').textContent).toBe('Every Saturday, 10:00–12:00');
      }
      isFocused(page, changeButton(page, section));
      // A dirty-form guard no longer holds: the page can be left.
      const leaving = new page.window.Event('beforeunload', { cancelable: true });
      page.window.dispatchEvent(leaving);
      expect(leaving.defaultPrevented).toBe(false);
    },
  );
});

// Task 11a fix round 1. Keep, the ×, Esc and the backdrop all close the Cancel modal while its request is still in
// flight. Bootstrap ignores a later hide() on a closed modal and fires no hidden event, so the answer cannot wait
// for one. Nothing left over may run when another modal closes later.
// holdBack lets every request through, but holds back `key`'s answer until the returned function is called.
const holdBack = (page, key) => {
  const answer = page.window.fetch;
  let release;
  const held = new Promise((resolve) => { release = resolve; });
  page.window.fetch = async (url, options = {}) => {
    const response = answer(url, options);
    if (`${(options.method || 'GET').toUpperCase()} ${url}` === key) await held;
    return response;
  };
  return async () => {
    release();
    await settle(page.window);
  };
};
const CANCEL_WRITE = 'POST /api/v1/subscriptions/1/cancel';

describe('the Cancel modal closed before its answer arrives', () => {
  it('a changed answer still says so on the card and shows the latest; a later picker close reloads nothing', async () => {
    const products = loadCase('write_items_save').exchanges.find((e) => e.path === '/api/v1/subscriptions/products');
    const page = await boot('write_cancel_stale', {
      answers: { [`GET ${products.path}`]: { status: 200, body: products.response } },
    });
    const answer = holdBack(page, CANCEL_WRITE);
    const modal = await openCancel(page);
    await press(page, page.$1('[data-action="cancel-confirm"]', modal));
    expect(keys(writes(page))).toEqual([CANCEL_WRITE]);
    await press(page, page.$1('[data-action="cancel-keep"]', modal));
    expect(modal.classList.contains('show')).toBe(false);
    expect(heroStatus(page).textContent).toBe('');

    await answer();
    sentExactlyTheContract(page);  // the write, its 409, then the list, its Orders and its menu again
    expect(heroStatus(page).textContent).toBe(STALE);
    expect(heroStatus(page).getAttribute('role')).toBe('alert');
    STALE_CARD.write_cancel_stale.latest(page);
    expect(page.document.activeElement.closest('.subs-hero__actions')).not.toBeNull();

    // Then the customer edits the items and closes the picker with its ×: the page reads nothing and the form stays.
    const form = await openSection(page, 'items');
    await press(page, page.$1('[data-action="open-picker"]'));
    await settle(page.window);
    const picker = page.$1('#subs-picker-modal');
    expect(picker.classList.contains('show')).toBe(true);
    const asked = page.requests.length;
    await press(page, page.$1('.close', picker));
    await settle(page.window);
    expect(picker.classList.contains('show')).toBe(false);
    expect(page.requests).toHaveLength(asked);
    expect(page.mismatches).toEqual([]);
    expect(form.isConnected).toBe(true);
    isFocused(page, page.$1('[data-action="open-picker"]'));
  });

  it('a cancel that went through still shows the card cancelled', async () => {
    const page = await boot('write_cancel_paid_kept');
    const answer = holdBack(page, CANCEL_WRITE);
    const modal = await openCancel(page);
    await press(page, page.$1('[data-action="cancel-confirm"]', modal));
    await press(page, page.$1('[data-action="cancel-keep"]', modal));
    expect(modal.classList.contains('show')).toBe(false);

    await answer();
    sentExactlyTheContract(page);
    expect(hero(page).className).toBe('subs-hero subs-hero--cancelled');
    expect(heroStatus(page).textContent).toBe('Subscription cancelled.');
    isFocused(page, page.$1('#sub-h'));
  });
});

describe('needs payment includes a delivered order not paid yet', () => {
  it('puts Pay for that order first, with the backend note under it', async () => {
    const page = await boot('delivered_unpaid');
    const { pay } = page.listed[0].allowed_actions;
    const [button] = primaries(page);
    expect(primaries(page)).toHaveLength(1);
    expect(button.dataset).toMatchObject({ action: 'pay', orderId: String(pay.order_id), amount: pay.expected_amount });
    expect(button.textContent).toBe(pay.label);
    expect(page.$$('.subs-hint', hero(page)).map((p) => p.textContent)).toEqual([pay.note]);
    expect(pay.note).toBe('For your order delivered on 29 October.');
  });
});

const rowRemove = (page) => page.$1('#subs-orders [data-action="order-remove"]');
const rowPanel = (page, id = 1) => page.$1(`#subs-orow-confirm-${id}`);
const rowPanelButtons = (page) => page.$$('.subs-confirm__row button', rowPanel(page)).map((b) => b.textContent);
const rowReasons = (page) => page.$$('[data-action="skip-reason"]', rowPanel(page));

describe("Orders rows: Skip or Cancel from the order's own answer", () => {
  it('Skip this delivery, the published consequence and reasons, then that order is skipped', async () => {
    const page = await boot('write_row_skip');
    const [[shown]] = historyAnswers(page);
    const { cancel } = shown.customer_actions;
    expect(cancel.via).toBe('skip');
    const button = rowRemove(page);
    expect([button.textContent, button.className]).toEqual([cancel.label, 'subs-btn subs-btn--secondary subs-orow__act']);
    expect([button.dataset.orderId, button.getAttribute('aria-controls'), button.getAttribute('aria-expanded'),
      button.getAttribute('aria-describedby')]).toEqual(['1', 'subs-orow-confirm-1', 'false', 'subs-orow-1-when']);
    expect(page.$1('#subs-orow-1-when').textContent).toBe(shown.date_label);
    expect([rowPanel(page).hidden, rowPanel(page).getAttribute('tabindex')]).toEqual([true, '-1']);

    await press(page, button);
    expect(button.getAttribute('aria-expanded')).toBe('true');
    expect(rowPanel(page).hidden).toBe(false);
    isFocused(page, rowPanel(page));
    expect(page.$$('.subs-confirm__text', rowPanel(page)).map((p) => p.textContent)).toEqual([cancel.confirm_text]);
    expect(rowReasons(page).map((chip) => chip.textContent)).toEqual(cancel.reasons.map((r) => r.label));
    expect(rowPanelButtons(page)).toEqual([cancel.label, 'Keep it']);
    expect(writes(page)).toEqual([]);  // nothing is sent before the second step

    await press(page, page.$1('[data-reason="away"]', rowPanel(page)));
    expect(page.$1('[data-reason="away"]', rowPanel(page)).getAttribute('aria-pressed')).toBe('true');
    await press(page, page.$1('[data-action="order-remove-confirm"]', rowPanel(page)));
    sentExactlyTheContract(page);
    const status = page.$1('#subs-sec-orders-status');
    expect([status.textContent, status.getAttribute('role')]).toEqual([writeAnswer(page).message, null]);
    expect(writeAnswer(page).message).toBeTruthy();
    expect(page.$1('#subs-status').textContent).toBe(writeAnswer(page).message);
    isFocused(page, page.$1('#subs-orders'));
    // The latest: that delivery is skipped, and the card names the next one.
    expect(page.$1('.subs-hero__date').textContent).toBe('Thursday, 5 November');
    expect(rowRemove(page)).toBeNull();
  });

  it('a leftover on a paused subscription: Cancel order, no reason asked', async () => {
    const page = await boot('write_row_cancel_leftover');
    const [[shown]] = historyAnswers(page);
    const { cancel } = shown.customer_actions;
    expect([cancel.via, cancel.reasons]).toEqual(['cancel', []]);
    expect(hero(page).className).toBe('subs-hero subs-hero--paused');
    await press(page, rowRemove(page));
    expect(page.$$('.subs-confirm__text', rowPanel(page)).map((p) => p.textContent)).toEqual([cancel.confirm_text]);
    expect(rowReasons(page)).toHaveLength(0);
    expect(rowPanelButtons(page)).toEqual([cancel.label, 'Keep it']);
    await press(page, page.$1('[data-action="order-remove-confirm"]', rowPanel(page)));
    sentExactlyTheContract(page);
    expect(page.$1('#subs-sec-orders-status').textContent).toBe(writeAnswer(page).message);
  });

  it('a changed answer is a 409: the message shows in Orders, nothing is removed', async () => {
    const page = await boot('write_row_skip_stale');
    await press(page, rowRemove(page));
    await press(page, page.$1('[data-reason="away"]', rowPanel(page)));
    await press(page, page.$1('[data-action="order-remove-confirm"]', rowPanel(page)));
    sentExactlyTheContract(page);
    const status = page.$1('#subs-sec-orders-status');
    expect([status.textContent, status.getAttribute('role')]).toEqual([STALE, 'alert']);
    isFocused(page, page.$1('#subs-orders'));
    // The latest: the subscription was paused meanwhile, so the order's removal is a plain Cancel now.
    const [, [latest]] = historyAnswers(page);
    expect(latest.customer_actions.cancel.via).toBe('cancel');
    expect(rowRemove(page).textContent).toBe(latest.customer_actions.cancel.label);
    expect(rowPanel(page).hidden).toBe(true);
    expect(hero(page).className).toBe('subs-hero subs-hero--paused');
  });

  it('any other failure keeps the row\'s confirm open with an inline error', async () => {
    const page = await boot('needs_payment', {
      answers: { 'POST /api/v1/orders/1/cancel': { status: 500, body: { success: false } } },
    });
    await press(page, rowRemove(page));
    const confirm = page.$1('[data-action="order-remove-confirm"]', rowPanel(page));
    await press(page, confirm);
    expect(rowPanel(page).hidden).toBe(false);
    const error = page.$1('.subs-error-line', rowPanel(page));
    expect([error.textContent, error.getAttribute('role')]).toEqual(["We couldn't do that just now. Please try again.", 'alert']);
    expect(page.$1('[data-action="order-remove-confirm"]', rowPanel(page)).getAttribute('aria-disabled')).toBeNull();
    expect(page.$1('#subs-sec-orders-status').textContent).toBe('');
  });

  it("one confirm at a time: opening a row's closes the card's", async () => {
    const page = await boot('needs_payment');
    const skip = page.$1('.subs-hero__row [data-action="skip"]');
    await press(page, skip);
    expect(confirmPanel(page).hidden).toBe(false);
    await press(page, rowRemove(page));
    expect([confirmPanel(page).hidden, skip.getAttribute('aria-expanded')]).toEqual([true, 'false']);
    expect([rowPanel(page).hidden, rowRemove(page).getAttribute('aria-expanded')]).toEqual([false, 'true']);
    await press(page, skip);
    expect([rowPanel(page).hidden, rowRemove(page).getAttribute('aria-expanded')]).toEqual([true, 'false']);
    expect(confirmPanel(page).hidden).toBe(false);
    // Keep it closes the row's and gives focus back to its button.
    await press(page, rowRemove(page));
    await press(page, page.$1('[data-action="confirm-keep"]', rowPanel(page)));
    expect(rowPanel(page).hidden).toBe(true);
    isFocused(page, rowRemove(page));
    expect(writes(page)).toEqual([]);
  });

  it("a paid order's row offers neither Skip nor Cancel, and a skipped row has nothing to offer", async () => {
    const page = await boot('paid');
    const [shown] = page.fixture.requests[history(1)].body.data.items;
    expect(shown.customer_actions.cancel.allowed).toBe(false);
    expect(page.$1('#subs-orders [data-action="order-remove"]')).toBeNull();
    expect(page.$1('#subs-orders .subs-orow__confirm')).toBeNull();
    const skipped = await boot('cancelled');
    expect(skipped.fixture.requests[history(1)].body.data.items[0].customer_actions).toBeNull();
    expect(skipped.$$('#subs-orders > li button')).toHaveLength(0);
  });
});

describe('the order modal changed an order', () => {
  it('reads the selected subscription again, so the card no longer shows what the modal removed', async () => {
    const page = await boot('needs_payment');
    expect(keys(page.requests)).toEqual([LIST, history(1), methods(1)]);
    page.document.dispatchEvent(new page.window.CustomEvent('orders:changed'));
    await settle(page.window);
    expect(keys(page.requests).slice(3)).toEqual([LIST, history(1), methods(1)]);
    expect(page.$1('#sub-h').textContent).toBe('Home water');
  });
});

// ------------------------------------------------------------------ the shared order modal (spec §12)

describe('the shared order modal', () => {
  it('shows the backend\'s labels, no zero rows, no .btn-sm, and draws the published Skip and the subscription link', async () => {
    // The backend's answer: the customer may pay this unpaid order, and its removal is a Skip on its subscription.
    const published = loadCase('write_order_modal').exchanges[3].response.data.order;
    expect([published.customer_actions.cancel.via, published.customer_actions.cancel.subscription_id]).toEqual(['skip', 1]);
    const page = await boot('write_order_modal', { modal: true });
    await press(page, page.$1('.subs-hero__order [data-action="open-order"]'));
    await settle(page.window);
    sentExactlyTheContract(page);
    const modal = page.$1('#orderDetailModal');
    expect(modal.classList.contains('show')).toBe(true);
    const content = squash(page.$1('#orderDetailContent').textContent);
    expect(content).toContain('Placed on Wednesday, 28 October');
    expect(page.$$('.order-summary-row', modal).map((r) => seen(r))).toEqual([
      `Subtotal ${money('30 000')}`, `Delivery Fee ${money('0')}`,
    ]);
    expect(seen(page.$1('.order-summary-total', modal))).toBe(`Total ${money('30 000')}`);
    expect(content).not.toMatch(/UZS|Subscription discount|Tax/);
    expect(page.$$('.btn-sm', modal)).toHaveLength(0);
    // Pay and Skip, drawn by order-actions.js from the order's customer_actions (Task 10a's orderSurfaces.test.js
    // drives their writes); the old Cancel button and subscription note are gone.
    const actions = page.$1('#orderDetailActions');
    const { pay, cancel } = published.customer_actions;
    expect(page.$$('[data-order-action]', actions).map((b) => squash(b.textContent))).toEqual([pay.label, cancel.label]);
    expect(page.$1('[data-order-action="cancel"]', actions).textContent).toBe(cancel.label);
    expect(page.$1('#cancelOrderBtn')).toBeNull();
    expect(page.$1('#orderSubscriptionNote')).toBeNull();
    const link = page.$1('a.order-actions__subscription', actions);
    expect(link.getAttribute('href')).toBe('/my-subscriptions?sub=1#subscription');
    expect(squash(link.textContent)).toBe(page.shell.modal.island.order_actions.open_subscription);
  });
});

// ------------------------------------------------------------------ the write side in Russian and Uzbek

describe.each(['needs_payment_ru', 'needs_payment_uz'])('%s: the write side in the page language', (name) => {
  it('the confirm\'s reasons and buttons are the published words, the save bar and the editors the island\'s', async () => {
    const page = await boot(name, {
      answers: { 'POST /api/v1/subscriptions/1/preview': { status: 200, body: loadCase('write_items_save').exchanges[3].response } },
    });
    const plan = page.listed[0].allowed_actions.skip;
    const englishPlan = loadCase('needs_payment').requests[LIST].body.data.items[0].allowed_actions.skip;
    await press(page, page.$1('[data-action="skip"]'));
    expect(reasonChipLabels(page)).toEqual(plan.reasons.map((reason) => reason.label));
    expect(confirmButtons(page)).toEqual([plan.confirm_label, plan.keep_label]);
    await press(page, page.$1('#subs-confirm [data-reason="other"]'));
    const skipped = page.$1('#subs-confirm').textContent;
    await press(page, page.$1('#subs-confirm [data-action="confirm-keep"]'));
    await openSection(page, 'items');
    await press(page, row(page, 1).querySelector('[data-action="inc"]'));
    await afterPreview(page);
    const editing = page.$1('#subs-edit-items').textContent;
    [...englishPlan.reasons.map((reason) => reason.label), englishPlan.keep_label, englishPlan.confirm_label].forEach((word) => {
      expect(skipped).not.toContain(word);
    });
    const english = SHELL.en.island.i18n;
    ['skip.reasons', 'skip.reason_text', 'edit.unsaved', 'edit.discard', 'edit.save',
      'items.add', 'items.edit_note', 'items.last_item'].forEach((key) => {
      expect(page.words[key]).not.toBe(english[key]);
      expect(skipped + editing).not.toContain(english[key]);
    });
    expect(skipped).toContain(page.words['skip.reason_text']);
    expect(editing).toContain(page.words['edit.unsaved']);
  });
});

describe('the write side stays free of business rules and native dialogs', () => {
  it('reads every decision from the published answers', () => {
    // The skip date the backend named is the one sent; Pay follows allowed_actions; no status, date or money maths.
    expect(PAGE_SCRIPT).toMatch(/expected_date: plan\.date/);
    expect(PAGE_SCRIPT).not.toMatch(/\bconfirm\(|\bprompt\(|\balert\(|new Date\(|toFixed\(|parseFloat\(|showNotification\(/);
    expect(PAGE_SCRIPT).not.toMatch(/status\s*===\s*'active'/);
    // Spec 2026-10-27 §16 M3: the sentences and labels are the backend's; the script chooses none.
    expect(PAGE_SCRIPT).not.toMatch(/function skipWords|function keptLine|offeredMethods/);
    // An order's Pay and Skip/Cancel go through the one write path every order surface shares (order-actions.js).
    expect(PAGE_SCRIPT).toMatch(/OrderActions\.pay\(/);
    expect(PAGE_SCRIPT).toMatch(/OrderActions\.remove\(/);
    expect(PAGE_SCRIPT).not.toMatch(/retry-payment|'\/orders\/'/);
  });
});
