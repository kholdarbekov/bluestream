// coverage-map.js on /coverage (website redesign step 3, group "info", 2026-11-14).
//
// The SHIPPED script runs in jsdom over the hooks coverage.html prints (tests/integration/test_web_info_contact.py
// pins the template side). Leaflet is a small stand-in that records what the script asks of the map; `fetch` answers
// with the payloads GET /api/public/check-delivery returns. What is pinned:
// - every outcome shows exactly one server-rendered panel, and the script never writes text of its own (it used to
//   write an English "Could not locate that address" over the translated "out of zone" line, which then stuck);
// - a geocoder miss is "not found", a failed request (429, offline) is "error", never "out of zone";
// - "my location" without geolocation, or refused, says so instead of doing nothing;
// - a dropped pin and the chips work, and without Leaflet the address check still works.
// Since the review (fix-info, 2026-11-14):
// - the page never decides the zone: a dropped pin and "my location" ask the endpoint (?lat=&lng=), a burst of taps
//   sends one request, and only the newest answer shows (the script had its own point-in-polygon);
// - on a phone the answer is scrolled into view, and a chip brings the map into view before moving it;
// - an address of spaces only shows a translated line under the field and drops the earlier answer;
// - the district dots leave the Tab order; on touch screens one finger scrolls the page (dragging off).
import fs from 'node:fs';
import path from 'node:path';
import { JSDOM } from 'jsdom';

const REPO = path.resolve(process.cwd(), '..');
// eslint-disable-next-line security/detect-non-literal-fs-filename
const SCRIPT = fs.readFileSync(path.join(REPO, 'business_app/static/js/coverage-map.js'), 'utf8');
const PANELS = ['checking', 'ok', 'no', 'notfound', 'error', 'geo'];
const DATA = {
  center: { latitude: 41.5, longitude: 69.5 },
  polygon: [[41, 69], [41, 70], [42, 70], [42, 69]],
  districts: [{ key: 'chilanzar', name: 'Чиланзар', center: [41.4, 69.4] }, { key: 'sergeli', name: 'Сергели', center: [41.2, 69.2] }],
  checkUrl: '/api/public/check-delivery',
};
const BODY = `
  <script type="application/json" id="coverage-data">${JSON.stringify(DATA)}</script>
  <div id="coverage-map"></div>
  <div><span data-map-hint="pointer">Нажмите на карту</span><span data-map-hint="touch" hidden>Двумя пальцами</span></div>
  <form id="coverage-form">
    <label for="coverage-address">Адрес</label><input id="coverage-address" required>
    <p id="coverage-address-error" hidden>Введите улицу и номер дома</p>
    <button id="coverage-check-btn" type="submit">Проверить</button>
    <button id="coverage-locate-btn" type="button">Моё местоположение</button>
  </form>
  <div class="info-cov__results" aria-live="polite">
    ${PANELS.map((p) => `<div id="coverage-result-${p}" hidden>panel ${p}</div>`).join('')}
  </div>
  <ul>${DATA.districts.map((d) => `<li><button type="button" data-district="${d.key}" aria-pressed="false">${d.name}</button></li>`).join('')}</ul>`;

function leaflet(log, window, { mobile = false } = {}) {
  const layer = (extra = {}) => ({
    // Like Leaflet, a layer fires 'add' once it is on the map (its SVG path exists only from then on).
    addTo() { if (this.on_add) this.on_add(); return this; }, bindTooltip() { return this; }, on(ev, fn) { this[`on_${ev}`] = fn; return this; }, ...extra,
  });
  const map = {
    options: null,
    handlers: {},
    on(ev, fn) { this.handlers[ev] = fn; return this; },
    fitBounds() {},
    setView(latlng, zoom) { log.push(['view', latlng, zoom]); },
    flyTo(latlng, zoom) { log.push(['view', latlng, zoom]); },
  };
  const dots = [];
  return {
    map: (_id, options) => { map.options = options; return map; },
    Browser: { mobile },
    Icon: { Default: {} },
    tileLayer: () => layer(),
    polygon: () => layer({ getBounds: () => ({}) }),
    circleMarker: (center, opts) => {
      const el = window.document.createElementNS('http://www.w3.org/2000/svg', 'path');
      dots.push(el);
      return layer({ getElement: () => el, getLatLng: () => center, openTooltip: () => log.push(['tooltip', center, opts.fillColor]) });
    },
    dots,
    marker: (latlng) => { log.push(['pin', latlng]); return layer({ setLatLng: (ll) => log.push(['pin', ll]) }); },
    theMap: map,
  };
}

