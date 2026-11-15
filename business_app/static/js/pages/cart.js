// /cart: renders the cart lines, the summary and the notices that hold checkout back.
//
// The lines are the local cart (localStorage "cart"; pages/base.js replaces it with the database cart first when
// signed in, window.cartSyncPromise). Product details and the charged price come from POST /products/bulk
// (pricing.current_price). Each line's stepper stops at the product's published minimum
// (inventory.min_order_quantity) and at its stock when stock caps the purchase (else MAX_QUANTITY_PER_ITEM); a line
// held below its minimum elsewhere (an older cart, the bot) says so and its "+" goes straight to the minimum. The
// order floor (MIN_ORDER_AMOUNT) is a notice. Checkout validates all of it again. Signed in, every change is
// saved to the database cart (PUT / DELETE /cart/items/<id>, POST /cart/clear, POST /cart/items for an undo);
// a change the server refuses is rolled back and said so. A line whose product /products/bulk no longer returns
// (retired or inactive) is taken out of the cart, the header count and, signed in, the database cart, and said so.
// "In stock" and "low stock" are the API's answers (inventory.is_in_stock / is_low_stock); the stock count only caps
// the stepper and names the number left.
(function () {
    var PAGE_DATA = getPageData();
    var I18N = PAGE_DATA.i18n || {};
    var MIN_ORDER_AMOUNT = Number(PAGE_DATA.min_order_amount) || 0;
    var MAX_QTY = parseInt(PAGE_DATA.max_qty, 10) || 100;
    var DELIVERY_FEE = Number(PAGE_DATA.delivery_fee) || 0;
    var isAuthenticated = !!window.CURRENT_USER;

    var cartItems = [];
    var productsById = {};
    var blockedNotice = null; // the notice element that explains why checkout is held back, or null
    var lastRemoved = null; // { item, index, name } for the undo bar
    var saving = Promise.resolve(); // database writes, one after another
    var state = 'loading';
    var actionsInView = false; // the summary's own checkout button is on screen (phones: the sticky bar steps aside)

    function $(id) { return document.getElementById(id); }

    function money(amount) { return window.AE.formatMoney(amount); }
    function esc(value) { return window.AE.escapeHtml(value); }

    // `{name}` placeholders, filled by base.js (AE.fill, the one fill every page script shares).
    var fill = window.AE.fill;

    function units(count) {
        return fill(count === 1 ? I18N.units_one : I18N.units_other, { n: count });
    }

    function readCart() {
        try {
            var stored = JSON.parse(localStorage.getItem('cart') || '[]');
            return Array.isArray(stored) ? stored : [];
        } catch (error) {
            return [];
        }
    }

    // One cart array for this page and base.js (window.cart feeds the header counter).
    function saveCart() {
        localStorage.setItem('cart', JSON.stringify(cartItems));
        window.cart = cartItems;
        if (typeof window.updateCartCount === 'function') window.updateCartCount();
    }

    // Signed in: run the database write after the previous one; resolve to whether the server accepted it.
    function persist(path, method, body) {
        if (!isAuthenticated) return Promise.resolve(true);
        saving = saving.then(function () {
            var options = { method: method };
            if (body) options.body = JSON.stringify(body);
            return apiRequest(path, options)
                .then(function (response) { return response.ok; })
                .catch(function () { return false; });
        });
        return saving;
    }

    function syncBar() {
        $('cart-bar').hidden = state !== 'content' || actionsInView;
    }

    function show(next) {
        state = next;
        $('cart-loading').hidden = state !== 'loading';
        $('cart-content').hidden = state !== 'content';
        $('empty-cart').hidden = state !== 'empty';
        $('cart-error').hidden = state !== 'error';
        syncBar();
    }

    function productUrl(product) {
        return product.slug
            ? PAGE_DATA.product_detail_url_slug_template.replace('__SLUG__', encodeURIComponent(product.slug))
            : PAGE_DATA.product_detail_url_template.replace('{id}', product.id);
    }

    function limits(product) {
        var inventory = product.inventory || {};
        var minQty = Math.max(1, parseInt(inventory.min_order_quantity || product.min_order_quantity, 10) || 1);
        var tracked = inventory.track_inventory && inventory.stock_quantity !== null && inventory.stock_quantity !== undefined;
        var stock = tracked ? Number(inventory.stock_quantity) : null;
        return { min: minQty, max: stock === null ? MAX_QTY : Math.min(stock, MAX_QTY), stock: stock };
    }

    function stockTag(product, quantity) {
        var inventory = product.inventory || {};
        var stock = limits(product).stock;
        if (inventory.is_in_stock === false) return window.AE.statusPill(I18N.stock_out, 'danger');
        if (stock === null) return '';
        // Low as the API rates it, or fewer left than this line asks for (the cap checkout will hold it to).
        if (inventory.is_low_stock || stock < quantity) return window.AE.statusPill(fill(I18N.stock_low, { n: stock }), 'warn');
        return '';
    }

    function lineHtml(item, product) {
        var price = Number((product.pricing || {}).current_price) || 0;
        var bounds = limits(product);
        var images = (product.media && product.media.images) || product.images || [];
        var image = images[0] || PAGE_DATA.default_image;
        var url = esc(productUrl(product));
        var name = esc(product.name);
        var id = product.id;
        var more = Math.min(bounds.max, Math.max(item.quantity + 1, bounds.min));
        var below = item.quantity < bounds.min;
        return '<li class="shop-cart__item' + (below ? ' is-below-min' : '') + '" data-product-id="' + id + '">' +
            '<a class="shop-cart__thumb" href="' + url + '" tabindex="-1" aria-hidden="true">' +
            '<img src="' + esc(image) + '" alt="" width="88" height="88" loading="lazy" decoding="async" data-fallback="' + esc(PAGE_DATA.default_image) + '">' +
            '</a>' +
            '<div class="shop-cart__info">' +
            '<a class="shop-cart__name" href="' + url + '">' + name + '</a>' +
            '<span class="shop-cart__unit">' + esc(fill(I18N.unit_price, { price: money(price) })) + '</span>' +
            (below ? '<span class="shop-cart__hint">' + esc(fill(I18N.min_qty_hint, { min: bounds.min })) + '</span>' : '') +
            stockTag(product, item.quantity) +
            '</div>' +
            '<div class="ae-qty shop-cart__qty" role="group" aria-label="' + esc(fill(I18N.qty_label, { name: product.name })) + '">' +
            '<button type="button" class="ae-qty__btn" data-action="update-qty" data-step="less" data-product-id="' + id + '" data-qty="' + (item.quantity - 1) + '" aria-label="' + esc(I18N.qty_less) + '"' + (item.quantity <= bounds.min ? ' disabled' : '') + '>' +
            '<i class="far fa-minus" aria-hidden="true"></i></button>' +
            '<input type="text" class="ae-qty__input" value="' + item.quantity + '" inputmode="numeric" pattern="[0-9]*" autocomplete="off" enterkeyhint="done" data-qty-input data-product-id="' + id + '" aria-label="' + esc(fill(I18N.qty_label, { name: product.name })) + '">' +
            '<button type="button" class="ae-qty__btn" data-action="update-qty" data-step="more" data-product-id="' + id + '" data-qty="' + more + '" aria-label="' + esc(I18N.qty_more) + '"' + (item.quantity >= bounds.max ? ' disabled' : '') + '>' +
            '<i class="far fa-plus" aria-hidden="true"></i></button>' +
            '</div>' +
            '<span class="shop-cart__line">' + esc(money(price * item.quantity)) + '</span>' +
            '<button type="button" class="shop-cart__remove" data-action="remove-item" data-product-id="' + id + '" aria-label="' + esc(fill(I18N.remove, { name: product.name })) + '">' +
            '<i class="far fa-trash-alt" aria-hidden="true"></i></button>' +
            '</li>';
    }

    function setNotice(element, visible) {
        element.hidden = !visible;
        return visible ? element : null;
    }

    function updateSummary(subtotal, count, violations) {
        var total = subtotal + DELIVERY_FEE;
        $('summary-units').textContent = '(' + units(count) + ')';
        $('summary-subtotal').textContent = money(subtotal);
        $('summary-total').textContent = money(total);
        $('cart-bar-total').textContent = money(total);
        $('items-count').textContent = units(count);

        // One notice at a time, the line minimum first: raising a line to its minimum usually clears the floor too, and
        // two stacked warnings read as more trouble than there is.
        var amountShort = !violations.length && subtotal < MIN_ORDER_AMOUNT;
        if (amountShort) {
            $('min-order-text').textContent = fill(I18N.min_amount_text, {
                remaining: money(MIN_ORDER_AMOUNT - subtotal),
                minimum: money(MIN_ORDER_AMOUNT)
            });
            $('min-order-progress').style.width = Math.min(100, (subtotal / MIN_ORDER_AMOUNT) * 100) + '%';
        }
        $('min-qty-warning-list').innerHTML = violations.map(function (line) {
            return '<li>' + esc(fill(I18N.min_qty_line, line)) + '</li>';
        }).join('');

        blockedNotice = setNotice($('min-order-warning'), amountShort) || null;
        blockedNotice = setNotice($('min-qty-warnings'), violations.length > 0) || blockedNotice;
        document.querySelectorAll('[data-checkout]').forEach(function (link) {
            if (blockedNotice) link.setAttribute('aria-disabled', 'true');
            else link.removeAttribute('aria-disabled');
        });
    }

    function render() {
        var rows = [];
        var subtotal = 0;
        var count = 0;
        var violations = [];
        cartItems.forEach(function (item) {
            var product = productsById[item.product_id];
            if (!product) return;
            var price = Number((product.pricing || {}).current_price) || 0;
            var bounds = limits(product);
            subtotal += price * item.quantity;
            count += item.quantity;
            if (item.quantity < bounds.min) {
                violations.push({ name: product.name, min: bounds.min, remaining: bounds.min - item.quantity });
            }
            rows.push(lineHtml(item, product));
        });
        if (!rows.length) {
            show('empty');
            return;
        }
        $('cart-items-container').innerHTML = rows.join('');
        updateSummary(subtotal, count, violations);
        show('content');
    }

    // Re-rendering replaces the controls: keep the keyboard on the one that was used (or its neighbour).
    function renderKeepingFocus(productId, step) {
        render();
        if (!productId) return;
        var line = document.querySelector('.shop-cart__item[data-product-id="' + productId + '"]');
        if (!line) return;
        var control = step === 'typed' ? line.querySelector('[data-qty-input]') : line.querySelector('[data-step="' + step + '"]');
        if (!control || control.disabled) control = line.querySelector('[data-action="update-qty"]:not([disabled])');
        if (control) control.focus({ preventScroll: true });
    }

    async function loadCart() {
        cartItems = readCart();
        window.cart = cartItems;
        if (!cartItems.length) {
            show('empty');
            return;
        }
        try {
            var response = await apiRequest('/products/bulk', {
                method: 'POST',
                body: JSON.stringify({
                    product_ids: cartItems.map(function (item) { return item.product_id; }),
                    language: PAGE_DATA.current_language
                })
            });
            if (!response.ok) throw new Error('products/bulk ' + response.status);
            var data = await response.json();
            productsById = {};
            ((data.data && data.data.products) || []).forEach(function (product) { productsById[product.id] = product; });
        } catch (error) {
            console.error('Error loading cart:', error);
            show('error');
            return;
        }
        dropUnsold();
        render();
    }

    // Lines the catalogue no longer sells cannot be shown, bought or stepped: take them out everywhere, once.
    function dropUnsold() {
        var unsold = cartItems.filter(function (item) { return !productsById[item.product_id]; });
        var notice = $('cart-dropped');
        if (notice) notice.hidden = !unsold.length;
        if (!unsold.length) return;
        cartItems = cartItems.filter(function (item) { return productsById[item.product_id]; });
        saveCart();
        unsold.forEach(function (item) { persist('/cart/items/' + item.product_id, 'DELETE'); });
    }

    function retryLoad() {
        show('loading');
        loadCart();
    }

    function findIndex(productId) {
        return cartItems.findIndex(function (item) { return item.product_id === productId; });
    }

    function hideUndo() {
        lastRemoved = null;
        $('cart-undo').hidden = true;
    }

    // A number typed into a line: held between the product's minimum and its cap, saved like a step.
    function commitTyped(field) {
        var productId = parseInt(field.getAttribute('data-product-id'), 10);
        var index = findIndex(productId);
        var product = productsById[productId];
        if (index === -1 || !product) return;
        var bounds = limits(product);
        var quantity = Math.min(Math.max(parseInt(field.value, 10) || bounds.min, bounds.min), bounds.max);
        if (quantity === cartItems[index].quantity) {
            field.value = quantity;
            return;
        }
        updateQuantity(productId, quantity, 'typed');
    }

    async function updateQuantity(productId, quantity, step) {
        var index = findIndex(productId);
        if (index === -1 || quantity < 1) return;
        var previous = cartItems[index].quantity;
        // Never step down below the product's minimum, even from a button forced back on.
        var product = productsById[productId];
        if (product && quantity < previous && quantity < limits(product).min) return;
        hideUndo();
        cartItems[index].quantity = quantity;
        saveCart();
        renderKeepingFocus(productId, step);
        if (!(await persist('/cart/items/' + productId, 'PUT', { quantity: quantity }))) {
            var again = findIndex(productId);
            if (again !== -1) cartItems[again].quantity = previous;
            saveCart();
            render();
            showNotification(I18N.update_error, 'error');
        }
    }

    async function removeItem(productId) {
        var index = findIndex(productId);
        if (index === -1) return;
        var item = cartItems.splice(index, 1)[0];
        var product = productsById[productId] || {};
        saveCart();
        render();
        if (!(await persist('/cart/items/' + productId, 'DELETE'))) {
            cartItems.splice(Math.min(index, cartItems.length), 0, item);
            saveCart();
            render();
            showNotification(I18N.update_error, 'error');
            return;
        }
        lastRemoved = { item: item, index: index };
        $('cart-undo-text').textContent = fill(I18N.removed, { name: product.name || '' });
        $('cart-undo').hidden = false;
        // The line and its button are gone: the keyboard lands on Undo, the next thing one may want.
        $('cart-undo').querySelector('button').focus({ preventScroll: true });
    }

    async function undoRemove() {
        if (!lastRemoved) return;
        var removed = lastRemoved;
        hideUndo();
        cartItems.splice(Math.min(removed.index, cartItems.length), 0, removed.item);
        saveCart();
        render();
        var restored = document.querySelector('.shop-cart__item[data-product-id="' + removed.item.product_id + '"] .shop-cart__name');
        if (restored) restored.focus({ preventScroll: true });
        if (!(await persist('/cart/items', 'POST', { product_id: removed.item.product_id, quantity: removed.item.quantity }))) {
            cartItems = cartItems.filter(function (item) { return item !== removed.item; });
            saveCart();
            render();
            showNotification(I18N.update_error, 'error');
        }
    }

    function toggleClearConfirm(open) {
        $('cart-clear-confirm').hidden = !open;
        $('cart-clear-btn').hidden = open;
        $('cart-clear-btn').setAttribute('aria-expanded', open ? 'true' : 'false');
        var focusTarget = open ? document.querySelector('[data-action="clear-cart-no"]') : $('cart-clear-btn');
        if (focusTarget) focusTarget.focus({ preventScroll: true });
    }

    async function clearCart() {
        var previous = cartItems.slice();
        toggleClearConfirm(false);
        hideUndo();
        cartItems = [];
        saveCart();
        show('empty');
        if (!(await persist('/cart/clear', 'POST'))) {
            cartItems = previous;
            saveCart();
            render();
            showNotification(I18N.update_error, 'error');
        }
    }

    async function addFromPicks(button) {
        if (typeof window.addToCart !== 'function') return;
        var quantity = Math.max(1, parseInt(button.getAttribute('data-qty'), 10) || 1);
        await window.addToCart(parseInt(button.getAttribute('data-product-id'), 10), quantity);
        await loadCart();
    }

    // Checkout held back: the link stays focusable and says why instead of doing nothing.
    function guardCheckout(event) {
        if (!blockedNotice) return;
        event.preventDefault();
        var text = blockedNotice.querySelector('p, li');
        showNotification(text ? text.textContent : '', 'warning');
        blockedNotice.focus({ preventScroll: true });
        var still = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        blockedNotice.scrollIntoView({ block: 'center', behavior: still ? 'auto' : 'smooth' });
    }

    document.addEventListener('click', function (event) {
        var checkout = event.target.closest('[data-checkout]');
        if (checkout) {
            guardCheckout(event);
            return;
        }
        var target = event.target.closest('[data-action]');
        if (!target) return;
        var productId = parseInt(target.getAttribute('data-product-id'), 10);
        switch (target.getAttribute('data-action')) {
            case 'update-qty':
                updateQuantity(productId, parseInt(target.getAttribute('data-qty'), 10), target.getAttribute('data-step'));
                break;
            case 'remove-item':
                removeItem(productId);
                break;
            case 'undo-remove':
                undoRemove();
                break;
            case 'clear-cart':
                toggleClearConfirm(true);
                break;
            case 'clear-cart-no':
                toggleClearConfirm(false);
                break;
            case 'clear-cart-yes':
                clearCart();
                break;
            case 'add-to-cart':
                addFromPicks(target);
                break;
            case 'retry-load':
                retryLoad();
                break;
        }
    });

    // The quantity fields: digits only while typing; the number is saved when the field is left or Enter is pressed.
    document.addEventListener('input', function (event) {
        var field = event.target.closest && event.target.closest('[data-qty-input]');
        if (!field) return;
        var digits = field.value.replace(/\D+/g, '');
        if (digits !== field.value) field.value = digits;
    });
    document.addEventListener('change', function (event) {
        var field = event.target.closest && event.target.closest('[data-qty-input]');
        if (field) commitTyped(field);
    });
    document.addEventListener('keydown', function (event) {
        var field = event.key === 'Enter' && event.target.closest && event.target.closest('[data-qty-input]');
        if (!field) return;
        event.preventDefault();
        commitTyped(field);
    });

    // Phones: the sticky bar keeps the total and checkout in reach while the lines scroll, and steps aside while the
    // summary's own button is on screen (two identical buttons one above the other). Desktop hides it in CSS.
    var summaryActions = document.querySelector('.shop-cart__actions');
    if (summaryActions && 'IntersectionObserver' in window) {
        new IntersectionObserver(function (entries) {
            actionsInView = entries[entries.length - 1].isIntersecting;
            syncBar();
        }).observe(summaryActions);
    }

    document.addEventListener('DOMContentLoaded', async function () {
        if (window.cartSyncPromise) await window.cartSyncPromise;
        await loadCart();
    });
})();
