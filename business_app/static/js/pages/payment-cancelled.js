// The payment-cancelled page: the order's status, and Pay when the backend publishes it (spec 2026-10-27 §4.5).
// `order-actions.js` draws the published `customer_actions.pay` and sends the Pay (channel web, the amount the screen
// showed). This page shows the status and Pay only (never Cancel or Confirm), so it hands the renderer the Pay
// answer alone. It decides nothing: a browser with no session, or not this customer's order, has no `order`.
(function () {
    var PAGE_DATA = getPageData();

    document.addEventListener('DOMContentLoaded', function () {
        var host = document.getElementById('paymentActions');
        var order = PAGE_DATA.order;
        if (!host || !order) return;
        var published = order.customer_actions || {};
        window.OrderActions.render(host, { id: order.id, customer_actions: { pay: published.pay } }, {
            words: PAGE_DATA.order_actions,
            // A refused or stale Pay (409): reload, so the page is drawn from the backend's new answer.
            onChanged: function () { window.OrderActions.navigate(window.location.href); }
        });
    });
})();
