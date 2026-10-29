// The payment-pending page: polls the payment through the session cookie until Click's answer reaches us
// (spec 2026-10-27 §4.5). Paid goes to the order's own page; failed or cancelled goes to the cancel page, which
// publishes the Pay answer. A browser with no session or order (the neutral page) has no `payment_id`: it polls
// nothing.
(function () {
    var PAGE_DATA = getPageData();
    var MAX_ATTEMPTS = 20;
    var INTERVAL_MS = 6000;
    var checkAttempts = 0;

    function scheduleNext() {
        checkAttempts += 1;
        if (checkAttempts < MAX_ATTEMPTS) {
            setTimeout(checkPaymentStatus, INTERVAL_MS);
        } else if (PAGE_DATA.i18n) {
            showNotification(PAGE_DATA.i18n.timeout, 'warning');
        }
    }

    async function checkPaymentStatus() {
        var payment = null;
        try {
            var response = await apiRequest('/payments/' + PAGE_DATA.payment_id + '/status');
            var body = await response.json();
            // A non-OK answer is not an answer about the payment: keep polling.
            if (response.ok && body.success && body.data && body.data.payment) payment = body.data.payment;
        } catch (err) {
            console.error('Error checking payment status:', err);
        }
        var status = payment && (payment.status || payment.payment_status);
        if (status === 'completed' || status === 'paid') {
            window.OrderActions.navigate(PAGE_DATA.order_page_url);
        } else if (status === 'failed' || status === 'cancelled') {
            showNotification(PAGE_DATA.i18n.failed, 'error');
            setTimeout(function () { window.OrderActions.navigate(PAGE_DATA.cancel_url); }, 2000);
        } else {
            scheduleNext();
        }
    }

    document.addEventListener('DOMContentLoaded', function () {
        if (!PAGE_DATA.payment_id) return;
        setTimeout(checkPaymentStatus, 3000);

        var btn = document.querySelector('[data-action="check-payment-status"]');
        if (btn) btn.addEventListener('click', checkPaymentStatus);
    });
})();