function boot({ answers = [], withLeaflet = true, geolocation, mobile = false, offscreen = [] } = {}) {
  const dom = new JSDOM(`<!doctype html><html><body>${BODY}</body></html>`, { url: 'http://localhost/coverage?lang=ru', runScripts: 'outside-only' });
  const { window } = dom;
  const log = [];
  // Layout: jsdom has none. Elements listed in `offscreen` report a box below an 844px viewport; scrolling is logged.
  Object.defineProperty(window, 'innerHeight', { value: 844, configurable: true });
  offscreen.forEach((id) => {
    window.document.getElementById(id).getBoundingClientRect = () => ({ top: 1080, bottom: 1180, left: 0, right: 390, width: 390, height: 100 });
  });
  window.Element.prototype.scrollIntoView = function scrollIntoView(opts) { log.push(['scroll', this.id, opts.block]); };
  const requests = [];
  window.fetch = async (url) => {
    requests.push(url);
    const answer = answers[requests.length - 1];
    if (!answer || answer.offline) throw new window.TypeError('Failed to fetch');
    return new Response(JSON.stringify(answer.body || {}), { status: answer.status || 200, headers: { 'Content-Type': 'application/json' } });
  };
  Object.defineProperty(window.navigator, 'geolocation', { value: geolocation, configurable: true });
  if (withLeaflet) window.L = leaflet(log, window, { mobile });
  window.eval(SCRIPT);
  return { window, document: window.document, log, requests };
}

const flush = async () => { for (let i = 0; i < 5; i += 1) await new Promise((r) => { setTimeout(r, 0); }); };
// A tap on the map asks after TAP_DELAY (350 ms), so a burst of taps sends one request.
const afterTap = async () => { await new Promise((r) => { setTimeout(r, 400); }); await flush(); };
const tap = (page, lat, lng) => page.window.L.theMap.handlers.click({ latlng: { lat, lng } });
const shown = (document) => PANELS.filter((p) => !document.getElementById(`coverage-result-${p}`).hidden);
const panelTexts = (document) => PANELS.map((p) => document.getElementById(`coverage-result-${p}`).textContent);

async function check(page, address) {
  page.document.getElementById('coverage-address').value = address;
  page.document.getElementById('coverage-form').dispatchEvent(new page.window.Event('submit', { bubbles: true, cancelable: true }));
  await flush();
}

