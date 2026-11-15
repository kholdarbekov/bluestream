// The sign-in pages' scripts (website redesign step 3, group "auth", 2026-11-14): auth-form.js, register.js,
// reset-password.js and login.js run in jsdom as the pages load them; only `fetch` is replaced, answering each request
// in turn with the backend's real answer shapes (api_responses.error_response: `errors` as "field: message" strings).
// - register.js sends the phone as typed and the page's SMS language, shows the code step in place of the form, posts
//   the code with the details from step one, and sends the new customer to the server-checked `after_register_url`;
// - a wrong code is written under the code field in the page's language, never the server's English;
// - the password checklist reads the rule from the markup and blocks a password it does not tick;
// - reset-password.js turns a refused token into the "link not valid" state, and a password the server refuses although
//   every rule is ticked into the "too easy to guess" message;
// - a server message that is an untranslated key never reaches the customer.
// Review fixes (fix-auth): a guest cart is synced (and awaited) before either sign-up or the login leaves the page;
// the email sign-up sends the page language and leaves the address to the server; a 409 shows the way in; the login
// words a refusal by the server's error_code; the checklist agrees with the server on tests/fixtures/password_rule_cases.json.
import fs from 'node:fs';
import path from 'node:path';
import { JSDOM } from 'jsdom';
import jsdomUtils from 'jsdom/lib/jsdom/living/generated/utils.js';

const REPO = path.resolve(process.cwd(), '..');
// Repo files named in this test (never user input).
// eslint-disable-next-line security/detect-non-literal-fs-filename
const read = (file) => fs.readFileSync(path.join(REPO, file), 'utf8');
const SCRIPTS = {
  pageData: 'business_app/static/js/pages/_page-data.js',
  base: 'business_app/static/js/pages/base.js',
  authForm: 'business_app/static/js/pages/auth-form.js',
  login: 'business_app/static/js/pages/login.js',
  register: 'business_app/static/js/pages/register.js',
  reset: 'business_app/static/js/pages/reset-password.js',
};
const BASE = {
  api_base_url: '/api/v1', current_user: null, is_authenticated: false, login_url: '/login', logout_url: '/logout', i18n: {},
};
// The rule PasswordValidator.requirements() publishes, as macros/auth.html writes it.
const RULES = `
  <div id="passwordRules" data-password-rules data-for="password"><ul>
    <li data-rule="min_length" data-min-length="8"><i class="far fa-circle"></i><span data-rule-state></span></li>
    <li data-rule="lowercase" data-pattern="[a-z]"><i class="far fa-circle"></i><span data-rule-state></span></li>
    <li data-rule="uppercase" data-pattern="[A-Z]"><i class="far fa-circle"></i><span data-rule-state></span></li>
    <li data-rule="digit" data-pattern="\\d"><i class="far fa-circle"></i><span data-rule-state></span></li>
  </ul></div>`;
const field = (name, type = 'text', extra = '') => `
  <div class="ae-field" data-field="${name}"><input type="${type}" id="${name}" name="${name}" ${extra}>
  <p class="ae-field__error" id="${name}-error"></p></div>`;
const alert = (id) => `<div class="ae-alert" id="${id}" hidden><p data-alert-text></p></div>`;

function boot({ pageData, body, scripts, answers = [], path: pagePath = '/register' }) {
  const dom = new JSDOM(
    `<!doctype html><html><head></head><body>
      <script type="application/json" id="base-page-data">${JSON.stringify(BASE)}</script>
      <script type="application/json" id="page-data">${JSON.stringify(pageData)}</script>${body}</body></html>`,
    { url: `http://localhost${pagePath}`, runScripts: 'outside-only', pretendToBeVisual: true },
  );
  const { window } = dom;
  const requests = [];
  window.fetch = async (url, options = {}) => {
    requests.push({ url, body: options.body ? JSON.parse(options.body) : undefined });
    const answer = answers[requests.length - 1];
    if (!answer) return new Response('{}', { status: 599 });
    if (answer.wait) await answer.wait; // hold this answer until the test lets it go
    return new Response(JSON.stringify(answer.body || {}), { status: answer.status, headers: { 'Content-Type': 'application/json' } });
  };
  const navigations = [];
  jsdomUtils.implForWrapper(window.location)._locationObjectNavigate = (url) => {
    navigations.push(`/${url.path.join('/')}${url.query ? `?${url.query}` : ''}`);
  };
  [SCRIPTS.pageData, SCRIPTS.base, SCRIPTS.authForm, ...scripts].map(read).forEach((source) => window.eval(source));
  window.dispatchEvent(new window.Event('DOMContentLoaded'));
  return { window, doc: window.document, requests, navigations };
}

