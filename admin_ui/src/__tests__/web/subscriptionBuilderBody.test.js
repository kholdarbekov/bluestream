// The WEBSITE subscription builder (business_app/static/js/subscription-constructor.js), driven through
// its real handlers. It is plain browser JS outside this app, so the shipped source is evaluated in a vm
// with only the browser seams replaced (document, sessionStorage, apiRequest). Nothing is re-implemented.
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const REPO = path.resolve(process.cwd(), '..');
const SOURCE = fs.readFileSync(path.join(REPO, 'business_app/static/js/subscription-constructor.js'), 'utf8');
const TEMPLATE = fs.readFileSync(path.join(REPO, 'business_app/templates/frontend/index.html'), 'utf8');
const FIXTURE = JSON.parse(fs.readFileSync(path.join(REPO, 'tests/fixtures/web_subscription_builder_bodies.json'), 'utf8'));
// The data island the home page renders per language (written by test_web_subscription_builder_contract.py):
// `{i18n, frequency_names, weekday_names}`, the whole of what the script gets from the page.
const ISLAND = JSON.parse(fs.readFileSync(path.join(REPO, 'tests/fixtures/web_subscription_builder_island.json'), 'utf8'));
const WORDS = ISLAND.en;

// A figure no local multiplier (30, 4, 2, 1) would produce.
const PREVIEW = {
  deliveries_per_month: 4.333333,
  deliveries_per_month_label: 'Примерно 4 доставки в месяц',
  estimated_monthly_cost: 130000,
};
const SLOTS = [{ id: 22, name: 'Morning', start_time: '10:00', end_time: '12:00', time_range: '10:00-12:00' }];

