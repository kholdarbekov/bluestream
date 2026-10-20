(function () {
    var PAGE_DATA = getPageData();
    var registrationData = {};
    var otpTimerInterval = null;
    var otpExpiresAt = null;
    var resendAvailableAt = null;

    function togglePassword(inputId, button) {
        var input = document.getElementById(inputId);
        var icon = button.querySelector('i');

        if (input.type === 'password') {
            input.type = 'text';
            icon.classList.remove('fa-eye');
            icon.classList.add('fa-eye-slash');
        } else {
            input.type = 'password';
            icon.classList.remove('fa-eye-slash');
            icon.classList.add('fa-eye');
        }
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

    function normalizeUzbekistanPhone(phone) {
        if (!phone) return null;
        var digits = phone.replace(/\D/g, '');
        if (digits.length === 12 && digits.startsWith('998')) return '+' + digits;
        if (digits.length === 9 && /^[3579]/.test(digits)) return '+998' + digits;
        if (digits.length === 11 && digits.startsWith('8998')) return '+998' + digits.substring(1);
        return null;
    }

    function isValidUzbekistanPhone(phone) {
        var normalized = normalizeUzbekistanPhone(phone);
        if (!normalized) return false;
        if (!/^\+998[0-9]{9}$/.test(normalized)) return false;
        var prefix = normalized.substring(4, 6);
        var validPrefixes = ['90', '91', '93', '94', '95', '97', '98', '99', '33', '50', '55', '77', '88'];
        return validPrefixes.includes(prefix);
    }

    function updateOtpCode() {
        var digits = Array.from(document.querySelectorAll('.otp-digit'))
            .map(function (input) { return input.value; })
            .join('');
        document.getElementById('otpCode').value = digits;
    }

    function startOtpTimer(expiresIn, resendAvailableIn) {
        otpExpiresAt = Date.now() + (expiresIn * 1000);
        resendAvailableAt = Date.now() + (resendAvailableIn * 1000);

        var timerEl = document.getElementById('otpTimer');
        var timerTextEl = document.getElementById('otpTimerText');
        var resendBtn = document.getElementById('resendOtpBtn');

        resendBtn.style.display = 'none';
        timerTextEl.style.display = 'block';

        if (otpTimerInterval) clearInterval(otpTimerInterval);

        otpTimerInterval = setInterval(function () {
            var now = Date.now();
            var remainingExpiry = Math.max(0, Math.floor((otpExpiresAt - now) / 1000));
            var remainingResend = Math.max(0, Math.floor((resendAvailableAt - now) / 1000));

            var minutes = Math.floor(remainingExpiry / 60);
            var seconds = remainingExpiry % 60;
            timerEl.textContent = minutes + ':' + String(seconds).padStart(2, '0');

            if (remainingResend === 0 && resendBtn.style.display === 'none') {
                resendBtn.style.display = 'inline-block';
            }

            if (remainingExpiry === 0) {
                clearInterval(otpTimerInterval);
                timerTextEl.textContent = PAGE_DATA.i18n.code_expired;
                resendBtn.style.display = 'inline-block';
            }
        }, 1000);
    }

    async function initiatePhoneRegistration(submitBtn) {
        try {
            var response = await apiRequest('/auth/phone/register/init', {
                method: 'POST',
                body: JSON.stringify({
                    phone: registrationData.phone,
                    preferred_language: PAGE_DATA.locale
                })
            });

            var result = await response.json();

            if (response.ok) {
                document.getElementById('registerForm').style.display = 'none';
                document.getElementById('otpVerificationSection').style.display = 'block';
                document.getElementById('maskedPhone').textContent =
                    result.data.phone_masked || registrationData.phone;

                startOtpTimer(result.data.expires_in, result.data.resend_available_in);

                document.querySelector('.otp-digit[data-index="0"]').focus();

                showNotification(PAGE_DATA.i18n.code_sent, 'success');
            } else {
                showNotification(result.message || PAGE_DATA.i18n.send_failed, 'error');
            }
        } catch (error) {
            showNotification(PAGE_DATA.i18n.network_error, 'error');
        } finally {
            submitBtn.classList.remove('loading');
        }
    }

    async function registerWithEmailOnly(submitBtn) {
        try {
            var response = await apiRequest('/auth/register', {
                method: 'POST',
                body: JSON.stringify(registrationData)
            });

            var result = await response.json();

            if (response.ok) {
                var message = PAGE_DATA.i18n.account_created_email;

                showNotification(message, 'success');

                setTimeout(function () {
                    window.location.href = PAGE_DATA.verify_email_url;
                }, 2000);
            } else {
                showNotification(result.message || PAGE_DATA.i18n.registration_failed, 'error');
            }
        } catch (error) {
            showNotification(PAGE_DATA.i18n.network_error, 'error');
        } finally {
            submitBtn.classList.remove('loading');
        }
    }

    async function resendOtp() {
        var resendBtn = document.getElementById('resendOtpBtn');
        resendBtn.disabled = true;
        resendBtn.textContent = PAGE_DATA.i18n.sending;

        try {
            var response = await apiRequest('/auth/phone/resend-otp', {
                method: 'POST',
                body: JSON.stringify({ phone: registrationData.phone })
            });

            var result = await response.json();

            if (response.ok) {
                showNotification(PAGE_DATA.i18n.new_code_sent, 'success');
                startOtpTimer(result.data.expires_in, result.data.resend_available_in);

                document.querySelectorAll('.otp-digit').forEach(function (input) { input.value = ''; });
                document.getElementById('otpCode').value = '';
                document.querySelector('.otp-digit[data-index="0"]').focus();
            } else {
                showNotification(result.message || PAGE_DATA.i18n.resend_failed, 'error');
            }
        } catch (error) {
            showNotification(PAGE_DATA.i18n.network_error, 'error');
        } finally {
            resendBtn.disabled = false;
            resendBtn.textContent = PAGE_DATA.i18n.resend_code;
        }
    }

    function goBackToForm() {
        if (otpTimerInterval) clearInterval(otpTimerInterval);
        document.getElementById('otpVerificationSection').style.display = 'none';
        document.getElementById('registerForm').style.display = 'block';

        document.querySelectorAll('.otp-digit').forEach(function (input) { input.value = ''; });
        document.getElementById('otpCode').value = '';
    }

    window.addEventListener('DOMContentLoaded', function () {
        document.querySelectorAll('.password-toggle').forEach(function (btn) {
            btn.addEventListener('click', function () {
                togglePassword(this.dataset.target, this);
            });
        });

        var passwordInput = document.querySelector('input[name="password"]');
        if (passwordInput) {
            passwordInput.addEventListener('input', function () {
                var password = this.value;
                var strengthContainer = document.getElementById('passwordStrength');
                var strengthFill = document.getElementById('strengthFill');
                var strengthText = document.getElementById('strengthText');

                if (password.length === 0) {
                    strengthContainer.style.display = 'none';
                    return;
                }

                strengthContainer.style.display = 'block';
                var strength = checkPasswordStrength(password);

                strengthFill.className = 'strength-fill';
                if (strength <= 1) {
                    strengthFill.classList.add('weak');
                    strengthText.textContent = PAGE_DATA.i18n.weak_password;
                } else if (strength === 2) {
                    strengthFill.classList.add('fair');
                    strengthText.textContent = PAGE_DATA.i18n.fair_password;
                } else if (strength === 3) {
                    strengthFill.classList.add('good');
                    strengthText.textContent = PAGE_DATA.i18n.good_password;
                } else {
                    strengthFill.classList.add('strong');
                    strengthText.textContent = PAGE_DATA.i18n.strong_password;
                }
            });
        }

        var registerForm = document.getElementById('registerForm');
        if (registerForm) {
            registerForm.addEventListener('submit', async function (e) {
                e.preventDefault();

                var submitBtn = this.querySelector('.btn-auth');
                var formData = new FormData(this);

                var phone = (formData.get('phone') || '').trim();
                var email = (formData.get('email') || '').trim();

                if (!phone && !email) {
                    showNotification(PAGE_DATA.i18n.need_phone_or_email, 'error');
                    return;
                }

                if (phone && !isValidUzbekistanPhone(phone)) {
                    showNotification(PAGE_DATA.i18n.invalid_uz_phone, 'error');
                    return;
                }

                if (email && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
                    showNotification(PAGE_DATA.i18n.invalid_email, 'error');
                    return;
                }

                if (formData.get('password') !== formData.get('confirm_password')) {
                    showNotification(PAGE_DATA.i18n.passwords_mismatch, 'error');
                    return;
                }

                submitBtn.classList.add('loading');

                registrationData = {
                    first_name: formData.get('first_name'),
                    last_name: formData.get('last_name') || '',
                    password: formData.get('password'),
                    referral_code: formData.get('referral_code') || null,
                    newsletter_subscription: formData.get('newsletter') === 'on'
                };

                if (email) registrationData.email = email;

                if (phone) {
                    registrationData.phone = normalizeUzbekistanPhone(phone);
                    await initiatePhoneRegistration(submitBtn);
                } else {
                    await registerWithEmailOnly(submitBtn);
                }
            });
        }

        document.querySelectorAll('.otp-digit').forEach(function (input, index, inputs) {
            input.addEventListener('input', function () {
                this.value = this.value.replace(/[^0-9]/g, '');
                if (this.value && index < inputs.length - 1) inputs[index + 1].focus();
                updateOtpCode();
            });

            input.addEventListener('keydown', function (e) {
                if (e.key === 'Backspace' && !this.value && index > 0) inputs[index - 1].focus();
            });

            input.addEventListener('paste', function (e) {
                e.preventDefault();
                var pastedData = e.clipboardData.getData('text').replace(/[^0-9]/g, '').slice(0, 6);
                pastedData.split('').forEach(function (char, i) {
                    if (inputs[i]) inputs[i].value = char;
                });
                updateOtpCode();
                if (pastedData.length === 6) inputs[5].focus();
            });
        });

        var otpForm = document.getElementById('otpForm');
        if (otpForm) {
            otpForm.addEventListener('submit', async function (e) {
                e.preventDefault();

                var otpCode = document.getElementById('otpCode').value;
                if (otpCode.length !== 6) {
                    showNotification(PAGE_DATA.i18n.incomplete_otp, 'error');
                    return;
                }

                var submitBtn = document.getElementById('verifyOtpBtn');
                submitBtn.classList.add('loading');

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

                    var result = await response.json();

                    if (response.ok) {
                        if (otpTimerInterval) clearInterval(otpTimerInterval);

                        var message = PAGE_DATA.i18n.account_created_verified;

                        showNotification(message, 'success');

                        setTimeout(function () {
                            window.location.href = PAGE_DATA.login_url;
                        }, 2000);
                    } else {
                        showNotification(result.message || PAGE_DATA.i18n.verification_failed, 'error');
                    }
                } catch (error) {
                    showNotification(PAGE_DATA.i18n.network_error, 'error');
                } finally {
                    submitBtn.classList.remove('loading');
                }
            });
        }

        var resendBtn = document.getElementById('resendOtpBtn');
        if (resendBtn) {
            resendBtn.addEventListener('click', function () { resendOtp(); });
        }

        var backBtn = document.getElementById('goBackToFormBtn');
        if (backBtn) {
            backBtn.addEventListener('click', goBackToForm);
        }
    });
})();
