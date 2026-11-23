// /addresses and the address wizard (website redesign 2026-11-14, group "checkout", unit "addresses").
//
// The SHIPPED scripts run in jsdom as the page loads them: jquery, popper, bootstrap (the modals), _page-data.js,
// base.js (its real apiRequest), map-helper.js, address-wizard.js and pages/addresses.js, over the markup and data
// islands tests/integration/test_web_checkout_addresses.py captured from the rendered page (shell.html). Only
// `fetch` is replaced. The bodies the wizard must send are the fixtures that integration test replays through the
// real endpoints (tests/fixtures/web_addresses/*_payload.json). Leaflet never loads here (jsdom fetches no
// scripts), so a point is chosen through the search box, which goes through the same pin path as a map tap.
import fs from 'node:fs';
import path from 'node:path';
import { JSDOM } from 'jsdom';

const REPO = path.resolve(process.cwd(), '..');
// Repo files named in this test (never user input).
// eslint-disable-next-line security/detect-non-literal-fs-filename
const read = (file) => fs.readFileSync(path.join(REPO, file), 'utf8');
const SCRIPTS = [
  'business_app/static/js/jquery.js',
  'business_app/static/js/popper.min.js',
  'business_app/static/js/bootstrap.min.js',
  'business_app/static/js/pages/_page-data.js',
  'business_app/static/js/pages/base.js',
  'business_app/static/js/map-helper.js',
  'business_app/static/js/address-wizard.js',
  'business_app/static/js/pages/addresses.js',
].map(read);
const SHELL = read('tests/fixtures/web_addresses/shell.html');
const NEW = JSON.parse(read('tests/fixtures/web_addresses/new_payload.json'));
const EDIT = JSON.parse(read('tests/fixtures/web_addresses/edit_payload.json'));

const GEO = {
  center: { latitude: 41.2995, longitude: 69.2401 },
  polygon: [[41.0, 69.0], [41.0, 69.5], [41.5, 69.5], [41.5, 69.0]],
  districts: [
    { key: 'chilanzar', name: 'Чиланзар' },
    { key: 'shaykhontohur', name: 'Шайхантаур' },
    { key: 'yunusabad', name: 'Юнусабад' },
  ],
};
const MAIN = {
  id: 16, title: 'Try-out', full_address: 'Toshkent, Chilonzor tumani', street_address: null, district: 'chilanzar',
  floor_number: null, apartment_number: null, landmark: null, delivery_instructions: null,
  latitude: 41.27, longitude: 69.21, is_default: true, is_business: false,
};
const BRANCH = {
  id: 67, title: 'Drive branch 3', full_address: EDIT.full_address, street_address: null, district: 'shaykhontohur',
  floor_number: '3', apartment_number: null, landmark: 'old', delivery_instructions: null,
  latitude: EDIT.latitude, longitude: EDIT.longitude, is_default: false, is_business: false,
};
const LIST = 'GET /api/v1/auth/addresses';
const ok = (data, status = 200) => ({ status, body: { success: true, data } });

async function settle(window, rounds = 30) {
  for (let i = 0; i < rounds; i += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, 0));
  }
}

const escapeJson = (value) => JSON.stringify(value).replace(/</g, '\\u003c');

