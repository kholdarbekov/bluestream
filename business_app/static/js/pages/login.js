/* Login page (templates/frontend/login.html). Posts {identifier, password} to /api/v1/auth/login; the answer sets the
   httpOnly login cookies. A guest cart then joins the account (AEAuth.syncGuestCart) and the browser goes where the
   server said (`after_login_url`: a safe `next` or the account).
   The page words its own errors for the answers it understands: a 401 by the `error_code` the server publishes (wrong
   details, an account that cannot sign in, a bot account with no password yet), a 423 lockout, a 429 rate limit. The
   form alert carries the error; there is no toast on top of it. */
(function () {
    var PAGE_DATA = getPageData();
    var I18N = PAGE_DATA.i18n || {};
    var form = document.getElementById('loginForm');
    if (!form) return;

    // POST /auth/login 401 `data.error_code` (auth_service.login_user) → the page's sentence. Anything else on a 401
    // is the wrong-details sentence.
    var REFUSALS = {
        ACCOUNT_DISABLED: I18N.disabled,
        TELEGRAM_PASSWORD_REQUIRED: I18N.telegram_only
    };

    AEAuth.wirePasswordToggles(form);
    AEAuth.clearOnInput(form);

    function fail(message) {
        AEAuth.showAlert('loginAlert', message);
    }

    function refusal(response, result) {
        if (response.status === 429) return I18N.rate_limited;
        if (response.status === 423) return I18N.locked;
        if (response.status === 401) {
            var code = result && result.data && result.data.error_code;
            return REFUSALS[code] || I18N.login_failed;
        }
        return AEAuth.serverMessage(result, I18N.login_failed);
    }

    form.addEventListener('submit', async function (e) {
        e.preventDefault();

        var submitBtn = form.querySelector('.btn-auth');
        if (AEAuth.isBusy(submitBtn)) return;
        AEAuth.clearErrors(form);
        AEAuth.hideAlert('loginAlert');

        if (!AEAuth.requireFields(form, I18N.required)) {
            AEAuth.focusFirstInvalid(form);
            return;
        }

        var formData = new FormData(form);
        var data = {
            identifier: String(formData.get('identifier') || '').trim(),
            password: formData.get('password')
        };

        AEAuth.setBusy(submitBtn, true);
        try {
            var response = await apiRequest('/auth/login', {
                method: 'POST',
                body: JSON.stringify(data)
            });
            var result = await AEAuth.readJson(response);

            if (response.ok) {
                await AEAuth.syncGuestCart();
                showNotification(I18N.login_success, 'success');
                // The server checked `next` (shared/safe_paths.py safe_next_path) and published where to go.
                window.location.href = PAGE_DATA.after_login_url;
                return;
            }

            AEAuth.setBusy(submitBtn, false);
            fail(refusal(response, result));
        } catch (error) {
            AEAuth.setBusy(submitBtn, false);
            fail(I18N.network_error);
        }
    });
})();
