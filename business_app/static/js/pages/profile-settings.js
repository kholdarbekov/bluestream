/* Profile settings (templates/frontend/profile_settings.html). Two controls write, each through apiRequest:
   - the personal details form: ONE save, PUT /api/v1/auth/profile with the names, the date of birth (YYYY-MM-DD from
     the day / month / year fields, or null when all three are empty), gender and the notification language. The
     phone is never sent: it changes on /verify-phone. Success is shown only on the server's 200, and the frame's
     name is left as the server drew it;
   - the reorder-reminders switch (below). */
(function () {
    var PAGE_DATA = getPageData();
    var I18N = PAGE_DATA.i18n || {};

    // The date of birth as the API reads it: "YYYY-MM-DD", null when all three parts are empty, false when only some
    // are filled or the day/year is not a number. The server judges the date itself (a real day, not in the future).
    function birthDate(form) {
        var day = form.elements.dob_day.value.trim();
        var month = form.elements.dob_month.value;
        var year = form.elements.dob_year.value.trim();
        if (!day && !month && !year) return null;
        if (!/^[0-9]{1,2}$/.test(day) || !month || !/^[0-9]{4}$/.test(year)) return false;
        return year + '-' + ('0' + month).slice(-2) + '-' + ('0' + day).slice(-2);
    }

    var personalForm = document.getElementById('personalInfoForm');
    if (personalForm) {
        AEAuth.clearOnInput(personalForm);
        personalForm.addEventListener('change', function (event) {
            if (event.target.id === 'dob_month') AEAuth.setFieldError(personalForm, 'date_of_birth', '');
        });

        personalForm.addEventListener('submit', async function (event) {
            event.preventDefault();
            var button = personalForm.querySelector('button[type="submit"]');
            if (AEAuth.isBusy(button)) return;
            AEAuth.clearErrors(personalForm);
            AEAuth.hideAlert('personalAlert');

            var ok = AEAuth.requireFields(personalForm, I18N.required);
            var dateOfBirth = birthDate(personalForm);
            if (dateOfBirth === false) {
                ok = false;
                AEAuth.setFieldError(personalForm, 'date_of_birth', I18N.dob_incomplete);
            }
            if (!ok) {
                AEAuth.focusFirstInvalid(personalForm);
                return;
            }

            var elements = personalForm.elements;
            AEAuth.setBusy(button, true);
            try {
                var response = await apiRequest('/auth/profile', {
                    method: 'PUT',
                    body: JSON.stringify({
                        first_name: elements.first_name.value.trim(),
                        last_name: elements.last_name.value.trim(),
                        date_of_birth: dateOfBirth,
                        gender: elements.gender.value,
                        preferred_language: elements.preferred_language.value
                    })
                });
                var result = await AEAuth.readJson(response);

                if (response.ok) {
                    // The frame's name (sidebar, header) is the server's greeting name (shared.user_names): it is
                    // never redrawn here, so a save cannot swap a company for a person's full name. It shows the
                    // new name on the next page.
                    showNotification(I18N.saved, 'success');
                } else if (response.status === 400 && AEAuth.fieldErrors(result).date_of_birth) {
                    AEAuth.setFieldError(personalForm, 'date_of_birth', I18N.dob_invalid);
                    AEAuth.focusFirstInvalid(personalForm);
                } else {
                    AEAuth.showAlert('personalAlert', AEAuth.serverMessage(result, I18N.save_failed));
                }
            } catch (error) {
                AEAuth.showAlert('personalAlert', I18N.network_error);
            } finally {
                AEAuth.setBusy(button, false);
            }
        });
    }

    // Reorder reminders (spec 2026-10-09 §8). The switch shows the EFFECTIVE value the
    // preferences API publishes, and that value is the only thing that ever enables it: the
    // switch starts disabled and stays so until a published value arrives. After a save it is
    // redrawn from the server's answer, never from the clicked state (a customer's "off" over an
    // admin's off changes nothing on the server, and the switch must show what is published). A
    // save that fails, or that answers without a published value, puts the last published value
    // back. This is the only place the page sends the field; the server names the route from the
    // X-Platform header apiRequest adds.
    var reorderSwitch = document.getElementById('reorderRemindersSwitch');
    if (reorderSwitch) {
        var publishedReorder = null;

        // Draws a published state. Returns false, drawing nothing, when there is none.
        var renderReorderReminders = function (state) {
            if (!state) {
                return false;
            }
            publishedReorder = state;
            reorderSwitch.checked = !!state.enabled;
            reorderSwitch.disabled = false;
            return true;
        };

        var revertReorderReminders = function () {
            renderReorderReminders(publishedReorder);
        };

        var failReorderSave = function () {
            revertReorderReminders();
            showNotification(PAGE_DATA.i18n.reorder_reminders_failed, 'error');
        };

        var loadReorderReminders = async function () {
            try {
                var response = await apiRequest('/notifications/preferences', { method: 'GET' });
                if (!response.ok) {
                    return;
                }
                var result = await response.json();
                renderReorderReminders(result.data.preferences.reorder_reminders);
            } catch (error) {
                showNotification(PAGE_DATA.i18n.network_error, 'error');
            }
        };

        reorderSwitch.addEventListener('change', async function () {
            var wanted = reorderSwitch.checked;
            var hadFocus = document.activeElement === reorderSwitch;
            reorderSwitch.disabled = true;
            try {
                var response = await apiRequest('/notifications/preferences', {
                    method: 'PUT',
                    body: JSON.stringify({ reorder_reminders_enabled: wanted })
                });
                if (!response.ok) {
                    failReorderSave();
                    return;
                }
                var result = await response.json();
                if (!renderReorderReminders(result.data.preferences.reorder_reminders)) {
                    failReorderSave();
                    return;
                }
                showNotification(PAGE_DATA.i18n.reorder_reminders_saved, 'success');
            } catch (error) {
                revertReorderReminders();
                showNotification(PAGE_DATA.i18n.network_error, 'error');
            } finally {
                // A disabled control drops keyboard focus; give it back once it is usable again.
                if (hadFocus && !reorderSwitch.disabled) {
                    reorderSwitch.focus();
                }
            }
        });

        loadReorderReminders();
    }
})();
