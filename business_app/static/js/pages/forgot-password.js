/* Forgot-password page (templates/frontend/forgot_password.html). Posts {identifier} to /api/v1/auth/forgot-password.
   The server answers the same for every identifier (no account enumeration), so the result card replaces the form
   with one truthful sentence (and takes the focus, so it is announced; no toast on top); "Try another" brings the
   form back. */
(function () {
    var PAGE_DATA = getPageData();
    var I18N = PAGE_DATA.i18n || {};
    var form = document.getElementById('forgotPasswordForm');
    if (!form) return;

    var step = document.getElementById('forgotStep');
    var done = document.getElementById('successMessage');
    var foot = document.getElementById('forgotFoot');
    AEAuth.clearOnInput(form);

    function fail(message) {
        AEAuth.showAlert('forgotAlert', message);
    }

    form.addEventListener('submit', async function (e) {
        e.preventDefault();

        var submitButton = form.querySelector('button[type="submit"]');
        if (AEAuth.isBusy(submitButton)) return;
        AEAuth.clearErrors(form);
        AEAuth.hideAlert('forgotAlert');

        var identifier = String(new FormData(form).get('identifier') || '').trim();
        if (!identifier) {
            AEAuth.setFieldError(form, 'identifier', I18N.required);
            AEAuth.focusFirstInvalid(form);
            return;
        }

        AEAuth.setBusy(submitButton, true);
        try {
            var response = await apiRequest('/auth/forgot-password', {
                method: 'POST',
                body: JSON.stringify({ identifier: identifier })
            });
            var result = await AEAuth.readJson(response);

            if (response.ok) {
                step.hidden = true;
                done.hidden = false;
                if (foot) foot.hidden = true;
                document.getElementById('forgotDoneTitle').focus();
            } else if (response.status === 429) {
                fail(I18N.rate_limited);
            } else {
                fail(AEAuth.serverMessage(result, I18N.send_failed));
            }
        } catch (err) {
            fail(I18N.network_error);
        } finally {
            AEAuth.setBusy(submitButton, false);
        }
    });

    var again = document.getElementById('forgotAgainBtn');
    if (again) {
        again.addEventListener('click', function () {
            done.hidden = true;
            step.hidden = false;
            if (foot) foot.hidden = false;
            form.reset();
            var input = form.querySelector('input[name="identifier"]');
            if (input) input.focus();
        });
    }
})();
