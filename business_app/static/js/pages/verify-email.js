/* Email confirmation page (templates/frontend/verify_email.html).
   A link from the letter arrives with its token in the page data (`checking`): it is posted to /auth/verify-email
   at once, and the code form shows only if that fails. A code typed from the letter goes the same way. Success
   replaces the card body. A signed-in customer can ask for a new letter (/auth/resend-email-verification); the busy
   button ignores a second tap while the request runs, and the server's own limit (429) is said in words. */
(function () {
    var PAGE_DATA = getPageData();
    var I18N = PAGE_DATA.i18n || {};

    var form = document.getElementById('emailVerificationForm');
    var input = document.getElementById('verificationCode');
    var resendBtn = document.getElementById('resendEmail');
    var resendNote = document.getElementById('resendNote');

    function showDone() {
        document.getElementById('verifyStep').hidden = true;
        document.getElementById('successMessage').hidden = false;
        var title = document.getElementById('verifyDoneTitle');
        if (title) title.focus();
    }

    function showForm() {
        var checking = document.getElementById('checkingNote');
        if (checking) checking.hidden = true;
        document.getElementById('verifyBody').hidden = false;
    }

    // `typed`: the code came from the field, so its error goes under the field; a link's goes above the form.
    async function verify(token, button, typed) {
        AEAuth.hideAlert('verifyAlert');
        AEAuth.setBusy(button, true);
        try {
            var response = await apiRequest('/auth/verify-email', {
                method: 'POST',
                body: JSON.stringify({ token: token })
            });
            if (response.ok) {
                showDone();
                return;
            }
            showForm();
            if (response.status === 429) {
                AEAuth.showAlert('verifyAlert', I18N.rate_limited);
            } else if (typed) {
                AEAuth.setFieldError(form, 'token', I18N.invalid);
                input.focus();
            } else {
                AEAuth.showAlert('verifyAlert', I18N.invalid_link);
                input.focus();
            }
        } catch (error) {
            showForm();
            AEAuth.showAlert('verifyAlert', I18N.network_error);
        } finally {
            AEAuth.setBusy(button, false);
        }
    }

    function note(message, ok) {
        resendNote.textContent = message;
        resendNote.classList.toggle('is-ok', ok);
        resendNote.classList.toggle('is-error', !ok);
        resendNote.hidden = false;
    }

    async function resend() {
        if (AEAuth.isBusy(resendBtn)) return;
        AEAuth.setBusy(resendBtn, true);
        resendNote.hidden = true;
        try {
            var response = await apiRequest('/auth/resend-email-verification', { method: 'POST' });
            if (response.ok) {
                note(I18N.sent, true);
            } else {
                note(response.status === 429 ? I18N.rate_limited : I18N.send_failed, false);
            }
        } catch (error) {
            note(I18N.network_error, false);
        } finally {
            AEAuth.setBusy(resendBtn, false);
        }
    }

    if (form) {
        AEAuth.clearOnInput(form);
        input.addEventListener('input', function () {
            var compact = input.value.replace(/\s/g, '');
            if (compact !== input.value) input.value = compact;
        });
        form.addEventListener('submit', function (event) {
            event.preventDefault();
            var button = document.getElementById('verifyEmailBtn');
            if (AEAuth.isBusy(button)) return;
            AEAuth.clearErrors(form);
            var token = input.value.trim();
            if (!token) {
                AEAuth.setFieldError(form, 'token', I18N.required);
                input.focus();
                return;
            }
            verify(token, button, true);
        });
    }

    if (resendBtn) resendBtn.addEventListener('click', resend);

    if (PAGE_DATA.token) verify(PAGE_DATA.token, null, false);
})();