const type = (page, id, value) => {
  const input = page.doc.getElementById(id);
  input.value = value;
  input.dispatchEvent(new page.window.Event('input', { bubbles: true }));
};
const submit = (page, id) => page.doc.getElementById(id).dispatchEvent(new page.window.Event('submit', { cancelable: true }));

describe('register.js', () => {
  const PAGE = {
    locale: 'ru',
    verify_email_url: '/verify-email',
    after_register_url: '/my-subscriptions',
    i18n: {
      required: 'Заполните это поле', password_rules: 'Пароль пока не отвечает всем требованиям.',
      wrong_code: 'Код не подошёл или устарел.', code_expired: 'Срок действия кода истёк', invalid_phone: 'Укажите мобильный номер',
      check_fields: 'Проверьте отмеченные поля.', need_terms: 'Отметьте согласие',
      invalid_email: 'Проверьте адрес email', need_email: 'Укажите email',
      phone_taken: 'С этим номером уже есть аккаунт.', email_taken: 'С этим email уже есть аккаунт.',
    },
  };
  const BODY = `
    <div id="registerStep">${alert('registerAlert')}
      <form id="registerForm">
        <button type="button" data-method="phone" aria-pressed="true"></button><button type="button" data-method="email" aria-pressed="false"></button>
        <div data-method-panel="phone">${field('phone', 'tel', 'required')}<div id="phoneTaken" hidden><a href="/login?next=%2Fcheckout">Войти</a></div></div>
        <div data-method-panel="email" hidden>${field('email', 'email', 'required disabled')}<div id="emailTaken" hidden></div></div>
        ${field('first_name', 'text', 'required')}${field('last_name')}
        ${field('password', 'password', 'required')}${RULES}${field('confirm_password', 'password', 'required')}
        ${field('referral_code')}
        <div data-field="terms"><input type="checkbox" name="terms" id="terms" required><p class="ae-field__error"></p></div>
        <button type="submit" class="btn-auth"><span>Создать аккаунт</span></button>
      </form>
    </div>
    <div id="otpVerificationSection" hidden><strong id="maskedPhone"></strong>${alert('otpAlert')}
      <form id="otpForm">
        <div class="ae-field" data-field="otp_code"><div class="ae-otp"><input id="otpCode" name="otp_code">
        <span class="ae-otp__cell"></span><span class="ae-otp__cell"></span><span class="ae-otp__cell"></span>
        <span class="ae-otp__cell"></span><span class="ae-otp__cell"></span><span class="ae-otp__cell"></span>
        </div><p class="ae-field__error"></p></div>
        <p id="otpTimerText" data-template="Код действует ещё {time}"></p><button type="button" id="resendOtpBtn" hidden></button>
        <button type="submit" id="verifyOtpBtn"><span>Подтвердить</span></button>
      </form>
      <button type="button" id="goBackToFormBtn"></button>
    </div>`;

  function fillStepOne(page, password = 'Abcdefg1') {
    type(page, 'phone', '90 123 45 67');
    type(page, 'first_name', 'Ali');
    type(page, 'password', password);
    type(page, 'confirm_password', password);
    page.doc.getElementById('terms').checked = true;
  }

  it('sends the phone as typed with the page language, then the code, and lands on next', async () => {
    const page = boot({
      pageData: PAGE, body: BODY, scripts: [SCRIPTS.register],
      answers: [
        { status: 200, body: { success: true, data: { phone_masked: '+998***4567', expires_in: 180, resend_available_in: 60 } } },
        { status: 201, body: { success: true } },
      ],
    });
    fillStepOne(page);

    submit(page, 'registerForm');
    await vi.waitFor(() => expect(page.doc.getElementById('otpVerificationSection').hidden).toBe(false));

    expect(page.requests[0]).toEqual({ url: '/api/v1/auth/phone/register/init', body: { phone: '90 123 45 67', preferred_language: 'ru' } });
    expect(page.doc.getElementById('registerStep').hidden).toBe(true);
    expect(page.doc.getElementById('maskedPhone').textContent).toBe('+998***4567');
    expect(page.doc.getElementById('otpTimerText').textContent).toBe('Код действует ещё 3:00');

    type(page, 'otpCode', '12a3456');
    expect(page.doc.getElementById('otpCode').value).toBe('123456');
    submit(page, 'otpForm');
    await vi.waitFor(() => expect(page.navigations).toEqual(['/my-subscriptions']));
    expect(page.requests[1]).toEqual({
      url: '/api/v1/auth/phone/register/verify',
      body: { phone: '90 123 45 67', otp_code: '123456', first_name: 'Ali', last_name: '', password: 'Abcdefg1', referral_code: null },
    });
  });

  it('sends the referral code a friend\'s link prefilled (/register?ref=CODE) with the sign-up', async () => {
    // register.html prints the ?ref= code into the field (tests/integration/test_web_integrate_ui.py); the script
    // posts that value as referral_code.
    const prefilled = BODY.replace(field('referral_code'), field('referral_code', 'text', 'value="REFAB12CD"'));
    const page = boot({
      pageData: PAGE, body: prefilled, scripts: [SCRIPTS.register], path: '/register?ref=REFAB12CD',
      answers: [
        { status: 200, body: { success: true, data: { phone_masked: '+998***4567', expires_in: 180, resend_available_in: 60 } } },
        { status: 201, body: { success: true } },
      ],
    });
    fillStepOne(page);

    submit(page, 'registerForm');
    await vi.waitFor(() => expect(page.doc.getElementById('otpVerificationSection').hidden).toBe(false));
    type(page, 'otpCode', '123456');
    submit(page, 'otpForm');
    await vi.waitFor(() => expect(page.requests).toHaveLength(2));

    expect(page.requests[1]).toEqual({
      url: '/api/v1/auth/phone/register/verify',
      body: { phone: '90 123 45 67', otp_code: '123456', first_name: 'Ali', last_name: '', password: 'Abcdefg1', referral_code: 'REFAB12CD' },
    });
  });

  it('writes a wrong code under the code field in the page language', async () => {
    const page = boot({
      pageData: PAGE, body: BODY, scripts: [SCRIPTS.register],
      answers: [
        { status: 200, body: { success: true, data: { phone_masked: '+998***4567', expires_in: 180, resend_available_in: 60 } } },
        { status: 400, body: { success: false, message: 'Invalid verification code. 2 attempts remaining.', errors: ['otp_code: Invalid OTP'] } },
      ],
    });
    fillStepOne(page);
    submit(page, 'registerForm');
    await vi.waitFor(() => expect(page.doc.getElementById('otpVerificationSection').hidden).toBe(false));

    type(page, 'otpCode', '000000');
    submit(page, 'otpForm');

    await vi.waitFor(() => {
      expect(page.doc.querySelector('[data-field="otp_code"] .ae-field__error').textContent).toBe('Код не подошёл или устарел.');
    });
    expect(page.doc.body.textContent).not.toContain('attempts remaining');
    expect(page.navigations).toEqual([]);
  });

  it('blocks a password the published rule does not tick, without asking the server', () => {
    const page = boot({ pageData: PAGE, body: BODY, scripts: [SCRIPTS.register] });
    fillStepOne(page, 'abcdefgh1');

    submit(page, 'registerForm');

    expect(page.requests).toEqual([]);
    expect(page.doc.querySelector('[data-field="password"] .ae-field__error').textContent).toBe('Пароль пока не отвечает всем требованиям.');
    const met = [...page.doc.querySelectorAll('[data-rule].is-met')].map((li) => li.dataset.rule);
    expect(met).toEqual(['min_length', 'lowercase', 'digit']);
  });

  it('puts the server refusal of the phone under the phone field', async () => {
    const page = boot({
      pageData: PAGE, body: BODY, scripts: [SCRIPTS.register],
      answers: [{ status: 400, body: { success: false, message: 'Validation failed', errors: ['phone: Value error, Phone number must be valid'] } }],
    });
    fillStepOne(page);

    submit(page, 'registerForm');

    await vi.waitFor(() => {
      expect(page.doc.querySelector('[data-field="phone"] .ae-field__error').textContent).toBe('Укажите мобильный номер');
    });
    expect(page.doc.getElementById('otpVerificationSection').hidden).toBe(true);
  });

  const GUEST_CART = [{ product_id: 2, quantity: 13 }];
  const ACCOUNT_CART = { success: true, data: { cart: { cart_items: [{ product_id: 2, quantity: 13, price: 15000 }] } } };

  it('joins the guest cart to the new account before it leaves for next (phone)', async () => {
    let release;
    const syncAnswered = new Promise((resolve) => { release = resolve; });
    const page = boot({
      pageData: { ...PAGE, after_register_url: '/checkout' }, body: BODY, scripts: [SCRIPTS.register], path: '/register?next=/checkout',
      answers: [
        { status: 200, body: { success: true, data: { phone_masked: '+998***4567', expires_in: 180, resend_available_in: 60 } } },
        { status: 201, body: { success: true } },
        { status: 200, body: ACCOUNT_CART, wait: syncAnswered },
      ],
    });
    page.window.localStorage.setItem('cart', JSON.stringify(GUEST_CART));
    fillStepOne(page);
    submit(page, 'registerForm');
    await vi.waitFor(() => expect(page.doc.getElementById('otpVerificationSection').hidden).toBe(false));
    type(page, 'otpCode', '123456');
    submit(page, 'otpForm');

    await vi.waitFor(() => expect(page.requests).toHaveLength(3));
    expect(page.requests[2]).toEqual({ url: '/api/v1/cart/sync', body: { cart_items: GUEST_CART } });
    expect(page.navigations).toEqual([]); // still waiting for the cart: base.js would empty it on the next page
    release();
    await vi.waitFor(() => expect(page.navigations).toEqual(['/checkout']));
    expect(JSON.parse(page.window.localStorage.getItem('cart'))).toEqual(ACCOUNT_CART.data.cart.cart_items);
  });

  it('sends the page language with an email sign-up, leaves the address to the server, and syncs the cart', async () => {
    const page = boot({
      pageData: { ...PAGE, verify_email_url: '/verify-email?next=%2Fcheckout' }, body: BODY, scripts: [SCRIPTS.register],
      answers: [
        { status: 400, body: { success: false, message: 'Validation failed', errors: ['email: Invalid email format'] } },
        { status: 201, body: { success: true } },
        { status: 200, body: ACCOUNT_CART },
      ],
    });
    page.window.localStorage.setItem('cart', JSON.stringify(GUEST_CART));
    page.doc.querySelector('[data-method="email"]').click();
    fillStepOne(page);
    type(page, 'email', 'ali@mail');

    submit(page, 'registerForm');
    await vi.waitFor(() => {
      expect(page.doc.querySelector('[data-field="email"] .ae-field__error').textContent).toBe('Проверьте адрес email');
    });
    expect(page.requests[0].body).toEqual({
      first_name: 'Ali', last_name: '', password: 'Abcdefg1', referral_code: null, email: 'ali@mail', preferred_language: 'ru',
    });

    type(page, 'email', 'ali@mail.uz');
    submit(page, 'registerForm');
    await vi.waitFor(() => expect(page.navigations).toEqual(['/verify-email?next=%2Fcheckout']));
    expect(page.requests[1]).toMatchObject({ url: '/api/v1/auth/register', body: { email: 'ali@mail.uz', preferred_language: 'ru' } });
    expect(page.requests[2]).toEqual({ url: '/api/v1/cart/sync', body: { cart_items: GUEST_CART } });
  });

  it('meets a number that already has an account with the way in, in the page copy', async () => {
    const page = boot({
      pageData: PAGE, body: BODY, scripts: [SCRIPTS.register],
      answers: [{ status: 409, body: { success: false, message: "Bu telefon raqami allaqachon ro'yxatdan o'tgan" } }],
    });
    fillStepOne(page);

    submit(page, 'registerForm');

    await vi.waitFor(() => expect(page.doc.getElementById('phoneTaken').hidden).toBe(false));
    expect(page.doc.querySelector('[data-field="phone"] .ae-field__error').textContent).toBe('С этим номером уже есть аккаунт.');
    expect(page.doc.getElementById('phone').getAttribute('aria-invalid')).toBe('true');
    expect(page.doc.getElementById('registerAlert').hidden).toBe(true);
    expect(page.doc.body.textContent).not.toContain("ro'yxatdan");
    expect(page.doc.querySelector('.toast-message')).toBeNull();

    type(page, 'phone', '90 765 43 21');
    expect(page.doc.getElementById('phoneTaken').hidden).toBe(true);
  });
});

