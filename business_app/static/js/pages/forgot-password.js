(function () {
    var PAGE_DATA = getPageData();
    var form = document.getElementById('forgotPasswordForm');
    if (!form) return;

    form.addEventListener('submit', async function (e) {
        e.preventDefault();

        var formData = new FormData(this);
        var identifier = formData.get('identifier');

        if (!identifier) {
            showNotification(PAGE_DATA.i18n.enter_identifier, 'error');
            return;
        }

        var submitButton = this.querySelector('button[type="submit"]');
        var originalText = submitButton.textContent;
        submitButton.disabled = true;
        submitButton.textContent = PAGE_DATA.i18n.sending;

        try {
            var response = await apiRequest('/auth/forgot-password', {
                method: 'POST',
                body: JSON.stringify({ identifier: identifier })
            });
            var result = await response.json();

            if (response.ok) {
                document.getElementById('forgotPasswordForm').style.display = 'none';
                document.getElementById('successMessage').style.display = 'block';
                showNotification(PAGE_DATA.i18n.sent_success, 'success');
            } else {
                showNotification(result.message || PAGE_DATA.i18n.send_failed, 'error');
            }
        } catch (err) {
            showNotification(PAGE_DATA.i18n.network_error, 'error');
        } finally {
            submitButton.disabled = false;
            submitButton.textContent = originalText;
        }
    });

    var identifierInput = document.querySelector('input[name="identifier"]');
    if (identifierInput) {
        identifierInput.addEventListener('input', function () {
            var value = this.value.trim();
            var isEmail = value.includes('@');
            var isPhone = /^[\+]?[0-9\s\-\(\)]{10,}$/.test(value);

            if (value.length === 0) {
                this.placeholder = PAGE_DATA.i18n.placeholder_default;
            } else if (isEmail) {
                this.placeholder = PAGE_DATA.i18n.placeholder_email;
            } else if (isPhone) {
                this.placeholder = PAGE_DATA.i18n.placeholder_phone;
            }
        });
    }
})();
