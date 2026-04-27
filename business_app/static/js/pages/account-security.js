(function () {
    var PAGE_DATA = getPageData();

    function checkPasswordStrength(password) {
        var strength = 0;
        if (password.length >= 8) strength++;
        if (/[a-z]/.test(password)) strength++;
        if (/[A-Z]/.test(password)) strength++;
        if (/[0-9]/.test(password)) strength++;
        if (/[^a-zA-Z0-9]/.test(password)) strength++;
        return strength;
    }

    function updatePasswordStrengthUI(strength) {
        var strengthFill = document.getElementById('strengthFill');
        var strengthText = document.getElementById('strengthText');

        strengthFill.className = 'strength-fill';

        switch (strength) {
            case 0:
            case 1:
                strengthFill.classList.add('weak');
                strengthFill.style.width = '20%';
                strengthText.textContent = PAGE_DATA.i18n.weak;
                break;
            case 2:
                strengthFill.classList.add('fair');
                strengthFill.style.width = '40%';
                strengthText.textContent = PAGE_DATA.i18n.fair;
                break;
            case 3:
                strengthFill.classList.add('good');
                strengthFill.style.width = '60%';
                strengthText.textContent = PAGE_DATA.i18n.good;
                break;
            case 4:
                strengthFill.classList.add('good');
                strengthFill.style.width = '80%';
                strengthText.textContent = PAGE_DATA.i18n.strong;
                break;
            case 5:
                strengthFill.classList.add('strong');
                strengthFill.style.width = '100%';
                strengthText.textContent = PAGE_DATA.i18n.very_strong;
                break;
        }
    }

    function setup2FA() {
        showNotification(PAGE_DATA.i18n.twofa_coming_soon, 'info');
    }

    function verifyPhoneFirst() {
        if (confirm(PAGE_DATA.i18n.verify_phone_confirm)) {
            window.location.href = PAGE_DATA.verify_phone_url;
        }
    }

    var passwordForm = document.getElementById('changePasswordForm');
    if (passwordForm) {
        passwordForm.addEventListener('submit', async function (e) {
            e.preventDefault();

            var formData = new FormData(this);
            var currentPassword = formData.get('current_password');
            var newPassword = formData.get('new_password');
            var confirmPassword = formData.get('confirm_password');

            if (newPassword !== confirmPassword) {
                showNotification(PAGE_DATA.i18n.mismatch, 'error');
                return;
            }

            var strength = checkPasswordStrength(newPassword);
            if (strength < 2) {
                showNotification(PAGE_DATA.i18n.too_weak, 'error');
                return;
            }

            try {
                var response = await apiRequest('/auth/change-password', {
                    method: 'POST',
                    body: JSON.stringify({
                        current_password: currentPassword,
                        new_password: newPassword
                    })
                });

                var result = await response.json();

                if (response.ok) {
                    showNotification(PAGE_DATA.i18n.changed_success, 'success');
                    this.reset();
                    document.getElementById('strengthFill').style.width = '0%';
                    document.getElementById('strengthText').textContent = PAGE_DATA.i18n.strength_label;
                } else {
                    showNotification(result.message || PAGE_DATA.i18n.change_failed, 'error');
                }
            } catch (error) {
                showNotification(PAGE_DATA.i18n.network_error, 'error');
            }
        });
    }

    var settingsForm = document.getElementById('securitySettingsForm');
    if (settingsForm) {
        settingsForm.addEventListener('submit', function (e) {
            e.preventDefault();
            showNotification(PAGE_DATA.i18n.settings_saved, 'success');
        });
    }

    var newPasswordInput = document.querySelector('input[name="new_password"]');
    if (newPasswordInput) {
        newPasswordInput.addEventListener('input', function () {
            var strength = checkPasswordStrength(this.value);
            updatePasswordStrengthUI(strength);
        });
    }

    var showPasswordsToggle = document.getElementById('show_passwords');
    if (showPasswordsToggle) {
        showPasswordsToggle.addEventListener('change', function () {
            var passwordFields = document.querySelectorAll('#changePasswordForm input[type="password"], #changePasswordForm input[type="text"][data-pwd="1"]');
            var type = this.checked ? 'text' : 'password';
            document.querySelectorAll('#changePasswordForm input[name="current_password"], #changePasswordForm input[name="new_password"], #changePasswordForm input[name="confirm_password"]').forEach(function (field) {
                field.type = type;
            });
        });
    }

    var confirmInput = document.querySelector('input[name="confirm_password"]');
    if (confirmInput) {
        confirmInput.addEventListener('input', function () {
            var password = document.querySelector('input[name="new_password"]').value;
            if (this.value && password !== this.value) {
                this.setCustomValidity(PAGE_DATA.i18n.mismatch);
                this.style.borderColor = '#dc3545';
            } else {
                this.setCustomValidity('');
                this.style.borderColor = '';
            }
        });
    }

    var setup2FABtn = document.querySelector('[data-action="setup-2fa"]');
    if (setup2FABtn) setup2FABtn.addEventListener('click', setup2FA);

    var verifyPhoneBtn = document.querySelector('[data-action="verify-phone-first"]');
    if (verifyPhoneBtn) verifyPhoneBtn.addEventListener('click', verifyPhoneFirst);
})();
