// Aqua Club in the cabinet (/my-loyalty, website redesign 2026-11-14, group "account"): loyalty.js draws the rewards,
// the AquaCoins history (group chips and "Show more") and the referral card over the page the server renders.
//
// The SHIPPED scripts run in jsdom as the page loads them: jquery.js, _page-data.js, base.js (its real apiRequest)
// and loyalty.js, over the member's markup and page-data island the real template renders
// (tests/fixtures/web_account_loyalty/shell.json, kept fresh by tests/integration/test_web_account_loyalty.py).
// Only `fetch` is replaced: each read is answered by its exact URL, in the shape the endpoints publish
// (paginated_response: data.items + meta). An unexpected request is recorded and answered 599.
import fs from 'node:fs';
import path from 'node:path';
import { JSDOM, VirtualConsole } from 'jsdom';

const REPO = path.resolve(process.cwd(), '..');
// Repo files named in this test (never user input).
// eslint-disable-next-line security/detect-non-literal-fs-filename
const read = (file) => fs.readFileSync(path.join(REPO, file), 'utf8');
const BASE_SCRIPTS = [
  'business_app/static/js/jquery.js',
  'business_app/static/js/pages/_page-data.js',
  'business_app/static/js/pages/base.js',
].map(read);
const LOYALTY_SCRIPT = read('business_app/static/js/pages/loyalty.js');
const SHELL = JSON.parse(read('tests/fixtures/web_account_loyalty/shell.json'));
const NBSP = ' ';

const HISTORY = '/api/v1/loyalty/points/history?page=1&per_page=20';
const REWARDS = '/api/v1/loyalty/rewards';
const REFERRAL = '/api/v1/loyalty/referral';
const ok = (data, meta) => ({ status: 200, body: { success: true, data, ...(meta ? { meta } : {}) } });
const page = (items, { page: number = 1, pages = 1, total = items.length } = {}) => ok(
  { items }, { page: number, per_page: 20, total, pages, has_next: number < pages, has_prev: number > 1 },
);
const txn = (id, points, transaction_type, display_category, extra = {}) => ({
  id, points, transaction_type, display_category, action_type: null, order_id: null, order_number: null, reward_name: null,
  description: 'English text the page must not print', created_at: '2026-11-13T09:30:00+05:00', ...extra,
});
const REWARD_ROWS = ok({
  rewards: [
    { id: 1, name: 'Скидка 10 000', description: '', reward_type: 'discount', points_cost: 2500, can_redeem: true, points_needed: 0, image_url: null, is_system_reward: false },
    { id: 2, name: 'Бутыль воды', description: '', reward_type: 'free_product', points_cost: 4000, can_redeem: false, points_needed: 2100, image_url: null, is_system_reward: false },
  ],
  user_points_balance: 1900,
  categories: [],
});

async function settle(window, rounds = 30) {
  for (let i = 0; i < rounds; i += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, 0));
  }
}

const escapeJson = (value) => JSON.stringify(value).replace(/</g, '\\u003c');