const load = ({ slots = SLOTS } = {}) => {
  const requests = [];
  const sandbox = {
    console: { error() {}, log() {}, warn() {} },
    CURRENT_USER: { id: 1 },
    API_BASE_URL: '/api/v1',
    getPageData: (id) => (id === 'subscription-constructor-data' ? WORDS : null),
    document: {
      addEventListener() {},
      getElementById() { return null; },
      querySelectorAll() { return []; },
    },
    sessionStorage: { setItem() {}, getItem() { return null; }, removeItem() {} },
    apiRequest: async (url, options = {}) => {
      requests.push({ url, options });
      if (url === '/subscriptions/preview') {
        return { ok: true, json: async () => ({ success: true, data: { preview: PREVIEW } }) };
      }
      return { ok: true, json: async () => ({ time_slots: slots }) };
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(`${SOURCE}\n;this.SubscriptionConstructor = SubscriptionConstructor;`, sandbox);
  const Builder = sandbox.SubscriptionConstructor;
  // No constructor: it starts the page (init, product fetch). The handlers under test need only state.
  const builder = Object.create(Builder.prototype);
  Object.assign(builder, {
    selectedProducts: new Map([[33, { product: { id: 33, name: 'Water' }, quantity: 2 }]]),
    selectedFrequency: null,
    selectedWeekday: null,
    selectedDayOfMonth: null,
    selectedTimeSlot: null,
    selectedAddress: { id: FIXTURE.ids.address },
    selectedPaymentMethod: 'cash',
    timeSlots: [],
    stateKey: 'k',
    showError() {},
    renderTimeSlots() {},
  });
  return { Builder, builder, requests };
};

const button = (dataset) => ({ dataset, classList: { add() {}, remove() {} } });

describe('the website builder weekday convention (JS getDay 0 = Sunday -> backend 0 = Monday)', () => {
  it('converts every JS day explicitly', () => {
    const { Builder } = load();
    expect([0, 1, 2, 3, 4, 5, 6].map((d) => Builder.weekdayFromJsDay(d))).toEqual([6, 0, 1, 2, 3, 4, 5]);
  });

  it('maps each day button of the shipped template to its weekday name, Monday = 0', () => {
    const { Builder } = load();
    const buttons = [...TEMPLATE.matchAll(/class="day-btn" data-day="(\d)">\{\{ 'landing\.custom_subscription\.day\.(\w+)'\|t \}\}/g)]
      .map((m) => [Number(m[1]), m[2]]);
    expect(buttons).toHaveLength(7);
    const names = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'];
    buttons.forEach(([jsDay, name]) => {
      expect(names[Builder.weekdayFromJsDay(jsDay)]).toBe(name);
    });
  });
});

describe('the body the website builder posts', () => {
  it.each(FIXTURE.cases)('$name', async (c) => {
    const { builder, requests } = load();
    builder.selectedFrequency = c.frequency;
    if (c.js_day !== undefined) {
      await builder.selectDay(button({ day: String(c.js_day) }));
      expect(builder.selectedWeekday).toBe(c.expected_weekday);
      // the slots offered that weekday (backend convention), not the JS number, for this frequency
      expect(requests.at(-1).url).toBe(`/delivery/time-slots?weekday=${c.expected_weekday}&frequency=${c.frequency}`);
    }
    if (c.day_of_month !== undefined) builder.selectedDayOfMonth = c.day_of_month;
    builder.selectedTimeSlot = c.any_time ? 'any' : FIXTURE.ids.slot;

    expect(builder.buildSubscriptionBody(c.body.name)).toEqual(c.body);
  });

  it('never sends the old pair or the other day field', () => {
    const { builder } = load();
    builder.selectedFrequency = 'weekly';
    builder.selectedWeekday = 3;
    builder.selectedDayOfMonth = 20; // stale from an earlier monthly choice
    builder.selectedTimeSlot = 22;
    const body = builder.buildSubscriptionBody('x');
    expect(body).not.toHaveProperty('billing_cycle');
    expect(body).not.toHaveProperty('delivery_frequency');
    expect(body).not.toHaveProperty('delivery_day_of_month');
  });

  it('changing the frequency forgets the day the new frequency does not use', async () => {
    const { builder } = load();
    builder.selectedWeekday = 3;
    builder.selectedDayOfMonth = 20;
    await builder.selectFrequency({ dataset: { frequency: 'daily' }, classList: { add() {} } });
    expect(builder.selectedWeekday).toBeNull();
    expect(builder.selectedDayOfMonth).toBeNull();
  });

  // Spec 2026-10-26 §8.2: a monthly delivery falls on a different weekday each month, so the backend offers it
  // only the slots available on every day. The builder names the frequency and lets the backend's rule (the one
  // create now enforces) pick; tests/integration/test_web_subscription_builder_contract.py replays this URL.
  it.each([
    ['monthly', '/delivery/time-slots?weekday=any&frequency=monthly'],
    ['daily', '/delivery/time-slots?weekday=any&frequency=daily'],
    ['weekly', '/delivery/time-slots?weekday=any&frequency=weekly'],
  ])('asks the backend for the slots %s may use', async (frequency, url) => {
    const { builder, requests } = load();
    await builder.selectFrequency({ dataset: { frequency }, classList: { add() {} } });
    expect(requests.at(-1).url).toBe(url);
  });

  it('names no frequency before one is chosen (the step can be reopened from a saved state)', async () => {
    const { builder, requests } = load();
    await builder.loadTimeSlots();
    expect(requests.at(-1).url).toBe('/delivery/time-slots?weekday=any');
  });

  it('drops a chosen slot the new weekday does not offer', async () => {
    const { builder } = load();
    builder.selectedTimeSlot = 999;
    await builder.selectFrequency({ dataset: { frequency: 'daily' }, classList: { add() {} } });
    expect(builder.selectedTimeSlot).toBeNull();
  });
});

describe('the payment step asks for the methods offered at the chosen address', () => {
  // Creation accepts only a method the backend menu offers for the subscription's address (spec 2026-10-26
  // B8), so the menu must be asked about that address too: a capped place shows no cash it would refuse.
  it('passes the address chosen on the step before', async () => {
    const { builder, requests } = load();
    await builder.loadPaymentMethods();
    expect(requests.map((r) => r.url)).toEqual([
      `/payments/methods?context=subscription&delivery_address_id=${FIXTURE.ids.address}`,
    ]);
  });

  it('asks without an address only while none is chosen', async () => {
    const { builder, requests } = load();
    builder.selectedAddress = null;
    await builder.loadPaymentMethods();
    expect(requests.map((r) => r.url)).toEqual(['/payments/methods?context=subscription']);
  });
});

describe('the review step shows the backend monthly estimate', () => {
  it('posts one frequency and the items to the preview and keeps its numbers', async () => {
    const { builder, requests } = load();
    builder.isAuthenticated = true;
    builder.selectedFrequency = 'weekly';
    await builder.loadPreview();
    expect(requests).toHaveLength(1);
    expect(requests[0].url).toBe('/subscriptions/preview');
    expect(JSON.parse(requests[0].options.body)).toEqual({
      frequency: 'weekly',
      items: [{ product_id: 33, quantity: 2 }],
    });
    expect(builder.preview).toEqual(PREVIEW);
    // The script has no word or plural rule for the hint: it renders the sentence the backend published.
    expect(builder.formatDeliveriesPerMonth).toBeUndefined();
  });

  // Preview vs apply: the saved subscription counts its slot's days (spec 2026-10-27 §6.4), so the review's
  // per-month figure is asked for the same slot the POST names. Any time names none.
  it('the review preview sends the chosen slot', async () => {
    const { builder, requests } = load();
    builder.isAuthenticated = true;
    builder.selectedFrequency = 'daily';
    builder.selectedTimeSlot = 22;
    await builder.loadPreview();
    expect(JSON.parse(requests[0].options.body)).toEqual({
      frequency: 'daily',
      delivery_time_slot_id: 22,
      items: [{ product_id: 33, quantity: 2 }],
    });

    builder.selectedTimeSlot = 'any';
    await builder.loadPreview();
    expect(JSON.parse(requests[1].options.body)).toEqual({
      frequency: 'daily',
      items: [{ product_id: 33, quantity: 2 }],
    });
  });

  it('shows no estimate, and asks nothing, for a visitor who is not signed in', async () => {
    const { builder, requests } = load();
    builder.isAuthenticated = false;
    builder.selectedFrequency = 'weekly';
    await builder.loadPreview();
    expect(requests).toHaveLength(0);
    expect(builder.preview).toBeNull();
  });

  it('the source holds no local deliveries-per-month multiplier', () => {
    expect(SOURCE).not.toMatch(/monthlyMultiplier|estimatedMonthlyCost/);
  });
});

// Spec 2026-10-26 §9 / §12: "the homepage builder's monthly day picker: likely broken (nice-select vs a native
// listener)". The theme (business_app/static/js/script.js) turns every <select> without `.ignore` into a
// nice-select, which hides the real <select> and reports the customer's pick with jQuery's
// `.trigger('change')`. That reaches jQuery handlers only, never a native addEventListener one, so a monthly
// customer's day was never stored and step 2 kept asking for it. Driven here with the shipped jQuery, the
// shipped nice-select plugin and the shipped builder, the customer clicking the day in the nice-select list.
describe('the monthly day picker under the theme\'s nice-select', () => {
  // Repo files named in this test (never user input).
  // eslint-disable-next-line security/detect-non-literal-fs-filename
  const read = (file) => fs.readFileSync(path.join(REPO, file), 'utf8');
  const THEME = read('business_app/static/js/script.js');
  const MONTH_SELECT = TEMPLATE.match(/<select id="month-day-select"[^>]*>/)[0];

  const page = async () => {
    const { JSDOM } = await import('jsdom');
    const days = Array.from({ length: 31 }, (_, i) => `<option value="${i + 1}">${i + 1}</option>`).join('');
    const dom = new JSDOM(
      `<!doctype html><body><div id="month-day-selector">${MONTH_SELECT}<option value="">Pick</option>${days}</select></div></body>`,
      { runScripts: 'outside-only' },
    );
    const { window } = dom;
    window.eval(read('business_app/static/js/jquery.js'));
    window.eval(read('business_app/static/js/jquery.nice-select.min.js'));
    window.eval(`${SOURCE}\n;window.SubscriptionConstructor = SubscriptionConstructor;`);
    window.$('select:not(.ignore)').niceSelect();  // the theme's own call (asserted below)
    const builder = Object.create(window.SubscriptionConstructor.prototype);
    Object.assign(builder, { selectedDayOfMonth: null, stateKey: 'k', saveState() {} });
    builder.setupEventListeners();
    return { window, builder };
  };

  it('the theme applies nice-select to the builder\'s day-of-month select', () => {
    expect(THEME).toContain("$('select:not(.ignore)').niceSelect();");
    expect(MONTH_SELECT).not.toMatch(/class="[^"]*\bignore\b/);
  });

  it('a day picked in the nice-select list is the day the builder keeps', async () => {
    const { window, builder } = await page();
    const option = window.document.querySelector('.nice-select .option[data-value="15"]');
    expect(option).not.toBeNull();
    option.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    expect(window.document.getElementById('month-day-select').value).toBe('15');
    expect(builder.selectedDayOfMonth).toBe(15);
  });
});

// Re-review G2: a slot is required only when the backend offers one for the chosen schedule. Monthly is offered
// only the slots available every day (spec 2026-10-26 §8.2), so the list can be empty; the backend creates without
// a slot (the time is confirmed later) and the bot skips its slot screen then. The builder must not dead-end.
describe('no slot offered for the schedule', () => {
  const monthly = { dataset: { frequency: 'monthly' }, classList: { add() {} } };
  const monthlyBody = FIXTURE.cases.find((c) => c.name === 'monthly on the 15th').body;

  it('step 2 goes on without a slot, and the body the builder posts names none', async () => {
    const { builder, requests } = load({ slots: [] });
    const errors = [];
    builder.showError = (message) => errors.push(message);
    await builder.selectFrequency(monthly);
    expect(requests.at(-1).url).toBe('/delivery/time-slots?weekday=any&frequency=monthly');
    builder.selectedDayOfMonth = 15;

    expect(builder.selectedTimeSlot).toBe('any');
    expect(builder.validateStep(2)).toBe(true);
    expect(errors).toEqual([]);
    const { delivery_time_slot_id: _slot, ...withoutSlot } = monthlyBody;
    expect(builder.buildSubscriptionBody(monthlyBody.name)).toEqual(withoutSlot);
  });

  it('with slots offered, step 2 waits for a time or Any time; Any time posts no slot', async () => {
    const { builder } = load();
    const errors = [];
    builder.showError = (message) => errors.push(message);
    await builder.selectFrequency(monthly);
    builder.selectedDayOfMonth = 15;

    expect(builder.validateStep(2)).toBe(false);
    expect(errors).toEqual([WORDS.i18n.error_time]);

    builder.selectedTimeSlot = 'any';
    expect(builder.validateStep(2)).toBe(true);
    expect(builder.buildSubscriptionBody(monthlyBody.name)).not.toHaveProperty('delivery_time_slot_id');
  });
});

// A page with the builder's real step markup (the template's own day buttons and day-of-month select), the shipped
// jQuery, nice-select (applied as the theme applies it) and the shipped builder. `answers` serves the requests.
const QUOTES = JSON.parse(fs.readFileSync(path.join(REPO, 'tests/fixtures/web_subscription_builder_quotes.json'), 'utf8'));
// Repo files named in this test (never user input).
// eslint-disable-next-line security/detect-non-literal-fs-filename
const readRepo = (file) => fs.readFileSync(path.join(REPO, file), 'utf8');
const ADDRESS = { id: 11, title: 'Home', full_address: 'Amir Temur 1' };

const builderPage = async ({ slots = SLOTS, lang = 'en', answer = () => undefined, saved = null } = {}) => {
  const { JSDOM } = await import('jsdom');
  const monthSelect = TEMPLATE.match(/<select id="month-day-select"[^>]*>/)[0];
  const days = Array.from({ length: 31 }, (_, i) => `<option value="${i + 1}">${i + 1}</option>`).join('');
  const dayButtons = [...TEMPLATE.matchAll(/<button type="button" class="day-btn" data-day="(\d)">/g)]
    .map((m) => `<button type="button" class="day-btn" data-day="${m[1]}">day ${m[1]}</button>`).join('');
  const frequencies = ['daily', 'weekly', 'biweekly', 'monthly']
    .map((f) => `<div class="frequency-option" data-frequency="${f}">${f}</div>`).join('');
  const dom = new JSDOM(
    `<!doctype html><html lang="${lang}"><body><div id="products-grid"></div><div class="frequency-options">${frequencies}</div>` +
    `<div id="day-selector" style="display: none;">${dayButtons}</div>` +
    `<div id="month-day-selector" style="display: none;">${monthSelect}<option value="">Pick a day</option>${days}</select></div>` +
    '<select id="time-slots-select"><option value="">Pick a time</option></select><div id="addresses-list"></div>' +
    '<div id="payment-methods-grid"></div><div id="review-container"></div></body></html>',
    { url: 'http://localhost/', runScripts: 'outside-only' },
  );
  const { window } = dom;
  const requests = [];
  window.CURRENT_USER = { id: 1 };
  window.API_BASE_URL = '/api/v1';
  window.getPageData = (id) => (id === 'subscription-constructor-data' ? (ISLAND[lang] || WORDS) : null);
  window.fetch = async (url, options = {}) => {
    requests.push({ url, options });
    return { ok: true, json: async () => QUOTES.catalog.response };
  };
  const errors = [];
  window.showNotification = (text, type) => { if (type === 'error') errors.push(text); };
  window.apiRequest = async (url, options = {}) => {
    requests.push({ url, options });
    const answered = answer(url);
    if (answered) return answered;
    if (url === '/subscriptions/preview') return { ok: true, json: async () => QUOTES.preview.response };
    if (url === '/auth/addresses') return { ok: true, json: async () => ({ success: true, data: { addresses: [ADDRESS] } }) };
    if (url.startsWith('/payments/methods')) {
      return { ok: true, json: async () => ({ data: { available_methods: [{ method: 'cash', display_name: 'Cash' }] } }) };
    }
    return { ok: true, json: async () => ({ time_slots: slots }) };
  };
  window.eval(readRepo('business_app/static/js/jquery.js'));
  window.eval(readRepo('business_app/static/js/jquery.nice-select.min.js'));
  window.eval(`${SOURCE}\n;window.SubscriptionConstructor = SubscriptionConstructor;`);
  window.$('select:not(.ignore)').niceSelect();
  let builder;
  if (saved) {
    // The page as a returning visitor opens it: this tab's saved state, then the real constructor and its init().
    window.sessionStorage.setItem('subscription_constructor_state', JSON.stringify({ timestamp: Date.now(), ...saved }));
    const { init } = window.SubscriptionConstructor.prototype;
    let started;
    window.SubscriptionConstructor.prototype.init = function initAndKeep() { started = init.call(this); return started; };
    builder = new window.SubscriptionConstructor();
    await started;
  } else {
    builder = Object.create(window.SubscriptionConstructor.prototype);
    Object.assign(builder, {
      currentStep: 2, selectedProducts: new Map(), selectedFrequency: null, selectedWeekday: null,
      selectedDayOfMonth: null, selectedTimeSlot: null, selectedAddress: { id: 11, title: 'Home' },
      selectedPaymentMethod: 'cash', subscriptionName: '', preview: null, products: [], timeSlots: [],
      addresses: [], paymentMethods: [], isAuthenticated: true, stateKey: 'k',
    });
    builder.setupEventListeners();
  }
  const click = async (el) => {
    el.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    await new Promise((resolve) => { window.setTimeout(resolve, 0); });
  };
  const $1 = (selector) => window.document.querySelector(selector);
  return { window, builder, requests, errors, click, $1 };
};

const slotOptions = ($1) => [...$1('#time-slots-select').options].map((o) => [o.value, o.textContent.trim()]);
const shownSlot = ($1) => $1('#time-slots-select').nextElementSibling.querySelector('.current').textContent;
const MORNING = { id: 22, name: 'Morning', start_time: '10:00', end_time: '12:00', time_range: '10:00-12:00' };
const EVENING = { id: 23, name: 'Evening', start_time: '18:00', end_time: '20:00', time_range: '18:00-20:00' };
const slotsFor = (byUrl) => (url) => {
  const slots = byUrl[url];
  return slots ? { ok: true, json: async () => ({ time_slots: slots }) } : undefined;
};

// Spec 2026-10-27 §6.2 (D15): "Any time" is always a choice, next to the slots and never picked for the visitor;
// with no slot offered it is the only choice, and it is held.
describe('the builder offers Any time', () => {
  it('next to the offered slots, never chosen for the visitor', async () => {
    const { builder, click, $1 } = await builderPage();
    await click($1('.frequency-option[data-frequency="monthly"]'));

    expect(slotOptions($1)).toEqual([['', 'Select a time slot'], ['any', 'Any time'], ['22', '10:00 - 12:00']]);
    expect(builder.selectedTimeSlot).toBeNull();
    expect(shownSlot($1)).toBe('Select a time slot');
  });

  it('a visitor choosing Any time, or a slot, in the list is held as exactly that', async () => {
    const { builder, click, $1 } = await builderPage();
    await click($1('.frequency-option[data-frequency="monthly"]'));

    await click($1('#time-slots-select + .nice-select .option[data-value="any"]'));
    expect(builder.selectedTimeSlot).toBe('any');
    await click($1('#time-slots-select + .nice-select .option[data-value="22"]'));
    expect(builder.selectedTimeSlot).toBe(22);
    await click($1('#time-slots-select + .nice-select .option[data-value=""]'));
    expect(builder.selectedTimeSlot).toBeNull();
  });

  it('as the only choice when none is offered: held, and named on the review', async () => {
    const { builder, click, $1 } = await builderPage({ slots: [] });
    await click($1('.frequency-option[data-frequency="monthly"]'));

    expect(slotOptions($1)).toEqual([['any', 'Any time']]);
    expect(shownSlot($1)).toBe('Any time');
    expect(builder.selectedTimeSlot).toBe('any');
    builder.renderReview();
    expect($1('[data-review="time-slot"]').textContent.trim()).toBe('Any time');
  });

  it('a weekday change keeps Any time and drops a slot the new day lacks', async () => {
    const { builder, click, $1 } = await builderPage({
      answer: slotsFor({
        '/delivery/time-slots?weekday=3&frequency=weekly': [MORNING],
        '/delivery/time-slots?weekday=4&frequency=weekly': [EVENING],
      }),
    });
    await click($1('.frequency-option[data-frequency="weekly"]'));
    await click($1('.day-btn[data-day="4"]'));
    await click($1('#time-slots-select + .nice-select .option[data-value="any"]'));

    await click($1('.day-btn[data-day="5"]'));
    expect(builder.selectedTimeSlot).toBe('any');
    expect(slotOptions($1)).toEqual([['', 'Select a time slot'], ['any', 'Any time'], ['23', '18:00 - 20:00']]);
    expect(shownSlot($1)).toBe('Any time');

    await click($1('#time-slots-select + .nice-select .option[data-value="23"]'));
    await click($1('.day-btn[data-day="4"]'));
    expect(builder.selectedTimeSlot).toBeNull();
    expect(shownSlot($1)).toBe('Select a time slot');
  });

  it('Any time held only because none was offered is not kept once slots are offered', async () => {
    const { builder, click, $1 } = await builderPage({
      answer: slotsFor({
        '/delivery/time-slots?weekday=any&frequency=monthly': [],
        '/delivery/time-slots?weekday=any&frequency=daily': [MORNING],
      }),
    });
    await click($1('.frequency-option[data-frequency="monthly"]'));
    expect(builder.selectedTimeSlot).toBe('any');

    await click($1('.frequency-option[data-frequency="daily"]'));

    expect(builder.selectedTimeSlot).toBeNull();
    expect(builder.validateStep(2)).toBe(false);
  });

  const saved = {
    currentStep: 2, selectedFrequency: 'weekly', selectedWeekday: 3, selectedDayOfMonth: null,
    selectedAddress: ADDRESS, selectedPaymentMethod: 'cash', subscriptionName: '', selectedProducts: [],
  };

  it('a restored Any time is shown on step 2 and posted as no slot', async () => {
    const { builder, $1 } = await builderPage({ saved: { ...saved, selectedTimeSlot: 'any' } });

    expect(builder.selectedTimeSlot).toBe('any');
    expect(shownSlot($1)).toBe('Any time');
    expect(builder.validateStep(2)).toBe(true);
    expect(builder.buildSubscriptionBody('x')).not.toHaveProperty('delivery_time_slot_id');
  });

  it('a restored "not chosen" from before this change still asks for a choice', async () => {
    const { builder, errors, $1 } = await builderPage({ saved: { ...saved, selectedTimeSlot: null } });

    expect(builder.selectedTimeSlot).toBeNull();
    expect(shownSlot($1)).toBe('Select a time slot');
    expect(builder.validateStep(2)).toBe(false);
    expect(errors).toEqual([WORDS.i18n.error_time]);
  });
});

// The weekly and biweekly day chooser offers the delivery days the slot-chooser answer publishes (spec §6.4):
// the backend decides them, the script only hides the others (and forgets a held day that stopped being one).
describe('the day options are the delivery days the backend published', () => {
  const weekdays = (days) => (url) => (url.startsWith('/delivery/time-slots')
    ? { ok: true, json: async () => ({ time_slots: SLOTS, delivery_weekdays: days }) } : undefined);
  const shown = (window) => [...window.document.querySelectorAll('.day-btn')]
    .filter((el) => el.style.display !== 'none').map((el) => el.dataset.day);

  it('weekly and biweekly offer only the delivery days the slot chooser published', async () => {
    const { window, click, $1 } = await builderPage({ answer: weekdays([0, 1, 2, 3, 4]) });
    await click($1('.frequency-option[data-frequency="weekly"]'));
    expect(shown(window)).toEqual(['1', '2', '3', '4', '5']);

    await click($1('.frequency-option[data-frequency="biweekly"]'));
    expect(shown(window)).toEqual(['1', '2', '3', '4', '5']);
  });

  it('offers all seven days when the answer names all seven, or none (a page that has not heard yet)', async () => {
    const all = await builderPage({ answer: weekdays([0, 1, 2, 3, 4, 5, 6]) });
    await all.click(all.$1('.frequency-option[data-frequency="weekly"]'));
    expect(shown(all.window)).toHaveLength(7);

    const unheard = await builderPage();
    expect(shown(unheard.window)).toHaveLength(7);
  });

  it('forgets a held weekday that is no longer a delivery day', async () => {
    const { builder, window, click, $1 } = await builderPage({
      saved: {
        currentStep: 2, selectedFrequency: 'weekly', selectedWeekday: 6, selectedDayOfMonth: null,
        selectedTimeSlot: 'any', selectedAddress: ADDRESS, selectedPaymentMethod: 'cash', subscriptionName: '',
        selectedProducts: [],
      },
      answer: weekdays([0, 1, 2, 3, 4]),
    });

    expect(builder.selectedWeekday).toBeNull();
    expect(window.document.querySelectorAll('.day-btn.selected')).toHaveLength(0);
    expect(shown(window)).toEqual(['1', '2', '3', '4', '5']);
    await click($1('.day-btn[data-day="1"]'));
    expect(builder.selectedWeekday).toBe(0);
  });
});

// Spec 2026-10-27 §11: the builder's errors, address list and review are the page's words. uz and ru come from
// the island the page really renders in those languages; no English value may remain on the screen.
describe.each(['uz', 'ru'])('%s: the builder speaks the page language', (lang) => {
  const words = ISLAND[lang].i18n;
  const english = (names) => names.map((name) => ISLAND.en.i18n[name]);
  const saved = {
    currentStep: 5, selectedFrequency: 'weekly', selectedWeekday: 3, selectedDayOfMonth: null, selectedTimeSlot: 'any',
    selectedAddress: { ...ADDRESS, apartment_number: '12' }, selectedPaymentMethod: 'cash', subscriptionName: '',
    selectedProducts: [],
  };

  it('the island really differs from English (the fixture is not an English copy)', () => {
    for (const name of Object.keys(ISLAND.en.i18n)) expect(words[name], name).toBeTruthy();
    expect(words.error_time).not.toBe(ISLAND.en.i18n.error_time);
    expect(words.any_time).not.toBe(ISLAND.en.i18n.any_time);
  });

  it('every error is the page language', async () => {
    const { builder, errors } = await builderPage({ lang });
    builder.selectedProducts = new Map();
    builder.validateStep(1);
    builder.selectedFrequency = null;
    builder.validateStep(2);
    builder.selectedFrequency = 'weekly';
    builder.validateStep(2);
    builder.selectedWeekday = 3;
    builder.timeSlotsAnswered = true;
    builder.timeSlots = [{ id: 22, label: '10:00 - 12:00' }];
    builder.validateStep(2);
    builder.selectedAddress = null;
    builder.validateStep(3);
    builder.selectedPaymentMethod = null;
    builder.validateStep(4);
    builder.selectedFrequency = 'monthly';
    builder.selectedDayOfMonth = null;
    builder.validateStep(2);

    expect(errors).toEqual([
      words.error_products, words.error_frequency, words.error_day, words.error_time,
      words.error_address, words.error_payment, words.select_day_of_month,
    ]);
  });

  it('the address list and the review carry no English word', async () => {
    const { builder, $1 } = await builderPage({ lang, saved });
    builder.addresses = [];
    builder.renderAddresses();
    const empty = $1('#addresses-list').textContent;
    builder.addresses = [{ id: 11, title: 'Home', full_address: 'Amir Temur 1', apartment_number: '12' }];
    builder.renderAddresses();
    const listed = $1('#addresses-list').textContent;
    builder.renderReview();
    const review = $1('#review-container').textContent;

    expect(empty).toContain(words.address_none_title);
    expect(empty).toContain(words.address_none_body);
    expect(listed).toContain(words.address_add);
    expect(listed).toContain(words.address_apartment.replace('{number}', '12'));
    expect(review).toContain(words.review_name_title);
    expect(review).toContain(words.review_cost_title);
    expect(review).toContain(words.review_note);
    expect(review).toContain(words.any_time);
    for (const text of english(['address_none_title', 'address_none_body', 'address_add'])) {
      expect(empty + listed).not.toContain(text);
    }
    for (const text of english([
      'review_name_title', 'review_name_label', 'review_products_title', 'review_schedule_title', 'review_frequency',
      'review_day', 'review_time', 'review_address_title', 'review_payment_title', 'review_cost_title',
      'review_per_delivery', 'review_per_month', 'review_note', 'any_time',
    ])) {
      expect(review).not.toContain(text);
    }
    // The frequency and the weekday are the island's names for them, not English.
    expect(review).toContain(ISLAND[lang].frequency_names.weekly);
    expect(review).toContain(ISLAND[lang].weekday_names[3]);
    expect(review).not.toContain(ISLAND.en.frequency_names.weekly);
    expect(review).not.toContain(ISLAND.en.weekday_names[3]);
  });

  it('the suggested subscription name is built from the page language', async () => {
    const { builder } = await builderPage({ lang, saved });
    builder.selectedProducts = new Map([
      [1, { product: { id: 1, product_name: 'A' }, quantity: 1 }],
      [2, { product: { id: 2, product_name: 'B' }, quantity: 1 }],
      [3, { product: { id: 3, product_name: 'C' }, quantity: 1 }],
    ]);

    expect(builder.generateSubscriptionName()).toBe(
      `${ISLAND[lang].frequency_names.weekly} - A, B ${words.name_more.replace('{count}', '1')} - ` +
      `${words.name_to.replace('{place}', 'Home')}`,
    );
    builder.selectedProducts = new Map();
    builder.selectedFrequency = null;
    builder.selectedAddress = null;
    expect(builder.generateSubscriptionName()).toBe(words.name_default);
  });
});

describe('the builder never invents what the backend did not answer', () => {
  // submitSubscription: only a backend answer's sentence is shown as it is; every thrown error (offline, a 5xx
  // HTML page, an expired session) shows the page-language create_failed word, never English or technical text.
  describe.each(['uz', 'ru'])('create errors in %s', (lang) => {
    const failures = {
      'a network failure': () => { throw new TypeError('Failed to fetch'); },
      'a 5xx HTML page': () => ({ ok: false, json: async () => { throw new SyntaxError("Unexpected token '<'"); } }),
      'an expired session': () => { throw new Error('Session expired. Redirecting to login...'); },
    };
    it.each(Object.keys(failures))('%s shows create_failed', async (name) => {
      const { builder, errors } = await builderPage({
        lang,
        answer: (url) => (url === '/subscriptions/' ? failures[name]() : undefined),
      });
      builder.currentStep = 5;
      builder.selectedFrequency = 'daily';
      builder.selectedTimeSlot = 'any';
      builder.selectedProducts = new Map([[1, { product: { id: 1, product_name: 'A' }, quantity: 1 }]]);
      await builder.submitSubscription();
      expect(errors).toEqual([ISLAND[lang].i18n.error_create_failed]);
    });

    it('a backend answer with a message shows that message, and one without shows create_failed', async () => {
      const answers = [{ success: false, message: 'Sizning tilingizda xabar' }, { success: false }];
      for (const body of answers) {
        const { builder, errors } = await builderPage({
          lang,
          answer: (url) => (url === '/subscriptions/' ? { ok: false, json: async () => body } : undefined),
        });
        builder.currentStep = 5;
        builder.selectedFrequency = 'daily';
        builder.selectedTimeSlot = 'any';
        builder.selectedProducts = new Map([[1, { product: { id: 1, product_name: 'A' }, quantity: 1 }]]);
        await builder.submitSubscription();
        expect(errors).toEqual([body.message || ISLAND[lang].i18n.error_create_failed]);
      }
    });
  });

  it('a failed payment menu says so and offers no cash of its own', async () => {
    const { builder, errors, $1 } = await builderPage({
      answer: (url) => (url.startsWith('/payments/methods') ? { ok: false, json: async () => ({ success: false }) } : undefined),
    });

    await builder.loadPaymentMethods();

    expect(builder.paymentMethods).toEqual([]);
    expect(errors).toEqual([WORDS.i18n.error_methods_failed]);
    expect($1('#payment-methods-grid').children).toHaveLength(0);
  });

  it('the review names the address by its title and full address, and posts no description', async () => {
    const { builder, $1 } = await builderPage();
    builder.selectedAddress = { id: 11, title: 'Office', full_address: 'Navoi 5', apartment_number: '7' };
    builder.selectedFrequency = 'daily';
    builder.selectedTimeSlot = 'any';
    builder.renderReview();

    const review = $1('#review-container').textContent;
    expect(review).toContain('Office');
    expect(review).toContain('Navoi 5');
    expect(review).toContain(WORDS.i18n.address_apartment.replace('{number}', '7'));
    expect(builder.buildSubscriptionBody('x')).not.toHaveProperty('description');
  });
});

// Re-review G9: switching frequency forgets the day the new frequency does not use, and the screen must forget it
// too, the theme's nice-select included, so it never shows a day the builder no longer holds.
describe('switching frequency clears the day controls the builder forgot', () => {
  it('monthly 15, then weekly, then monthly again: no day is shown and none is held', async () => {
    const { builder, click, $1 } = await builderPage();
    await click($1('.frequency-option[data-frequency="monthly"]'));
    await click($1('#month-day-selector .nice-select .option[data-value="15"]'));
    expect(builder.selectedDayOfMonth).toBe(15);
    expect($1('#month-day-selector .nice-select .current').textContent).toBe('15');

    await click($1('.frequency-option[data-frequency="weekly"]'));
    await click($1('.frequency-option[data-frequency="monthly"]'));

    expect(builder.selectedDayOfMonth).toBeNull();
    expect($1('#month-day-select').value).toBe('');
    expect($1('#month-day-selector .nice-select .current').textContent).toBe('Pick a day');
    expect($1('#month-day-selector .nice-select .option.selected').dataset.value).toBe('');
  });

  it('weekly Thursday, then monthly: no weekday button stays highlighted', async () => {
    const { builder, click, $1 } = await builderPage();
    await click($1('.frequency-option[data-frequency="weekly"]'));
    await click($1('.day-btn[data-day="4"]'));
    expect(builder.selectedWeekday).toBe(3);

    await click($1('.frequency-option[data-frequency="monthly"]'));

    expect(builder.selectedWeekday).toBeNull();
    expect($1('#day-selector').querySelectorAll('.day-btn.selected')).toHaveLength(0);
  });

  it('weekly to biweekly keeps the weekday and its button, both use it', async () => {
    const { builder, click, $1 } = await builderPage();
    await click($1('.frequency-option[data-frequency="weekly"]'));
    await click($1('.day-btn[data-day="4"]'));

    await click($1('.frequency-option[data-frequency="biweekly"]'));

    expect(builder.selectedWeekday).toBe(3);
    expect($1('.day-btn.selected').dataset.day).toBe('4');
  });
});

// Re-review G6 (spec 2026-10-26 B10): every figure the builder shows is one the backend published for the
// subscription: the card's price from the subscription catalog, the review's lines, per-delivery and monthly
// amounts from the create preview. QUOTES is the real answers for a product on sale (base 8 000, subscription
// price 6 000), written by tests/integration/test_web_subscription_builder_contract.py.
describe('the builder shows the subscription prices the backend publishes', () => {
  const [ROW] = QUOTES.catalog.response.data.items;
  const QUOTE = QUOTES.preview.response.data.preview;

  it('lists the subscription catalog, asked without cookies in the page language, every row at its label', async () => {
    const { builder, requests, $1 } = await builderPage({ lang: 'ru' });
    await builder.loadProducts();

    expect(requests).toEqual([{
      url: '/api/v1/subscriptions/products',
      options: { method: 'GET', headers: { 'Accept-Language': 'ru' }, credentials: 'omit' },
    }]);
    const card = $1(`.product-card[data-product-id="${ROW.id}"]`);
    expect(card.querySelector('.product-name').textContent).toBe(ROW.product_name);
    expect(card.querySelector('.product-price').textContent).toBe(ROW.unit_price_label);
    expect(card.querySelector('.quantity-display').textContent).toBe(String(ROW.min_order_quantity));
  });

  it('prices the review from the create preview: each line, per delivery and the month', async () => {
    const { builder, requests, click, $1 } = await builderPage();
    await builder.loadProducts();
    await click($1(`.product-card[data-product-id="${ROW.id}"]`));
    builder.selectedFrequency = 'weekly';
    builder.selectedWeekday = 3;

    await builder.loadPreview();
    builder.renderReview();

    expect(JSON.parse(requests.at(-1).options.body)).toEqual(QUOTES.preview.body);
    const [line] = QUOTE.items;
    expect($1('[data-review="line-price"]').textContent.trim()).toBe(line.unit_price_label);
    expect($1('[data-review="line-total"]').textContent.trim()).toBe(line.line_total_label);
    expect($1('[data-review="per-delivery"]').textContent.trim()).toBe(QUOTE.total_amount_label);
    expect($1('[data-review="per-month"]').textContent.trim()).toBe(QUOTE.estimated_monthly_cost_label);
  });

  it('the source holds no price of its own and no sum', () => {
    expect(SOURCE).not.toMatch(/base_price|pricing\b|price \* quantity|subtotal|\/products\?/);
  });
});

// Re-review web-builder-1 / tests-G2: "no slot offered" is the backend's answer for THIS schedule, an empty list it
// sent. A list that failed, has not arrived yet, or answers an older question is not that answer: step 2 must not
// treat it as "none offered" and let the customer through without the slot they never declined.
describe('only a slot list the backend answered means "no slot offered"', () => {
  const failing = (url) => (url.startsWith('/delivery/time-slots') ? { ok: false, json: async () => ({ success: false }) } : undefined);

  it('a failed slot list keeps step 2, says so, asks again, and never reads "Time to be confirmed"', async () => {
    const { builder, requests, errors, click, $1 } = await builderPage({ answer: failing });
    await click($1('.frequency-option[data-frequency="monthly"]'));
    await click($1('#month-day-selector .nice-select .option[data-value="15"]'));
    const asked = requests.filter((r) => r.url.startsWith('/delivery/time-slots')).length;

    expect(builder.validateStep(2)).toBe(false);

    expect(errors.at(-1)).toBe(WORDS.i18n.error_slots_failed);
    expect(requests.filter((r) => r.url.startsWith('/delivery/time-slots'))).toHaveLength(asked + 1);
    expect([...$1('#time-slots-select').options].map((o) => o.textContent.trim())).toEqual(['Select a time slot']);
  });

  it('a chosen slot the failed list cannot name is kept, and the review never calls it "to be confirmed"', async () => {
    const { builder, $1 } = await builderPage({ answer: failing });
    Object.assign(builder, { selectedFrequency: 'weekly', selectedWeekday: 3, selectedTimeSlot: 22 });

    await builder.loadTimeSlots();
    builder.renderReview();

    expect(builder.selectedTimeSlot).toBe(22);
    expect($1('[data-review="time-slot"]').textContent.trim()).toBe('');
    expect(builder.validateStep(2)).toBe(false);
  });

  it('a list still on its way is not "none offered", and an answer to an older question is dropped', async () => {
    const pending = [];
    const deferred = (url) => {
      if (!url.startsWith('/delivery/time-slots')) return undefined;
      return new Promise((resolve) => { pending.push({ url, resolve }); });
    };
    const { builder, window, $1 } = await builderPage({ answer: deferred });
    const reply = (slots) => ({ ok: true, json: async () => ({ time_slots: slots }) });
    $1('.frequency-option[data-frequency="weekly"]').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    $1('.day-btn[data-day="4"]').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    expect(pending.map((p) => p.url)).toEqual([
      '/delivery/time-slots?weekday=any&frequency=weekly', '/delivery/time-slots?weekday=3&frequency=weekly',
    ]);

    expect(builder.validateStep(2)).toBe(false);  // Thursday's list has not arrived: stay, and wait for it
    expect(pending).toHaveLength(2);

    pending[1].resolve(reply(SLOTS));
    pending[0].resolve(reply([]));  // the older "any weekday" answer arrives last
    await new Promise((resolve) => { setTimeout(resolve, 0); });
    expect(builder.timeSlots.map((slot) => slot.id)).toEqual([22]);
    expect(builder.validateStep(2)).toBe(false);  // a slot is offered, none chosen
    builder.selectedTimeSlot = 22;
    expect(builder.validateStep(2)).toBe(true);
  });

  it('the slot list placeholder is translated and never calls a required slot optional', async () => {
    const { click, $1 } = await builderPage();
    await click($1('.frequency-option[data-frequency="weekly"]'));
    await click($1('.day-btn[data-day="4"]'));

    expect([...$1('#time-slots-select').options].map((o) => o.textContent.trim()))
      .toEqual(['Select a time slot', 'Any time', '10:00 - 12:00']);
    expect(SOURCE).not.toMatch(/slot \(optional\)/i);
  });
});

// Re-review web-builder-2 / tests-G6: a visitor coming back to the page (from sign-in, or a reload) gets this tab's
// saved builder back. The schedule controls show it, its slot list is asked, the restored step loads its data, and
// the basket's rows are the subscription catalog's current ones.
describe('a builder restored from this tab', () => {
  const [ROW] = QUOTES.catalog.response.data.items;
  const OLD_ROW = { id: ROW.id, name: 'Old name', pricing: { base_price: 8000 }, inventory: { min_order_quantity: 1 } };
  const DELISTED = { id: 999, name: 'Gone', pricing: { base_price: 1000 } };
  const weeklyThursday = {
    currentStep: 3, selectedFrequency: 'weekly', selectedWeekday: 3, selectedDayOfMonth: null, selectedTimeSlot: 22,
    selectedAddress: ADDRESS, selectedPaymentMethod: 'cash', subscriptionName: '',
    selectedProducts: [[ROW.id, { product: OLD_ROW, quantity: 2 }], [DELISTED.id, { product: DELISTED, quantity: 1 }]],
  };

  it('holds the catalog row of each basket product and drops one the catalog no longer lists', async () => {
    const { builder } = await builderPage({ saved: weeklyThursday });

    expect([...builder.selectedProducts.keys()]).toEqual([ROW.id]);
    const { product, quantity } = builder.selectedProducts.get(ROW.id);
    expect(product).toEqual(ROW);
    expect([product.product_name, product.unit_price_label, quantity]).toEqual([ROW.product_name, ROW.unit_price_label, 2]);
  });

  it('shows the restored schedule on step 2 and asks its slot list', async () => {
    const { builder, requests, window, $1 } = await builderPage({ saved: { ...weeklyThursday, currentStep: 2, selectedTimeSlot: null } });

    expect([...window.document.querySelectorAll('.frequency-option.selected')].map((el) => el.dataset.frequency)).toEqual(['weekly']);
    expect($1('#day-selector').style.display).toBe('block');
    expect($1('#month-day-selector').style.display).toBe('none');
    expect([...window.document.querySelectorAll('.day-btn.selected')].map((el) => el.dataset.day)).toEqual(['4']);
    expect(requests.map((r) => r.url)).toContain('/delivery/time-slots?weekday=3&frequency=weekly');

    expect(builder.validateStep(2)).toBe(false);  // Thursday offers a slot, and none is chosen yet
  });

  it('the review names the restored slot, the one the POST sends', async () => {
    const { builder, $1 } = await builderPage({ saved: weeklyThursday });

    await builder.nextStep();
    await builder.nextStep();

    expect(builder.currentStep).toBe(5);
    expect($1('[data-review="time-slot"]').textContent.trim()).toBe('10:00 - 12:00');
    expect(builder.buildSubscriptionBody('x').delivery_time_slot_id).toBe(22);
  });

  it('a reload on the review names the method the POST sends, after loading the steps before it', async () => {
    const menu = { data: { available_methods: [{ method: 'cash', display_name: 'Cash' }, { method: 'click', display_name: 'Card' }] } };
    const { builder, requests, $1 } = await builderPage({
      saved: { ...weeklyThursday, currentStep: 5, selectedPaymentMethod: 'click' },
      answer: (url) => (url.startsWith('/payments/methods') ? { ok: true, json: async () => menu } : undefined),
    });

    const asked = requests.map((r) => r.url).filter((url) => !url.startsWith('/delivery/time-slots'));
    expect(asked.slice(-3)).toEqual([
      '/auth/addresses',
      `/payments/methods?context=subscription&delivery_address_id=${ADDRESS.id}`,
      '/subscriptions/preview',
    ]);
    const review = $1('#review-container').textContent;
    expect(review).toContain('Card');
    expect(review).not.toContain('Cash on Delivery');
    expect(builder.buildSubscriptionBody('x').payment_method).toBe('click');
  });

  it('the review never names cash for a method the menu does not list', async () => {
    const { $1 } = await builderPage({ saved: { ...weeklyThursday, currentStep: 5, selectedPaymentMethod: 'click' } });

    expect($1('#review-container').textContent).not.toContain('Cash on Delivery');
  });

  it('a restored monthly day is shown in the nice-select, and choosing monthly again keeps it shown', async () => {
    const { builder, click, $1 } = await builderPage({
      saved: { ...weeklyThursday, currentStep: 2, selectedFrequency: 'monthly', selectedWeekday: null, selectedDayOfMonth: 15, selectedTimeSlot: null },
    });
    expect($1('#month-day-selector').style.display).toBe('block');
    expect($1('#month-day-selector .nice-select .current').textContent).toBe('15');

    await click($1('.frequency-option[data-frequency="monthly"]'));

    expect(builder.selectedDayOfMonth).toBe(15);
    expect($1('#month-day-select').value).toBe('15');
    expect($1('#month-day-selector .nice-select .current').textContent).toBe('15');
  });
});
