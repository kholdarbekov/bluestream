// The verify pages' scripts (website redesign step 3, group "auth", unit "auth-verify", 2026-11-14): auth-form.js,
// verify-phone.js and verify-email.js run in jsdom as the pages load them; only `fetch` is replaced, answering each
// request in turn with the backend's answer shapes.
// - verify-phone.js sends the number as typed (the cookies are the identity, no user_id), runs the countdown from the
//   server's expires_in, shows "send a new code" only once the server's resend_available_in has passed (the code's
//   expiry), posts only the code, and shows the number the server saved; a wrong code, a lapsed request and a number
//   another account holds each land where the customer can act on them; a 429 RESEND_COOLDOWN (the number's code is
//   still valid) opens the code step with the page's own note and the server's time left, never the server sentence;
//   "send a new code" still shows at expiry when the page's ticks land off whole seconds (a busy page, a hidden tab);
// - verify-email.js posts the link's token at once, swaps to the result, writes a typed code's error under the field,
//   and rests the "send again" button with the translated {seconds} template.
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
  verifyPhone: 'business_app/static/js/pages/verify-phone.js',
  verifyEmail: 'business_app/static/js/pages/verify-email.js',
};
const BASE = {
  api_base_url: '/api/v1', current_user: null, is_authenticated: false, login_url: '/login', logout_url: '/logout', i18n: {},
};
const field = (name, id = name, extra = '') => `
  <div class="ae-field" data-field="${name}"><input type="text" id="${id}" name="${name}" ${extra}>
  <p class="ae-field__error" id="${id}-error"></p></div>`;
const alert = (id) => `<div class="ae-alert" id="${id}" hidden><p data-alert-text></p></div>`;

// The page's clock, moved by hand: Date.now and the countdown's one-second interval (any other timer stays real).
function fakeClock(start = Date.UTC(2026, 10, 14, 9)) {
  const timers = new Map();
  const clock = { now: start, next: 1 };
  clock.install = (window) => {
    const realSet = window.setInterval.bind(window);
    const realClear = window.clearInterval.bind(window);
    window.Date.now = () => clock.now;
    window.setInterval = (fn, ms) => {
      if (ms !== 1000) return realSet(fn, ms);
      clock.next += 1;
      timers.set(-clock.next, fn);
      return -clock.next;
    };
    window.clearInterval = (id) => (timers.has(id) ? timers.delete(id) : realClear(id));
  };
  clock.advance = (seconds) => {
    for (let i = 0; i < seconds; i += 1) {
      clock.now += 1000;
      [...timers.values()].forEach((tick) => tick());
    }
  };
  // Moves the time without a tick, so the ticks after it land `ms` past each whole second since the send.
  clock.shift = (ms) => { clock.now += ms; };
  clock.pending = () => timers.size;
  return clock;
}

function boot({ pageData, body, script, answers = [], pagePath, clock }) {
  const dom = new JSDOM(
    `<!doctype html><html><head></head><body>
      <script type="application/json" id="base-page-data">${JSON.stringify(BASE)}</script>
      <script type="application/json" id="page-data">${JSON.stringify(pageData)}</script>${body}</body></html>`,
    { url: `http://localhost${pagePath}`, runScripts: 'outside-only', pretendToBeVisual: true },
  );
  const { window } = dom;
  if (clock) clock.install(window);
  const requests = [];
  window.fetch = async (url, options = {}) => {
    requests.push({ url, body: options.body ? JSON.parse(options.body) : undefined });
    const answer = answers[requests.length - 1];
    if (!answer) return new Response('{}', { status: 599 });
    return new Response(JSON.stringify(answer.body || {}), { status: answer.status, headers: { 'Content-Type': 'application/json' } });
  };
  window.showNotification = () => {};
  [SCRIPTS.pageData, SCRIPTS.base, SCRIPTS.authForm, script].map(read).forEach((source) => window.eval(source));
  window.dispatchEvent(new window.Event('DOMContentLoaded'));
  return { window, doc: window.document, requests };
}

const settle = () => new Promise((resolve) => { setTimeout(resolve, 0); });
const flush = async () => { for (let i = 0; i < 6; i += 1) await settle(); }; // eslint-disable-line no-await-in-loop
const type = (page, id, value) => {
  const input = page.doc.getElementById(id);
  input.value = value;
  input.dispatchEvent(new page.window.Event('input', { bubbles: true }));
};
const submit = (page, id) => page.doc.getElementById(id).dispatchEvent(new page.window.Event('submit', { cancelable: true }));