async function boot(answers) {
  const base = { api_base_url: '/api/v1', current_user: null, is_authenticated: true, login_url: '/login', language: SHELL.language, i18n: {} };
  const virtualConsole = new VirtualConsole();
  const navigations = [];
  virtualConsole.on('jsdomError', (error) => {
    if (/navigation/i.test(error.message)) navigations.push(error.message);
  });
  const dom = new JSDOM(
    `<!doctype html><html lang="${SHELL.language}"><body>` +
    `<script type="application/json" id="base-page-data">${escapeJson(base)}</script>` +
    `<div id="toast-container"></div>${SHELL.html}` +
    `<script type="application/json" id="page-data">${escapeJson(SHELL.island)}</script></body></html>`,
    { url: 'http://localhost/my-loyalty', runScripts: 'outside-only', pretendToBeVisual: true, virtualConsole },
  );
  const { window } = dom;
  const requests = [];
  const unexpected = [];
  window.fetch = (url, options = {}) => {
    const method = (options.method || 'GET').toUpperCase();
    requests.push(`${method} ${url}`);
    const queue = answers[url];
    const answer = Array.isArray(queue) ? queue.shift() : queue;
    if (!answer || method !== 'GET') {
      if (url.startsWith('/api/v1/loyalty')) unexpected.push(`${method} ${url}`);
      return Promise.resolve({ ok: false, status: 599, json: async () => ({ success: false }) });
    }
    return Promise.resolve({ ok: answer.status < 400, status: answer.status, json: async () => JSON.parse(JSON.stringify(answer.body)) });
  };
  BASE_SCRIPTS.forEach((source) => window.eval(source));
  window.eval(LOYALTY_SCRIPT);
  if (window.document.readyState !== 'loading') window.document.dispatchEvent(new window.Event('DOMContentLoaded'));
  await settle(window);
  const document = window.document;
  const $$ = (selector) => Array.from(document.querySelectorAll(selector));
  const $1 = (selector) => document.querySelector(selector);
  const loyaltyRequests = () => requests.filter((key) => key.includes('/api/v1/loyalty'));
  return { window, document, requests, loyaltyRequests, unexpected, navigations, $$, $1 };
}

const squash = (value) => (value || '').replace(/[ \t\n\r]+/g, ' ').trim();
const I18N = SHELL.island.loyalty.i18n;
const TXN = SHELL.island.loyalty.txn;

