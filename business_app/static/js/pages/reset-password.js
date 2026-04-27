(function () {
    var PAGE_DATA = getPageData();
    var resetToken = null;

    function showError(message) {
        document.getElementById('errorText').textContent = message;
        document.getElementById('errorMessage').style.display = 'block';
        document.getElementById('successMessage').style.display = 'none';
    }

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

    document.addEventListener('DOMContentLoaded', function () {
        var urlParams = new URLSearchParams(window.location.search);
        resetToken = urlParams.get('token') || window.location.pathname.split('/').pop();

        if (!resetToken || resetToken === 'reset-password') {
            showError(PAGE_DATA.i18n.invalid_token_msg);
            document.getElementById('resetPasswordForm').style.display = 'none';
        }
    });

    var form = document.getElementById('resetPasswordForm');
    if (form) {
        form.addEventListener('submit', async function (e) {
            e.preventDefault();

            var formData = new FormData(this);
            var password = formData.get('password');
            var confirmPassword = formData.get('confirm_password');

            if (password !== confirmPassword) {
                showError(PAGE_DATA.i18n.passwords_mismatch);
                return;
            }

            var strength = checkPasswordStrength(password);
            if (strength < 2) {
                showError(PAGE_DATA.i18n.password_too_weak);
                return;
            }

            if (!resetToken) {
                showError(PAGE_DATA.i18n.invalid_token);
                return;
            }

            var submitButton = this.querySelector('button[type="submit"]');
            var originalText = submitButton.textContent;
            submitButton.disabled = true;
            submitButton.textContent = PAGE_DATA.i18n.resetting;

            try {
                var response = await apiRequest('/auth/reset-password', {
                    method: 'POST',
                    body: JSON.stringify({
                        token: resetToken,
                        new_password: password
                    })
                });

                var result = await response.json();

                if (response.ok) {
                    document.getElementById('resetPasswordForm').style.display = 'none';
                    document.getElementById('errorMessage').style.display = 'none';
                    document.getElementById('successMessage').style.display = 'block';

                    showNotification(PAGE_DATA.i18n.reset_success, 'success');
                } else {
                    showError(result.message || PAGE_DATA.i18n.reset_failed);
                }
            } catch (error) {
                showError(PAGE_DATA.i18n.network_error);
            } finally {
                submitButton.disabled = false;
                submitButton.textContent = originalText;
            }
        });
    }

    var passwordInput = document.querySelector('input[name="password"]');
    if (passwordInput) {
        passwordInput.addEventListener('input', function () {
            var strength = checkPasswordStrength(this.value);
            updatePasswordStrengthUI(strength);
        });
    }

    var showPasswordCheckbox = document.getElementById('show_password');
    if (showPasswordCheckbox) {
        showPasswordCheckbox.addEventListener('change', function () {
            var passwordFields = document.querySelectorAll('#resetPasswordForm input[name="password"], #resetPasswordForm input[name="confirm_password"]');
            var type = this.checked ? 'text' : 'password';
            passwordFields.forEach(function (field) {
                field.type = type;
            });
        });
    }

    var confirmInput = document.querySelector('input[name="confirm_password"]');
    if (confirmInput) {
        confirmInput.addEventListener('input', function () {
            var password = document.querySelector('input[name="password"]').value;
            if (this.value && password !== this.value) {
                this.setCustomValidity(PAGE_DATA.i18n.passwords_mismatch);
            } else {
                this.setCustomValidity('');
            }
        });
    }
})();