describe('the password checklist (auth-form.js) against the server', () => {
  // tests/integration/test_web_auth_forms.py proves every verdict in this file is the server's.
  const CASES = JSON.parse(read('tests/fixtures/password_rule_cases.json'));
  const attr = (name, value) => (value === undefined ? '' : ` ${name}="${String(value).replace(/"/g, '&quot;')}"`);
  const CHECKLIST = `<input type="password" id="password"><div id="passwordRules" data-password-rules data-for="password"><ul>${
    CASES.requirements.map((rule) => `<li data-rule="${rule.id}"${attr('data-min-length', rule.min_length)}${attr('data-pattern', rule.pattern)}><i></i></li>`).join('')
  }</ul></div>`;

  it.each(CASES.cases.map((c) => [c.password, c.ticks_all]))('%s ticks every rule: %s', (password, ticksAll) => {
    const page = boot({ pageData: { i18n: {} }, body: CHECKLIST, scripts: [] });
    const rules = page.window.AEAuth.wirePasswordRules(page.doc.getElementById('passwordRules'));
    type(page, 'password', password);

    expect(rules.allMet()).toBe(ticksAll);
  });
});

describe('reset-password.js', () => {
  const PAGE = {
    token: 'emailed-token',
    i18n: { required: 'Fill in', password_rules: 'Not every rule', password_too_common: 'Too easy to guess', passwords_mismatch: 'Mismatch' },
  };
  const BODY = `
    <div id="resetStep">${alert('errorMessage')}
      <form id="resetPasswordForm">${field('password', 'password', 'required')}${RULES}${field('confirm_password', 'password', 'required')}
        <button type="submit"><span>Save</span></button></form>
    </div>
    <div id="successMessage" hidden><h2 id="resetDoneTitle" tabindex="-1"></h2></div>
    <div id="invalidLink" hidden><h2 id="invalidLinkTitle" tabindex="-1"></h2></div>`;

  it('posts the token the server published and shows "link not valid" when the server refuses it', async () => {
    const page = boot({
      pageData: PAGE, body: BODY, scripts: [SCRIPTS.reset], path: '/reset-password/emailed-token',
      answers: [{ status: 400, body: { success: false, message: 'Invalid authentication token' } }],
    });
    type(page, 'password', 'Abcdefg1');
    type(page, 'confirm_password', 'Abcdefg1');

    submit(page, 'resetPasswordForm');

    await vi.waitFor(() => expect(page.doc.getElementById('invalidLink').hidden).toBe(false));
    expect(page.requests).toEqual([{ url: '/api/v1/auth/reset-password', body: { token: 'emailed-token', new_password: 'Abcdefg1' } }]);
    expect(page.doc.getElementById('resetStep').hidden).toBe(true);
  });

  it('says "too easy to guess" when every rule is ticked and the server still refuses the password', async () => {
    const page = boot({
      pageData: PAGE, body: BODY, scripts: [SCRIPTS.reset], path: '/reset-password/emailed-token',
      answers: [{ status: 400, body: { success: false, message: 'error.validation.invalid_password', errors: ['password: password is too common and weak'] } }],
    });
    type(page, 'password', 'Password123');
    type(page, 'confirm_password', 'Password123');

    submit(page, 'resetPasswordForm');

    await vi.waitFor(() => {
      expect(page.doc.querySelector('[data-field="password"] .ae-field__error').textContent).toBe('Too easy to guess');
    });
    expect(page.doc.body.textContent).not.toContain('error.validation');
  });
});