describe('/my-loyalty: history, rewards and the referral card', () => {
  it('reads the rewards and the first history page once, and labels each row by its published category', async () => {
    const page1 = page([txn(1, 1800, 'earned', 'order_earn'), txn(2, 400, 'bonus', 'referral'), txn(3, -250, 'redeemed', 'redeem')]);
    const { loyaltyRequests, unexpected, $$, $1 } = await boot({ [HISTORY]: page1, [REWARDS]: REWARD_ROWS });

    expect(unexpected).toEqual([]);
    expect(loyaltyRequests().sort()).toEqual([`GET ${HISTORY}`, `GET ${REWARDS}`].sort());
    const rows = $$('#pointsHistoryList .ae-acl__txn');
    expect(rows.map((row) => squash(row.querySelector('.ae-acl__txn-title').textContent)))
      .toEqual([TXN.order_earn, TXN.referral, TXN.redeem]);
    expect(rows.map((row) => row.querySelector('.ae-acl__txn-amount').firstChild.textContent))
      .toEqual([`+1${NBSP}800`, '+400', '−250']);
    expect($1('#pointsHistoryList').textContent).not.toContain('English text');
    // A row without an order shows no order line.
    expect(rows[1].querySelector('.ae-acl__txn-order')).toBeNull();
    // The date in the page's language and Tashkent time (base.js AE.formatDate), never en-US.
    expect(rows[0].querySelector('time').textContent).toMatch(/13 ноября 2026/);
    expect($1('#historyMore').hidden).toBe(true);
    // Rewards: the cost, and either "enough" or what is still missing, from the API's own fields.
    const cards = $$('#rewardsGrid .ae-acl__reward');
    expect(cards.map((card) => squash(card.querySelector('.ae-acl__reward-cost').textContent)))
      .toEqual([`2${NBSP}500${NBSP}${SHELL.island.loyalty.unit}`, `4${NBSP}000${NBSP}${SHELL.island.loyalty.unit}`]);
    expect(squash(cards[0].querySelector('.ae-acl__reward-state').textContent)).toBe(I18N.reward_ready);
    expect(squash(cards[1].querySelector('.ae-acl__reward-state').textContent))
      .toBe(squash(I18N.reward_missing.replace('{pts}', `2${NBSP}100`)));
  });

  it('names the order a row belongs to by the number the API publishes, escaped', async () => {
    const page1 = page([txn(1, 540, 'earned', 'order_earn', { order_id: 25, order_number: 'AD_000025_26' }),
      txn(2, 10, 'earned', 'order_earn', { order_id: 26, order_number: '<b>X</b>' })]);
    const { $$ } = await boot({ [HISTORY]: page1, [REWARDS]: REWARD_ROWS });

    const orders = $$('#pointsHistoryList .ae-acl__txn-order');
    expect(orders.map((node) => node.textContent))
      .toEqual([I18N.txn_order.replace('{number}', 'AD_000025_26'), I18N.txn_order.replace('{number}', '<b>X</b>')]);
    expect(orders[1].querySelector('b')).toBeNull();
  });

  it.each([
    ['earned', 'earned', 'order_earn'], ['bonus', 'earned', 'streak'], ['bonus', 'bonus', 'referral'],
    ['redeemed', 'redeemed', 'redeem'], ['expired', 'expired', 'expired'],
  ])('the "%s" chip asks for its group only and marks itself chosen', async (group, type, category) => {
    const filtered = `/api/v1/loyalty/points/history?page=1&per_page=20&group=${group}`;
    const { window, loyaltyRequests, unexpected, $$, $1 } = await boot({
      [HISTORY]: [page([txn(1, 10, 'earned', 'order_earn')]), page([txn(1, 10, 'earned', 'order_earn')])],
      [REWARDS]: REWARD_ROWS,
      [filtered]: page([txn(9, -5, type, category)]),
    });

    $1(`#historyFilter [data-group="${group}"]`).click();
    await settle(window);

    expect(unexpected).toEqual([]);
    expect(loyaltyRequests()).toContain(`GET ${filtered}`);
    expect($$('#historyFilter [aria-pressed="true"]').map((chip) => chip.getAttribute('data-group'))).toEqual([group]);
    expect($$('#pointsHistoryList .ae-acl__txn').map((row) => squash(row.querySelector('.ae-acl__txn-title').textContent)))
      .toEqual([TXN[category]]);

    // Back to "All": the unfiltered list again.
    $1('#historyFilter [data-group=""]').click();
    await settle(window);
    expect(loyaltyRequests().filter((key) => key === `GET ${HISTORY}`)).toHaveLength(2);
  });

  it('says so when a chip has nothing, in the filtered wording', async () => {
    const empty = '/api/v1/loyalty/points/history?page=1&per_page=20&group=expired';
    const { window, $1 } = await boot({ [HISTORY]: page([txn(1, 10, 'earned', 'order_earn')]), [REWARDS]: REWARD_ROWS, [empty]: page([]) });

    $1('#historyFilter [data-group="expired"]').click();
    await settle(window);

    expect($1('#historyState').hidden).toBe(false);
    expect(squash($1('#historyState').textContent)).toBe(I18N.history_empty_filtered);
    expect($1('#pointsHistoryList').children).toHaveLength(0);
  });

  it('"Show more" asks for the next page and adds it under the first', async () => {
    const page2 = '/api/v1/loyalty/points/history?page=2&per_page=20';
    const { window, loyaltyRequests, $$, $1 } = await boot({
      [HISTORY]: page([txn(1, 10, 'earned', 'order_earn')], { pages: 2, total: 2 }),
      [page2]: page([txn(2, 20, 'bonus', 'birthday')], { page: 2, pages: 2, total: 2 }),
      [REWARDS]: REWARD_ROWS,
    });

    expect($1('#historyMore').hidden).toBe(false);
    expect($1('#historyInfo').textContent).toBe(I18N.history_shown.replace('{shown}', '1').replace('{total}', '2'));
    $1('[data-action="history-more"]').click();
    await settle(window);

    expect(loyaltyRequests()).toContain(`GET ${page2}`);
    expect($$('#pointsHistoryList .ae-acl__txn')).toHaveLength(2);
    expect($1('#historyMore').hidden).toBe(true);
  });

  it('a failed read offers Try again, which asks again', async () => {
    const { window, loyaltyRequests, $$, $1 } = await boot({
      [HISTORY]: [{ status: 500, body: { success: false } }, page([txn(1, 10, 'earned', 'order_earn')])],
      [REWARDS]: { status: 500, body: { success: false } },
    });

    expect(squash($1('#historyState').textContent)).toContain(I18N.history_failed);
    expect(squash($1('#rewardsGrid').textContent)).toBe(I18N.rewards_failed);
    $1('[data-action="history-retry"]').click();
    await settle(window);

    expect(loyaltyRequests().filter((key) => key === `GET ${HISTORY}`)).toHaveLength(2);
    expect($$('#pointsHistoryList .ae-acl__txn')).toHaveLength(1);
    expect($1('#historyState').hidden).toBe(true);
  });

  it('an account the program stops covering is handed back to the server, never shown invented figures', async () => {
    const refused = { status: 403, body: { success: false, data: { code: 'loyalty_not_available' } } };
    const { navigations, $$ } = await boot({ [HISTORY]: refused, [REWARDS]: refused });

    // The page reloads (the server then draws the "not available" state); nothing is drawn in the meantime.
    expect(navigations.length).toBeGreaterThan(0);
    expect($$('#pointsHistoryList .ae-acl__txn-title').length).toBe(0);
    expect($$('#rewardsGrid .ae-acl__reward').length).toBe(0);
  });

  it('shows the referral code on request and shares the link the backend built', async () => {
    const referral = ok({
      referral_code: 'REFCODE42',
      referral_link: 'https://aqua-element.uz/register?ref=REFCODE42',
      statistics: { total_referrals: 3, points_earned_from_referrals: 1500 },
    });
    const { window, loyaltyRequests, $1 } = await boot({ [HISTORY]: page([]), [REWARDS]: REWARD_ROWS, [REFERRAL]: referral });
    const shared = [];
    Object.defineProperty(window.navigator, 'share', { value: (data) => { shared.push(data); return Promise.resolve(); }, configurable: true });

    expect(loyaltyRequests()).not.toContain(`GET ${REFERRAL}`);
    $1('[data-action="generate-referral"]').click();
    await settle(window);

    expect(loyaltyRequests()).toContain(`GET ${REFERRAL}`);
    expect($1('#referralCard').hidden).toBe(false);
    expect($1('#referralCodeText').textContent).toBe('REFCODE42');
    expect($1('#referralCount').textContent).toBe('3');
    expect($1('#referralPoints').textContent).toBe(`1${NBSP}500`);
    $1('[data-action="share-referral"]').click();
    await settle(window);

    expect(shared).toEqual([{
      title: I18N.share_title,
      text: I18N.share_text.replace('{code}', 'REFCODE42'),
      url: 'https://aqua-element.uz/register?ref=REFCODE42',
    }]);
  });

  it('without a share sheet, Share copies the message with the link, and Copy copies the bare code', async () => {
    const referral = ok({
      referral_code: 'REFCODE42',
      referral_link: 'https://aqua-element.uz/register?ref=REFCODE42',
      statistics: { total_referrals: 0, points_earned_from_referrals: 0 },
    });
    const { window, $1 } = await boot({ [HISTORY]: page([]), [REWARDS]: REWARD_ROWS, [REFERRAL]: referral });
    const copied = [];
    Object.defineProperty(window.navigator, 'share', { value: undefined, configurable: true });
    Object.defineProperty(window.navigator, 'clipboard', {
      value: { writeText: (text) => { copied.push(text); return Promise.resolve(); } }, configurable: true,
    });

    $1('[data-action="generate-referral"]').click();
    await settle(window);
    $1('[data-action="share-referral"]').click();
    await settle(window);
    $1('[data-action="copy-referral"]').click();
    await settle(window);

    expect(copied).toEqual([
      `${I18N.share_text.replace('{code}', 'REFCODE42')} https://aqua-element.uz/register?ref=REFCODE42`,
      'REFCODE42',
    ]);
  });
});
