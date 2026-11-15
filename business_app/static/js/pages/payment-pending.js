// The payment-pending page: polls the payment through the session cookie until Click's answer reaches us
// (spec 2026-10-27 §4.5). Paid goes to the order's own page; failed or cancelled goes to the cancel page, which
// publishes the Pay answer. A browser with no session or order (the neutral page) has no `payment_id`: it polls
// nothing. After the last poll the page says, in place, that the answer will come by message.
(function () {
    var PAGE_DATA = getPageData();
    var MAX_ATTEMPTS = 20;
    var INTERVAL_MS = 6000;
    var checkAttempts = 0;

    function showTimeout() {
        var live = document.getElementById('pending-live');
        var notice = document.getElementById('pending-timeout');
        if (live) live.hidden = true;
        if (notice) notice.hidden = false;
    }

    function showStillPending() {
        var caption = document.querySelector('#pending-live [data-pending-caption]');
        if (caption && PAGE_DATA.i18n && PAGE_DATA.i18n.still_pending) caption.textContent = PAGE_DATA.i18n.still_pending;
    }

    function scheduleNext() {
        checkAttempts += 1;
        if (checkAttempts < MAX_ATTEMPTS) {
            setTimeout(checkPaymentStatus, INTERVAL_MS);
        } else {
            showTimeout();
        }
    }

    // The payment's status as the backend answers it, or null when the answer is not about the payment.
    async function readStatus() {
        try {
            var response = await apiRequest('/payments/' + PAGE_DATA.payment_id + '/status');
            var body = await response.json();
            // A non-OK answer is not an answer about the payment.
            var payment = response.ok && body.success && body.data ? body.data.payment : null;
            return payment ? (payment.status || payment.payment_status) : null;
        } catch (err) {
            return null;
        }
    }

    // Moves on when the payment is settled either way; true when it did.
    function moveOn(status) {
        if (status === 'completed' || status === 'paid') {
            window.OrderActions.navigate(PAGE_DATA.order_page_url);
            return true;
        }
        if (status === 'failed' || status === 'cancelled') {
            showNotification(PAGE_DATA.i18n.failed, 'error');
            setTimeout(function () { window.OrderActions.navigate(PAGE_DATA.cancel_url); }, 2000);
            return true;
        }
        return false;
    }

    async function checkPaymentStatus() {
        if (!moveOn(await readStatus())) scheduleNext();
    }

    document.addEventListener('DOMContentLoaded', function () {
        if (!PAGE_DATA.payment_id) return;
        setTimeout(checkPaymentStatus, 3000);

        // A manual check asks once more; it does not start a second polling loop.
        var btn = document.querySelector('[data-action="check-payment-status"]');
        if (!btn) return;
        btn.addEventListener('click', async function () {
            if (btn.getAttribute('aria-busy') === 'true') return;
            btn.setAttribute('aria-busy', 'true');
            // Still pending: the caption (a status region) says the check happened, instead of nothing at all.
            if (!moveOn(await readStatus())) showStillPending();
            btn.removeAttribute('aria-busy');
        });
    });
})();
