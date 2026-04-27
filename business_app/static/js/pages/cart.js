(function () {
    var PAGE_DATA = getPageData();
    var PRODUCT_DETAIL_URL_TEMPLATE = PAGE_DATA.product_detail_url_template;
    var PRODUCT_DETAIL_URL_SLUG_TEMPLATE = PAGE_DATA.product_detail_url_slug_template;
    var MIN_ORDER_AMOUNT = PAGE_DATA.min_order_amount || 20000;

    var cartItems = [];
    var isAuthenticated = typeof CURRENT_USER !== 'undefined' && CURRENT_USER !== null;

    function escapeHtml(str) {
        if (str === null || str === undefined) return '';
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function formatPrice(amount) {
        var num = parseFloat(amount) || 0;
        return new Intl.NumberFormat('uz-UZ').format(Math.round(num)) + ' UZS';
    }

    function updateCartCount() {
        var count = cartItems.reduce(function (sum, item) { return sum + item.quantity; }, 0);
        document.querySelectorAll('.cart-count, .cart-badge').forEach(function (el) {
            el.textContent = count;
            el.style.display = count > 0 ? 'block' : 'none';
        });
    }

    function showLoading() {
        document.getElementById('cart-loading').style.display = 'block';
        document.getElementById('cart-content').style.display = 'none';
        document.getElementById('empty-cart').style.display = 'none';
    }

    function hideLoading() {
        document.getElementById('cart-loading').style.display = 'none';
    }

    function showEmptyCart() {
        document.getElementById('cart-loading').style.display = 'none';
        document.getElementById('cart-content').style.display = 'none';
        document.getElementById('empty-cart').style.display = 'block';
    }

    function showError(message) {
        showNotification(message, 'error');
        hideLoading();
    }

    function updateSummary(subtotal) {
        document.getElementById('summary-subtotal').textContent = formatPrice(subtotal);
        document.getElementById('summary-total').textContent = formatPrice(subtotal);

        var warningEl = document.getElementById('min-order-warning');
        var titleEl = document.getElementById('min-order-title');
        var textEl = document.getElementById('min-order-text');
        var progressEl = document.getElementById('min-order-progress');
        var checkoutBtn = document.getElementById('checkout-btn');

        if (subtotal < MIN_ORDER_AMOUNT) {
            var remaining = MIN_ORDER_AMOUNT - subtotal;
            var progress = Math.min((subtotal / MIN_ORDER_AMOUNT) * 100, 100);

            warningEl.style.display = 'flex';
            warningEl.classList.add('error');
            warningEl.classList.remove('success');
            warningEl.querySelector('i').className = 'far fa-exclamation-triangle';

            titleEl.textContent = PAGE_DATA.i18n.min_not_met;
            textEl.textContent = PAGE_DATA.i18n.add + ' ' + formatPrice(remaining) + ' ' +
                PAGE_DATA.i18n.more_to_checkout + '. ' + PAGE_DATA.i18n.min_order_amount + ': ' + formatPrice(MIN_ORDER_AMOUNT);
            progressEl.style.width = progress + '%';

            checkoutBtn.classList.add('checkout-btn-disabled');
            checkoutBtn.onclick = function (e) {
                e.preventDefault();
                showNotification(PAGE_DATA.i18n.min_warning + ' ' + formatPrice(MIN_ORDER_AMOUNT), 'warning');
            };
        } else {
            warningEl.style.display = 'flex';
            warningEl.classList.remove('error');
            warningEl.classList.add('success');
            warningEl.querySelector('i').className = 'far fa-check-circle';

            titleEl.textContent = PAGE_DATA.i18n.ready_checkout;
            textEl.textContent = PAGE_DATA.i18n.meets_minimum;
            progressEl.style.width = '100%';

            checkoutBtn.classList.remove('checkout-btn-disabled');
            checkoutBtn.onclick = null;
        }
    }

    function renderCart(products) {
        var container = document.getElementById('cart-items-container');
        var cartHTML = '';
        var subtotal = 0;

        cartItems.forEach(function (cartItem) {
            var product = products.find(function (p) { return p.id === cartItem.product_id; });
            if (!product) return;

            var pricing = product.pricing || {};
            var price = parseFloat(pricing.current_price || pricing.base_price || product.base_price || 0);
            var itemTotal = price * cartItem.quantity;
            subtotal += itemTotal;

            var stockStatus = PAGE_DATA.i18n.in_stock;
            var stockClass = 'in-stock';
            var inventory = product.inventory || {};
            var trackInventory = inventory.track_inventory !== undefined ? inventory.track_inventory : product.track_inventory;
            var stockQuantity = inventory.stock_quantity !== undefined ? inventory.stock_quantity : product.stock_quantity;

            if (trackInventory && stockQuantity !== undefined && stockQuantity !== null) {
                if (stockQuantity === 0) {
                    stockStatus = PAGE_DATA.i18n.out_of_stock;
                    stockClass = 'out-of-stock';
                } else if (stockQuantity < cartItem.quantity) {
                    stockStatus = PAGE_DATA.i18n.only + ' ' + stockQuantity + ' ' + PAGE_DATA.i18n.available;
                    stockClass = 'low-stock';
                } else if (stockQuantity < 10) {
                    stockStatus = PAGE_DATA.i18n.low_stock + ' (' + stockQuantity + ')';
                    stockClass = 'low-stock';
                }
            }

            var productImages = (product.media && product.media.images) || product.images || [];
            var productImage = productImages.length > 0 ? productImages[0] : PAGE_DATA.default_image;

            var productUrl = product.slug
                ? PRODUCT_DETAIL_URL_SLUG_TEMPLATE.replace('__SLUG__', encodeURIComponent(product.slug))
                : PRODUCT_DETAIL_URL_TEMPLATE.replace('{id}', product.id);

            var decBtnDisabled = cartItem.quantity <= 1 ? ' disabled' : '';

            cartHTML += '<div class="cart-item-card" data-product-id="' + product.id + '">' +
                '<div class="item-image">' +
                '<img src="' + escapeHtml(productImage) + '" alt="' + escapeHtml(product.name) + '">' +
                '</div>' +
                '<div class="item-details">' +
                '<div class="item-name">' +
                '<a href="' + escapeHtml(productUrl) + '">' + escapeHtml(product.name) + '</a>' +
                '</div>' +
                '<div class="item-stock ' + stockClass + '">' + escapeHtml(stockStatus) + '</div>' +
                '<div class="item-price-row">' +
                '<span class="item-unit-price">' + formatPrice(price) + ' × ' + cartItem.quantity + '</span>' +
                '<span class="item-total-price">' + formatPrice(itemTotal) + '</span>' +
                '</div></div>' +
                '<div class="item-actions">' +
                '<div class="quantity-control">' +
                '<button class="qty-btn" data-action="update-qty" data-product-id="' + product.id + '" data-qty="' + (cartItem.quantity - 1) + '"' + decBtnDisabled + '>' +
                '<i class="far fa-minus"></i></button>' +
                '<input type="text" class="qty-input" value="' + cartItem.quantity + '" readonly>' +
                '<button class="qty-btn" data-action="update-qty" data-product-id="' + product.id + '" data-qty="' + (cartItem.quantity + 1) + '">' +
                '<i class="far fa-plus"></i></button>' +
                '</div>' +
                '<button class="remove-btn" data-action="remove-item" data-product-id="' + product.id + '" title="' + escapeHtml(PAGE_DATA.i18n.remove) + '">' +
                '<i class="far fa-times"></i></button>' +
                '</div></div>';
        });

        container.innerHTML = cartHTML;

        updateSummary(subtotal);
        updateCartCount();

        document.getElementById('cart-content').style.display = 'block';
        document.getElementById('items-count').textContent = cartItems.length;
    }

    async function loadCart() {
        cartItems = JSON.parse(localStorage.getItem('cart') || '[]');

        if (cartItems.length === 0) {
            showEmptyCart();
            updateCartCount();
            return;
        }

        try {
            var productIds = cartItems.map(function (item) { return item.product_id; });
            var response = await apiRequest('/products/bulk', {
                method: 'POST',
                body: JSON.stringify({
                    product_ids: productIds,
                    language: PAGE_DATA.current_language
                })
            });

            if (response.ok) {
                var data = await response.json();
                if (data.success && data.data && data.data.products && data.data.products.length > 0) {
                    renderCart(data.data.products);
                } else {
                    showEmptyCart();
                }
            } else {
                showError(PAGE_DATA.i18n.load_failed);
            }
        } catch (error) {
            console.error('Error loading cart:', error);
            showError(PAGE_DATA.i18n.load_error);
        } finally {
            hideLoading();
        }
    }

    async function updateQuantity(productId, newQuantity) {
        if (newQuantity < 1) return;

        var itemIndex = cartItems.findIndex(function (item) { return item.product_id === productId; });
        if (itemIndex === -1) return;

        cartItems[itemIndex].quantity = newQuantity;
        localStorage.setItem('cart', JSON.stringify(cartItems));

        if (isAuthenticated) {
            try {
                await apiRequest('/cart/items/' + productId, {
                    method: 'PUT',
                    body: JSON.stringify({ quantity: newQuantity })
                });
            } catch (error) {
                console.error('Error updating cart in database:', error);
            }
        }

        await loadCart();
    }

    async function removeFromCart(productId) {
        if (!confirm(PAGE_DATA.i18n.remove_confirm)) return;

        cartItems = cartItems.filter(function (item) { return item.product_id !== productId; });
        localStorage.setItem('cart', JSON.stringify(cartItems));

        if (isAuthenticated) {
            try {
                await apiRequest('/cart/items/' + productId, { method: 'DELETE' });
            } catch (error) {
                console.error('Error removing from database:', error);
            }
        }

        showNotification(PAGE_DATA.i18n.removed, 'success');

        if (cartItems.length === 0) {
            showEmptyCart();
        } else {
            await loadCart();
        }

        updateCartCount();
    }

    async function clearCart() {
        if (!confirm(PAGE_DATA.i18n.clear_confirm)) return;

        cartItems = [];
        localStorage.setItem('cart', JSON.stringify(cartItems));

        if (isAuthenticated) {
            try {
                await apiRequest('/cart/clear', { method: 'POST' });
            } catch (error) {
                console.error('Error clearing database cart:', error);
            }
        }

        showNotification(PAGE_DATA.i18n.cleared, 'success');
        showEmptyCart();
        updateCartCount();
    }

    document.addEventListener('DOMContentLoaded', async function () {
        showLoading();

        if (window.cartSyncPromise) {
            await window.cartSyncPromise;
        }

        await loadCart();

        var clearBtn = document.querySelector('[data-action="clear-cart"]');
        if (clearBtn) clearBtn.addEventListener('click', clearCart);

        document.body.addEventListener('click', function (e) {
            var target = e.target.closest('[data-action]');
            if (!target) return;

            var action = target.dataset.action;
            var productId = parseInt(target.dataset.productId, 10);

            if (action === 'update-qty') {
                updateQuantity(productId, parseInt(target.dataset.qty, 10));
            } else if (action === 'remove-item') {
                removeFromCart(productId);
            }
        });
    });
})();
