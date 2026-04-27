(function () {
    var PAGE_DATA = getPageData();

    async function resendEmailVerification() {
        try {
            var response = await apiRequest('/auth/resend-email-verification', {
                method: 'POST'
            });

            var result = await response.json();

            if (response.ok) {
                showNotification(PAGE_DATA.i18n.verification_sent, 'success');
            } else {
                showNotification(result.message || PAGE_DATA.i18n.verification_failed, 'error');
            }
        } catch (error) {
            showNotification(PAGE_DATA.i18n.network_error, 'error');
        }
    }

    function verifyPhone() {
        window.location.href = PAGE_DATA.verify_phone_url;
    }

    var personalForm = document.getElementById('personalInfoForm');
    if (personalForm) {
        personalForm.addEventListener('submit', async function (e) {
            e.preventDefault();

            var formData = new FormData(this);
            var data = {
                first_name: formData.get('first_name'),
                last_name: formData.get('last_name'),
                date_of_birth: formData.get('date_of_birth') || null,
                gender: formData.get('gender') || null,
                preferred_language: formData.get('preferred_language')
            };

            try {
                var response = await apiRequest('/auth/profile', {
                    method: 'PUT',
                    body: JSON.stringify(data)
                });

                var result = await response.json();

                if (response.ok) {
                    showNotification(PAGE_DATA.i18n.personal_updated, 'success');
                } else {
                    showNotification(result.message || PAGE_DATA.i18n.personal_failed, 'error');
                }
            } catch (error) {
                showNotification(PAGE_DATA.i18n.network_error, 'error');
            }
        });
    }

    var contactForm = document.getElementById('contactInfoForm');
    if (contactForm) {
        contactForm.addEventListener('submit', async function (e) {
            e.preventDefault();

            var formData = new FormData(this);
            var data = { phone: formData.get('phone') };

            try {
                var response = await apiRequest('/auth/profile', {
                    method: 'PUT',
                    body: JSON.stringify(data)
                });

                var result = await response.json();

                if (response.ok) {
                    showNotification(PAGE_DATA.i18n.contact_updated, 'success');

                    if (data.phone && data.phone !== PAGE_DATA.current_phone) {
                        setTimeout(function () {
                            if (confirm(PAGE_DATA.i18n.verify_new_phone)) {
                                verifyPhone();
                            }
                        }, 2000);
                    }
                } else {
                    showNotification(result.message || PAGE_DATA.i18n.contact_failed, 'error');
                }
            } catch (error) {
                showNotification(PAGE_DATA.i18n.network_error, 'error');
            }
        });
    }

    var prefsForm = document.getElementById('preferencesForm');
    if (prefsForm) {
        prefsForm.addEventListener('submit', function (e) {
            e.preventDefault();
            showNotification(PAGE_DATA.i18n.preferences_saved, 'success');
        });
    }

    var resendEmailBtn = document.querySelector('[data-action="resend-email-verification"]');
    if (resendEmailBtn) {
        resendEmailBtn.addEventListener('click', resendEmailVerification);
    }

    var verifyPhoneBtn = document.querySelector('[data-action="verify-phone"]');
    if (verifyPhoneBtn) {
        verifyPhoneBtn.addEventListener('click', verifyPhone);
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
})();
