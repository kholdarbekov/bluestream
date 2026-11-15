/* Registration page (templates/frontend/register.html).
   By phone: /auth/phone/register/init sends an SMS code, the code step replaces the card, and
   /auth/phone/register/verify creates the account and signs the customer in, who then goes to `after_register_url`
   (a safe `next`, else the account). By email: /auth/register (in the page's language, which the account keeps for its
   letters) signs the customer in too, then the verify-email page, which carries `next` on. Either way a guest cart
   joins the new account before the page is left (AEAuth.syncGuestCart).
   The server is the one judge of the phone number, the email address and the password: the page sends what was typed
   and shows the server's answer under the field it is about; the checklist reads the server's password rule from the
   markup. A number or address that already has an account (409) gets the way in instead: sign in, or the bot. */
(function () {
    var PAGE_DATA = getPageData();
    var I18N = PAGE_DATA.i18n || {};
    var registrationData = {};
    var stopOtpTimer = null;
    var otp = null;

    var step = document.getElementById('registerStep');
    var otpSection = document.getElementById('otpVerificationSection');
    var registerForm = document.getElementById('registerForm');
    var otpForm = document.getElementById('otpForm');
    var otpInput = document.getElementById('otpCode');
    var rules;

    function method() {
        var active = registerForm.querySelector('[data-method][aria-pressed="true"]');
        return active ? active.getAttribute('data-method') : 'phone';
    }

    function chooseMethod(name) {
        registerForm.querySelectorAll('[data-method]').forEach(function (button) {
            button.setAttribute('aria-pressed', button.getAttribute('data-method') === name ? 'true' : 'false');
        });
        registerForm.querySelectorAll('[data-method-panel]').forEach(function (panel) {
            var on = panel.getAttribute('data-method-panel') === name;
            panel.hidden = !on;
            panel.querySelectorAll('input').forEach(function (input) {
                input.disabled = !on;
                input.required = on;
            });
        });
        AEAuth.clearErrors(registerForm);
        AEAuth.hideAlert('registerAlert');
        hideTaken();
    }

    function fail(alertId, message) {
        AEAuth.showAlert(alertId, message);
    }

    // 409: the number (or address) already has an account. The field says so; the panel under it offers the way in.
    function showTaken(how) {
        hideTaken();
        AEAuth.setFieldError(registerForm, how, how === 'email' ? I18N.email_taken : I18N.phone_taken);
        var panel = document.getElementById(how === 'email' ? 'emailTaken' : 'phoneTaken');
        if (panel) panel.hidden = false;
        AEAuth.focusFirstInvalid(registerForm);
    }

    function hideTaken() {
        ['phoneTaken', 'emailTaken'].forEach(function (id) {
            var panel = document.getElementById(id);
            if (panel) panel.hidden = true;
        });
    }

    // Puts the server's per-field errors under their fields. Returns true when at least one was placed.
    function placeFieldErrors(result) {
        var errors = AEAuth.fieldErrors(result);
        var placed = false;
        if (errors.phone) placed = AEAuth.setFieldError(registerForm, 'phone', I18N.invalid_phone) || placed;
        if (errors.email) placed = AEAuth.setFieldError(registerForm, 'email', I18N.invalid_email) || placed;
        if (errors.password) {
            // Every published rule met and still refused: the server's list of too-common passwords.
            var message = rules.allMet() ? I18N.password_too_common : I18N.password_rules;
            placed = AEAuth.setFieldError(registerForm, 'password', message) || placed;
        }
        if (errors.first_name) placed = AEAuth.setFieldError(registerForm, 'first_name', I18N.required) || placed;
        if (placed) AEAuth.focusFirstInvalid(registerForm);
        return placed;
    }

    // The code's countdown and the resend wait both come from the server (expires_in, resend_available_in).
    function startOtpTimer(expiresIn, resendAvailableIn) {
        if (stopOtpTimer) stopOtpTimer();
        stopOtpTimer = AEAuth.countdown(document.getElementById('otpTimerText'), document.getElementById('resendOtpBtn'),
            expiresIn, resendAvailableIn, I18N.code_expired);
    }

    function resetOtp() {
        if (otp) otp.reset();
        AEAuth.clearErrors(otpForm);
        AEAuth.hideAlert('otpAlert');
    }

    function showOtpStep(maskedPhone) {
        step.hidden = true;
        otpSection.hidden = false;
        document.getElementById('maskedPhone').textContent = maskedPhone;
        resetOtp();
        otpInput.focus();
    }

    async function initiatePhoneRegistration(submitBtn) {
        try {
            var response = await apiRequest('/auth/phone/register/init', {
                method: 'POST',
                body: JSON.stringify({ phone: registrationData.phone, preferred_language: PAGE_DATA.locale })
            });
            var result = await AEAuth.readJson(response);

            if (response.ok) {
                var data = result.data || {};
                showOtpStep(data.phone_masked || registrationData.phone);
                startOtpTimer(data.expires_in, data.resend_available_in);
            } else if (response.status === 409) {
                showTaken('phone');
            } else if (response.status === 429) {
                // The per-number resend wait, a lockout after wrong codes or the per-address limit.
                fail('registerAlert', I18N.resend_wait);
            } else if (response.status === 400 && placeFieldErrors(result)) {
                AEAuth.showAlert('registerAlert', I18N.check_fields);
            } else {
                fail('registerAlert', AEAuth.serverMessage(result, I18N.send_failed));
            }
        } catch (error) {
            fail('registerAlert', I18N.network_error);
        } finally {
            AEAuth.setBusy(submitBtn, false);
        }
    }

    async function registerWithEmail(submitBtn) {
        try {
            var response = await apiRequest('/auth/register', {
                method: 'POST',
                body: JSON.stringify(Object.assign({}, registrationData, { preferred_language: PAGE_DATA.locale }))
            });
            var result = await AEAuth.readJson(response);

            if (response.ok) {
                // The answer set the login cookies: the guest cart joins the new account before the page is left.
                await AEAuth.syncGuestCart();
                showNotification(I18N.account_created_email, 'success');
                window.location.href = PAGE_DATA.verify_email_url;
                return;
            }
            AEAuth.setBusy(submitBtn, false);
            if (response.status === 409) showTaken('email');
            else if (response.status === 429) fail('registerAlert', I18N.rate_limited);
            else if (response.status === 400 && placeFieldErrors(result)) AEAuth.showAlert('registerAlert', I18N.check_fields);
            else fail('registerAlert', AEAuth.serverMessage(result, I18N.registration_failed));
        } catch (error) {
            AEAuth.setBusy(submitBtn, false);
            fail('registerAlert', I18N.network_error);
        }
    }

    async function resendOtp() {
        var resendBtn = document.getElementById('resendOtpBtn');
        if (AEAuth.isBusy(resendBtn)) return;
        AEAuth.setBusy(resendBtn, true);
        AEAuth.hideAlert('otpAlert');

        try {
            var response = await apiRequest('/auth/phone/resend-otp', {
                method: 'POST',
                body: JSON.stringify({ phone: registrationData.phone })
            });
            var result = await AEAuth.readJson(response);

            if (response.ok) {
                var data = result.data || {};
                resetOtp();
                startOtpTimer(data.expires_in, data.resend_available_in);
                showNotification(I18N.new_code_sent, 'success');
                otpInput.focus();
            } else if (response.status === 429) {
                fail('otpAlert', I18N.resend_wait);
            } else {
                fail('otpAlert', AEAuth.serverMessage(result, I18N.resend_failed));
            }
        } catch (error) {
            fail('otpAlert', I18N.network_error);
        } finally {
            AEAuth.setBusy(resendBtn, false);
        }
    }

    function goBackToForm() {
        if (stopOtpTimer) stopOtpTimer();
        otpSection.hidden = true;
        step.hidden = false;
        resetOtp();
        var phone = document.getElementById('phone');
        if (phone && !phone.disabled) phone.focus();
    }

    function validateForm(formData) {
        var ok = AEAuth.requireFields(registerForm, I18N.required);
        var how = method();
        if (how === 'phone' && !String(formData.get('phone') || '').trim()) {
            AEAuth.setFieldError(registerForm, 'phone', I18N.need_phone);
        }
        // Only "is it there": the server judges the address (a 400 `email` error, placed under the field).
        if (how === 'email' && !String(formData.get('email') || '').trim()) {
            AEAuth.setFieldError(registerForm, 'email', I18N.need_email);
        }
        var password = formData.get('password') || '';
        if (password && !rules.allMet()) {
            ok = false;
            AEAuth.setFieldError(registerForm, 'password', I18N.password_rules);
        }
        var confirm = formData.get('confirm_password') || '';
        if (password && confirm && password !== confirm) {
            ok = false;
            AEAuth.setFieldError(registerForm, 'confirm_password', I18N.passwords_mismatch);
        }
        if (!registerForm.querySelector('input[name="terms"]').checked) {
            ok = false;
            AEAuth.setFieldError(registerForm, 'terms', I18N.need_terms);
        }
        return ok;
    }

    window.addEventListener('DOMContentLoaded', function () {
        if (!registerForm) return;

        AEAuth.wirePasswordToggles(document);
        AEAuth.clearOnInput(registerForm);
        AEAuth.clearOnInput(otpForm);
        rules = AEAuth.wirePasswordRules(document.getElementById('passwordRules'), { met: I18N.rule_met, unmet: I18N.rule_unmet });

        registerForm.querySelectorAll('[data-method]').forEach(function (button) {
            button.addEventListener('click', function () { chooseMethod(button.getAttribute('data-method')); });
        });

        registerForm.querySelector('input[name="terms"]').addEventListener('change', function () {
            AEAuth.setFieldError(registerForm, 'terms', '');
        });

        ['phone', 'email'].forEach(function (id) {
            var input = document.getElementById(id);
            if (input) input.addEventListener('input', hideTaken);
        });

        registerForm.addEventListener('submit', async function (e) {
            e.preventDefault();

            var submitBtn = registerForm.querySelector('.btn-auth');
            if (AEAuth.isBusy(submitBtn)) return;
            AEAuth.clearErrors(registerForm);
            AEAuth.hideAlert('registerAlert');
            hideTaken();

            var formData = new FormData(registerForm);
            if (!validateForm(formData)) {
                AEAuth.focusFirstInvalid(registerForm);
                return;
            }

            registrationData = {
                first_name: String(formData.get('first_name') || '').trim(),
                last_name: String(formData.get('last_name') || '').trim(),
                password: formData.get('password'),
                referral_code: String(formData.get('referral_code') || '').trim() || null
            };

            AEAuth.setBusy(submitBtn, true);
            if (method() === 'phone') {
                registrationData.phone = String(formData.get('phone') || '').trim();
                await initiatePhoneRegistration(submitBtn);
            } else {
                registrationData.email = String(formData.get('email') || '').trim();
                await registerWithEmail(submitBtn);
            }
        });

        otp = AEAuth.wireOtp(otpInput);

        otpForm.addEventListener('submit', async function (e) {
            e.preventDefault();

            var submitBtn = document.getElementById('verifyOtpBtn');
            if (AEAuth.isBusy(submitBtn)) return;
            AEAuth.clearErrors(otpForm);
            AEAuth.hideAlert('otpAlert');

            var otpCode = otpInput.value;
            if (!/^[0-9]{6}$/.test(otpCode)) {
                AEAuth.setFieldError(otpForm, 'otp_code', I18N.incomplete_otp);
                otpInput.focus();
                return;
            }

            AEAuth.setBusy(submitBtn, true);
            try {
                var response = await apiRequest('/auth/phone/register/verify', {
                    method: 'POST',
                    body: JSON.stringify({
                        phone: registrationData.phone,
                        otp_code: otpCode,
                        first_name: registrationData.first_name,
                        last_name: registrationData.last_name,
                        password: registrationData.password,
                        referral_code: registrationData.referral_code
                    })
                });
                var result = await AEAuth.readJson(response);

                if (response.ok) {
                    if (stopOtpTimer) stopOtpTimer();
                    // The answer set the login cookies: the guest cart joins the new account before the page is left,
                    // then the browser goes where the server said (it checked `next`).
                    await AEAuth.syncGuestCart();
                    showNotification(I18N.account_created_verified, 'success');
                    window.location.href = PAGE_DATA.after_register_url;
                    return;
                }

                AEAuth.setBusy(submitBtn, false);
                var errors = AEAuth.fieldErrors(result);
                if (response.status === 409) {
                    // The number got an account between the two steps.
                    goBackToForm();
                    showTaken('phone');
                } else if (response.status === 429) {
                    fail('otpAlert', I18N.too_many_codes);
                } else if (errors.otp_code) {
                    AEAuth.setFieldError(otpForm, 'otp_code', I18N.wrong_code);
                    otpInput.focus();
                } else if (errors.password || errors.phone || errors.first_name) {
                    // A problem with the details typed on the first step: back there, under the field.
                    goBackToForm();
                    placeFieldErrors(result);
                    AEAuth.showAlert('registerAlert', I18N.check_fields);
                } else {
                    fail('otpAlert', AEAuth.serverMessage(result, I18N.verification_failed));
                }
            } catch (error) {
                AEAuth.setBusy(submitBtn, false);
                fail('otpAlert', I18N.network_error);
            }
        });

        document.getElementById('resendOtpBtn').addEventListener('click', resendOtp);
        document.getElementById('goBackToFormBtn').addEventListener('click', goBackToForm);
    });
})();
