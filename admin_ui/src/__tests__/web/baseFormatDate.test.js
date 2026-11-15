// base.js AE.formatDate writes a date exactly as the server prints it (site review SITE-07, 2026-11-14).
//
// The server's full date is web.integration.date_year with the month's name from web.display_labels.month.N (the
// offer block's "as of", the blog, routes._account_date_label): "30 September 2026", "30 сентября 2026",
// "2026-yil 30-sentabr". base.html hands those words to the page as `date_words`; the words here are the ones the
// real page published when tests/integration/test_my_subscriptions_page.py rendered it (shell.json `base`). The
// server half of the pin is tests/integration/test_web_account_settings.py
// test_the_server_full_date_matches_the_script_one (the same three strings).
import fs from 'node:fs';
import path from 'node:path';
import { JSDOM } from 'jsdom';

const REPO = path.resolve(process.cwd(), '..');
// Repo files named in this test (never user input).
// eslint-disable-next-line security/detect-non-literal-fs-filename
const read = (file) => fs.readFileSync(path.join(REPO, file), 'utf8');
const SHELL = JSON.parse(read('tests/fixtures/web_subscription_management/shell.json'));
const SERVER_LABEL = { en: '30 September 2026', ru: '30 сентября 2026', uz: '2026-yil 30-sentabr' };

function boot(lang) {
  const data = { api_base_url: '/api/v1', language: lang, i18n: {}, ...SHELL[lang].base };
  const dom = new JSDOM(
    `<!doctype html><html lang="${lang}"><head></head><body>` +
    `<script type="application/json" id="base-page-data">${JSON.stringify(data)}</script></body></html>`,
    { url: 'http://localhost/my-orders', runScripts: 'outside-only', pretendToBeVisual: true },
  );
  dom.window.eval(read('business_app/static/js/pages/_page-data.js'));
  dom.window.eval(read('business_app/static/js/pages/base.js'));
  return dom.window;
}

describe('AE.formatDate reads like the server label', () => {
  it.each(['en', 'ru', 'uz'])('%s: a calendar date and a timestamp of that Tashkent day', (lang) => {
    const window = boot(lang);
    expect(SHELL[lang].base.date_words.date_year).toMatch(/\{day\}/);
    expect(window.AE.formatDate('2026-09-30')).toBe(SERVER_LABEL[lang]);
    // 2026-09-29 20:30 UTC is already 30 September in Tashkent (UTC+5).
    expect(window.AE.formatDate('2026-09-29T20:30:00+00:00')).toBe(SERVER_LABEL[lang]);
  });

  it('ru never gets Intl\'s " г." suffix', () => {
    expect(boot('ru').AE.formatDate('2026-11-13')).toBe('13 ноября 2026');
  });
});

// Review SITE-10 / CAB-R14: one placeholder fill (base.js AE.fill) for every page script, no local copies.
describe('AE.fill is the one placeholder fill', () => {
  it('fills known names and keeps unknown ones as written', () => {
    const { AE } = boot('en');
    expect(AE.fill('{a} of {b} ({c})', { a: 2, b: 5 })).toBe('2 of 5 ({c})');
    expect(AE.fill(null, {})).toBe('');
  });

  it.each(['checkout', 'cart', 'loyalty', 'order-detail-modal', 'orders', 'my-subscriptions'])(
    'pages/%s.js keeps no copy of its own', (name) => {
      const source = read(`business_app/static/js/pages/${name}.js`);
      expect(source).not.toMatch(/function fill\(/);
      expect(source).toMatch(/var fill = window\.AE\.fill;/);
    },
  );
});
