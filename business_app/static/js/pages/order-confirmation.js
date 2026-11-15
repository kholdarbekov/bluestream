// The order confirmation page (/order-confirmation). The page itself says the order is placed, so there is no toast
// (it used to pop "Order placed successfully!" on every later visit too); the header's cart count is refreshed, since
// checkout has just emptied the cart.
document.addEventListener('DOMContentLoaded', function () {
    if (typeof window.updateCartCount === 'function') window.updateCartCount();
});