describe('the address check', () => {
  it('asks the public endpoint and shows "we deliver here" with the pin on the answer', async () => {
    const page = boot({ answers: [{ body: { latitude: 41.31, longitude: 69.28, is_deliverable: true } }] });
    const before = panelTexts(page.document);
    await check(page, 'улица Амира Темура, 12');
    expect(page.requests).toEqual(['/api/public/check-delivery?address=%D1%83%D0%BB%D0%B8%D1%86%D0%B0%20%D0%90%D0%BC%D0%B8%D1%80%D0%B0%20%D0%A2%D0%B5%D0%BC%D1%83%D1%80%D0%B0%2C%2012']);
    expect(shown(page.document)).toEqual(['ok']);
    expect(page.log).toContainEqual(['pin', [41.31, 69.28]]);
    expect(page.document.getElementById('coverage-check-btn').disabled).toBe(false);
    expect(panelTexts(page.document)).toEqual(before);
  });

  it('shows "outside the zone" when the endpoint says so', async () => {
    const page = boot({ answers: [{ body: { latitude: 40.9, longitude: 69.9, is_deliverable: false } }] });
    await check(page, 'Far away');
    expect(shown(page.document)).toEqual(['no']);
  });

  it('shows "not found" for a geocoder miss and never rewrites a panel', async () => {
    const page = boot({ answers: [
      { body: { latitude: null, longitude: null, is_deliverable: null, reason: 'not_geocoded' } },
      { body: { latitude: 40.9, longitude: 69.9, is_deliverable: false } },
    ] });
    const before = panelTexts(page.document);
    await check(page, 'qwerty');
    expect(shown(page.document)).toEqual(['notfound']);
    await check(page, 'Far away');
    expect(shown(page.document)).toEqual(['no']);
    expect(panelTexts(page.document)).toEqual(before);
  });

  it.each([
    ['rate limited', { status: 429, body: { error: 'Too many requests' } }],
    ['server error', { status: 500, body: {} }],
    ['offline', { offline: true }],
  ])('shows "error", not "outside the zone", when the request fails (%s)', async (_name, answer) => {
    const page = boot({ answers: [answer] });
    await check(page, 'улица Амира Темура, 12');
    expect(shown(page.document)).toEqual(['error']);
    expect(page.document.getElementById('coverage-check-btn').disabled).toBe(false);
  });

  it('sends nothing for an address of spaces, says what is missing and drops the earlier answer', async () => {
    const page = boot({ answers: [{ body: { latitude: 41.31, longitude: 69.28, is_deliverable: true } }] });
    await check(page, 'улица Амира Темура, 12');
    expect(shown(page.document)).toEqual(['ok']);
    const input = page.document.getElementById('coverage-address');
    const error = page.document.getElementById('coverage-address-error');
    expect(error.hidden).toBe(true);
    await check(page, '   ');
    expect(page.requests).toHaveLength(1);
    expect(shown(page.document)).toEqual([]);
    expect(error.hidden).toBe(false);
    expect(error.textContent).toBe('Введите улицу и номер дома');
    expect(input.getAttribute('aria-invalid')).toBe('true');
    expect(input.getAttribute('aria-describedby')).toBe('coverage-address-error');
    expect(page.document.activeElement).toBe(input);
    input.value = 'Чиланзар';
    input.dispatchEvent(new page.window.Event('input', { bubbles: true }));
    expect(error.hidden).toBe(true);
    expect(input.hasAttribute('aria-invalid')).toBe(false);
  });

  it('scrolls the answer into view when it is below the screen', async () => {
    const page = boot({ answers: [{ body: { latitude: 41.31, longitude: 69.28, is_deliverable: true } }], offscreen: ['coverage-result-ok'] });
    await check(page, 'улица Амира Темура, 12');
    expect(page.log).toContainEqual(['scroll', 'coverage-result-ok', 'nearest']);
  });

  it('leaves the page where it is when the answer is already in view', async () => {
    const page = boot({ answers: [{ body: { latitude: 41.31, longitude: 69.28, is_deliverable: true } }] });
    await check(page, 'улица Амира Темура, 12');
    expect(page.log.filter(([kind]) => kind === 'scroll')).toEqual([]);
  });

  it('still checks an address when Leaflet did not load', async () => {
    const page = boot({ withLeaflet: false, answers: [{ body: { latitude: 41.31, longitude: 69.28, is_deliverable: true } }] });
    await check(page, 'улица Амира Темура, 12');
    expect(shown(page.document)).toEqual(['ok']);
  });
});

