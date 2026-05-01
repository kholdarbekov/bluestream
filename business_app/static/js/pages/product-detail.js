(function () {
    var PAGE_DATA = getPageData();
    var qtyInput = document.getElementById('qtyInput');
    var minQty = parseInt((qtyInput && qtyInput.dataset.minQty) || PAGE_DATA.min_order_quantity || 1, 10) || 1;
    var qty = parseInt((qtyInput && qtyInput.value) || minQty, 10) || minQty;
    var maxQty = PAGE_DATA.max_qty;

    function changeQty(delta) {
        var newQty = qty + delta;
        if (newQty >= minQty && newQty <= maxQty) {
            qty = newQty;
            document.getElementById('qtyInput').value = qty;
        }
    }

    function changeImage(src, element) {
        document.getElementById('mainProductImage').src = src;
        document.querySelectorAll('.thumb-item').forEach(function (el) {
            el.classList.remove('active');
        });
        element.classList.add('active');
    }

    function addToWishlist(id) {
        showNotification(PAGE_DATA.i18n.added_to_wishlist, 'success');
    }

    document.addEventListener('DOMContentLoaded', function () {
        document.body.addEventListener('click', function (e) {
            var target = e.target.closest('[data-action]');
            if (!target) return;

            var action = target.dataset.action;
            switch (action) {
                case 'change-qty':
                    changeQty(parseInt(target.dataset.delta, 10));
                    break;
                case 'change-image':
                    changeImage(target.dataset.image, target);
                    break;
                case 'add-to-cart-qty':
                    if (typeof window.addToCart === 'function') {
                        window.addToCart(parseInt(target.dataset.productId, 10), qty);
                    }
                    break;
                case 'add-to-wishlist':
                    addToWishlist(parseInt(target.dataset.productId, 10));
                    break;
                case 'add-to-cart':
                    if (typeof window.addToCart === 'function') {
                        window.addToCart(parseInt(target.dataset.productId, 10));
                    }
                    break;
            }
        });
    });
})();
