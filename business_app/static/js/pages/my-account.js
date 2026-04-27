(function () {
    var PAGE_DATA = getPageData();

    function updateSyncStatus(status) {
        var syncElement = document.getElementById('syncStatus');
        if (!syncElement) return;

        switch (status) {
            case 'synchronized':
                syncElement.textContent = PAGE_DATA.i18n.synchronized;
                syncElement.className = 'text-success';
                break;
            case 'syncing':
                syncElement.textContent = PAGE_DATA.i18n.syncing;
                syncElement.className = 'text-warning';
                break;
            case 'error':
                syncElement.textContent = PAGE_DATA.i18n.sync_error;
                syncElement.className = 'text-danger';
                break;
        }
    }

    async function loadDashboardData() {
        try {
            var addressResponse = await apiRequest('/auth/addresses');
            var addressResult = await addressResponse.json();
            if (addressResponse.ok && addressResult.success) {
                document.getElementById('addressesCount').textContent =
                    addressResult.data.addresses.length;
            }

            var ordersResponse = await apiRequest('/orders/summary');
            var ordersResult = await ordersResponse.json();
            if (ordersResponse.ok && ordersResult.success) {
                document.getElementById('totalOrdersCount').textContent =
                    ordersResult.data.total_orders || PAGE_DATA.recent_orders_count;
            }
        } catch (error) {
            console.error('Failed to load dashboard data:', error);
        }
    }

    function updateDashboardData() {
        loadDashboardData();

        apiRequest('/auth/profile')
            .then(function (response) { return response.json(); })
            .then(function (data) {
                if (data.success) {
                    updateSyncStatus('synchronized');
                }
            })
            .catch(function (error) {
                console.error('Failed to update dashboard data:', error);
                updateSyncStatus('error');
            });
    }

    function monitorSyncStatus() {
        setInterval(function () {
            var syncElement = document.getElementById('syncStatus');
            if (syncElement) updateSyncStatus('synchronized');
        }, 60000);
    }

    function checkVerificationStatus() {
        if (!PAGE_DATA.email_verified) {
            showNotification(PAGE_DATA.i18n.verify_email_prompt, 'warning');
        }
        if (!PAGE_DATA.phone_verified) {
            showNotification(PAGE_DATA.i18n.verify_phone_prompt, 'info');
        }
    }

    function connectTelegram() {
        showNotification(PAGE_DATA.i18n.telegram_coming_soon, 'info');
    }

    async function downloadAccountData() {
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
        }
    }

    document.addEventListener('DOMContentLoaded', function () {
        checkVerificationStatus();
        loadDashboardData();
        setInterval(updateDashboardData, 300000);
        monitorSyncStatus();

        document.querySelectorAll('[data-nav-href]').forEach(function (el) {
            el.addEventListener('click', function () {
                window.location.href = this.dataset.navHref;
            });
        });

        document.querySelectorAll('[data-action="show-order"]').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var orderId = this.dataset.orderId;
                if (typeof window.showOrderDetail === 'function') {
                    window.showOrderDetail(orderId);
                }
            });
        });

        var telegramBtn = document.querySelector('[data-action="connect-telegram"]');
        if (telegramBtn) telegramBtn.addEventListener('click', connectTelegram);

        var downloadBtn = document.querySelector('[data-action="download-account-data"]');
        if (downloadBtn) downloadBtn.addEventListener('click', downloadAccountData);
    });
})();