describe('the map', () => {
  it('asks the endpoint about a dropped pin and shows its answer, not its own', async () => {
    // Inside the page's polygon, but the endpoint (the one rule) says no: the page shows "no".
    const page = boot({ answers: [{ body: { latitude: 41.5, longitude: 69.5, is_deliverable: false } }] });
    tap(page, 41.5, 69.5);
    expect(page.log).toContainEqual(['pin', [41.5, 69.5]]);
    expect(shown(page.document)).toEqual(['checking']);
    await afterTap();
    expect(page.requests).toEqual(['/api/public/check-delivery?lat=41.5&lng=69.5']);
    expect(shown(page.document)).toEqual(['no']);
  });

  it('sends one request for a burst of taps, for the last pin', async () => {
    const page = boot({ answers: [{ body: { latitude: 41.3, longitude: 69.3, is_deliverable: true } }] });
    tap(page, 41.1, 69.1);
    tap(page, 41.2, 69.2);
    tap(page, 41.3, 69.3);
    await afterTap();
    expect(page.requests).toEqual(['/api/public/check-delivery?lat=41.3&lng=69.3']);
    expect(shown(page.document)).toEqual(['ok']);
  });

  it('shows "error" when the pin check fails', async () => {
    const page = boot({ answers: [{ status: 429, body: {} }] });
    tap(page, 41.3, 69.3);
    await afterTap();
    expect(shown(page.document)).toEqual(['error']);
  });

  it('scrolls the answer to a tap into view on a phone, where it sits a screen below the map', async () => {
    const page = boot({ answers: [{ body: { latitude: 41.3, longitude: 69.3, is_deliverable: true } }], offscreen: ['coverage-result-ok'] });
    tap(page, 41.3, 69.3);
    await afterTap();
    expect(page.log).toContainEqual(['scroll', 'coverage-result-ok', 'nearest']);
  });

  it('moves to a district and opens its label when its chip is pressed', () => {
    const page = boot();
    page.document.querySelector('[data-district="chilanzar"]').click();
    const pressed = [...page.document.querySelectorAll('[data-district]')].map((b) => [b.dataset.district, b.getAttribute('aria-pressed')]);
    expect(pressed).toEqual([['chilanzar', 'true'], ['sergeli', 'false']]);
    expect(page.log).toContainEqual(['view', [41.4, 69.4], 13]);
    expect(page.log).toContainEqual(['tooltip', [41.4, 69.4], '#002C8F']);
    expect(page.log.filter(([kind]) => kind === 'scroll')).toEqual([]);
  });

  it('brings the map into view before moving it when a chip is pressed below it', () => {
    const page = boot({ offscreen: ['coverage-map'] });
    page.document.querySelector('[data-district="sergeli"]').click();
    const scroll = page.log.findIndex(([kind]) => kind === 'scroll');
    const view = page.log.findIndex(([kind]) => kind === 'view');
    expect(page.log[scroll]).toEqual(['scroll', 'coverage-map', 'nearest']);
    expect(scroll).toBeLessThan(view);
  });

  it('keeps the district dots out of the Tab order (the chips are the keyboard path)', () => {
    const page = boot();
    expect(page.window.L.dots).toHaveLength(2);
    page.window.L.dots.forEach((el) => expect(el.getAttribute('tabindex')).toBe('-1'));
  });

  it('lets one finger scroll the page on a phone and shows the two-finger hint', () => {
    const phone = boot({ mobile: true });
    expect(phone.window.L.theMap.options.dragging).toBe(false);
    const hints = (page) => [...page.document.querySelectorAll('[data-map-hint]')].map((h) => [h.dataset.mapHint, h.hidden]);
    expect(hints(phone)).toEqual([['pointer', true], ['touch', false]]);
    const desktop = boot();
    expect(desktop.window.L.theMap.options.dragging).toBe(true);
    expect(hints(desktop)).toEqual([['pointer', false], ['touch', true]]);
  });
});

describe('my location', () => {
  it('says the location is unavailable when the browser has no geolocation', () => {
    const page = boot({ geolocation: undefined });
    page.document.getElementById('coverage-locate-btn').click();
    expect(shown(page.document)).toEqual(['geo']);
  });

  it('says so when the visitor refuses, and re-enables the button', () => {
    const page = boot({ geolocation: { getCurrentPosition: (_ok, fail) => fail({ code: 1 }) } });
    page.document.getElementById('coverage-locate-btn').click();
    expect(shown(page.document)).toEqual(['geo']);
    expect(page.document.getElementById('coverage-locate-btn').disabled).toBe(false);
  });

  it('asks the endpoint about the position', async () => {
    const page = boot({
      answers: [{ body: { latitude: 41.5, longitude: 69.5, is_deliverable: true } }],
      geolocation: { getCurrentPosition: (ok) => ok({ coords: { latitude: 41.5, longitude: 69.5 } }) },
    });
    page.document.getElementById('coverage-locate-btn').click();
    expect(page.log).toContainEqual(['pin', [41.5, 69.5]]);
    await flush();
    expect(page.requests).toEqual(['/api/public/check-delivery?lat=41.5&lng=69.5']);
    expect(shown(page.document)).toEqual(['ok']);
    expect(page.document.getElementById('coverage-locate-btn').disabled).toBe(false);
  });
});
