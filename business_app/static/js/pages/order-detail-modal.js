(function () {
    var PAGE_DATA = getPageData('order-detail-modal-data');
    var I18N = PAGE_DATA.i18n || {};
    var orderDeliveryMap = null;
    // What opened the modal (a recent-order row, a card's Details button), so closing it puts focus back there.
    var opener = null;

    // base.js's one copy (window.AE, loaded before every page script).
    var escapeHtml = window.AE.escapeHtml;

    // `{name}` placeholders, filled by base.js (AE.fill, the one fill every page script shares).
    var fill = window.AE.fill;

    // Every amount and the date are the backend's labels (`data.labels`, spec 2026-10-26 §12). A discount, reward or
    // tax row is drawn only when the server publishes its label, which it does only for a non-zero amount. The one
    // figure it has no label for, a line's unit price, goes through base.js AE.formatMoney (the same money words).
    function summaryRow(label, value) {
        return '<div class="order-summary-row ae-dl__row"><dt class="label">' + escapeHtml(label) + '</dt><dd class="value">' + escapeHtml(value || '') + '</dd></div>';
    }

    function discountRow(label, value) {
        if (!value) return '';
        return '<div class="order-summary-row ae-dl__row ae-od__discount"><dt class="label">' + escapeHtml(label) + '</dt><dd class="value">−' + escapeHtml(value) + '</dd></div>';
    }

    // F15: the status the backend publishes (`display_status`: the wait for a new date, else the status itself), in
    // the words and colour of macros/order_status.html, the one place that words a status, handed over in the island.
    function statusLabel(order) {
        return (PAGE_DATA.i18n.status_labels || {})[order.display_status] || '';
    }

    function statusPill(order) {
        var tone = (PAGE_DATA.status_tones || {})[order.display_status] || 'muted';
        var pill = window.AE ? window.AE.statusPill(statusLabel(order), tone)
            : '<span class="ae-status">' + escapeHtml(statusLabel(order)) + '</span>';
        return pill.replace('class="ae-status', 'class="order-status status-' + escapeHtml(order.display_status) + ' ae-status');
    }

    function initOrderDeliveryMap(lat, lng, label) {
        if (orderDeliveryMap) {
            orderDeliveryMap.remove();
            orderDeliveryMap = null;
        }

        var mapContainer = document.getElementById('orderDeliveryMap');
        if (!mapContainer || !window.L) return;

        // Leaflet's own controls speak English: the zoom buttons get the page's words, and the popup (a label, not
        // a dialog) has no close button to announce.
        orderDeliveryMap = L.map(mapContainer, {
            zoomControl: false,
            scrollWheelZoom: false,
            dragging: true
        });
        orderDeliveryMap.setView([lat, lng], 16);
        L.control.zoom({ zoomInTitle: I18N.zoom_in, zoomOutTitle: I18N.zoom_out }).addTo(orderDeliveryMap);

        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
            maxZoom: 19
        }).addTo(orderDeliveryMap);

        L.marker([lat, lng]).addTo(orderDeliveryMap)
            .bindPopup('<strong>' + escapeHtml(label) + '</strong>', { closeButton: false })
            .openPopup();

        setTimeout(function () {
            if (orderDeliveryMap) orderDeliveryMap.invalidateSize();
        }, 100);
    }

    function hideMap() {
        // No map without Leaflet; the address above it still says where the order goes.
        var mapContainer = document.getElementById('orderDeliveryMap');
        if (mapContainer) mapContainer.hidden = true;
    }

    // The vendored Leaflet through map-helper.js (AEMap.load: once per page, pins from static/vendor/leaflet/images),
    // the loader order_tracking.html and the address wizard use; the island publishes its URLs.
    function drawMap(address) {
        var label = address.title || address.label || address.street_address || I18N.delivery_location;
        if (!window.AEMap || !PAGE_DATA.leaflet) { hideMap(); return; }
        window.AEMap.load(PAGE_DATA.leaflet).then(function () {
            setTimeout(function () {
                try {
                    initOrderDeliveryMap(address.latitude, address.longitude, label);
                } catch (error) {
                    hideMap();
                }
            }, 300);
        }).catch(hideMap);
    }

    // Focus goes back to what opened the modal; after a redraw (the list behind it reloaded) to the same order's
    // Details button or row.
    function returnFocus() {
        var target = opener && opener.element;
        if (!target || !document.body.contains(target) || target === document.body) {
            var id = opener && opener.orderId;
            target = id ? document.querySelector(
                'button[data-action="show-order-detail"][data-id="' + id + '"], [data-action="show-order"][data-order-id="' + id + '"]'
            ) : null;
        }
        opener = null;
        if (target && typeof target.focus === 'function') target.focus();
    }

    async function showOrderDetail(orderId) {
        // The first open remembers its opener; a reload of the open modal (after a write) keeps it.
        if (!$('#orderDetailModal').hasClass('show')) {
            opener = { element: document.activeElement, orderId: String(orderId) };
        }
        try {
            var response = await apiRequest('/orders/' + orderId);
            var result = await response.json();

            if (response.ok && result.success) {
                displayOrderDetail(result.data.order, result.data.labels);
                $('#orderDetailModal').modal('show');
            } else {
                showNotification(result.message || PAGE_DATA.i18n.details_failed, 'error');
            }
        } catch (error) {
            console.error('Failed to load order detail:', error);
            showNotification(PAGE_DATA.i18n.details_failed, 'error');
        }
    }

    function itemRow(item, itemLabels) {
        var placeholder = PAGE_DATA.image_placeholder || '/static/images/product-placeholder.svg';
        var name = item.product_display_name || item.product_name || '';
        var unit = window.AE ? window.AE.formatMoney(item.unit_price) : '';
        // A courtesy line prints the split the API published (AE.linePriceText), never quantity x unit price beside
        // the net total.
        var detail = item.is_reward
            ? '<span class="ae-badge ae-badge--gold">' + escapeHtml(I18N.reward) + '</span>'
            : item.courtesy_quantity
                ? escapeHtml(window.AE.linePriceText(item, { courtesy_part: I18N.courtesy_part }))
                : escapeHtml(fill(I18N.qty_price, { quantity: item.quantity, price: unit }));
        return '<li class="ae-line modal-order-item">' +
            '<img class="ae-thumb ae-thumb--lg" src="' + escapeHtml(item.product_image || placeholder) + '" alt="" width="56" height="56" ' +
            'data-fallback="' + escapeHtml(placeholder) + '">' +
            '<span class="ae-line__text"><span class="ae-line__name">' + escapeHtml(name) + '</span>' +
            '<span class="ae-line__meta">' + detail + '</span></span>' +
            '<span class="ae-line__sum">' + escapeHtml(itemLabels[String(item.id)] || '') + '</span></li>';
    }

    // `district` is the order's published `delivery_address_district_label` (its name in the page language), never
    // the stored key.
    function addressBlock(addr, district) {
        if (!addr) return '<p class="ae-od__muted">' + escapeHtml(I18N.no_address) + '</p>';
        var title = addr.title || addr.label;
        var line = addr.street_address || addr.full_address || '';
        var notes = addr.delivery_instructions || addr.delivery_notes;
        return '<div class="ae-od__address">' +
            (title ? '<p class="ae-od__address-title">' + escapeHtml(title) + '</p>' : '') +
            (line ? '<p class="ae-od__address-line">' + escapeHtml(line) + '</p>' : '') +
            (district ? '<p class="ae-od__address-line"><span class="ae-od__muted">' + escapeHtml(I18N.district) + ':</span> ' +
                escapeHtml(district) + '</p>' : '') +
            (notes ? '<p class="ae-od__note"><i class="far fa-sticky-note" aria-hidden="true"></i>' + escapeHtml(notes) + '</p>' : '') +
            ((addr.latitude && addr.longitude) ? '<div id="orderDeliveryMap" class="ae-od__map ae-map"></div>' : '') +
            '</div>';
    }

    // The day it was placed: the backend's label ("Wednesday, 30 September"), which names no year, for an order from
    // this year; an older one with its year, as the order cards print it (base.js AE.formatDate, Tashkent time).
    function tashkentYear(date) {
        return new Intl.DateTimeFormat('en-GB', { timeZone: 'Asia/Tashkent', year: 'numeric' }).format(date);
    }

    function placedOn(order, labels) {
        var placed = new Date(order.created_at);
        if (!isNaN(placed.getTime()) && tashkentYear(placed) !== tashkentYear(new Date(Date.now()))) {
            return window.AE.formatDate(order.created_at) || labels.placed_on || '';
        }
        return labels.placed_on || '';
    }

    function displayOrderDetail(order, labels) {
        labels = labels || {};
        var itemLabels = labels.items || {};
        var modalContent = document.getElementById('orderDetailContent');
        var reorderBtn = document.getElementById('reorderBtn');

        // Reorder is the backend's `customer_actions.reorder` (the one reorder rule), with its label.
        var reorder = (order.customer_actions && order.customer_actions.reorder) || {};
        reorderBtn.style.display = reorder.allowed ? 'inline-flex' : 'none';
        reorderBtn.querySelector('[data-reorder-label]').textContent = reorder.allowed ? reorder.label : '';
        reorderBtn.onclick = function () { reorderItems(order.id); };

        // Track order: the order's own page, the route the server spells (`track_url`, with ORDER_ID to fill in).
        var trackLink = document.getElementById('orderTrackLink');
        if (trackLink && PAGE_DATA.track_url) trackLink.href = PAGE_DATA.track_url.replace('ORDER_ID', encodeURIComponent(order.id));

        // Pay, Skip/Cancel/Decline and Confirm: the backend's `customer_actions` for this order, drawn by the one
        // script every order surface shares (order-actions.js), with the link to the subscription a skip belongs to.
        window.OrderActions.render(document.getElementById('orderDetailActions'), order, {
            words: PAGE_DATA.order_actions,
            subscriptionsUrl: PAGE_DATA.subscriptions_url,
            onChanged: function () {
                // The card behind the modal (/my-orders) redraws too.
                document.dispatchEvent(new window.CustomEvent('orders:changed'));
                return showOrderDetail(order.id);
            }
        });

        var title = document.getElementById('orderDetailModalLabel');
        if (title) title.textContent = fill(I18N.order_number, { number: order.order_number });

        var itemsHtml = (order.order_items || []).map(function (item) { return itemRow(item, itemLabels); }).join('');

        modalContent.innerHTML =
            '<div class="order-detail-content ae-od__body">' +
            '<div class="ae-od__head">' + statusPill(order) +
            (placedOn(order, labels) ? '<p class="ae-od__placed"><i class="far fa-calendar-alt" aria-hidden="true"></i><span>' +
                escapeHtml(fill(I18N.placed_on, { date: placedOn(order, labels) })) + '</span></p>' : '') +
            '</div>' +
            '<section class="ae-od__section" aria-labelledby="od-items">' +
            '<h3 class="ae-od__title" id="od-items">' + escapeHtml(I18N.order_items) + '</h3>' +
            '<ul class="ae-od__items modal-order-items">' + itemsHtml + '</ul></section>' +
            '<div class="ae-od__grid">' +
            '<section class="ae-od__section" aria-labelledby="od-address">' +
            '<h3 class="ae-od__title" id="od-address">' + escapeHtml(I18N.delivery_address) + '</h3>' +
            addressBlock(order.delivery_address, order.delivery_address_district_label) + '</section>' +
            '<section class="ae-od__section" aria-labelledby="od-summary">' +
            '<h3 class="ae-od__title" id="od-summary">' + escapeHtml(I18N.order_summary) + '</h3>' +
            '<dl class="ae-dl ae-dl--end ae-od__summary">' +
            summaryRow(I18N.subtotal, labels.subtotal) +
            summaryRow(I18N.delivery_fee, labels.delivery_fee) +
            discountRow(I18N.tier_discount, labels.tier_discount) +
            discountRow(I18N.subscription_discount, labels.subscription_discount) +
            discountRow(I18N.reward_discount, labels.reward_discount) +
            (labels.tax ? summaryRow(I18N.tax, labels.tax) : '') +
            '<div class="order-summary-total ae-dl__row ae-dl__total"><dt class="label">' + escapeHtml(I18N.total) + '</dt><dd class="value">' + escapeHtml(labels.total || '') + '</dd></div>' +
            '</dl></section>' +
            '</div></div>';

        var addr = order.delivery_address;
        if (addr && addr.latitude && addr.longitude) drawMap(addr);
    }

    function reorderItems(orderId) {
        return window.OrderActions.reorder(orderId, { cartUrl: PAGE_DATA.cart_url, words: PAGE_DATA.order_actions });
    }

    $('#orderDetailModal').on('hidden.bs.modal', function () {
        if (orderDeliveryMap) {
            orderDeliveryMap.remove();
            orderDeliveryMap = null;
        }
        returnFocus();
    });

    window.showOrderDetail = showOrderDetail;
    window.displayOrderDetail = displayOrderDetail;
    window.reorderItems = reorderItems;
    window.initOrderDeliveryMap = initOrderDeliveryMap;
})();
