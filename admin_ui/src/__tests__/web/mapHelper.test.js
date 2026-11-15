// AEMap.load (business_app/static/js/map-helper.js), the shipped loader the address wizard, the tracking map and the
// cabinet order modal share. Review checkout-r03: after one failed Leaflet download the loader waited on the dead
// <script> for ever, so reopening the wizard showed a blank map with no message. A failed tag is now taken out and the
// next load inserts a fresh one; the contract stays AEMap.load({js, css, images}) -> Promise<L>.
import fs from 'node:fs';
import path from 'node:path';
import { JSDOM } from 'jsdom';

const REPO = path.resolve(process.cwd(), '..');
const MAP_HELPER = fs.readFileSync(path.join(REPO, 'business_app/static/js/map-helper.js'), 'utf8');
const ASSETS = { js: '/static/vendor/leaflet/leaflet.js', css: '/static/vendor/leaflet/leaflet.css', images: '/static/vendor/leaflet/images' };

function boot() {
  const dom = new JSDOM('<!doctype html><html><head></head><body></body></html>', { url: 'http://localhost/addresses', runScripts: 'outside-only' });
  dom.window.eval(MAP_HELPER);
  const { document } = dom.window;
  const tags = () => ({
    scripts: Array.from(document.querySelectorAll(`script[data-ae-map="${ASSETS.js}"]`)),
    links: Array.from(document.querySelectorAll(`link[data-ae-map="${ASSETS.css}"]`)),
  });
  const fire = (el, type) => el.dispatchEvent(new dom.window.Event(type));
  return { window: dom.window, tags, fire };
}

describe('AEMap.load', () => {
  it('after a failed download, the next load inserts a fresh script and resolves with Leaflet', async () => {
    const { window, tags, fire } = boot();

    const first = window.AEMap.load(ASSETS);
    const firstScript = tags().scripts[0];
    fire(tags().links[0], 'load');
    fire(firstScript, 'error');
    await expect(first).rejects.toThrow(`Failed to load ${ASSETS.js}`);
    // The dead tag is gone; the stylesheet that did load stays and is not fetched again.
    expect(tags().scripts).toHaveLength(0);
    expect(tags().links).toHaveLength(1);

    const second = window.AEMap.load(ASSETS);
    const { scripts, links } = tags();
    expect(scripts).toHaveLength(1);
    expect(scripts[0]).not.toBe(firstScript);
    expect(scripts[0].getAttribute('src')).toBe(ASSETS.js);
    expect(links).toHaveLength(1);
    window.L = { map() {}, Icon: { Default: {} } };
    fire(scripts[0], 'load');

    await expect(second).resolves.toBe(window.L);
    expect(window.L.Icon.Default.imagePath).toBe(`${ASSETS.images}/`);
  });

  it('loads each file once while a download is in flight', async () => {
    const { window, tags, fire } = boot();

    const first = window.AEMap.load(ASSETS);
    const again = window.AEMap.load(ASSETS);
    expect(again).toBe(first);
    expect(tags().scripts).toHaveLength(1);
    window.L = { map() {}, Icon: { Default: {} } };
    fire(tags().links[0], 'load');
    fire(tags().scripts[0], 'load');

    await expect(first).resolves.toBe(window.L);
    await expect(window.AEMap.load(ASSETS)).resolves.toBe(window.L);
    expect(tags().scripts).toHaveLength(1);
  });
});
