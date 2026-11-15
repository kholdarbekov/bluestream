// The account settings pages' scripts (website redesign step 3, group "account", unit "account-settings",
// 2026-11-14): auth-form.js with profile-settings.js or account-security.js run in jsdom as the pages load them; only
// `fetch` is replaced, answering each request in turn with the backend's answer shapes.
// - profile-settings.js sends one PUT /auth/profile with the names, the date of birth composed from day / month /
//   year (null when all three are empty), gender and language, and never the phone; a partly filled date or an empty
//   first name sends nothing; the toast says "saved" only after a 200;
// - account-security.js posts only {current_password, new_password}, checks the server's rule and the repeat first,
//   maps a 401 to the current-password field and a refused password to the new-password field, and shows "changed"
//   only after a 200.
import fs from 'node:fs';
import path from 'node:path';
import { JSDOM } from 'jsdom';

const REPO = path.resolve(process.cwd(), '..');
// Repo files named in this test (never user input).
// eslint-disable-next-line security/detect-non-literal-fs-filename
const read = (file) => fs.readFileSync(path.join(REPO, file), 'utf8');
const SCRIPTS = {
  pageData: 'business_app/static/js/pages/_page-data.js',
  base: 'business_app/static/js/pages/base.js',
  authForm: 'business_app/static/js/pages/auth-form.js',
  profile: 'business_app/static/js/pages/profile-settings.js',
  security: 'business_app/static/js/pages/account-security.js',
};
const BASE = {
  api_base_url: '/api/v1', current_user: null, is_authenticated: false, login_url: '/login', logout_url: '/logout', i18n: {},
};
const I18N = {
  required: 'REQUIRED', dob_incomplete: 'DOB_INCOMPLETE', dob_invalid: 'DOB_INVALID', saved: 'SAVED', save_failed: 'SAVE_FAILED',
  network_error: 'NETWORK', passwords_mismatch: 'MISMATCH', password_rules: 'RULES', password_too_common: 'TOO_COMMON',
  rule_met: 'met', rule_unmet: 'unmet', wrong_current: 'WRONG_CURRENT', changed: 'CHANGED', change_failed: 'CHANGE_FAILED',
  rate_limited: 'RATE', link_failed: 'LINK_FAILED',
};
const field = (name, attrs = '') => `
  <div class="ae-field" data-field="${name}"><input type="text" id="${name}" name="${name}" ${attrs}>
  <p class="ae-field__error" id="${name}-error"></p></div>`;
const password = (name) => `
  <div class="ae-field" data-field="${name}"><input type="password" id="${name}" name="${name}" required>
  <button type="button" class="password-toggle" data-target="${name}" aria-pressed="false"><i class="fa-eye"></i></button>
  <p class="ae-field__error" id="${name}-error"></p></div>`;
const alert = (id) => `<div class="ae-alert" id="${id}" hidden><p data-alert-text></p></div>`;

const PROFILE = `
  <span class="ae-cabinet-nav__avatar">A</span><p class="ae-cabinet-nav__name">Aqua Element</p>
  <p class="ae-cabinet-nav__sub">\u{1D4E4}\u{1D4F6}</p>
  <form id="personalInfoForm" novalidate>${alert('personalAlert')}
    ${field('first_name', 'required value="Umar"')}${field('last_name', 'value=""')}
    <fieldset class="ae-field" data-field="date_of_birth">
      <input type="text" id="dob_day" name="dob_day" value="7">
      <select id="dob_month" name="dob_month"><option value=""></option><option value="3" selected>March</option></select>
      <input type="text" id="dob_year" name="dob_year" value="1994">
      <p class="ae-field__error" id="dob-error"></p>
    </fieldset>
    <select id="gender" name="gender"><option value="unknown" selected></option><option value="female"></option></select>
    <select id="preferred_language" name="preferred_language"><option value="uz"></option><option value="ru" selected></option></select>
    <button type="submit">Save</button>
  </form>`;
const SECURITY = `
  <form id="changePasswordForm" novalidate>${alert('passwordAlert')}
    <div id="passwordDone" hidden tabindex="-1"></div>
    ${password('current_password')}${password('new_password')}${password('confirm_password')}
    <div id="passwordRules" data-password-rules data-for="new_password"><ul>
      <li data-rule="length" data-min-length="8"><i></i><span data-rule-state></span></li>
      <li data-rule="upper" data-pattern="[A-Z]"><i></i><span data-rule-state></span></li>
      <li data-rule="digit" data-pattern="[0-9]"><i></i><span data-rule-state></span></li>
    </ul></div>
    <button type="submit">Change</button>
  </form>`;

const LINK = `
  <div data-password-state="none">
    <form id="passwordLinkForm" novalidate>${alert('linkAlert')}<button type="submit"><span>Email me the link</span></button></form>
    <div id="passwordLinkSent" role="status" tabindex="-1" hidden>SENT</div>
  </div>`;

