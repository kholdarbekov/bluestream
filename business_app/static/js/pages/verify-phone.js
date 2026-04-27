(function () {
    var PAGE_DATA = getPageData();
    var currentUserId = null;
    var currentPhone = null;
    var timerInterval = null;

    function startTimer(seconds) {
        clearInterval(timerInterval);

        var timerElement = document.getElementById('timer');
        var timeLeft = seconds;

        timerInterval = setInterval(function () {
            var minutes = Math.floor(timeLeft / 60);
            var secs = timeLeft % 60;

            timerElement.textContent =
                String(minutes).padStart(2, '0') + ':' + String(secs).padStart(2, '0');

            if (timeLeft <= 0) {
                clearInterval(timerInterval);
                timerElement.textContent = '00:00';
                showNotification(PAGE_DATA.i18n.code_expired, 'warning');
            }

            timeLeft--;
        }, 1000);
    }

    var phoneForm = document.getElementById('phoneForm');
    if (phoneForm) {
        phoneForm.addEventListener('submit', async function (e) {
            e.preventDefault();

            var formData = new FormData(this);
            var phone = formData.get('phone');

            if (!phone) {
                showNotification(PAGE_DATA.i18n.invalid_phone, 'error');
                return;
            }

            try {
                var token = getAuthToken();
                if (!token) {
                    showNotification(PAGE_DATA.i18n.please_login, 'error');
                    window.location.href = PAGE_DATA.login_url;
                    return;
                }

                var response = await apiRequest('/auth/send-otp', {
                    method: 'POST',
                    body: JSON.stringify({
                        phone: phone,
                        user_id: CURRENT_USER ? CURRENT_USER.id : null
                    })
                });

                var result = await response.json();

                if (response.ok) {
                    currentUserId = result.user_id;
                    currentPhone = phone;

                    document.getElementById('phoneForm').style.display = 'none';
                    document.getElementById('otpForm').style.display = 'block';
                    document.getElementById('sentToPhone').textContent = phone;

                    startTimer(300);

                    showNotification(PAGE_DATA.i18n.code_sent, 'success');
                } else {
                    showNotification(result.message || PAGE_DATA.i18n.send_failed, 'error');
                }
            } catch (error) {
                showNotification(PAGE_DATA.i18n.network_error, 'error');
            }
        });
    }

    var otpForm = document.getElementById('otpForm');
    if (otpForm) {
        otpForm.addEventListener('submit', async function (e) {
            e.preventDefault();

            var formData = new FormData(this);
            var otp = formData.get('otp');

            if (!otp || otp.length !== 6) {
                showNotification(PAGE_DATA.i18n.invalid_otp, 'error');
                return;
            }

            try {
                var response = await apiRequest('/auth/verify-otp', {
                    method: 'POST',
                    body: JSON.stringify({
                        user_id: currentUserId,
                        otp: otp
                    })
                });

                var result = await response.json();

                if (response.ok) {
                    document.getElementById('otpForm').style.display = 'none';
                    document.getElementById('successMessage').style.display = 'block';

                    clearInterval(timerInterval);

                    showNotification(PAGE_DATA.i18n.verified, 'success');
                } else {
                    showNotification(result.message || PAGE_DATA.i18n.invalid_code, 'error');
                }
            } catch (error) {
                showNotification(PAGE_DATA.i18n.network_error, 'error');
            }
        });
    }

    var resendBtn = document.getElementById('resendCode');
    if (resendBtn) {
        resendBtn.addEventListener('click', async function (e) {
            e.preventDefault();

            if (!currentPhone) {
                showNotification(PAGE_DATA.i18n.no_phone, 'error');
                return;
            }

            try {
                var response = await apiRequest('/auth/send-otp', {
                    method: 'POST',
                    body: JSON.stringify({
                        phone: currentPhone,
                        user_id: currentUserId
                    })
                });

                var result = await response.json();

                if (response.ok) {
                    startTimer(300);
                    showNotification(PAGE_DATA.i18n.code_resent, 'success');
                } else {
                    showNotification(result.message || PAGE_DATA.i18n.resend_failed, 'error');
                }
            } catch (error) {
                showNotification(PAGE_DATA.i18n.network_error, 'error');
            }
        });
    }

    var changePhoneBtn = document.getElementById('changePhone');
    if (changePhoneBtn) {
        changePhoneBtn.addEventListener('click', function (e) {
            e.preventDefault();

            document.getElementById('phoneForm').style.display = 'block';
            document.getElementById('otpForm').style.display = 'none';

            clearInterval(timerInterval);

            document.getElementById('phoneForm').reset();
        });
    }

    var phoneInput = document.querySelector('input[name="phone"]');
    if (phoneInput) {
        phoneInput.addEventListener('input', function () {
            var value = this.value.replace(/\D/g, '');

            if (value.length > 0 && !value.startsWith('998')) {
                if (value.startsWith('0')) {
                    value = '998' + value.substring(1);
                } else {
                    value = '998' + value;
                }
            }

            if (value.length > 0) {
                this.value = '+' + value;
            }
        });
    }

    var otpInput = document.querySelector('input[name="otp"]');
    if (otpInput) {
        otpInput.addEventListener('input', function () {
            this.value = this.value.replace(/\D/g, '');
        });
    }
})();
