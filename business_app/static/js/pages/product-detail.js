// /product/<id|slug>: the quantity stepper, the gallery thumbnails, "Add to cart" and the phone sticky bar.
// The stepper starts at, and never goes below, the smallest order the backend publishes (#qtyInput data-min-qty =
// offer facts smallest_order_quantity) and stops at page data max_qty (stock that caps the purchase, else
// MAX_QUANTITY_PER_ITEM). A number typed into it is held between the two once the field is left. The cart and
// checkout still validate both. Related product cards are the shared card, whose button base.js handles
// (add-to-cart-shop adds the card's own smallest order).
(function () {
    var PAGE_DATA = getPageData();
    var input = document.getElementById('qtyInput');
    var sum = document.getElementById('shop-pd-sum');
    var barQty = document.getElementById('shop-pd-bar-qty');
    var barSum = document.getElementById('shop-pd-bar-sum');
    var minQty = Math.max(1, parseInt((input && input.getAttribute('data-min-qty')) || PAGE_DATA.min_order_quantity, 10) || 1);
    var maxQty = Math.max(minQty, parseInt(PAGE_DATA.max_qty, 10) || minQty);
    var qty = clamp(parseInt(input && input.value, 10));

    function clamp(value) {
        return Math.min(Math.max(value || minQty, minQty), maxQty);
    }

    function stepButtons(delta) {
        return document.querySelectorAll('[data-action="change-qty"][data-delta="' + delta + '"]');
    }

    function render() {
        if (!input) return;
        input.value = qty;
        stepButtons(-1).forEach(function (button) { button.disabled = qty <= minQty; });
        stepButtons(1).forEach(function (button) { button.disabled = qty >= maxQty; });
        var unitPrice = Number(PAGE_DATA.unit_price);
        var total = unitPrice > 0 && window.AE ? window.AE.formatMoney(unitPrice * qty) : null;
        if (total && sum) sum.textContent = total;
        if (total && barSum) barSum.textContent = total;
        if (barQty && PAGE_DATA.units_label) barQty.textContent = String(PAGE_DATA.units_label).replace('{n}', qty);
    }

    function changeQty(delta) {
        var next = qty + delta;
        if (next < minQty || next > maxQty) return;
        qty = next;
        render();
    }

    // Typing: digits only while typing; the number is held between the minimum and the cap when the field is left
    // (or Enter is pressed), so "1" on the way to "12" is never corrected under the visitor's fingers.
    function typed() {
        var digits = input.value.replace(/\D+/g, '');
        if (digits !== input.value) input.value = digits;
    }

    function commitTyped() {
        qty = clamp(parseInt(input.value, 10));
        render();
    }

    function changeImage(button) {
        var main = document.getElementById('mainProductImage');
        if (!main) return;
        main.removeAttribute('data-ae-fallback');
        main.src = button.getAttribute('data-image');
        document.querySelectorAll('.thumb-item').forEach(function (thumb) {
            var active = thumb === button;
            thumb.classList.toggle('active', active);
            thumb.setAttribute('aria-pressed', active ? 'true' : 'false');
        });
    }

    document.addEventListener('click', function (event) {
        var target = event.target.closest('[data-action]');
        if (!target) return;
        switch (target.getAttribute('data-action')) {
            case 'change-qty':
                changeQty(parseInt(target.getAttribute('data-delta'), 10) || 0);
                break;
            case 'change-image':
                changeImage(target);
                break;
            case 'add-to-cart-qty':
                // A number still being typed counts: the field may not have been left yet.
                if (input && document.activeElement === input) commitTyped();
                if (typeof window.addToCart === 'function') {
                    window.addToCart(parseInt(target.getAttribute('data-product-id'), 10), qty);
                }
                break;
        }
    });

    if (input) {
        input.addEventListener('input', typed);
        input.addEventListener('change', commitTyped);
        input.addEventListener('keydown', function (event) {
            if (event.key === 'Enter') {
                event.preventDefault();
                commitTyped();
            }
        });
    }

    // Phones: the sticky bar shows whenever the main "Add to cart" is not wholly on screen, below the first screen on
    // load (a sliver at the bottom edge does not count) as much as scrolled away above it, and hides while it is.
    // Desktop hides the bar in CSS (ae-ui.css .ae-sticky-bar).
    var bar = document.getElementById('shop-pd-bar');
    var mainAdd = document.querySelector('#shop-pd-actions [data-action="add-to-cart-qty"]');
    if (bar && mainAdd && 'IntersectionObserver' in window) {
        new IntersectionObserver(function (entries) {
            bar.hidden = entries[entries.length - 1].intersectionRatio >= 0.99;
        }, { threshold: [0, 0.99] }).observe(mainAdd);
    }

    render();
})();