/** `answers` maps "METHOD /api/v1/path" to an answer or a queue of answers; every request is recorded. */
async function boot(answers, { leaflet = null } = {}) {
  const base = { api_base_url: '/api/v1', current_user: null, is_authenticated: true, login_url: '/login', language: 'ru', i18n: {} };
  const dom = new JSDOM(
    '<!doctype html><html lang="ru"><body>' +
    `<script type="application/json" id="base-page-data">${escapeJson(base)}</script>` +
    `<div id="toast-container"></div>${SHELL}</body></html>`,
    { url: 'http://localhost/addresses', runScripts: 'outside-only', pretendToBeVisual: true },
  );
  const { window } = dom;
  window.document.cookie = 'csrf_access_token=csrf-cookie-value';
  const requests = [];
  window.fetch = (url, options = {}) => {
    const method = (options.method || 'GET').toUpperCase();
    const key = `${method} ${url.split('?')[0]}`;
    requests.push({ key, url, body: options.body ? JSON.parse(options.body) : undefined });
    const queue = answers[key];
    const answer = Array.isArray(queue) ? (queue.length > 1 ? queue.shift() : queue[0]) : queue;
    if (!answer) return Promise.resolve({ ok: false, status: 599, json: async () => ({ success: false }) });
    return Promise.resolve({ ok: answer.status < 400, status: answer.status, json: async () => JSON.parse(JSON.stringify(answer.body)) });
  };
  SCRIPTS.forEach((source) => window.eval(source));
  // A fake Leaflet through the loader the wizard asks, so a map tap and a marker drag can be driven.
  if (leaflet) window.AEMap.load = () => Promise.resolve(leaflet);
  if (window.document.readyState !== 'loading') window.document.dispatchEvent(new window.Event('DOMContentLoaded'));
  await settle(window);
  const document = window.document;
  const $1 = (selector) => document.querySelector(selector);
  const $$ = (selector) => Array.from(document.querySelectorAll(selector));
  const click = async (selector) => { $1(selector).click(); await settle(window); };
  const type = (selector, value) => {
    const input = $1(selector);
    input.value = value;
    input.dispatchEvent(new window.Event('input', { bubbles: true }));
  };
  const sent = (key) => requests.filter((request) => request.key === key);
  return { window, document, requests, sent, $1, $$, click, type };
}

/** The slice of Leaflet the wizard touches. It records the map's handlers and every marker (options, handlers). */
function fakeLeaflet() {
  const rec = { mapHandlers: {}, markers: [] };
  const chain = () => ({ addTo() { return this; }, getBounds() { return {}; } });
  const map = {
    on(event, handler) { rec.mapHandlers[event] = handler; return this; },
    setView() { return this; }, fitBounds() {}, invalidateSize() {}, removeLayer() {}, getZoom: () => 16,
  };
  const L = {
    map: () => map,
    tileLayer: chain,
    polygon: chain,
    marker: (latlng, options) => {
      const marker = {
        options, handlers: {}, at: { lat: latlng[0], lng: latlng[1] },
        addTo() { return this; },
        on(event, handler) { this.handlers[event] = handler; return this; },
        setLatLng(ll) { this.at = { lat: ll[0], lng: ll[1] }; return this; },
        getLatLng() { return this.at; },
      };
      rec.markers.push(marker);
      return marker;
    },
  };
  return { L, rec };
}

const GEO_ANSWER = { 'GET /api/v1/addresses/geo-config': ok(GEO) };

