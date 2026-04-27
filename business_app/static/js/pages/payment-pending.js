(function () {
    var PAGE_DATA = getPageData();
    var checkAttempts = 0;
    var maxAttempts = 20;

    function checkPaymentStatus() {
        fetch('/api/v1/payments/' + PAGE_DATA.payment_id + '/status', {
            headers: {
                'Authorization': 'Bearer ' + localStorage.getItem('access_token')
            }
        })
        .then(function (response) { return response.json(); })
        .then(function (data) {
            if (!(data.success && data.data.payment)) return;
            var payment = data.data.payment;
            var paymentStatus = payment.status || payment.payment_status;

            if (paymentStatus === 'completed' || paymentStatus === 'paid') {
                window.location.href = '/order-confirmation?order_id=' + PAGE_DATA.order_id;
            } else if (paymentStatus === 'failed' || paymentStatus === 'cancelled') {
                showNotification(PAGE_DATA.i18n.failed, 'error');
                setTimeout(function () {
                    window.location.href = '/payment/cancel?order_id=' + PAGE_DATA.order_id;
                }, 2000);
            } else {
                checkAttempts++;
                if (checkAttempts < maxAttempts) {
                    setTimeout(checkPaymentStatus, 6000);
                } else {
                    showNotification(PAGE_DATA.i18n.timeout, 'warning');
                }
            }
        })
        .catch(function (err) {
            console.error('Error checking payment status:', err);
            checkAttempts++;
            if (checkAttempts < maxAttempts) {
                setTimeout(checkPaymentStatus, 6000);
            }
        });
    }

    document.addEventListener('DOMContentLoaded', function () {
        setTimeout(checkPaymentStatus, 3000);

        var btn = document.querySelector('[data-action="check-payment-status"]');
        if (btn) btn.addEventListener('click', checkPaymentStatus);
    });
})();
