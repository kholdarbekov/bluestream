/* Account security (templates/frontend/account_security.html), loaded when the account has a website password, or
   when a bot signup with a real email gets the set-a-password panel.
   - The panel's #passwordLinkForm posts {identifier: <the account's email>} to /api/v1/auth/forgot-password (the
     emailed set-a-password link, the same request the forgot-password page sends) and, on the server's 200, swaps
     the button for "check your email". The email is the server's (page data `link_email`), never typed.
   - The change form posts {current_password, new_password} to /api/v1/auth/change-password with apiRequest, which sends the login
   cookies and their CSRF header. The checklist under "New password" is the server's rule, read from the markup; the server is
   still the judge: a 400 about the password goes under the field ("too easy to guess" when every listed rule is met),
   a 401 means the current password is not the one on the account. Success is shown only on the server's 200. */
(function () {
    var PAGE_DATA = getPageData();
    var I18N = PAGE_DATA.i18n || {};
    function wireLinkForm(linkForm) {
        var sent = document.getElementById('passwordLinkSent');
        function linkFail(message) {
            AEAuth.showAlert('linkAlert', message);
            var alert = document.getElementById('linkAlert');
            if (alert) alert.focus();
        }
        linkForm.addEventListener('submit', async function (event) {
            event.preventDefault();
            var button = linkForm.querySelector('button[type="submit"]');
            if (AEAuth.isBusy(button) || !PAGE_DATA.link_email) return;
            AEAuth.hideAlert('linkAlert');
            AEAuth.setBusy(button, true);
            try {
                var response = await apiRequest('/auth/forgot-password', {
                    method: 'POST',
                    body: JSON.stringify({ identifier: PAGE_DATA.link_email })
                });
                var result = await AEAuth.readJson(response);
                if (response.ok) {
                    linkForm.hidden = true;
                    if (sent) {
                        sent.hidden = false;
                        sent.focus();
                    }
                } else if (response.status === 429) {
                    linkFail(I18N.rate_limited);
                } else {
                    linkFail(AEAuth.serverMessage(result, I18N.link_failed));
                }
            } catch (error) {
                linkFail(I18N.network_error);
            } finally {
                AEAuth.setBusy(button, false);
            }
        });
    }

    var linkForm = document.getElementById('passwordLinkForm');
    if (linkForm) wireLinkForm(linkForm);

    var form = document.getElementById('changePasswordForm');
    if (!form) return;

    var done = document.getElementById('passwordDone');
    var rules = AEAuth.wirePasswordRules(document.getElementById('passwordRules'), { met: I18N.rule_met, unmet: I18N.rule_unmet });
    AEAuth.wirePasswordToggles(form);
    AEAuth.clearOnInput(form);
    form.addEventListener('input', function () {
        if (done) done.hidden = true;
    });

    function fail(message) {
        AEAuth.showAlert('passwordAlert', message);
        var alert = document.getElementById('passwordAlert');
        if (alert) alert.focus();
    }

    form.addEventListener('submit', async function (event) {
        event.preventDefault();
        var button = form.querySelector('button[type="submit"]');
        if (AEAuth.isBusy(button)) return;
        AEAuth.clearErrors(form);
        AEAuth.hideAlert('passwordAlert');
        if (done) done.hidden = true;

        var current = form.elements.current_password.value;
        var password = form.elements.new_password.value;
        var confirmPassword = form.elements.confirm_password.value;

        var ok = AEAuth.requireFields(form, I18N.required);
        if (password && !rules.allMet()) {
            ok = false;
            AEAuth.setFieldError(form, 'new_password', I18N.password_rules);
        }
        if (password && confirmPassword && password !== confirmPassword) {
            ok = false;
            AEAuth.setFieldError(form, 'confirm_password', I18N.passwords_mismatch);
        }
        if (!ok) {
            AEAuth.focusFirstInvalid(form);
            return;
        }

        AEAuth.setBusy(button, true);
        try {
            var response = await apiRequest('/auth/change-password', {
                method: 'POST',
                body: JSON.stringify({ current_password: current, new_password: password })
            });
            var result = await AEAuth.readJson(response);

            if (response.ok) {
                form.reset();
                // Hide any password the customer chose to show.
                form.querySelectorAll('.password-toggle[aria-pressed="true"]').forEach(function (toggle) {
                    toggle.click();
                });
                // The checklist redraws on input: untick it for the emptied field (before the note is shown, since
                // any input hides the note).
                form.elements.new_password.dispatchEvent(new Event('input', { bubbles: true }));
                if (done) {
                    done.hidden = false;
                    done.focus();
                }
                showNotification(I18N.changed, 'success');
            } else if (response.status === 401) {
                AEAuth.setFieldError(form, 'current_password', I18N.wrong_current);
                AEAuth.focusFirstInvalid(form);
            } else if (response.status === 400 && AEAuth.fieldErrors(result).password) {
                AEAuth.setFieldError(form, 'new_password', rules.allMet() ? I18N.password_too_common : I18N.password_rules);
                AEAuth.focusFirstInvalid(form);
            } else if (response.status === 429) {
                fail(I18N.rate_limited);
            } else {
                fail(AEAuth.serverMessage(result, I18N.change_failed));
            }
        } catch (error) {
            fail(I18N.network_error);
        } finally {
            AEAuth.setBusy(button, false);
        }
    });
})();
