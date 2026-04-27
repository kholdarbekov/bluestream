(function () {
    var PAGE_DATA = getPageData();
    var currentPage = 1;
    var currentFilters = {};

    function escapeHtml(str) {
        if (str === null || str === undefined) return '';
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function formatDate(dateString) {
        var date = new Date(dateString);
        return date.toLocaleDateString('en-US', {
            year: 'numeric',
            month: 'short',
            day: 'numeric'
        });
    }

    function formatAmount(amount) {
        var intAmount = Math.floor(parseFloat(amount) || 0);
        var formatted = intAmount.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
        return formatted + ' UZS';
    }

    function capitalizeFirst(str) {
        if (!str) return '';
        return str.charAt(0).toUpperCase() + str.slice(1).toLowerCase();
    }

    function debounce(func, wait) {
        var timeout;
        return function () {
            var args = arguments;
            clearTimeout(timeout);
            timeout = setTimeout(function () { func.apply(null, args); }, wait);
        };
    }

    function displayOrders(orders) {
        var ordersListElement = document.getElementById('ordersList');

        if (orders.length === 0) {
            ordersListElement.innerHTML = '';
            return;
        }

        var html = orders.map(function (order) {
            var itemsHtml = (order.order_items || []).slice(0, 2).map(function (item) {
                return '<div class="order-item">' +
                    '<img src="' + escapeHtml(item.product_image_url || '/static/images/products/default.jpg') + '" alt="' + escapeHtml(item.product_name) + '">' +
                    '<div class="item-details">' +
                    '<div class="item-name">' + escapeHtml(item.product_name) + '</div>' +
                    '<div class="item-specs">' + escapeHtml(PAGE_DATA.i18n.quantity) + ': ' + item.quantity + '</div>' +
                    '</div>' +
                    '<div class="item-price">' + formatAmount(item.unit_price) + '</div>' +
                    '</div>';
            }).join('');

            var moreItems = (order.order_items || []).length > 2
                ? '<div class="text-muted mt-2"><small>' + escapeHtml(PAGE_DATA.i18n.and) + ' ' + (order.order_items.length - 2) + ' ' + escapeHtml(PAGE_DATA.i18n.more_items) + '</small></div>'
                : '';

            var estDelivery = order.estimated_delivery
                ? '<small class="text-primary">' + escapeHtml(PAGE_DATA.i18n.est_delivery) + ': ' + escapeHtml(formatDate(order.estimated_delivery)) + '</small>'
                : '';

            var trackingBlock = order.tracking_number
                ? '<small class="text-muted">' + escapeHtml(PAGE_DATA.i18n.tracking) + ': ' + escapeHtml(order.tracking_number) + '</small>'
                : '';

            var reorderBtn = order.status === 'delivered'
                ? '<button class="btn btn-sm btn-primary ml-2" data-action="reorder" data-id="' + order.id + '">' +
                  escapeHtml(PAGE_DATA.i18n.reorder) + '</button>'
                : '';

            var cancelBtn = ['pending', 'confirmed'].includes(order.status)
                ? '<button class="btn btn-sm btn-outline-danger ml-2" data-action="cancel-order" data-id="' + order.id + '">' +
                  escapeHtml(PAGE_DATA.i18n.cancel) + '</button>'
                : '';

            return '<div class="order-card" data-action="show-order-detail" data-id="' + order.id + '">' +
                '<div class="order-header">' +
                '<div class="row align-items-center">' +
                '<div class="col-md-3"><strong>' + escapeHtml(PAGE_DATA.i18n.order) + ' #' + escapeHtml(order.order_number) + '</strong></div>' +
                '<div class="col-md-3">' +
                '<small class="text-muted"><i class="far fa-calendar-alt mr-1"></i>' + escapeHtml(PAGE_DATA.i18n.order_date) + ': ' + escapeHtml(formatDate(order.created_at)) + '</small>' +
                '</div>' +
                '<div class="col-md-3">' +
                '<span class="order-status status-' + escapeHtml(order.status) + '">' + escapeHtml(capitalizeFirst(order.status)) + '</span>' +
                '</div>' +
                '<div class="col-md-3 text-right"><strong>' + formatAmount(order.total_amount) + '</strong></div>' +
                '</div></div>' +
                '<div class="order-body">' +
                '<div class="row">' +
                '<div class="col-md-8">' + itemsHtml + moreItems + '</div>' +
                '<div class="col-md-4">' +
                '<div class="delivery-info">' +
                '<h6>' + escapeHtml(PAGE_DATA.i18n.delivery_address) + '</h6>' +
                '<p class="text-muted mb-1">' + escapeHtml((order.delivery_address && order.delivery_address.street) || '') + '</p>' +
                '<p class="text-muted">' + escapeHtml((order.delivery_address && order.delivery_address.city) || '') + '</p>' +
                estDelivery +
                '</div></div></div></div>' +
                '<div class="order-footer">' +
                '<div class="row align-items-center">' +
                '<div class="col-md-6">' + trackingBlock + '</div>' +
                '<div class="col-md-6 text-right">' +
                '<button class="btn btn-sm btn-outline-primary" data-action="show-order-detail" data-id="' + order.id + '" data-stop-propagation="1">' +
                escapeHtml(PAGE_DATA.i18n.view_details) + '</button>' +
                reorderBtn + cancelBtn +
                '</div></div></div></div>';
        }).join('');

        ordersListElement.innerHTML = html;
    }

    function updatePagination(pagination) {
        var paginationSection = document.getElementById('paginationSection');
        var paginationElement = document.getElementById('ordersPagination');

        if (pagination.total_pages <= 1) {
            paginationSection.style.display = 'none';
            return;
        }

        paginationSection.style.display = 'block';

        var html = '';

        if (pagination.current_page > 1) {
            html += '<li class="page-item"><a class="page-link" href="#" data-action="page" data-page="' + (pagination.current_page - 1) + '">&laquo;</a></li>';
        }

        for (var i = 1; i <= pagination.total_pages; i++) {
            if (i === pagination.current_page) {
                html += '<li class="page-item active"><span class="page-link">' + i + '</span></li>';
            } else {
                html += '<li class="page-item"><a class="page-link" href="#" data-action="page" data-page="' + i + '">' + i + '</a></li>';
            }
        }

        if (pagination.current_page < pagination.total_pages) {
            html += '<li class="page-item"><a class="page-link" href="#" data-action="page" data-page="' + (pagination.current_page + 1) + '">&raquo;</a></li>';
        }

        paginationElement.innerHTML = html;
        currentPage = pagination.current_page;
    }

    async function loadOrders(page) {
        page = page || 1;
        var loadingElement = document.getElementById('loadingOrders');
        var emptyStateElement = document.getElementById('emptyState');

        loadingElement.style.display = 'block';
        emptyStateElement.style.display = 'none';

        try {
            var queryParams = new URLSearchParams(Object.assign(
                { page: page, per_page: 10 },
                currentFilters
            ));

            var response = await apiRequest('/orders?' + queryParams);
            var result = await response.json();

            if (response.ok && result.success) {
                displayOrders(result.data.orders);
                updatePagination(result.data.pagination);

                if (result.data.orders.length === 0) {
                    emptyStateElement.style.display = 'block';
                }
            } else {
                showNotification(result.message || PAGE_DATA.i18n.load_failed, 'error');
                emptyStateElement.style.display = 'block';
            }
        } catch (error) {
            console.error('Failed to load orders:', error);
            showNotification(PAGE_DATA.i18n.load_failed_retry, 'error');
            emptyStateElement.style.display = 'block';
        } finally {
            loadingElement.style.display = 'none';
        }
    }

    function applyFilters() {
        currentFilters = {
            status: document.getElementById('statusFilter').value,
            days: document.getElementById('dateFilter').value,
            search: document.getElementById('searchOrder').value
        };

        Object.keys(currentFilters).forEach(function (key) {
            if (!currentFilters[key]) delete currentFilters[key];
        });

        currentPage = 1;
        loadOrders(currentPage);
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
            return '<div class="modal-order-item">' +
                '<img class="item-image" src="' + escapeHtml(item.product_image_url || '/static/images/products/default.jpg') + '" alt="' + escapeHtml(item.product_name) + '">' +
                '<div class="item-info">' +
                '<div class="item-name">' + escapeHtml(item.product_name) + '</div>' +
                '<div class="item-sku">' + escapeHtml(PAGE_DATA.i18n.sku) + ': ' + escapeHtml(item.product_sku || 'N/A') + '</div>' +
                '</div>' +
                '<div class="item-qty">' +
                '<span class="qty-label">' + escapeHtml(PAGE_DATA.i18n.quantity) + '</span>' +
                '<span class="qty-value">' + item.quantity + '</span>' +
                '</div>' +
                '<div class="item-total">' +
                '<span class="total-label">' + escapeHtml(PAGE_DATA.i18n.price) + '</span>' +
                '<span class="total-value">' + formatAmount(item.total_price || item.unit_price * item.quantity) + '</span>' +
                '</div></div>';
        }).join('');

        var addressBlock;
        if (order.delivery_address) {
            var addrLabel = order.delivery_address.title
                ? '<div class="address-label"><i class="far fa-tag"></i>' + escapeHtml(order.delivery_address.title) + '</div>'
                : '';
            var district = order.delivery_address.district
                ? '<div class="address-line">' + escapeHtml(order.delivery_address.district) + '</div>'
                : '';
            var instructions = (order.delivery_address.delivery_instructions || order.delivery_address.delivery_notes)
                ? '<div class="text-muted mb-2" style="font-size: 13px;"><i class="far fa-sticky-note mr-1"></i>' +
                  escapeHtml(order.delivery_address.delivery_instructions || order.delivery_address.delivery_notes) + '</div>'
                : '';
            var mapDiv = (order.delivery_address.latitude && order.delivery_address.longitude)
                ? '<div id="orderDeliveryMap"></div>' : '';
            addressBlock = '<div class="address-card-content">' + addrLabel +
                '<div class="address-text">' +
                '<div class="address-line">' + escapeHtml(order.delivery_address.street_address || order.delivery_address.full_address || '') + '</div>' +
                district + '</div>' + instructions + mapDiv + '</div>';
        } else {
            addressBlock = '<div class="address-card-content"><p class="no-address">' + escapeHtml(PAGE_DATA.i18n.no_address) + '</p></div>';
        }

        var trackingSection = order.tracking_number
            ? buildTrackingSection(order) : '';

        modalContent.innerHTML =
            '<div class="order-detail-content">' +
            '<div class="row mb-4 align-items-center">' +
            '<div class="col-md-8">' +
            '<h4 class="mb-2">' + escapeHtml(PAGE_DATA.i18n.order) + ' #' + escapeHtml(order.order_number) + '</h4>' +
            '<p class="text-muted mb-0"><i class="far fa-calendar-alt mr-1"></i>' + escapeHtml(PAGE_DATA.i18n.placed_on) + ' ' + escapeHtml(formatDate(order.created_at)) + '</p>' +
            '</div>' +
            '<div class="col-md-4 text-right">' +
            '<span class="order-status status-large status-' + escapeHtml(order.status) + '">' + escapeHtml(capitalizeFirst(order.status)) + '</span>' +
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
            '<div class="d-flex justify-content-between mb-2"><span>' + escapeHtml(PAGE_DATA.i18n.subtotal) + '</span><span class="font-weight-medium">' + formatAmount(order.subtotal || order.total_amount) + '</span></div>' +
            '<div class="d-flex justify-content-between mb-2"><span>' + escapeHtml(PAGE_DATA.i18n.delivery_fee) + '</span><span class="font-weight-medium">' + formatAmount(order.delivery_fee || 0) + '</span></div>' +
            '<div class="d-flex justify-content-between mb-2"><span>' + escapeHtml(PAGE_DATA.i18n.tax) + '</span><span class="font-weight-medium">' + formatAmount(order.tax_amount || 0) + '</span></div>' +
            '<hr>' +
            '<div class="d-flex justify-content-between h5 mb-0"><span class="font-weight-bold">' + escapeHtml(PAGE_DATA.i18n.total) + '</span><span class="font-weight-bold text-primary">' + formatAmount(order.total_amount) + '</span></div>' +
            '</div></div></div></div>' +
            trackingSection +
            '</div>';

        if (order.delivery_address && order.delivery_address.latitude && order.delivery_address.longitude) {
            setTimeout(function () {
                if (typeof initOrderDeliveryMap === 'function') {
                    initOrderDeliveryMap(
                        order.delivery_address.latitude,
                        order.delivery_address.longitude,
                        order.delivery_address.title || order.delivery_address.label || order.delivery_address.street_address || PAGE_DATA.i18n.delivery_location
                    );
                }
            }, 300);
        }
    }

    function buildTrackingSection(order) {
        var timeline = order.status_timeline || {};
        function stepClass(key) {
            if (timeline[key]) return 'completed';
            if (order.status === key) return 'active';
            return '';
        }
        return '<div class="tracking-section mb-4">' +
            '<h5>' + escapeHtml(PAGE_DATA.i18n.order_tracking) + '</h5>' +
            '<div class="tracking-info">' +
            '<div class="d-flex justify-content-between align-items-center mb-3">' +
            '<span><strong>' + escapeHtml(PAGE_DATA.i18n.tracking_number) + ':</strong> ' + escapeHtml(order.tracking_number) + '</span>' +
            '<button class="btn btn-sm btn-outline-primary" data-action="track-order" data-tracking="' + escapeHtml(order.tracking_number) + '">' +
            '<i class="far fa-external-link"></i> ' + escapeHtml(PAGE_DATA.i18n.track_package) + '</button>' +
            '</div>' +
            '<div class="tracking-steps">' +
            '<div class="tracking-step ' + (timeline.confirmed ? 'completed' : '') + '">' +
            '<div class="step-icon"><i class="far fa-check"></i></div>' +
            '<div class="step-title">' + escapeHtml(PAGE_DATA.i18n.confirmed) + '</div>' +
            '<div class="step-time">' + escapeHtml(timeline.confirmed || '') + '</div>' +
            '</div>' +
            '<div class="tracking-step ' + stepClass('processing') + '">' +
            '<div class="step-icon"><i class="far fa-cog"></i></div>' +
            '<div class="step-title">' + escapeHtml(PAGE_DATA.i18n.processing) + '</div>' +
            '<div class="step-time">' + escapeHtml(timeline.processing || '') + '</div>' +
            '</div>' +
            '<div class="tracking-step ' + stepClass('shipped') + '">' +
            '<div class="step-icon"><i class="far fa-truck"></i></div>' +
            '<div class="step-title">' + escapeHtml(PAGE_DATA.i18n.shipped) + '</div>' +
            '<div class="step-time">' + escapeHtml(timeline.shipped || '') + '</div>' +
            '</div>' +
            '<div class="tracking-step ' + stepClass('delivered') + '">' +
            '<div class="step-icon"><i class="far fa-home"></i></div>' +
            '<div class="step-title">' + escapeHtml(PAGE_DATA.i18n.delivered) + '</div>' +
            '<div class="step-time">' + escapeHtml(timeline.delivered || '') + '</div>' +
            '</div>' +
            '</div></div></div>';
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

    async function reorderItems(orderId) {
        try {
            var response = await apiRequest('/orders/' + orderId + '/reorder', { method: 'POST' });
            var result = await response.json();

            if (response.ok && result.success) {
                showNotification(PAGE_DATA.i18n.reorder_success, 'success');
                if (typeof updateCartCount === 'function') updateCartCount();
                $('#orderDetailModal').modal('hide');
            } else {
                showNotification(result.message || PAGE_DATA.i18n.reorder_failed, 'error');
            }
        } catch (error) {
            console.error('Failed to reorder items:', error);
            showNotification(PAGE_DATA.i18n.reorder_failed, 'error');
        }
    }

    async function cancelOrder(orderId) {
        if (!confirm(PAGE_DATA.i18n.cancel_confirm)) return;

        try {
            var response = await apiRequest('/orders/' + orderId + '/cancel', { method: 'POST' });
            var result = await response.json();

            if (response.ok && result.success) {
                showNotification(PAGE_DATA.i18n.cancel_success, 'success');
                $('#orderDetailModal').modal('hide');
                loadOrders(currentPage);
            } else {
                showNotification(result.message || PAGE_DATA.i18n.cancel_failed, 'error');
            }
        } catch (error) {
            console.error('Failed to cancel order:', error);
            showNotification(PAGE_DATA.i18n.cancel_failed, 'error');
        }
    }

    function trackOrder(trackingNumber) {
        window.open('https://tracking.carrier.com/track?number=' + encodeURIComponent(trackingNumber), '_blank');
    }

    window.showOrderDetail = showOrderDetail;

    document.addEventListener('DOMContentLoaded', function () {
        loadOrders();

        var statusFilter = document.getElementById('statusFilter');
        if (statusFilter) statusFilter.addEventListener('change', applyFilters);

        var dateFilter = document.getElementById('dateFilter');
        if (dateFilter) dateFilter.addEventListener('change', applyFilters);

        var searchOrder = document.getElementById('searchOrder');
        if (searchOrder) searchOrder.addEventListener('input', debounce(applyFilters, 500));

        var filterBtn = document.querySelector('[data-action="apply-filters"]');
        if (filterBtn) filterBtn.addEventListener('click', applyFilters);

        document.body.addEventListener('click', function (e) {
            var target = e.target.closest('[data-action]');
            if (!target) return;

            if (target.dataset.stopPropagation === '1') e.stopPropagation();

            var action = target.dataset.action;
            var id = parseInt(target.dataset.id, 10);

            switch (action) {
                case 'show-order-detail':
                    showOrderDetail(id);
                    break;
                case 'reorder':
                    e.stopPropagation();
                    reorderItems(id);
                    break;
                case 'cancel-order':
                    e.stopPropagation();
                    cancelOrder(id);
                    break;
                case 'track-order':
                    trackOrder(target.dataset.tracking);
                    break;
                case 'page':
                    e.preventDefault();
                    loadOrders(parseInt(target.dataset.page, 10));
                    break;
            }
        });
    });
})();