function boot({ body, script, answers = [], pageData = {} }) {
  const dom = new JSDOM(
    `<!doctype html><html><head></head><body>
      <script type="application/json" id="base-page-data">${JSON.stringify(BASE)}</script>
      <script type="application/json" id="page-data">${JSON.stringify({ i18n: I18N, ...pageData })}</script>${body}</body></html>`,
    { url: 'http://localhost/profile-settings', runScripts: 'outside-only', pretendToBeVisual: true },
  );
  const { window } = dom;
  const requests = [];
  window.fetch = async (url, options = {}) => {
    requests.push({ url, method: options.method, body: options.body ? JSON.parse(options.body) : undefined });
    const answer = answers[requests.length - 1];
    if (!answer) return new Response('{}', { status: 599 });
    return new Response(JSON.stringify(answer.body || {}), { status: answer.status, headers: { 'Content-Type': 'application/json' } });
  };
  [SCRIPTS.pageData, SCRIPTS.base, SCRIPTS.authForm, script].map(read).forEach((source) => window.eval(source));
  const toasts = [];
  window.showNotification = (message, kind) => toasts.push([kind, message]);
  window.dispatchEvent(new window.Event('DOMContentLoaded'));
  return { window, doc: window.document, requests, toasts };
}

const settle = () => new Promise((resolve) => { setTimeout(resolve, 0); });
const flush = async () => { for (let i = 0; i < 6; i += 1) await settle(); }; // eslint-disable-line no-await-in-loop
const set = (page, id, value) => {
  const input = page.doc.getElementById(id);
  input.value = value;
  input.dispatchEvent(new page.window.Event('input', { bubbles: true }));
};
const submit = async (page, id) => {
  page.doc.getElementById(id).dispatchEvent(new page.window.Event('submit', { cancelable: true }));
  await flush();
};
const error = (page, id) => page.doc.getElementById(id).textContent;

describe('profile-settings.js', () => {
  it('saves the whole form once, with the composed date and never the phone, and says so only after the 200', async () => {
    const page = boot({ body: PROFILE, script: SCRIPTS.profile, answers: [{ status: 200, body: { success: true, data: { user: { full_name: 'Umar Xoldarbekov' } } } }] });
    set(page, 'last_name', '  Xoldarbekov ');
    await submit(page, 'personalInfoForm');

    expect(page.requests).toHaveLength(1);
    expect(page.requests[0].url).toBe('/api/v1/auth/profile');
    expect(page.requests[0].method).toBe('PUT');
    expect(page.requests[0].body).toEqual({
      first_name: 'Umar', last_name: 'Xoldarbekov', date_of_birth: '1994-03-07', gender: 'unknown', preferred_language: 'ru',
    });
    expect(page.toasts).toEqual([['success', 'SAVED']]);
  });

  it('leaves the frame\'s name as the server drew it: an entity keeps its company, an astral initial stays whole', async () => {
    // The answer PUT /auth/profile gives for an entity account whose contact person's name starts with an astral letter.
    const page = boot({
      body: PROFILE,
      script: SCRIPTS.profile,
      answers: [{ status: 200, body: { success: true, data: { user: { full_name: '\u{1D4E4}\u{1D4F6}', company_name: 'Aqua Element' } } } }],
    });
    await submit(page, 'personalInfoForm');

    expect(page.toasts).toEqual([['success', 'SAVED']]);
    expect(page.doc.querySelector('.ae-cabinet-nav__name').textContent).toBe('Aqua Element');
    expect(page.doc.querySelector('.ae-cabinet-nav__avatar').textContent).toBe('A');
    expect(page.doc.querySelector('.ae-cabinet-nav__sub').textContent).toBe('\u{1D4E4}\u{1D4F6}');
  });

  it('sends null when the three date parts are empty', async () => {
    const page = boot({ body: PROFILE, script: SCRIPTS.profile, answers: [{ status: 200, body: { data: { user: {} } } }] });
    set(page, 'dob_day', '');
    set(page, 'dob_year', '');
    page.doc.getElementById('dob_month').value = '';
    await submit(page, 'personalInfoForm');

    expect(page.requests[0].body.date_of_birth).toBeNull();
  });

  it('sends nothing for a partly filled date or an empty first name, and says why under each field', async () => {
    const page = boot({ body: PROFILE, script: SCRIPTS.profile });
    set(page, 'dob_day', '');
    set(page, 'first_name', '  ');
    await submit(page, 'personalInfoForm');

    expect(page.requests).toHaveLength(0);
    expect(error(page, 'dob-error')).toBe('DOB_INCOMPLETE');
    expect(error(page, 'first_name-error')).toBe('REQUIRED');
    expect(page.toasts).toEqual([]);
  });

  it('puts a date the server refuses under the date, and a failure in the alert, never a "saved" toast', async () => {
    const page = boot({
      body: PROFILE,
      script: SCRIPTS.profile,
      answers: [
        { status: 400, body: { message: 'Validation failed', details: { date_of_birth: ['Date of birth cannot be in the future'] } } },
        { status: 500, body: { message: 'error.server_error' } },
      ],
    });
    await submit(page, 'personalInfoForm');
    expect(error(page, 'dob-error')).toBe('DOB_INVALID');

    await submit(page, 'personalInfoForm');
    expect(page.doc.getElementById('personalAlert').hidden).toBe(false);
    expect(page.doc.querySelector('#personalAlert [data-alert-text]').textContent).toBe('SAVE_FAILED'); // never a raw key
    expect(page.toasts).toEqual([]);
  });
});