describe('verify-phone.js', () => {
  const I18N = {
    need_phone: 'Укажите номер', invalid_phone: 'Неверный номер', taken: 'Номер занят', send_failed: 'Не отправили',
    new_code_sent: 'Новый код', resend_failed: 'Не отправили код', incomplete_otp: 'Код целиком',
    wrong_code: 'Код не подошёл', too_many_codes: 'Слишком много', code_expired: 'Код истёк', request_expired: 'Запрос устарел',
    verification_failed: 'Не удалось', rate_limited: 'Подождите', network_error: 'Нет связи',
  };
  const BODY = `
    <div id="phoneStep">${alert('phoneAlert')}
      <form id="phoneForm">${field('phone')}<button type="submit" id="sendCodeBtn"></button></form></div>
    <div id="otpStep" hidden><strong id="sentToPhone"></strong>
      <div class="ae-alert" id="codeStillValid" role="status" hidden><p>Код, который мы отправили, ещё действует</p></div>
      ${alert('otpAlert')}
      <form id="otpForm">
        <div class="ae-field" data-field="otp"><div class="ae-otp"><input id="otpCode" name="otp" maxlength="6">
          <div class="ae-otp__cells">${'<span class="ae-otp__cell"></span>'.repeat(6)}</div></div>
          <p class="ae-field__error" id="otpCode-error"></p></div>
        <p id="timer" data-template="Код действует ещё {time}"></p><button type="button" id="resendCode" hidden></button>
        <button type="submit" id="verifyCodeBtn"></button></form>
      <button type="button" id="changePhone"></button></div>
    <div id="successMessage" hidden><h2 id="phoneDoneTitle" tabindex="-1"></h2>
      <p id="phoneDoneText" data-template="Теперь в аккаунте номер {phone}."></p></div>`;
  const open = (answers, clock) => boot({
    pageData: { i18n: I18N }, body: BODY, script: SCRIPTS.verifyPhone, answers, pagePath: '/verify-phone', clock,
  });
  const toCode = async (page) => {
    type(page, 'phone', '+998 90 765 43 21');
    submit(page, 'phoneForm');
    await flush();
  };

  it('sends the number as typed, counts down from expires_in, and shows the number the server saved', async () => {
    const page = open([
      { status: 200, body: { success: true, data: { phone_masked: '+99***4321', expires_in: 300, resend_available_in: 300 } } },
      { status: 200, body: { success: true, data: { phone: '+998907654321', phone_verified: true } } },
    ]);
    await toCode(page);

    expect(page.requests[0]).toEqual({ url: '/api/v1/auth/send-otp', body: { phone: '+998 90 765 43 21' } });
    expect(page.doc.getElementById('otpStep').hidden).toBe(false);
    expect(page.doc.getElementById('phoneStep').hidden).toBe(true);
    expect(page.doc.getElementById('sentToPhone').textContent).toBe('+998 90 765 43 21');
    expect(page.doc.getElementById('timer').textContent).toBe('Код действует ещё 5:00');
    expect(page.doc.getElementById('resendCode').hidden).toBe(true); // a new code only once this one expires
    expect(page.doc.getElementById('codeStillValid').hidden).toBe(true);

    type(page, 'otpCode', '12a3456');
    expect(page.doc.getElementById('otpCode').value).toBe('123456');
    expect(page.doc.querySelectorAll('.ae-otp__cell.is-filled')).toHaveLength(6);
    submit(page, 'otpForm');
    await flush();

    expect(page.requests[1]).toEqual({ url: '/api/v1/auth/verify-phone', body: { otp: '123456' } });
    expect(page.doc.getElementById('successMessage').hidden).toBe(false);
    expect(page.doc.getElementById('phoneDoneText').textContent).toBe('Теперь в аккаунте номер +998907654321.');
  });

  it('shows "send a new code" only once the server\'s resend_available_in has passed, and a new code restarts the wait', async () => {
    const clock = fakeClock();
    const page = open([
      { status: 200, body: { success: true, data: { expires_in: 300, resend_available_in: 300 } } },
      { status: 200, body: { success: true, data: { expires_in: 300, resend_available_in: 300 } } },
    ], clock);
    const resend = page.doc.getElementById('resendCode');
    const timer = page.doc.getElementById('timer');
    await toCode(page);

    clock.advance(299);
    expect(timer.textContent).toBe('Код действует ещё 0:01');
    expect(resend.hidden).toBe(true);

    clock.advance(1);
    expect(timer.textContent).toBe('Код истёк');
    expect(resend.hidden).toBe(false);

    resend.click();
    await flush();
    expect(page.requests[1]).toEqual({ url: '/api/v1/auth/send-otp', body: { phone: '+998 90 765 43 21' } });
    expect(timer.textContent).toBe('Код действует ещё 5:00');
    expect(resend.hidden).toBe(true);
  });

  // Review RESEND-2: the countdown stopped once the rounded time left read 0, up to half a second before the resend
  // time (the same instant on this page), so a tick landing in that last half second left the code expired with the
  // button hidden for good. A tick 1 ms early is one 999 ms late from the second before.
  it.each([1, 600, 999])('still shows "send a new code" at expiry when each tick lands %i ms past the second', async (phaseMs) => {
    const clock = fakeClock();
    const page = open([{ status: 200, body: { success: true, data: { expires_in: 300, resend_available_in: 300 } } }], clock);
    const resend = page.doc.getElementById('resendCode');
    const timer = page.doc.getElementById('timer');
    await toCode(page);
    clock.shift(phaseMs);

    clock.advance(299); // the last tick before expiry: under a second of the code's life left
    expect(timer.textContent).toBe('Код действует ещё 0:01');
    expect(resend.hidden).toBe(true);

    clock.advance(1);
    expect(timer.textContent).toBe('Код истёк');
    expect(resend.hidden).toBe(false);
    expect(clock.pending()).toBe(0); // the clock stops only once the button has shown
  });

  it('opens the code step with its own note when the number\'s code is still valid (429 RESEND_COOLDOWN)', async () => {
    const SERVER = 'The code we already sent to this number still works — enter it.';
    const clock = fakeClock();
    const page = open([
      { status: 429, body: { success: false, message: SERVER, data: { error_code: 'RESEND_COOLDOWN', expires_in: 120, resend_available_in: 120 } } },
      { status: 200, body: { success: true, data: { expires_in: 300, resend_available_in: 300 } } },
    ], clock);
    const resend = page.doc.getElementById('resendCode');
    await toCode(page);

    expect(page.doc.getElementById('otpStep').hidden).toBe(false);
    expect(page.doc.getElementById('codeStillValid').hidden).toBe(false);
    expect(page.doc.getElementById('phoneAlert').hidden).toBe(true);
    expect(page.doc.getElementById('otpAlert').hidden).toBe(true);
    expect(page.doc.body.textContent).not.toContain(SERVER);
    expect(page.doc.getElementById('timer').textContent).toBe('Код действует ещё 2:00');
    expect(resend.hidden).toBe(true);

    clock.advance(119);
    expect(resend.hidden).toBe(true);
    clock.advance(1);
    expect(resend.hidden).toBe(false);

    resend.click();
    await flush();
    expect(page.requests).toHaveLength(2);
    expect(page.doc.getElementById('codeStillValid').hidden).toBe(true);
    expect(page.doc.getElementById('timer').textContent).toBe('Код действует ещё 5:00');
  });

  it('keeps the address-wide limit (a 429 without RESEND_COOLDOWN) on the number step with the page\'s wait copy', async () => {
    const page = open([{ status: 429, body: { success: false, message: 'Rate limit exceeded. Try again in 41 seconds.' } }]);
    await toCode(page);

    expect(page.doc.getElementById('otpStep').hidden).toBe(true);
    expect(page.doc.querySelector('#phoneAlert [data-alert-text]').textContent).toBe('Подождите');
  });

  it('puts a wrong code under the field, and a lapsed request back on the number step', async () => {
    const page = open([
      { status: 200, body: { success: true, data: { expires_in: 300 } } },
      { status: 400, body: { success: false, message: 'Неверный email или пароль' } },
      { status: 404, body: { success: false, message: 'Ресурс не найден' } },
    ]);
    await toCode(page);

    type(page, 'otpCode', '000000');
    submit(page, 'otpForm');
    await flush();
    expect(page.doc.getElementById('otpCode-error').textContent).toBe('Код не подошёл');
    expect(page.doc.getElementById('otpCode').value).toBe('');

    type(page, 'otpCode', '111111');
    submit(page, 'otpForm');
    await flush();
    expect(page.doc.getElementById('phoneStep').hidden).toBe(false);
    expect(page.doc.getElementById('phoneAlert').hidden).toBe(false);
    expect(page.doc.querySelector('#phoneAlert [data-alert-text]').textContent).toBe('Запрос устарел');
  });

  it('words a number another account holds and a bad number in the page copy, never the server sentence', async () => {
    // The server's 409 sentence was uz "ro'yxatdan o'tgan" with straight apostrophes (review AUTH-R3).
    const page = open([
      { status: 409, body: { success: false, message: "Telefon raqami allaqachon ro'yxatdan o'tgan" } },
      { status: 400, body: { success: false, errors: ['Неверный формат номера телефона'] } },
    ]);
    await toCode(page);
    expect(page.doc.getElementById('phone-error').textContent).toBe('Номер занят');
    expect(page.doc.getElementById('otpStep').hidden).toBe(true);

    await toCode(page);
    expect(page.doc.getElementById('phone-error').textContent).toBe('Неверный номер');
  });
});

