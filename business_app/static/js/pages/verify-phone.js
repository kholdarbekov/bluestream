/* Phone confirmation, a cabinet page (templates/frontend/verify_phone.html). The login cookies are the identity:
   apiRequest sends them, and a lapsed login is its job (sign in, then back here).
   Step 1 posts {phone} to /auth/send-otp. The server is the one judge of the number (400), refuses a number another
   account holds (409) and answers the code's life in `expires_in` and when "send a new code" may show in
   `resend_available_in` (at expiry). A number whose code is still valid gets no new one: a 429 RESEND_COOLDOWN with
   that code's time left in the same two fields; the page then shows the code step with its own note, as the code
   already sent is the one to enter. Step 2 posts {otp} to /auth/verify-phone, which
   saves the number to the account only when the code sent to it matches; 404 means the request lapsed. The page words
   its own errors (a number another account holds is `taken`, whichever step finds it). */
(function () {
    var PAGE_DATA = getPageData();
    var I18N = PAGE_DATA.i18n || {};

    var phoneStep = document.getElementById('phoneStep');
    var otpStep = document.getElementById('otpStep');
    var phoneForm = document.getElementById('phoneForm');
    var otpForm = document.getElementById('otpForm');
    var phoneInput = document.getElementById('phone');
    var otpInput = document.getElementById('otpCode');
    var resendBtn = document.getElementById('resendCode');
    var stillValidNote = document.getElementById('codeStillValid');
    var otp = AEAuth.wireOtp(otpInput);
    var stopTimer = null;
    var currentPhone = null;

    function fail(alertId, message) {
        AEAuth.showAlert(alertId, message);
        var alert = document.getElementById(alertId);
        if (alert) alert.focus();
    }

    function startTimer(expiresIn, resendAvailableIn) {
        if (stopTimer) stopTimer();
        stopTimer = AEAuth.countdown(document.getElementById('timer'), resendBtn, expiresIn, resendAvailableIn,
            I18N.code_expired);
    }

    function resetCode() {
        otp.reset();
        AEAuth.clearErrors(otpForm);
        AEAuth.hideAlert('otpAlert');
    }

    function showOtpStep(phone) {
        phoneStep.hidden = true;
        otpStep.hidden = false;
        document.getElementById('sentToPhone').textContent = phone;
        resetCode();
        otpInput.focus();
    }

    function showPhoneStep() {
        if (stopTimer) stopTimer();
        stillValidNote.hidden = true;
        otpStep.hidden = true;
        phoneStep.hidden = false;
        resetCode();
        phoneInput.focus();
    }

    // The refusal of a number, under the phone field (step 1) or above the code (a resend).
    function refuseNumber(message) {
        if (phoneStep.hidden) {
            fail('otpAlert', message);
        } else {
            AEAuth.setFieldError(phoneForm, 'phone', message);
            phoneInput.focus();
        }
    }

    // Asks the server for a code: 'sent', 'still_valid' (the code this number holds is still the one to enter), or
    // null when there is no code to enter (the reason is shown).
    async function sendCode(phone, button) {
        AEAuth.setBusy(button, true);
        try {
            var response = await apiRequest('/auth/send-otp', {
                method: 'POST',
                body: JSON.stringify({ phone: phone })
            });
            var result = await AEAuth.readJson(response);
            var data = result.data || {};
            var stillValid = response.status === 429 && data.error_code === 'RESEND_COOLDOWN';
            if (response.ok || stillValid) {
                currentPhone = phone;
                stillValidNote.hidden = !stillValid;
                startTimer(data.expires_in, data.resend_available_in);
                return stillValid ? 'still_valid' : 'sent';
            }
            if (response.status === 400) refuseNumber(I18N.invalid_phone);
            else if (response.status === 409) refuseNumber(I18N.taken);
            else if (response.status === 429) fail(phoneStep.hidden ? 'otpAlert' : 'phoneAlert', I18N.rate_limited);
            else fail(phoneStep.hidden ? 'otpAlert' : 'phoneAlert', phoneStep.hidden ? I18N.resend_failed : I18N.send_failed);
        } catch (error) {
            fail(phoneStep.hidden ? 'otpAlert' : 'phoneAlert', I18N.network_error);
        } finally {
            AEAuth.setBusy(button, false);
        }
        return null;
    }

    phoneForm.addEventListener('submit', async function (event) {
        event.preventDefault();
        var button = document.getElementById('sendCodeBtn');
        if (AEAuth.isBusy(button)) return;
        AEAuth.clearErrors(phoneForm);
        AEAuth.hideAlert('phoneAlert');

        var phone = phoneInput.value.trim();
        if (!phone) {
            AEAuth.setFieldError(phoneForm, 'phone', I18N.need_phone);
            phoneInput.focus();
            return;
        }
        if (await sendCode(phone, button)) showOtpStep(phone);
    });

    otpForm.addEventListener('submit', async function (event) {
        event.preventDefault();
        var button = document.getElementById('verifyCodeBtn');
        if (AEAuth.isBusy(button)) return;
        AEAuth.clearErrors(otpForm);
        AEAuth.hideAlert('otpAlert');

        var code = otpInput.value;
        if (!/^[0-9]{6}$/.test(code)) {
            AEAuth.setFieldError(otpForm, 'otp', I18N.incomplete_otp);
            otpInput.focus();
            return;
        }

        AEAuth.setBusy(button, true);
        try {
            var response = await apiRequest('/auth/verify-phone', {
                method: 'POST',
                body: JSON.stringify({ otp: code })
            });
            var result = await AEAuth.readJson(response);

            if (response.ok) {
                if (stopTimer) stopTimer();
                var saved = (result.data && result.data.phone) || currentPhone;
                var text = document.getElementById('phoneDoneText');
                text.textContent = (text.getAttribute('data-template') || '{phone}').replace('{phone}', saved);
                otpStep.hidden = true;
                document.getElementById('successMessage').hidden = false;
                document.getElementById('phoneDoneTitle').focus();
                return;
            }
            if (response.status === 400) {
                otp.reset();
                AEAuth.setFieldError(otpForm, 'otp', I18N.wrong_code);
                otpInput.focus();
            } else if (response.status === 404) {
                showPhoneStep();
                fail('phoneAlert', I18N.request_expired);
            } else if (response.status === 409) {
                showPhoneStep();
                AEAuth.setFieldError(phoneForm, 'phone', I18N.taken);
            } else if (response.status === 429) {
                fail('otpAlert', I18N.too_many_codes);
            } else {
                fail('otpAlert', I18N.verification_failed);
            }
        } catch (error) {
            fail('otpAlert', I18N.network_error);
        } finally {
            AEAuth.setBusy(button, false);
        }
    });

    resendBtn.addEventListener('click', async function () {
        if (AEAuth.isBusy(resendBtn) || !currentPhone) return;
        AEAuth.hideAlert('otpAlert');
        var sent = await sendCode(currentPhone, resendBtn);
        if (sent === 'sent') {
            resetCode();
            showNotification(I18N.new_code_sent, 'success');
        }
        if (sent) otpInput.focus();
    });

    document.getElementById('changePhone').addEventListener('click', showPhoneStep);

    AEAuth.clearOnInput(phoneForm);
    AEAuth.clearOnInput(otpForm);
})();
