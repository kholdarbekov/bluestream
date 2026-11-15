(function () {
    var PAGE_DATA = getPageData();

    // The order dates in the page's language and Tashkent time (base.js AE.formatDate, the helper /my-orders uses):
    // the server prints a plain dd.mm.yyyy until this runs.
    function localiseDates() {
        if (!window.AE || typeof window.AE.formatDate !== 'function') return;
        document.querySelectorAll('time[data-ae-date]').forEach(function (el) {
            var text = window.AE.formatDate(el.getAttribute('datetime'));
            if (text) el.textContent = text;
        });
    }

    // The backend mints the one-time link; the page only navigates to what it answers.
    async function connectTelegram(event) {
        var button = event.currentTarget;
        button.disabled = true;
        button.setAttribute('aria-busy', 'true');
        try {
            var response = await apiRequest('/auth/telegram-link?lang=' + encodeURIComponent(PAGE_DATA.language), { method: 'POST' });
            var result = await response.json();
            if (response.ok && result.success) {
                window.location.href = result.data.telegram_link;
            } else {
                showNotification(result.message || PAGE_DATA.i18n.telegram_link_failed, 'error');
            }
        } catch (error) {
            showNotification(PAGE_DATA.i18n.telegram_link_failed, 'error');
        } finally {
            button.disabled = false;
            button.removeAttribute('aria-busy');
        }
    }

    async function downloadAccountData(event) {
        var button = event.currentTarget;
        button.setAttribute('aria-busy', 'true');
        try {
            showNotification(PAGE_DATA.i18n.preparing_download, 'info');

            var response = await apiRequest('/auth/export-data', { method: 'POST' });

            if (response.ok) {
                var blob = await response.blob();
                var url = window.URL.createObjectURL(blob);
                var a = document.createElement('a');
                a.style.display = 'none';
                a.href = url;
                a.download = 'account-data.json';
                document.body.appendChild(a);
                a.click();
                window.URL.revokeObjectURL(url);
                showNotification(PAGE_DATA.i18n.download_success, 'success');
            } else {
                throw new Error('Failed to download data');
            }
        } catch (error) {
            console.error('Failed to download account data:', error);
            showNotification(PAGE_DATA.i18n.download_failed, 'error');
        } finally {
            button.removeAttribute('aria-busy');
        }
    }

    document.addEventListener('DOMContentLoaded', function () {
        localiseDates();

        // A write in the order modal (a cancel, a skip, a Pay) makes the recent orders behind it out of date.
        document.addEventListener('orders:changed', function () { window.location.reload(); });

        document.querySelectorAll('[data-action="show-order"]').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var orderId = this.dataset.orderId;
                if (typeof window.showOrderDetail === 'function') {
                    window.showOrderDetail(orderId);
                }
            });
        });

        document.querySelectorAll('[data-action="connect-telegram"]').forEach(function (btn) {
            btn.addEventListener('click', connectTelegram);
        });

        var downloadBtn = document.querySelector('[data-action="download-account-data"]');
        if (downloadBtn) downloadBtn.addEventListener('click', downloadAccountData);
    });
})();
