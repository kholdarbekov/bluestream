/* Shared behaviour of the sign-in pages (login, register, forgot and reset password, verify email and phone; markup
   from templates/frontend/macros/auth.html). Exposes window.AEAuth; each page script uses it for:
   - the show/hide password buttons (aria-pressed and a translated aria-label);
   - the password checklist, which reads the server's rule from the markup (PasswordValidator.requirements);
   - inline field errors, the form-level alert and the busy state of the submit button;
   - reading an API answer: per-field errors, and the server's message unless it is missing or an untranslated key;
   - the one-time code field (six cells) and its countdown, fed by the server's expires_in;
   - joining a guest cart to the account once the customer is signed in (login and both ways of signing up). */
(function () {
    'use strict';

    // A translation key that has no row comes back as the key itself ("api.auth.something"): never show one.
    var RAW_KEY = /^[a-z0-9_]+(\.[a-z0-9_]+)+$/i;

    function wirePasswordToggles(root) {
        (root || document).querySelectorAll('.password-toggle').forEach(function (button) {
            button.addEventListener('click', function () {
                var input = document.getElementById(button.getAttribute('data-target'));
                if (!input) return;
                var show = input.type === 'password';
                input.type = show ? 'text' : 'password';
                button.setAttribute('aria-pressed', show ? 'true' : 'false');
                var label = button.getAttribute(show ? 'data-label-hide' : 'data-label-show');
                if (label) button.setAttribute('aria-label', label);
                var icon = button.querySelector('i');
                if (icon) {
                    icon.classList.toggle('fa-eye', !show);
                    icon.classList.toggle('fa-eye-slash', show);
                }
            });
        });
    }

    // The rule is checked the way the server's Python checks it (PasswordValidator): len() counts characters (code
    // points), where .length counts UTF-16 units and an emoji would count twice; and Python's \d is any Unicode
    // decimal digit, where JavaScript's is 0-9 only, so \d reads as \p{Nd} in a Unicode pattern.
    function ruleMet(item, value) {
        var min = item.getAttribute('data-min-length');
        if (min) return Array.from(value).length >= parseInt(min, 10);
        var pattern = item.getAttribute('data-pattern');
        if (pattern) return new RegExp(pattern.replace(/\\d/g, '\\p{Nd}'), 'u').test(value);
        return true;
    }

    // Ticks each rule as the customer types. Returns { allMet() } for the submit check.
    function wirePasswordRules(container, labels) {
        if (!container) return { allMet: function () { return true; } };
        var input = document.getElementById(container.getAttribute('data-for'));
        var items = Array.prototype.slice.call(container.querySelectorAll('[data-rule]'));
        labels = labels || {};

        function update() {
            var value = input ? input.value : '';
            var met = 0;
            items.forEach(function (item) {
                var ok = ruleMet(item, value);
                if (ok) met += 1;
                item.classList.toggle('is-met', ok);
                var icon = item.querySelector('i');
                if (icon) {
                    icon.classList.toggle('fa-check-circle', ok);
                    icon.classList.toggle('fa-circle', !ok);
                }
                var state = item.querySelector('[data-rule-state]');
                if (state && labels.met && labels.unmet) state.textContent = ok ? labels.met : labels.unmet;
            });
            return met === items.length;
        }

        if (input) input.addEventListener('input', function () {
            container.classList.remove('is-invalid');
            update();
        });
        update();
        return {
            allMet: function () {
                var ok = update();
                container.classList.toggle('is-invalid', !ok);
                return ok;
            }
        };
    }

    function fieldOf(form, name) {
        return form.querySelector('[data-field="' + name + '"]');
    }

    function setFieldError(form, name, message) {
        var field = fieldOf(form, name);
        if (!field) return false;
        var input = field.querySelector('input');
        var slot = field.querySelector('.ae-field__error');
        field.classList.toggle('ae-field--invalid', Boolean(message));
        if (input) {
            if (message) input.setAttribute('aria-invalid', 'true');
            else input.removeAttribute('aria-invalid');
        }
        if (slot) slot.textContent = message || '';
        return true;
    }

    function clearErrors(form) {
        form.querySelectorAll('[data-field]').forEach(function (field) {
            setFieldError(form, field.getAttribute('data-field'), '');
        });
    }

    // Clears a field's error as soon as the customer edits it.
    function clearOnInput(form) {
        form.addEventListener('input', function (event) {
            var field = event.target.closest('[data-field]');
            if (field && field.classList.contains('ae-field--invalid')) setFieldError(form, field.getAttribute('data-field'), '');
        });
    }

    function focusFirstInvalid(form) {
        var input = form.querySelector('[aria-invalid="true"]');
        if (input) input.focus();
        return Boolean(input);
    }

    // Marks every empty required field that is shown and enabled. Returns true when none is empty.
    function requireFields(form, message) {
        var ok = true;
        form.querySelectorAll('input[required]').forEach(function (input) {
            if (input.disabled || input.closest('[hidden]')) return;
            var empty = input.type === 'checkbox' ? !input.checked : !String(input.value || '').trim();
            if (empty) {
                ok = false;
                setFieldError(form, input.closest('[data-field]') ? input.closest('[data-field]').getAttribute('data-field') : input.name, message);
            }
        });
        return ok;
    }

    function showAlert(id, message) {
        var alert = document.getElementById(id);
        if (!alert) return;
        var text = alert.querySelector('[data-alert-text]') || alert;
        text.textContent = message;
        alert.hidden = false;
    }

    function hideAlert(id) {
        var alert = document.getElementById(id);
        if (alert) alert.hidden = true;
    }

    // The spinner state (.ae-btn.is-loading). Not `disabled`, which would fade the button; isBusy() guards a second
    // submit from the keyboard instead.
    function setBusy(button, busy) {
        if (!button) return;
        button.classList.toggle('is-loading', busy);
        if (busy) button.setAttribute('aria-busy', 'true');
        else button.removeAttribute('aria-busy');
    }

    function isBusy(button) {
        return Boolean(button) && button.classList.contains('is-loading');
    }

    async function readJson(response) {
        try {
            return await response.json();
        } catch (error) {
            return {};
        }
    }

    // The API's per-field errors: {field: [messages]} from `errors` ("field: message" strings) or `details`.
    function fieldErrors(result) {
        var out = {};
        var list = result && result.errors;
        if (Array.isArray(list)) {
            list.forEach(function (entry) {
                var match = /^([a-z_]+):\s*(.*)$/i.exec(String(entry));
                if (match) (out[match[1]] = out[match[1]] || []).push(match[2]);
            });
        }
        var details = result && result.details;
        if (details && typeof details === 'object') {
            Object.keys(details).forEach(function (key) {
                if (Array.isArray(details[key])) out[key] = (out[key] || []).concat(details[key]);
            });
        }
        return out;
    }

    function serverMessage(result, fallback) {
        var message = result && typeof result.message === 'string' ? result.message.trim() : '';
        return message && !RAW_KEY.test(message) ? message : fallback;
    }

    // The one-time code input (macros/auth.html otp_field): digits only, drawn into the six cells. Returns
    // { render(), reset() }.
    function wireOtp(input) {
        var cells = input.closest('.ae-otp').querySelectorAll('.ae-otp__cell');

        function render() {
            var value = input.value;
            cells.forEach(function (cell, index) {
                cell.textContent = value.charAt(index);
                cell.classList.toggle('is-filled', index < value.length);
                cell.classList.toggle('is-current', index === Math.min(value.length, cells.length - 1));
            });
        }

        input.addEventListener('input', function () {
            var digits = input.value.replace(/[^0-9]/g, '').slice(0, 6);
            if (digits !== input.value) input.value = digits;
            render();
        });
        input.addEventListener('focus', render);
        render();
        return {
            render: render,
            reset: function () {
                input.value = '';
                render();
            }
        };
    }

    function formatTime(seconds) {
        var minutes = Math.floor(seconds / 60);
        return minutes + ':' + String(seconds % 60).padStart(2, '0');
    }

    // The code's countdown line: both times come from the server (expires_in, and resend_available_in where the flow
    // has a resend wait; without one the resend button shows at once). `line` carries the {time} template in
    // data-template; `expiredText` replaces it at zero. Returns a stop() function.
    // Ticks need not land on whole seconds (a busy page, or a hidden tab whose timers the browser runs late), so the
    // time left is rounded up, reaching zero only at expiry, and the clock keeps ticking until the resend button has
    // shown: when both times are the same instant, an early stop would leave the code expired with no way to a new one.
    function countdown(line, resendButton, expiresIn, resendAvailableIn, expiredText) {
        var startedAt = Date.now();
        var expiresAt = startedAt + (Number(expiresIn) || 0) * 1000;
        var resendAt = startedAt + (Number(resendAvailableIn) || 0) * 1000;
        var template = line.getAttribute('data-template') || '{time}';
        var interval = null;

        if (resendButton) resendButton.hidden = true;

        function tick() {
            var now = Date.now();
            var left = Math.max(0, Math.ceil((expiresAt - now) / 1000));
            var resendReady = now >= resendAt;
            line.textContent = left > 0 ? template.replace('{time}', formatTime(left)) : expiredText;
            line.classList.toggle('is-expired', left === 0);
            if (resendButton && resendReady) resendButton.hidden = false;
            if (left === 0 && resendReady) clearInterval(interval);
        }
        tick();
        interval = setInterval(tick, 1000);
        return function stop() { clearInterval(interval); };
    }

    // A guest cart kept in the browser (localStorage 'cart') joins the account's cart. Call it once the answer has
    // signed the customer in and await it before leaving the page: on every signed-in page base.js replaces the
    // browser cart with the account's, so a cart not sent by then is lost.
    async function syncGuestCart() {
        var localCart;
        try {
            localCart = JSON.parse(localStorage.getItem('cart') || '[]');
        } catch (error) {
            return;
        }
        if (!Array.isArray(localCart) || !localCart.length) return;
        try {
            var response = await apiRequest('/cart/sync', {
                method: 'POST',
                body: JSON.stringify({ cart_items: localCart })
            });
            if (!response.ok) return;
            var syncData = await readJson(response);
            if (syncData.success && syncData.data && syncData.data.cart) {
                localStorage.setItem('cart', JSON.stringify(syncData.data.cart.cart_items || []));
            }
        } catch (error) {
            console.error('Cart sync failed:', error);
        }
    }

    window.AEAuth = {
        wirePasswordToggles: wirePasswordToggles,
        wirePasswordRules: wirePasswordRules,
        setFieldError: setFieldError,
        clearErrors: clearErrors,
        clearOnInput: clearOnInput,
        focusFirstInvalid: focusFirstInvalid,
        requireFields: requireFields,
        showAlert: showAlert,
        hideAlert: hideAlert,
        setBusy: setBusy,
        isBusy: isBusy,
        readJson: readJson,
        fieldErrors: fieldErrors,
        serverMessage: serverMessage,
        wireOtp: wireOtp,
        countdown: countdown,
        syncGuestCart: syncGuestCart
    };
})();
