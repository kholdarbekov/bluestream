(function () {
    var PAGE_DATA = getPageData();
    var form = document.getElementById('contactForm');
    if (!form) return;

    form.addEventListener('submit', async function (e) {
        e.preventDefault();

        var formData = new FormData(this);
        var data = {
            name: formData.get('name'),
            email: formData.get('email'),
            phone: formData.get('phone'),
            subject: formData.get('subject'),
            message: formData.get('message')
        };

        try {
            var response = await apiRequest('/contact', {
                method: 'POST',
                body: JSON.stringify(data)
            });
            if (response.ok) {
                showNotification(PAGE_DATA.i18n.success, 'success');
                this.reset();
            } else {
                showNotification(PAGE_DATA.i18n.error, 'error');
            }
        } catch (err) {
            showNotification(PAGE_DATA.i18n.network_error, 'error');
        }
    });
})();