describe('verify-email.js', () => {
  const I18N = {
    required: 'Введите код', invalid: 'Код не подошёл', invalid_link: 'Ссылка не работает', sent: 'Отправили',
    send_failed: 'Не отправили', rate_limited: 'Подождите', network_error: 'Нет связи',
  };
  const BODY = (checking) => `
    <div id="verifyStep">${checking ? '<div id="checkingNote"></div>' : ''}${alert('verifyAlert')}
      <div id="verifyBody"${checking ? ' hidden' : ''}>
        <form id="emailVerificationForm">${field('token', 'verificationCode')}<button type="submit" id="verifyEmailBtn"></button></form>
        <button type="button" id="resendEmail"><span>Отправить письмо ещё раз</span></button>
        <p id="resendNote" hidden></p></div></div>
    <div id="successMessage" hidden><h2 id="verifyDoneTitle" tabindex="-1"></h2></div>`;

  it('posts the link token at once and shows the result', async () => {
    const page = boot({ pageData: { token: 'tok-123', i18n: I18N }, body: BODY(true), script: SCRIPTS.verifyEmail, answers: [{ status: 200, body: { success: true } }], pagePath: '/verify-email?token=tok-123' });
    await flush();

    expect(page.requests).toEqual([{ url: '/api/v1/auth/verify-email', body: { token: 'tok-123' } }]);
    expect(page.doc.getElementById('successMessage').hidden).toBe(false);
    expect(page.doc.getElementById('verifyStep').hidden).toBe(true);
  });

  it('turns a refused link into the code form with the link error above it', async () => {
    const page = boot({ pageData: { token: 'old', i18n: I18N }, body: BODY(true), script: SCRIPTS.verifyEmail, answers: [{ status: 400, body: { success: false, message: 'api.auth.token_invalid' } }], pagePath: '/verify-email?token=old' });
    await flush();

    expect(page.doc.getElementById('verifyBody').hidden).toBe(false);
    expect(page.doc.getElementById('checkingNote').hidden).toBe(true);
    expect(page.doc.querySelector('#verifyAlert [data-alert-text]').textContent).toBe('Ссылка не работает');
    expect(page.doc.body.textContent).not.toContain('api.auth.');
  });

  it('writes a typed code error under the field and says when a new letter went out, with no pause of its own', async () => {
    const page = boot({
      pageData: { token: null, i18n: I18N }, body: BODY(false), script: SCRIPTS.verifyEmail, pagePath: '/verify-email',
      answers: [{ status: 400, body: { success: false } }, { status: 200, body: { success: true } }],
    });
    submit(page, 'emailVerificationForm');
    expect(page.doc.getElementById('verificationCode-error').textContent).toBe('Введите код');
    expect(page.requests).toHaveLength(0);

    type(page, 'verificationCode', ' ab cd ');
    submit(page, 'emailVerificationForm');
    await flush();
    expect(page.requests[0]).toEqual({ url: '/api/v1/auth/verify-email', body: { token: 'abcd' } });
    expect(page.doc.getElementById('verificationCode-error').textContent).toBe('Код не подошёл');

    page.doc.getElementById('resendEmail').click();
    await flush();
    expect(page.requests[1].url).toBe('/api/v1/auth/resend-email-verification');
    expect(page.doc.getElementById('resendNote').textContent).toBe('Отправили');
    // The server's own limit (5 an hour, a 429) is the only wait: the page invents none (review AUTH-R9).
    expect(page.doc.getElementById('resendEmail').disabled).toBe(false);
    expect(page.doc.querySelector('#resendEmail span').textContent).toBe('Отправить письмо ещё раз');
  });
});