describe('login.js', () => {
  it('never shows an untranslated key; the page message stands in', async () => {
    const page = boot({
      path: '/login',
      pageData: { after_login_url: '/my-account', i18n: { login_failed: 'Не удалось войти.', required: 'Заполните' } },
      body: `${alert('loginAlert')}<form id="loginForm">${field('identifier', 'text', 'required')}${field('password', 'password', 'required')}
        <button type="submit" class="btn-auth"><span>Войти</span></button></form>`,
      scripts: [SCRIPTS.login],
      answers: [{ status: 401, body: { success: false, message: 'api.auth.account_disabled' } }],
    });
    type(page, 'identifier', '+998901234567');
    type(page, 'password', 'secret');

    submit(page, 'loginForm');

    await vi.waitFor(() => expect(page.doc.getElementById('loginAlert').hidden).toBe(false));
    expect(page.doc.querySelector('#loginAlert [data-alert-text]').textContent).toBe('Не удалось войти.');
    expect(page.requests).toEqual([{ url: '/api/v1/auth/login', body: { identifier: '+998901234567', password: 'secret' } }]);
  });

  const LOGIN_I18N = {
    login_failed: 'Не удалось войти. Проверьте телефон или email и пароль.', disabled: 'Вход в этот аккаунт сейчас закрыт.',
    telegram_only: 'Регистрировались в боте? Пароль не нужен.', locked: 'Слишком много попыток.', required: 'Заполните',
  };
  const loginPage = (answers) => {
    const page = boot({
      path: '/login',
      pageData: { after_login_url: '/checkout', i18n: LOGIN_I18N },
      body: `${alert('loginAlert')}<form id="loginForm">${field('identifier', 'text', 'required')}${field('password', 'password', 'required')}
        <button type="submit" class="btn-auth"><span>Войти</span></button></form>`,
      scripts: [SCRIPTS.login],
      answers,
    });
    type(page, 'identifier', '+998901234567');
    type(page, 'password', 'secret');
    return page;
  };

  it.each([
    [401, { error_code: 'INVALID_CREDENTIALS' }, "Noto'g'ri email yoki parol", LOGIN_I18N.login_failed],
    [401, { error_code: 'ACCOUNT_DISABLED' }, 'Account has been disabled', LOGIN_I18N.disabled],
    [401, { error_code: 'TELEGRAM_PASSWORD_REQUIRED' }, 'Password is required for Telegram accounts', LOGIN_I18N.telegram_only],
    [423, undefined, "Hisob ko'p marta noto'g'ri kirishlar tufayli bloklangan", LOGIN_I18N.locked],
  ])('words a %i (%o) in the page copy, once, not the server sentence', async (status, data, serverSentence, expected) => {
    const page = loginPage([{ status, body: { success: false, message: serverSentence, data } }]);

    submit(page, 'loginForm');

    await vi.waitFor(() => expect(page.doc.getElementById('loginAlert').hidden).toBe(false));
    expect(page.doc.querySelector('#loginAlert [data-alert-text]').textContent).toBe(expected);
    expect(page.doc.body.textContent).not.toContain(serverSentence);
    expect(page.doc.querySelector('.toast-message')).toBeNull(); // the alert says it; no toast repeats it
    expect(page.navigations).toEqual([]);
  });

  it('joins the guest cart to the account before it leaves', async () => {
    const page = loginPage([{ status: 200, body: { success: true } }, { status: 200, body: { success: true, data: { cart: { cart_items: [] } } } }]);
    page.window.localStorage.setItem('cart', JSON.stringify([{ product_id: 2, quantity: 3 }]));

    submit(page, 'loginForm');

    await vi.waitFor(() => expect(page.navigations).toEqual(['/checkout']));
    expect(page.requests[1]).toEqual({ url: '/api/v1/cart/sync', body: { cart_items: [{ product_id: 2, quantity: 3 }] } });
  });
});
