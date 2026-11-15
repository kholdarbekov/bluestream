/* Reset-password page (templates/frontend/reset_password.html), loaded only when the route found a token, which it
   publishes in the page data. Posts {token, new_password} to /api/v1/auth/reset-password. A 400 about the password
   goes under the field; any other 400 means the server refused the token, and the "link not valid" state replaces
   the form. The checklist reads the server's password rule from the markup. */
(function () {
    var PAGE_DATA = getPageData();
    var I18N = PAGE_DATA.i18n || {};
    var form = document.getElementById('resetPasswordForm');
    if (!form) return;

    var rules = AEAuth.wirePasswordRules(document.getElementById('passwordRules'), { met: I18N.rule_met, unmet: I18N.rule_unmet });
    AEAuth.wirePasswordToggles(form);
    AEAuth.clearOnInput(form);

    function showState(id, titleId) {
        document.getElementById('resetStep').hidden = true;
        document.getElementById(id).hidden = false;
        document.getElementById(titleId).focus();
    }

    function fail(message) {
        AEAuth.showAlert('errorMessage', message);
    }

    form.addEventListener('submit', async function (e) {
        e.preventDefault();

        var submitButton = form.querySelector('button[type="submit"]');
        if (AEAuth.isBusy(submitButton)) return;
        AEAuth.clearErrors(form);
        AEAuth.hideAlert('errorMessage');

        var formData = new FormData(form);
        var password = formData.get('password') || '';
        var confirmPassword = formData.get('confirm_password') || '';

        var ok = AEAuth.requireFields(form, I18N.required);
        if (password && !rules.allMet()) {
            ok = false;
            AEAuth.setFieldError(form, 'password', I18N.password_rules);
        }
        if (password && confirmPassword && password !== confirmPassword) {
            ok = false;
            AEAuth.setFieldError(form, 'confirm_password', I18N.passwords_mismatch);
        }
        if (!ok) {
            AEAuth.focusFirstInvalid(form);
            return;
        }

        AEAuth.setBusy(submitButton, true);
        try {
            var response = await apiRequest('/auth/reset-password', {
                method: 'POST',
                body: JSON.stringify({ token: PAGE_DATA.token, new_password: password })
            });
            var result = await AEAuth.readJson(response);

            if (response.ok) {
                showState('successMessage', 'resetDoneTitle');
            } else if (response.status === 429) {
                fail(I18N.rate_limited);
            } else if (response.status === 400 && AEAuth.fieldErrors(result).password) {
                // Every published rule met and still refused: the server's list of too-common passwords.
                AEAuth.setFieldError(form, 'password', rules.allMet() ? I18N.password_too_common : I18N.password_rules);
                AEAuth.focusFirstInvalid(form);
            } else if (response.status === 400) {
                showState('invalidLink', 'invalidLinkTitle');
            } else {
                fail(AEAuth.serverMessage(result, I18N.reset_failed));
            }
        } catch (error) {
            fail(I18N.network_error);
        } finally {
            AEAuth.setBusy(submitButton, false);
        }
    });
})();