describe('/addresses: the list and the address wizard', () => {
  it('fills the district list from geo-config into a native select the customer can open', async () => {
    const { $$, $1, requests } = await boot({ ...GEO_ANSWER, [LIST]: ok({ addresses: [MAIN] }) });

    expect(requests.map((r) => r.url)).toContain('/api/v1/addresses/geo-config?lang=ru');
    expect($$('#districtSelect option').map((o) => o.value)).toEqual(['', 'chilanzar', 'shaykhontohur', 'yunusabad']);
    expect($1('#districtSelect').classList.contains('ignore')).toBe(true);
    // The card names the district in the page language, never by its key.
    expect($1('.ae-addr-card[data-address-id="16"]').textContent).toContain('Чиланзар');
    expect($1('#addressesHead').hidden).toBe(false);
    expect($1('#noAddressesMessage').hidden).toBe(true);
  });

  it('shows one empty state with one action when there is no address yet', async () => {
    const { $1, $$ } = await boot({ ...GEO_ANSWER, [LIST]: ok({ addresses: [] }) });

    expect($1('#noAddressesMessage').hidden).toBe(false);
    expect($1('#addressesHead').hidden).toBe(true);
    expect($$('.ae-addr-card')).toHaveLength(0);
  });

  it('edits without asking the geocoder, keeps the saved text, PUTs the wizard body and refreshes the list', async () => {
    const edited = { ...BRANCH, ...EDIT, id: 67 };
    const { $1, sent, requests, click, type, window } = await boot({
      ...GEO_ANSWER,
      [LIST]: [ok({ addresses: [MAIN, BRANCH] }), ok({ addresses: [{ ...MAIN, is_default: false }, edited] })],
      'PUT /api/v1/auth/addresses/67': ok({ address: edited }),
    });
    const events = [];
    window.document.addEventListener('ae:address-saved', (event) => events.push(event.detail));

    await click('.ae-addr-card[data-address-id="67"] [data-action="edit-address"]');

    expect($1('#wizardStep2').classList.contains('is-active')).toBe(true);
    expect($1('#fullAddress').value).toBe(EDIT.full_address);
    expect($1('#districtSelect').value).toBe('shaykhontohur');
    expect(requests.some((r) => r.key.includes('geocode'))).toBe(false);

    type('#addressTitle', EDIT.title);
    type('#floorNumber', '');
    type('#landmark', '');
    await click('#wizardNextBtn');
    expect($1('#wizardSummary').textContent).toContain(EDIT.full_address);
    $1('#isDefault').checked = true;
    $1('#isBusiness').checked = true;
    await click('#wizardSaveBtn');

    expect(sent('PUT /api/v1/auth/addresses/67').map((r) => r.body)).toEqual([EDIT]);
    expect(events).toEqual([{ address: edited, created: false }]);
    expect(sent(LIST)).toHaveLength(2);
    expect($1('.ae-addr-card[data-address-id="67"] .ae-addr-card__title').textContent).toBe(EDIT.title);
  });

  it('adds an address through the documented API: the pin fills address and district, the POST body is the wizard body', async () => {
    const created = { ...NEW, id: 90 };
    const { $1, $$, sent, click, type, window } = await boot({
      ...GEO_ANSWER,
      [LIST]: ok({ addresses: [MAIN] }),
      'POST /api/v1/addresses/geocode': ok({ latitude: NEW.latitude, longitude: NEW.longitude, formatted_address: 'x' }),
      'POST /api/v1/addresses/reverse-geocode': ok({ formatted_address: NEW.full_address, district: 'Chilonzor tumani', district_key: 'chilanzar' }),
      'POST /api/v1/auth/addresses': ok({ address: created }, 201),
    });
    const saved = [];
    window.AddressWizard.open({ onSaved: (address, meta) => saved.push([address, meta]) });
    await settle(window);

    await click('#wizardNextBtn');  // no point yet: the step stays and says why
    expect($1('#wizardStep1').classList.contains('is-active')).toBe(true);
    expect($1('#locationStatusText').textContent).not.toBe('');

    type('#addressSearchInput', 'Bunyodkor 5');
    await click('#searchAddressBtn');
    expect(sent('POST /api/v1/addresses/reverse-geocode').map((r) => r.body)).toEqual([{ latitude: NEW.latitude, longitude: NEW.longitude }]);
    await click('#wizardNextBtn');
    expect($1('#fullAddress').value).toBe(NEW.full_address);
    expect($1('#districtSelect').value).toBe('chilanzar');

    await click('#wizardNextBtn');  // no name yet: the inline error, not a toast
    expect($1('#addressTitleError').hidden).toBe(false);
    $$('.quick-title-btn')[0].click();
    expect($1('#addressTitle').value).toBe(NEW.title);
    expect($$('.quick-title-btn')[0].getAttribute('aria-pressed')).toBe('true');
    type('#streetAddress', NEW.street_address);
    type('#floorNumber', NEW.floor_number);
    type('#apartmentNumber', NEW.apartment_number);
    type('#landmark', NEW.landmark);
    await click('#wizardNextBtn');
    type('#deliveryInstructions', NEW.delivery_instructions);
    await click('#wizardSaveBtn');

    expect(sent('POST /api/v1/auth/addresses').map((r) => r.body)).toEqual([NEW]);
    expect(saved).toEqual([[created, { created: true }]]);
  });

  it('offers the geocoder text when a moved pin disagrees with what the customer typed', async () => {
    const { $1, click, type } = await boot({
      ...GEO_ANSWER,
      [LIST]: ok({ addresses: [MAIN, BRANCH] }),
      'POST /api/v1/addresses/geocode': ok({ latitude: 41.31, longitude: 69.25 }),
      'POST /api/v1/addresses/reverse-geocode': ok({ formatted_address: 'Toshkent, Yunusobod tumani', district_key: 'yunusabad' }),
    });
    await click('.ae-addr-card[data-address-id="67"] [data-action="edit-address"]');
    await click('#wizardPrevBtn');
    type('#addressSearchInput', 'Yunusobod');
    await click('#searchAddressBtn');

    expect($1('#fullAddress').value).toBe(EDIT.full_address);
    expect($1('#districtSelect').value).toBe('shaykhontohur');
    expect($1('#wizardAddressSuggest').hidden).toBe(false);
    expect($1('#wizardAddressSuggestText').textContent).toBe('Toshkent, Yunusobod tumani');
    await click('#wizardAddressSuggestBtn');
    expect($1('#fullAddress').value).toBe('Toshkent, Yunusobod tumani');
  });

  it('refuses a search result outside the delivery zone without pinning it', async () => {
    const { $1, sent, click, type, window } = await boot({
      ...GEO_ANSWER,
      [LIST]: ok({ addresses: [MAIN] }),
      'POST /api/v1/addresses/geocode': ok({ latitude: 40.5, longitude: 70.9 }),
    });
    window.AddressWizard.open();
    await settle(window);
    type('#addressSearchInput', 'Samarqand');
    await click('#searchAddressBtn');

    expect($1('#wizardZoneAlert').hidden).toBe(false);
    expect($1('#wizardZoneAlert a').getAttribute('href')).toMatch(/\/coverage/);
    expect(sent('POST /api/v1/addresses/reverse-geocode')).toHaveLength(0);
    expect($1('#selectedLatitude').value).toBe('');
  });

  it('makes a card the main address through set-default and reloads the list', async () => {
    const { sent, click } = await boot({
      ...GEO_ANSWER,
      [LIST]: ok({ addresses: [MAIN, BRANCH] }),
      'PATCH /api/v1/auth/addresses/67/set-default': ok({ address: { ...BRANCH, is_default: true } }),
    });
    await click('.ae-addr-card[data-address-id="67"] [data-action="set-default"]');

    expect(sent('PATCH /api/v1/auth/addresses/67/set-default')).toHaveLength(1);
    expect(sent(LIST)).toHaveLength(2);
  });

  it('closes on Escape, while a click on the backdrop still keeps it open', async () => {
    const { $1, window } = await boot({ ...GEO_ANSWER, [LIST]: ok({ addresses: [MAIN] }) });
    window.AddressWizard.open({});
    await settle(window);
    const modal = $1('#addressWizardModal');
    expect(modal.classList.contains('show')).toBe(true);

    // A stray click outside: Bootstrap's static backdrop answers it with hidePrevented; the form stays.
    window.$(modal).trigger(window.$.Event('hidePrevented.bs.modal'));
    await settle(window);
    expect(modal.classList.contains('show')).toBe(true);

    // Escape, as Bootstrap 4.4 hears it (jQuery keydown, which 27).
    window.$(modal).trigger(window.$.Event('keydown', { which: 27, key: 'Escape' }));
    await settle(window);
    expect(modal.classList.contains('show')).toBe(false);
  });

  it('asks in a modal before deleting, then deletes and reloads', async () => {
    const { $1, sent, click } = await boot({
      ...GEO_ANSWER,
      [LIST]: ok({ addresses: [MAIN, BRANCH] }),
      'DELETE /api/v1/auth/addresses/67': ok(null),
    });
    await click('.ae-addr-card[data-address-id="67"] [data-action="delete-address"]');

    expect(sent('DELETE /api/v1/auth/addresses/67')).toHaveLength(0);
    expect($1('#addressDeleteModal').classList.contains('show')).toBe(true);
    expect($1('#addressDeleteText').textContent).toContain('Drive branch 3');
    await click('#addressDeleteConfirm');

    expect(sent('DELETE /api/v1/auth/addresses/67')).toHaveLength(1);
    expect(sent(LIST)).toHaveLength(2);
  });

  // ---- the duplicate-address guard: where the point came from, the duplicate pane, the locked map ------------------
  const PIN = { lat: 41.2856, lng: 69.2034 };            // the point NEW sits at
  const NEW_AT_PIN = { ...NEW, latitude: PIN.lat, longitude: PIN.lng };
  const CANDIDATE = {
    id: 67, title: 'Home', display_label: 'Home', full_address: 'Toshkent, Chilonzor tumani, Bunyodkor 5', district: 'chilanzar',
    latitude: PIN.lat, longitude: PIN.lng, is_default: false, is_business: false, distance_m: 14,
  };
  const OTHER = { ...CANDIDATE, id: 68, title: null, display_label: 'Toshkent, Chilonzor tumani', distance_m: 33 };
  const DUPLICATE = (details) => ({
    status: 409,
    body: { success: false, message: 'You already have an address saved at this spot.', error_code: 'ADDRESS_POSSIBLE_DUPLICATE', details },
  });
  const DUP_TEXT = {
    use: 'Выбрать этот адрес', move: 'Перенести на новую точку', fresh: 'Сохранить как новый адрес', anyway: 'Всё равно сохранить',
  };
  const POST_ADDRESS = 'POST /api/v1/auth/addresses';
  const GEOCODER = {
    'POST /api/v1/addresses/geocode': ok({ latitude: PIN.lat, longitude: PIN.lng, formatted_address: 'x' }),
    'POST /api/v1/addresses/reverse-geocode': ok({ formatted_address: NEW.full_address, district_key: 'chilanzar' }),
  };

  /** Step 1 by search (point_source "search") or by a map tap ("map_pin"), then the details, then Save. */
  async function newAddressSavedFrom(h, how) {
    const { window, click, type, $$ } = h;
    window.AddressWizard.open({ onSaved: (address, meta) => h.saved.push([address, meta]) });
    await settle(window);
    if (how === 'tap') {
      h.leaflet.rec.mapHandlers.click({ latlng: { lat: PIN.lat, lng: PIN.lng } });
      await settle(window);
    } else {
      type('#addressSearchInput', 'Bunyodkor 5');
      await click('#searchAddressBtn');
    }
    await click('#wizardNextBtn');
    $$('.quick-title-btn')[0].click();
    type('#streetAddress', NEW.street_address);
    type('#floorNumber', NEW.floor_number);
    type('#apartmentNumber', NEW.apartment_number);
    type('#landmark', NEW.landmark);
    await click('#wizardNextBtn');
    type('#deliveryInstructions', NEW.delivery_instructions);
    await click('#wizardSaveBtn');
  }

  async function bootDuplicate(answers) {
    const leaflet = fakeLeaflet();
    const h = await boot({ ...GEO_ANSWER, ...GEOCODER, [LIST]: ok({ addresses: [MAIN] }), ...answers }, { leaflet: leaflet.L });
    h.leaflet = leaflet;
    h.saved = [];
    return h;
  }

  it('a search-sourced point sends point_source "search"; a tap and a drag send "map_pin"', async () => {
    const h = await bootDuplicate({ [POST_ADDRESS]: ok({ address: { ...NEW, id: 90 } }, 201) });

    await newAddressSavedFrom(h, 'search');
    await newAddressSavedFrom(h, 'tap');
    // Dragging the marker of a point that came from search turns it into a placed pin.
    h.window.AddressWizard.open();
    await settle(h.window);
    h.type('#addressSearchInput', 'Bunyodkor 5');
    await h.click('#searchAddressBtn');
    const marker = h.leaflet.rec.markers.at(-1);
    marker.at = { lat: 41.3, lng: 69.3 };
    marker.handlers.dragend({ target: marker });
    await settle(h.window);
    await h.click('#wizardNextBtn');
    h.$$('.quick-title-btn')[0].click();
    await h.click('#wizardNextBtn');
    await h.click('#wizardSaveBtn');

    expect(h.sent(POST_ADDRESS).map((r) => [r.body.point_source, r.body.latitude, r.body.longitude])).toEqual([
      ['search', PIN.lat, PIN.lng],
      ['map_pin', PIN.lat, PIN.lng],
      ['map_pin', 41.3, 69.3],
    ]);
  });

  it('my location sends point_source "gps"', async () => {
    const h = await bootDuplicate({ [POST_ADDRESS]: ok({ address: { ...NEW, id: 90 } }, 201) });
    h.window.navigator.geolocation = { getCurrentPosition: (done) => done({ coords: { latitude: PIN.lat, longitude: PIN.lng } }) };
    h.window.AddressWizard.open();
    await settle(h.window);
    await h.click('#useMyLocationBtn');
    await h.click('#wizardNextBtn');
    h.$$('.quick-title-btn')[0].click();
    await h.click('#wizardNextBtn');
    await h.click('#wizardSaveBtn');

    expect(h.sent(POST_ADDRESS).map((r) => r.body.point_source)).toEqual(['gps']);
  });

  it('a 409 opens the duplicate pane; “Use this address” hands the full row to onSaved and sends nothing', async () => {
    const h = await bootDuplicate({ [POST_ADDRESS]: DUPLICATE({ move_address_id: null, candidates: [CANDIDATE, OTHER] }) });
    const events = [];
    h.window.document.addEventListener('ae:address-saved', (event) => events.push(event.detail));

    await newAddressSavedFrom(h, 'search');

    const pane = h.$1('#addressWizardDuplicate');
    expect(pane.hidden).toBe(false);
    const cards = h.$$('#addressWizardDuplicate [data-candidate-id]');
    expect(cards.map((card) => card.dataset.candidateId)).toEqual(['67', '68']);
    expect(cards[0].textContent).toContain('Home');
    expect(cards[0].textContent).toContain('Toshkent, Chilonzor tumani, Bunyodkor 5');   // the line under a title
    expect(cards[0].textContent).toContain('~14 м отсюда');
    expect(cards[1].textContent).toContain('~33 м отсюда');
    expect(cards[1].querySelectorAll('.ae-dup__line')).toHaveLength(0);                  // untitled: the label is the line
    expect(h.$$('#addressWizardDuplicate [data-action="duplicate-move"]')).toHaveLength(0);
    expect(h.$1('#addressWizardModal .modal-footer').hidden).toBe(true);

    const before = h.requests.length;
    await h.click('#addressWizardDuplicate [data-candidate-id="67"] [data-action="duplicate-use"]');

    expect(h.saved).toEqual([[CANDIDATE, { created: false }]]);
    expect(h.requests.length).toBe(before);
    expect(events).toEqual([]);
    expect(h.$1('#addressWizardModal').classList.contains('show')).toBe(false);
  });

  it('Move shows only on move_address_id and posts payload + confirm to that move route', async () => {
    const moved = { ...CANDIDATE, latitude: PIN.lat, longitude: PIN.lng };
    const h = await bootDuplicate({
      [POST_ADDRESS]: DUPLICATE({ move_address_id: 67, candidates: [CANDIDATE, OTHER] }),
      'POST /api/v1/auth/addresses/67/move': ok({ address: moved }),
    });
    const events = [];
    h.window.document.addEventListener('ae:address-saved', (event) => events.push(event.detail));

    await newAddressSavedFrom(h, 'tap');

    const moves = h.$$('#addressWizardDuplicate [data-action="duplicate-move"]');
    expect(moves).toHaveLength(1);
    expect(h.$1('#addressWizardDuplicate [data-candidate-id="67"] [data-action="duplicate-move"]')).toBe(moves[0]);
    expect(h.$1('#addressWizardDuplicate [data-candidate-id="67"]').textContent).toContain(
      'Название и сохранённые детали останутся; то, что вы ввели, дополнит только пустые поля.',
    );
    await h.click('#addressWizardDuplicate [data-action="duplicate-move"]');

    expect(h.sent('POST /api/v1/auth/addresses/67/move').map((r) => r.body)).toEqual([
      { ...NEW_AT_PIN, point_source: 'map_pin', confirm_duplicate: true },
    ]);
    expect(h.sent(POST_ADDRESS)).toHaveLength(1);
    expect(h.saved).toEqual([[moved, { created: false }]]);
    expect(events).toEqual([{ address: moved, created: false }]);
    expect(h.$1('#addressWizardModal').classList.contains('show')).toBe(false);
  });

  it('a refused Move shows the server’s words and drops the Move button', async () => {
    const h = await bootDuplicate({
      [POST_ADDRESS]: DUPLICATE({ move_address_id: 67, candidates: [CANDIDATE] }),
      'POST /api/v1/auth/addresses/67/move': {
        status: 400,
        body: { success: false, message: 'Your order is already on its way to this address.', error_code: 'ADDRESS_PIN_WITH_DRIVER', details: { reason: 'WITH_DRIVER' } },
      },
    });
    await newAddressSavedFrom(h, 'tap');
    await h.click('#addressWizardDuplicate [data-action="duplicate-move"]');

    expect(h.$$('#addressWizardDuplicate [data-action="duplicate-move"]')).toHaveLength(0);
    expect(h.$1('#addressWizardDuplicate').textContent).toContain('Your order is already on its way to this address.');
    expect(h.$$('#addressWizardDuplicate [data-action="duplicate-use"]')).toHaveLength(1);
    expect(h.saved).toEqual([]);
  });

  it('“Save as a new address” posts the same body again with confirm_duplicate: true', async () => {
    const created = { ...NEW, id: 91 };
    const h = await bootDuplicate({
      [POST_ADDRESS]: [DUPLICATE({ move_address_id: null, candidates: [CANDIDATE] }), ok({ address: created }, 201)],
    });
    await newAddressSavedFrom(h, 'search');
    expect(h.$1('#addressWizardDuplicate [data-action="duplicate-new"]').textContent).toBe(DUP_TEXT.fresh);
    await h.click('#addressWizardDuplicate [data-action="duplicate-new"]');

    expect(h.sent(POST_ADDRESS).map((r) => r.body)).toEqual([NEW, { ...NEW, confirm_duplicate: true }]);
    expect(h.saved).toEqual([[created, { created: true }]]);
    expect(h.$1('#addressWizardModal').classList.contains('show')).toBe(false);
  });

  it('a new address’s pane offers Back: the form comes back at the map step with what was typed, nothing sent', async () => {
    const h = await bootDuplicate({
      [POST_ADDRESS]: DUPLICATE({ move_address_id: null, candidates: [CANDIDATE] }),
    });
    await newAddressSavedFrom(h, 'search');

    const footer = h.$$('#addressWizardDuplicateActions [data-action]').map((node) => node.dataset.action);
    expect(footer).toEqual(['duplicate-new', 'duplicate-back']);
    expect(h.$1('#addressWizardDuplicateActions [data-action="duplicate-back"]').textContent).toBe('Назад');

    await h.click('#addressWizardDuplicate [data-action="duplicate-back"]');

    expect(h.$1('#addressWizardDuplicate').hidden).toBe(true);
    expect(h.$1('#addressWizardForm').hidden).toBe(false);
    expect(h.$1('#addressWizardModal .modal-footer').hidden).toBe(false);
    expect(h.$1('#wizardStep1').classList.contains('is-active')).toBe(true);
    expect(h.$1('#addressWizardModal').classList.contains('show')).toBe(true);
    expect(h.sent(POST_ADDRESS)).toHaveLength(1);
    // What the customer typed is still there: saving again sends the same body.
    await h.click('#wizardNextBtn');
    await h.click('#wizardNextBtn');
    await h.click('#wizardSaveBtn');
    expect(h.sent(POST_ADDRESS).map((r) => r.body)).toEqual([NEW, NEW]);
  });

  it('the pane takes the focus when it opens, for a new address and for an edit', async () => {
    const fresh = await bootDuplicate({ [POST_ADDRESS]: DUPLICATE({ move_address_id: null, candidates: [CANDIDATE] }) });
    await newAddressSavedFrom(fresh, 'search');
    expect(fresh.document.activeElement.id).toBe('addressWizardDuplicateTitle');

    const edit = await bootDuplicate({
      [LIST]: ok({ addresses: [MAIN, BRANCH] }),
      'PUT /api/v1/auth/addresses/67': DUPLICATE({ move_address_id: null, candidates: [CANDIDATE] }),
    });
    await edit.click('.ae-addr-card[data-address-id="67"] [data-action="edit-address"]');
    await edit.click('#wizardNextBtn');
    await edit.click('#wizardSaveBtn');
    expect(edit.$1('#addressWizardDuplicate').hidden).toBe(false);
    expect(edit.document.activeElement.id).toBe('addressWizardDuplicateTitle');
  });

  it('editing offers only Save anyway and Back; Save anyway repeats the PUT with confirm_duplicate: true', async () => {
    const edited = { ...BRANCH, ...EDIT, id: 67 };
    const h = await bootDuplicate({
      [LIST]: [ok({ addresses: [MAIN, BRANCH] }), ok({ addresses: [MAIN, edited] })],
      'PUT /api/v1/auth/addresses/67': [
        DUPLICATE({ move_address_id: null, candidates: [CANDIDATE] }),
        DUPLICATE({ move_address_id: null, candidates: [CANDIDATE] }),
        ok({ address: edited }),
      ],
    });
    await h.click('.ae-addr-card[data-address-id="67"] [data-action="edit-address"]');
    h.type('#addressTitle', EDIT.title);
    h.type('#floorNumber', '');
    h.type('#landmark', '');
    await h.click('#wizardNextBtn');
    h.$1('#isDefault').checked = true;
    h.$1('#isBusiness').checked = true;
    await h.click('#wizardSaveBtn');

    const actions = h.$$('#addressWizardDuplicate [data-action]').map((node) => node.dataset.action);
    expect(actions).toEqual(['duplicate-anyway', 'duplicate-back']);
    expect(h.$1('#addressWizardDuplicate [data-action="duplicate-anyway"]').textContent).toBe(DUP_TEXT.anyway);

    // Back: the pane closes, the wizard returns to the map step, nothing is sent.
    await h.click('#addressWizardDuplicate [data-action="duplicate-back"]');
    expect(h.$1('#addressWizardDuplicate').hidden).toBe(true);
    expect(h.$1('#wizardStep1').classList.contains('is-active')).toBe(true);
    expect(h.sent('PUT /api/v1/auth/addresses/67')).toHaveLength(1);

    await h.click('#wizardNextBtn');
    await h.click('#wizardNextBtn');
    await h.click('#wizardSaveBtn');
    await h.click('#addressWizardDuplicate [data-action="duplicate-anyway"]');

    expect(h.sent('PUT /api/v1/auth/addresses/67').map((r) => r.body)).toEqual([
      EDIT, EDIT, { ...EDIT, confirm_duplicate: true },
    ]);
    expect(h.sent('POST /api/v1/auth/addresses/67/move')).toHaveLength(0);
  });

  it('a row with pin_move_refusal opens with a locked map and its reason', async () => {
    const h = await bootDuplicate({
      [LIST]: ok({ addresses: [MAIN, { ...BRANCH, pin_move_refusal: 'WITH_DRIVER' }] }),
      'POST /api/v1/addresses/geocode': ok({ latitude: 41.31, longitude: 69.25 }),
    });
    await h.click('.ae-addr-card[data-address-id="67"] [data-action="edit-address"]');
    await h.click('#wizardPrevBtn');

    expect(h.leaflet.rec.markers.at(-1).options.draggable).toBe(false);
    expect(h.$1('#searchAddressBtn').disabled).toBe(true);
    expect(h.$1('#useMyLocationBtn').disabled).toBe(true);
    const note = h.$1('#addressWizardPinLocked');
    expect(note.hidden).toBe(false);
    expect(note.textContent).toContain('Ваш заказ уже едет по этому адресу.');

    // A tap on the map, and a drag of the marker, change nothing.
    h.leaflet.rec.mapHandlers.click({ latlng: { lat: 41.31, lng: 69.25 } });
    const marker = h.leaflet.rec.markers.at(-1);
    marker.at = { lat: 41.32, lng: 69.26 };
    marker.handlers.dragend({ target: marker });
    await settle(h.window);
    expect(h.$1('#selectedLatitude').value).toBe(String(BRANCH.latitude));
    expect(h.sent('POST /api/v1/addresses/reverse-geocode')).toHaveLength(0);

    // The next address opens unlocked again.
    h.window.AddressWizard.open({});
    await settle(h.window);
    expect(h.$1('#addressWizardPinLocked').hidden).toBe(true);
    expect(h.$1('#searchAddressBtn').disabled).toBe(false);
    expect(h.$1('#useMyLocationBtn').disabled).toBe(false);
  });

  it('shows the reason for each pin_move_refusal in the page’s words', async () => {
    const h = await bootDuplicate({});
    const reasons = {};
    for (const refusal of ['WITH_DRIVER', 'SHARED_PLACE', 'OUTLET']) {
      h.window.AddressWizard.open({ address: { ...BRANCH, pin_move_refusal: refusal } });
      await settle(h.window);
      reasons[refusal] = h.$1('#addressWizardPinLocked').textContent.trim();
    }

    expect(reasons.WITH_DRIVER).toContain('Ваш заказ уже едет по этому адресу.');
    expect(reasons.SHARED_PLACE).toContain('общий с другими клиентами');
    expect(reasons.OUTLET).toContain('принадлежит филиалу');
    expect(new Set(Object.values(reasons)).size).toBe(3);
  });
});
