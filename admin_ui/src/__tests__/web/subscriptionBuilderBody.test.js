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

// A figure no local multiplier (30, 4, 2, 1) would produce.
const PREVIEW = { deliveries_per_month: 4.333333, estimated_monthly_cost: 130000 };
const SLOTS = [{ id: 22, name: 'Morning', start_time: '10:00', end_time: '12:00', time_range: '10:00-12:00' }];

const load = () => {
  const requests = [];
  const sandbox = {
    console: { error() {}, log() {}, warn() {} },
    CURRENT_USER: { id: 1 },
    API_BASE_URL: '/api/v1',
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
      return { ok: true, json: async () => ({ time_slots: SLOTS }) };
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
      // the slots offered that weekday (backend convention), not the JS number
      expect(requests.at(-1).url).toBe(`/delivery/time-slots?weekday=${c.expected_weekday}`);
    }
    if (c.day_of_month !== undefined) builder.selectedDayOfMonth = c.day_of_month;
    builder.selectedTimeSlot = FIXTURE.ids.slot;

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

  it('asks the backend for every active slot when no weekday applies', async () => {
    const { builder, requests } = load();
    await builder.selectFrequency({ dataset: { frequency: 'monthly' }, classList: { add() {} } });
    expect(requests.at(-1).url).toBe('/delivery/time-slots?weekday=any');
  });

  it('drops a chosen slot the new weekday does not offer', async () => {
    const { builder } = load();
    builder.selectedTimeSlot = 999;
    await builder.selectFrequency({ dataset: { frequency: 'daily' }, classList: { add() {} } });
    expect(builder.selectedTimeSlot).toBeNull();
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
    expect(builder.formatDeliveriesPerMonth(builder.preview.deliveries_per_month)).toBe('4.3');
    expect(builder.formatDeliveriesPerMonth(1)).toBe('1');
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
