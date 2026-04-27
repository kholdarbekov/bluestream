(function () {
    var PAGE_DATA = getPageData();

    function retryPayment() {
        if (!PAGE_DATA.order_id) {
            window.location.href = PAGE_DATA.cart_url;
            return;
        }
        showNotification(PAGE_DATA.i18n.preparing, 'info');
        fetch('/api/v1/orders/' + PAGE_DATA.order_id + '/retry-payment', {
            method: 'POST',
            headers: {
                'Authorization': 'Bearer ' + localStorage.getItem('access_token'),
                'Content-Type': 'application/json'
            }
        })
        .then(function (response) { return response.json(); })
        .then(function (data) {
            if (data.success && data.data.payment_url) {
                window.location.href = data.data.payment_url;
            } else {
                showNotification(PAGE_DATA.i18n.retry_failed, 'error');
            }
        })
        .catch(function (err) {
            console.error('Error retrying payment:', err);
            showNotification(PAGE_DATA.i18n.error, 'error');
        });
    }

    document.addEventListener('DOMContentLoaded', function () {
        var btn = document.querySelector('[data-action="retry-payment"]');
        if (btn) btn.addEventListener('click', retryPayment);
    });
})();
