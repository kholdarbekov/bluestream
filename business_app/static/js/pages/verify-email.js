(function () {
    var PAGE_DATA = getPageData();

    function showError(message) {
        document.getElementById('errorText').textContent = message;
        document.getElementById('errorMessage').style.display = 'flex';
        document.getElementById('successMessage').style.display = 'none';
    }

    async function verifyEmailToken(token, submitBtn) {
        try {
            var response = await apiRequest('/auth/verify-email', {
                method: 'POST',
                body: JSON.stringify({ token: token })
            });

            var result = await response.json();

            if (response.ok) {
                document.getElementById('emailVerificationForm').style.display = 'none';
                document.getElementById('errorMessage').style.display = 'none';
                document.querySelector('.auth-divider').style.display = 'none';
                document.querySelector('.resend-section').style.display = 'none';
                document.querySelector('.auth-info-box').style.display = 'none';
                document.getElementById('successMessage').style.display = 'block';

                showNotification(PAGE_DATA.i18n.verified_success, 'success');

                setTimeout(function () {
                    window.location.href = PAGE_DATA.my_account_url;
                }, 3000);
            } else {
                if (submitBtn) submitBtn.classList.remove('loading');
                showError(result.message || PAGE_DATA.i18n.invalid_code);
            }
        } catch (error) {
            if (submitBtn) submitBtn.classList.remove('loading');
            showError(PAGE_DATA.i18n.network_error);
        }
    }

    document.addEventListener('DOMContentLoaded', function () {
        var urlParams = new URLSearchParams(window.location.search);
        var token = urlParams.get('token');

        if (token) {
            document.querySelector('input[name="token"]').value = token;
            verifyEmailToken(token);
        }
    });

    var form = document.getElementById('emailVerificationForm');
    if (form) {
        form.addEventListener('submit', async function (e) {
            e.preventDefault();

            var formData = new FormData(this);
            var token = formData.get('token');

            if (!token) {
                showError(PAGE_DATA.i18n.enter_code);
                return;
            }

            var submitBtn = this.querySelector('.btn-auth');
            submitBtn.classList.add('loading');

            await verifyEmailToken(token, submitBtn);
        });
    }

    var resendBtn = document.getElementById('resendEmail');
    if (resendBtn) {
        resendBtn.addEventListener('click', async function (e) {
            e.preventDefault();

            var button = this;
            button.disabled = true;
            button.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + PAGE_DATA.i18n.sending;

            try {
                var response = await apiRequest('/auth/resend-email-verification', {
                    method: 'POST'
                });

                var result = await response.json();

                if (response.ok) {
                    showNotification(PAGE_DATA.i18n.sent_success, 'success');

                    var countdown = 60;
                    button.innerHTML = '<i class="fas fa-clock"></i> ' + PAGE_DATA.i18n.resend_in + ' ' + countdown + 's';

                    var timer = setInterval(function () {
                        countdown--;
                        button.innerHTML = '<i class="fas fa-clock"></i> ' + PAGE_DATA.i18n.resend_in + ' ' + countdown + 's';

                        if (countdown <= 0) {
                            clearInterval(timer);
                            button.disabled = false;
                            button.innerHTML = '<i class="fas fa-paper-plane"></i> ' + PAGE_DATA.i18n.resend_button;
                        }
                    }, 1000);
                } else {
                    button.disabled = false;
                    button.innerHTML = '<i class="fas fa-paper-plane"></i> ' + PAGE_DATA.i18n.resend_button;
                    showError(result.message || PAGE_DATA.i18n.send_failed);
                }
            } catch (error) {
                button.disabled = false;
                button.innerHTML = '<i class="fas fa-paper-plane"></i> ' + PAGE_DATA.i18n.resend_button;
                showError(PAGE_DATA.i18n.network_error);
            }
        });
    }

    var tokenInput = document.querySelector('input[name="token"]');
    if (tokenInput) {
        tokenInput.addEventListener('input', function () {
            this.value = this.value.replace(/\s/g, '');
        });
    }
})();
