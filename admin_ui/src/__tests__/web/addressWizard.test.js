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
async function boot(answers) {
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
});
