(function () {
    var PAGE_DATA = getPageData('order-detail-modal-data');
    var orderDeliveryMap = null;

    function escapeHtml(str) {
        if (str === null || str === undefined) return '';
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function formatOrderAmount(amount) {
        if (!amount) return '0 UZS';
        return Math.round(amount).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ' ') + ' UZS';
    }

    function formatOrderDate(dateString) {
        if (!dateString) return '';
        var date = new Date(dateString);
        return date.toLocaleDateString('en-GB', {
            year: 'numeric', month: 'short', day: 'numeric'
        });
    }

    function capitalizeStatus(str) {
        if (!str) return '';
        return str.charAt(0).toUpperCase() + str.slice(1).replace(/_/g, ' ');
    }

    function initOrderDeliveryMap(lat, lng, label) {
        if (orderDeliveryMap) {
            orderDeliveryMap.remove();
            orderDeliveryMap = null;
        }

        var mapContainer = document.getElementById('orderDeliveryMap');
        if (!mapContainer) return;

        orderDeliveryMap = L.map('orderDeliveryMap', {
            zoomControl: true,
            scrollWheelZoom: false,
            dragging: true
        }).setView([lat, lng], 16);

        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
            maxZoom: 19
        }).addTo(orderDeliveryMap);

        var marker = L.marker([lat, lng]).addTo(orderDeliveryMap);
        marker.bindPopup('<strong>' + escapeHtml(label) + '</strong>').openPopup();

        setTimeout(function () {
            orderDeliveryMap.invalidateSize();
        }, 100);
    }

    async function showOrderDetail(orderId) {
        try {
            var response = await apiRequest('/orders/' + orderId);
            var result = await response.json();

            if (response.ok && result.success) {
                displayOrderDetail(result.data.order);
                $('#orderDetailModal').modal('show');
            } else {
                showNotification(result.message || PAGE_DATA.i18n.details_failed, 'error');
            }
        } catch (error) {
            console.error('Failed to load order detail:', error);
            showNotification(PAGE_DATA.i18n.details_failed, 'error');
        }
    }

    function displayOrderDetail(order) {
        var modalContent = document.getElementById('orderDetailContent');
        var reorderBtn = document.getElementById('reorderBtn');
        var cancelBtn = document.getElementById('cancelOrderBtn');

        reorderBtn.style.display = order.status === 'delivered' ? 'inline-block' : 'none';
        cancelBtn.style.display = ['pending', 'confirmed'].includes(order.status) ? 'inline-block' : 'none';

        reorderBtn.onclick = function () { reorderItems(order.id); };
        cancelBtn.onclick = function () { cancelOrder(order.id); };

        var itemsHtml = (order.order_items || []).map(function (item) {
            var product = item.product || {};
            var imgUrl = item.product_image_url || product.image_url || '/static/images/products/default.jpg';
            var name = item.product_name || product.name || 'Product';
            var sku = item.product_sku || product.sku || 'N/A';

            return '<div class="modal-order-item">' +
                '<img class="item-image" src="' + escapeHtml(imgUrl) + '" alt="' + escapeHtml(name) + '">' +
                '<div class="item-info">' +
                '<div class="item-name">' + escapeHtml(name) + '</div>' +
                '<div class="item-sku">' + escapeHtml(PAGE_DATA.i18n.sku) + ': ' + escapeHtml(sku) + '</div>' +
                '</div>' +
                '<div class="item-qty">' +
                '<span class="qty-label">' + escapeHtml(PAGE_DATA.i18n.quantity) + '</span>' +
                '<span class="qty-value">' + item.quantity + '</span>' +
                '</div>' +
                '<div class="item-total">' +
                '<span class="total-label">' + escapeHtml(PAGE_DATA.i18n.price) + '</span>' +
                '<span class="total-value">' + formatOrderAmount(item.total_price || item.unit_price * item.quantity) + '</span>' +
                '</div></div>';
        }).join('');

        var addressBlock;
        if (order.delivery_address) {
            var addr = order.delivery_address;
            var title = addr.title || addr.label;
            var labelHtml = title ? '<div class="address-label"><i class="far fa-tag"></i>' + escapeHtml(title) + '</div>' : '';
            var district = addr.district ? '<div class="address-line">' + escapeHtml(addr.district) + '</div>' : '';
            var instructions = (addr.delivery_instructions || addr.delivery_notes)
                ? '<div class="text-muted mb-2" style="font-size: 13px;"><i class="far fa-sticky-note mr-1"></i>' +
                  escapeHtml(addr.delivery_instructions || addr.delivery_notes) + '</div>'
                : '';
            var mapDiv = (addr.latitude && addr.longitude) ? '<div id="orderDeliveryMap"></div>' : '';
            addressBlock = '<div class="address-card-content">' + labelHtml +
                '<div class="address-text">' +
                '<div class="address-line">' + escapeHtml(addr.street_address || addr.full_address || '') + '</div>' +
                district + '</div>' + instructions + mapDiv + '</div>';
        } else {
            addressBlock = '<div class="address-card-content"><p class="no-address">' + escapeHtml(PAGE_DATA.i18n.no_address) + '</p></div>';
        }

        var trackingBlock = order.tracking_number
            ? '<div class="tracking-section">' +
              '<h5><i class="far fa-truck mr-2"></i>' + escapeHtml(PAGE_DATA.i18n.order_tracking) + '</h5>' +
              '<div class="d-flex justify-content-between align-items-center">' +
              '<span><strong>' + escapeHtml(PAGE_DATA.i18n.tracking_number) + ':</strong> ' + escapeHtml(order.tracking_number) + '</span>' +
              '<button class="btn btn-sm btn-outline-primary" data-action="track-order-package" data-tracking="' + escapeHtml(order.tracking_number) + '">' +
              '<i class="far fa-external-link"></i> ' + escapeHtml(PAGE_DATA.i18n.track_package) + '</button>' +
              '</div></div>'
            : '';

        modalContent.innerHTML =
            '<div class="order-detail-content">' +
            '<div class="row mb-4 align-items-center">' +
            '<div class="col-md-8">' +
            '<h4 class="mb-2">' + escapeHtml(PAGE_DATA.i18n.order) + ' #' + escapeHtml(order.order_number) + '</h4>' +
            '<p class="text-muted mb-0"><i class="far fa-calendar-alt mr-1"></i>' + escapeHtml(PAGE_DATA.i18n.placed_on) + ' ' + escapeHtml(formatOrderDate(order.created_at)) + '</p>' +
            '</div>' +
            '<div class="col-md-4 text-right">' +
            '<span class="order-status status-' + escapeHtml(order.status) + '">' + escapeHtml(capitalizeStatus(order.status)) + '</span>' +
            '</div></div>' +
            '<div class="order-items-section mb-4">' +
            '<h5 class="mb-3"><i class="far fa-box mr-2"></i>' + escapeHtml(PAGE_DATA.i18n.order_items) + '</h5>' +
            '<div class="card"><div class="card-body"><div class="modal-order-items">' + itemsHtml + '</div></div></div></div>' +
            '<div class="row mb-4 align-items-start">' +
            '<div class="col-md-6">' +
            '<h5 class="mb-3"><i class="far fa-map-marker-alt mr-2"></i>' + escapeHtml(PAGE_DATA.i18n.delivery_address) + '</h5>' +
            '<div class="card h-100"><div class="card-body">' + addressBlock + '</div></div></div>' +
            '<div class="col-md-6">' +
            '<h5 class="mb-3"><i class="far fa-receipt mr-2"></i>' + escapeHtml(PAGE_DATA.i18n.order_summary) + '</h5>' +
            '<div class="card h-100"><div class="card-body">' +
            '<div class="order-summary-row"><span class="label">' + escapeHtml(PAGE_DATA.i18n.subtotal) + '</span><span class="value">' + formatOrderAmount(order.subtotal || order.total_amount) + '</span></div>' +
            '<div class="order-summary-row"><span class="label">' + escapeHtml(PAGE_DATA.i18n.delivery_fee) + '</span><span class="value">' + formatOrderAmount(order.delivery_fee || 0) + '</span></div>' +
            '<div class="order-summary-row"><span class="label">' + escapeHtml(PAGE_DATA.i18n.tax) + '</span><span class="value">' + formatOrderAmount(order.tax_amount || 0) + '</span></div>' +
            '<div class="order-summary-total"><span class="label">' + escapeHtml(PAGE_DATA.i18n.total) + '</span><span class="value">' + formatOrderAmount(order.total_amount) + '</span></div>' +
            '</div></div></div></div>' +
            trackingBlock +
            '</div>';

        if (order.delivery_address && order.delivery_address.latitude && order.delivery_address.longitude) {
            setTimeout(function () {
                initOrderDeliveryMap(
                    order.delivery_address.latitude,
                    order.delivery_address.longitude,
                    order.delivery_address.title || order.delivery_address.label ||
                        order.delivery_address.street_address || PAGE_DATA.i18n.delivery_location
                );
            }, 300);
        }
    }

    async function cancelOrder(orderId) {
        if (!confirm(PAGE_DATA.i18n.cancel_confirm)) return;
        try {
            var response = await apiRequest('/orders/' + orderId + '/cancel', { method: 'POST' });
            if (response.ok) {
                showNotification(PAGE_DATA.i18n.cancel_success, 'success');
                $('#orderDetailModal').modal('hide');
                setTimeout(function () { window.location.reload(); }, 1000);
            } else {
                var result = await response.json();
                showNotification(result.message || PAGE_DATA.i18n.cancel_failed, 'error');
            }
        } catch (error) {
            showNotification(PAGE_DATA.i18n.cancel_failed, 'error');
        }
    }

    async function reorderItems(orderId) {
        try {
            showNotification(PAGE_DATA.i18n.adding_to_cart, 'info');
            var response = await apiRequest('/orders/' + orderId + '/reorder', { method: 'POST' });
            if (response.ok) {
                showNotification(PAGE_DATA.i18n.items_added, 'success');
                setTimeout(function () { window.location.href = PAGE_DATA.cart_url; }, 1000);
            } else {
                var result = await response.json();
                showNotification(result.message || PAGE_DATA.i18n.add_failed, 'error');
            }
        } catch (error) {
            showNotification(PAGE_DATA.i18n.add_failed, 'error');
        }
    }

    function trackOrderPackage(trackingNumber) {
        window.open('https://track.example.com/' + encodeURIComponent(trackingNumber), '_blank');
    }

    $('#orderDetailModal').on('hidden.bs.modal', function () {
        if (orderDeliveryMap) {
            orderDeliveryMap.remove();
            orderDeliveryMap = null;
        }
    });

    document.body.addEventListener('click', function (e) {
        var target = e.target.closest('[data-action="track-order-package"]');
        if (target) trackOrderPackage(target.dataset.tracking);
    });

    window.showOrderDetail = showOrderDetail;
    window.displayOrderDetail = displayOrderDetail;
    window.cancelOrder = cancelOrder;
    window.reorderItems = reorderItems;
    window.initOrderDeliveryMap = initOrderDeliveryMap;
})();