describe('account-security.js', () => {
  const fill = (page, current, next, repeat) => {
    set(page, 'current_password', current);
    set(page, 'new_password', next);
    set(page, 'confirm_password', repeat);
  };

  it('checks the server rule and the repeat before sending anything', async () => {
    const page = boot({ body: SECURITY, script: SCRIPTS.security });
    fill(page, 'Old12345', 'short', 'other');
    await submit(page, 'changePasswordForm');

    expect(page.requests).toHaveLength(0);
    expect(error(page, 'new_password-error')).toBe('RULES');
    expect(error(page, 'confirm_password-error')).toBe('MISMATCH');
  });

  it('posts only the two passwords and shows "changed" after the 200, with the form emptied and hidden again', async () => {
    const page = boot({ body: SECURITY, script: SCRIPTS.security, answers: [{ status: 200, body: { success: true } }] });
    fill(page, 'Old12345', 'NewPass123', 'NewPass123');
    page.doc.querySelector('[data-target="new_password"]').click();
    expect(page.doc.getElementById('new_password').type).toBe('text');
    await submit(page, 'changePasswordForm');

    expect(page.requests).toEqual([{
      url: '/api/v1/auth/change-password', method: 'POST', body: { current_password: 'Old12345', new_password: 'NewPass123' },
    }]);
    expect(page.doc.getElementById('passwordDone').hidden).toBe(false);
    expect(page.toasts).toEqual([['success', 'CHANGED']]);
    expect(page.doc.getElementById('new_password').value).toBe('');
    expect(page.doc.getElementById('new_password').type).toBe('password');
  });

  it('maps a 401 to the current password and a refused password to the new one, never claiming success', async () => {
    const page = boot({
      body: SECURITY,
      script: SCRIPTS.security,
      answers: [
        { status: 401, body: { success: false, message: 'api.auth.current_password_incorrect' } },
        { status: 400, body: { success: false, errors: ['password: Password is too common'] } },
      ],
    });
    fill(page, 'Wrong123', 'NewPass123', 'NewPass123');
    await submit(page, 'changePasswordForm');
    expect(error(page, 'current_password-error')).toBe('WRONG_CURRENT');

    fill(page, 'Old12345', 'Password123', 'Password123');
    await submit(page, 'changePasswordForm');
    expect(error(page, 'new_password-error')).toBe('TOO_COMMON');
    expect(page.doc.getElementById('passwordDone').hidden).toBe(true);
    expect(page.toasts).toEqual([]);
  });
});

describe('account-security.js: the set-a-password panel of a bot signup', () => {
  it('posts the account\'s own email to forgot-password and shows "check your email" only after the 200', async () => {
    const page = boot({ body: LINK, script: SCRIPTS.security, pageData: { link_email: 'dilnoza@example.com' }, answers: [{ status: 200, body: { success: true } }] });
    await submit(page, 'passwordLinkForm');

    expect(page.requests).toEqual([{ url: '/api/v1/auth/forgot-password', method: 'POST', body: { identifier: 'dilnoza@example.com' } }]);
    expect(page.doc.getElementById('passwordLinkForm').hidden).toBe(true);
    expect(page.doc.getElementById('passwordLinkSent').hidden).toBe(false);
  });

  it('keeps the button and says why when the server refuses or limits the request', async () => {
    const page = boot({
      body: LINK,
      script: SCRIPTS.security,
      pageData: { link_email: 'dilnoza@example.com' },
      answers: [{ status: 429, body: { message: 'too many' } }, { status: 500, body: { message: 'error.server_error' } }],
    });
    await submit(page, 'passwordLinkForm');
    expect(page.doc.querySelector('#linkAlert [data-alert-text]').textContent).toBe('RATE');

    await submit(page, 'passwordLinkForm');
    expect(page.doc.querySelector('#linkAlert [data-alert-text]').textContent).toBe('LINK_FAILED');
    expect(page.doc.getElementById('passwordLinkForm').hidden).toBe(false);
    expect(page.doc.getElementById('passwordLinkSent').hidden).toBe(true);
  });
});
