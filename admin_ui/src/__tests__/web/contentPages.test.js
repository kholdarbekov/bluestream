// The content pages' inline scripts (website redesign step 3, group "content"; review fixes 2026-11-14).
//
// The SHIPPED scripts are read out of the templates and run in jsdom over the markup the templates print
// (tests/integration/test_web_content_*.py pin the template side). What is pinned:
// - the policy pages' contents list (partials/content_ui.html, legal_document): a closed disclosure under 992px, an
//   open list with an untabbable label from 992px, and it follows the layout when a tablet rotates (review C-13: it
//   used to check the width once, so a portrait-loaded tablet turned to landscape showed an empty, dead box);
// - the article script (blog_detail.html): a comparison table with a header row gets each column's header as its
//   cells' data-label, so phones stack it into labelled rows (review C-04: the 10 L column hid behind an unhinted
//   sideways scroll); every table sits in a focusable, named frame; the article's sections are listed beside it.
import fs from 'node:fs';
import path from 'node:path';
import { JSDOM } from 'jsdom';

const REPO = path.resolve(process.cwd(), '..');
const read = (file) => fs.readFileSync(path.join(REPO, file), 'utf8'); // eslint-disable-line security/detect-non-literal-fs-filename

const CONTENT_UI = read('business_app/templates/frontend/partials/content_ui.html');
const TOC_SCRIPT = CONTENT_UI.match(/<script>(\(function \(d\) \{ if \(!window\.matchMedia\)[\s\S]*?)<\/script>/)[1];
const BLOG_DETAIL = read('business_app/templates/frontend/blog_detail.html');
const ARTICLE_SCRIPT = BLOG_DETAIL.match(/\{% block extra_js %\}\s*<script>([\s\S]*?)<\/script>/)[1];

// A matchMedia stand-in whose width can change, firing `change` like a rotating tablet.
function mediaAt(width) {
  const listeners = [];
  const query = { matches: width >= 992, addEventListener: (_, fn) => listeners.push(fn) };
  return {
    matchMedia: () => query,
    resize(next) {
      query.matches = next >= 992;
      listeners.forEach((fn) => fn(query));
    },
  };
}

function tocPage(width) {
  const media = mediaAt(width);
  const dom = new JSDOM(
    `<!doctype html><body>
      <details class="ct-toc" data-ct-toc>
        <summary class="ct-toc__title">Содержание</summary>
        <ol class="ct-toc__list"><li><a href="#a">1</a></li><li><a href="#b">2</a></li></ol>
      </details><script>${TOC_SCRIPT}</script></body>`,
    { runScripts: 'dangerously', beforeParse(window) { window.matchMedia = media.matchMedia; } },
  );
  const details = dom.window.document.querySelector('[data-ct-toc]');
  return { details, summary: details.querySelector('summary'), resize: media.resize };
}

describe('policy contents list', () => {
  it('is a closed disclosure with a tabbable summary on a phone', () => {
    const { details, summary } = tocPage(390);
    expect(details.open).toBe(false);
    expect(summary.hasAttribute('tabindex')).toBe(false);
  });

  it('is open with an untabbable label beside the text from 992px', () => {
    const { details, summary } = tocPage(1280);
    expect(details.open).toBe(true);
    expect(summary.getAttribute('tabindex')).toBe('-1');
  });

  it('opens when a portrait tablet turns to landscape, and is a disclosure again when it turns back', () => {
    const { details, summary, resize } = tocPage(820);
    expect(details.open).toBe(false);
    resize(1180);
    expect(details.open).toBe(true);
    expect(summary.getAttribute('tabindex')).toBe('-1');
    resize(820);
    expect(summary.hasAttribute('tabindex')).toBe(false);
  });
});

function articlePage(body) {
  const dom = new JSDOM(
    `<!doctype html><body>
      <div class="ct-article"><div class="blog-content ae-prose" data-table-label="Таблица из статьи">${body}</div></div>
      <nav class="ct-article-toc" hidden data-ct-article-toc><h2>В статье</h2><ol class="ct-article-toc__list"></ol></nav>
      <script>${ARTICLE_SCRIPT}</script></body>`,
    { runScripts: 'dangerously' },
  );
  return dom.window.document;
}

const COMPARISON = `
  <table>
    <thead><tr><th>Критерий</th><th>18,9 л (19 л)</th><th>10 л</th></tr></thead>
    <tbody>
      <tr><td>Вес</td><td>~20 кг</td><td>~10,4 кг</td></tr>
      <tr><td>Возвратная тара</td><td>Да</td><td>Нет</td></tr>
    </tbody>
  </table>`;

describe('article tables and sections', () => {
  it('labels every value cell with its column header, so a phone stacks the rows instead of hiding a column', () => {
    const doc = articlePage(COMPARISON);
    const table = doc.querySelector('table');
    expect(table.classList.contains('ct-stack')).toBe(true);
    const rows = [...table.querySelectorAll('tbody tr')].map((row) =>
      [...row.children].map((cell) => cell.getAttribute('data-label')),
    );
    expect(rows).toEqual([
      [null, '18,9 л (19 л)', '10 л'],
      [null, '18,9 л (19 л)', '10 л'],
    ]);
  });

  it('puts every table in a focusable, named frame (a headerless one keeps its scroll, unstacked)', () => {
    const doc = articlePage(`${COMPARISON}<table><tbody><tr><td>a</td><td>b</td></tr></tbody></table>`);
    const frames = [...doc.querySelectorAll('.blog-content .ae-table-scroll')];
    expect(frames).toHaveLength(2);
    frames.forEach((frame) => {
      expect(frame.firstElementChild.tagName).toBe('TABLE');
      expect(frame.getAttribute('tabindex')).toBe('0');
      expect(frame.getAttribute('role')).toBe('region');
      expect(frame.getAttribute('aria-label')).toBe('Таблица из статьи');
    });
    expect(frames[1].firstElementChild.classList.contains('ct-stack')).toBe(false);
  });

  it('lists the article sections beside it from three headings, giving each heading an anchor', () => {
    const doc = articlePage('<h2>Один</h2><p>.</p><h2 id="dva">Два</h2><p>.</p><h2>Три</h2>');
    const toc = doc.querySelector('[data-ct-article-toc]');
    expect(toc.hidden).toBe(false);
    expect([...toc.querySelectorAll('a')].map((a) => [a.getAttribute('href'), a.textContent])).toEqual([
      ['#section-1', 'Один'],
      ['#dva', 'Два'],
      ['#section-3', 'Три'],
    ]);
  });

  it('keeps the list hidden for a short article', () => {
    const doc = articlePage('<h2>Один</h2><h2>Два</h2>');
    expect(doc.querySelector('[data-ct-article-toc]').hidden).toBe(true);
  });
});
