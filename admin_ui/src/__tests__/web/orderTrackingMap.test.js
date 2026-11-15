// The order tracking page's map (website redesign 2026-11-14, group "checkout", unit "order-pages").
// Owner decision 4: the order pages show the delivery address. The tracking page used to gate its map on Delivery
// columns that do not exist and on a Google key, so a 400 px grey placeholder said "Delivery location will be shown
// here" on every order. Now the address card holds a small map of the address's pin, drawn by the shipped
// order-tracking.js with the vendored Leaflet that map-helper.js (AEMap) loads once the box comes into view; an
// unpinned address gets no box, and a Leaflet that fails to load hides the box (the address text stays).
import fs from 'node:fs';
import path from 'node:path';
import { JSDOM } from 'jsdom';

const REPO = path.resolve(process.cwd(), '..');
// eslint-disable-next-line security/detect-non-literal-fs-filename
const read = (file) => fs.readFileSync(path.join(REPO, file), 'utf8');
const PAGE_DATA_SCRIPT = read('business_app/static/js/pages/_page-data.js');
const TRACKING_SCRIPT = read('business_app/static/js/pages/order-tracking.js');
const LEAFLET = { js: '/static/vendor/leaflet/leaflet.js', css: '/static/vendor/leaflet/leaflet.css', images: '/static/vendor/leaflet/images/' };
const PIN = { lat: 41.323989, lng: 69.193841, label: 'Uy' };

async function settle(window, rounds = 10) {
  for (let i = 0; i < rounds; i += 1) await new Promise((resolve) => window.setTimeout(resolve, 0));
}

// The page as order_tracking.html renders it (the address card's box only when the address is pinned), with the
// island's `map` and `leaflet`, Leaflet and AEMap stubbed, and IntersectionObserver held for the test to fire.
async function boot({ pinned = true, leafletFails = false } = {}) {
  const island = { map: pinned ? PIN : null, leaflet: LEAFLET, auto_refresh: false, order: null };
  const dom = new JSDOM(
    '<!doctype html><html><body><section class="ae-track__address"><div class="ae-order-addr">' +
    '<p class="ae-order-addr__line">Tashkent</p>' +
    (pinned ? '<div id="orderMap" class="ae-order-addr__map ae-map"></div>' : '') +
    `</div></section><script type="application/json" id="page-data">${JSON.stringify(island)}</script></body></html>`,
    { url: 'http://localhost/order-tracking?order_id=1', runScripts: 'outside-only' },
  );
  const { window } = dom;
  const calls = { load: [], map: [], view: [], tiles: [], marker: [] };
  const L = {
    Browser: { mobile: false },
    map: (box, options) => { calls.map.push({ box, options }); return { setView: (...args) => calls.view.push(args) }; },
    tileLayer: (url) => { calls.tiles.push(url); return { addTo() {} }; },
    marker: (at, options) => { calls.marker.push({ at, options }); return { addTo() {} }; },
  };
  window.AEMap = {
    load: (assets) => { calls.load.push(assets); return leafletFails ? Promise.reject(new Error('offline')) : Promise.resolve(L); },
  };
  const observers = [];
  window.IntersectionObserver = function (callback) {
    this.observe = (target) => observers.push({ callback, target });
    this.disconnect = () => { observers.length = 0; };
  };
  window.eval(PAGE_DATA_SCRIPT);
  window.eval(TRACKING_SCRIPT);
  if (window.document.readyState !== 'loading') window.document.dispatchEvent(new window.Event('DOMContentLoaded'));
  await settle(window);
  const seen = async () => {
    observers.slice().forEach(({ callback, target }) => callback([{ isIntersecting: true, target }]));
    await settle(window);
  };
  return { window, document: window.document, calls, observers, seen };
}

describe('order-tracking.js draws the address pin', () => {
  it('loads the vendored Leaflet only once the map box comes into view, and pins the published point', async () => {
    const page = await boot();
    expect(page.calls.load).toEqual([]);
    expect(page.observers).toHaveLength(1);
    await page.seen();
    expect(page.calls.load).toEqual([LEAFLET]);
    expect(page.calls.map).toHaveLength(1);
    expect(page.calls.map[0].box).toBe(page.document.getElementById('orderMap'));
    expect(page.calls.map[0].options.scrollWheelZoom).toBe(false);
    expect(page.calls.view).toEqual([[[PIN.lat, PIN.lng], 16]]);
    expect(page.calls.tiles).toEqual(['https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png']);
    expect(page.calls.marker.map((m) => m.at)).toEqual([[PIN.lat, PIN.lng]]);
    expect(page.calls.marker[0].options).toMatchObject({ title: 'Uy', interactive: false });
  });

  it('draws nothing for an address with no pin', async () => {
    const page = await boot({ pinned: false });
    await page.seen();
    expect(page.observers).toHaveLength(0);
    expect(page.calls.load).toEqual([]);
  });

  it('hides the box when Leaflet cannot load, and keeps the address text', async () => {
    const page = await boot({ leafletFails: true });
    await page.seen();
    expect(page.document.getElementById('orderMap').hidden).toBe(true);
    expect(page.document.querySelector('.ae-order-addr__line').textContent).toBe('Tashkent');
  });
});
