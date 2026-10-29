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

    // F15: label the status the backend publishes. The wait for a new date is not an
    // order status, so PAGE_DATA carries its copy; any other value is the status itself.
    // Tolerant of the deploy window (spec §9): this script goes live at `git pull`, before
    // the template that publishes `status_labels` and the API that publishes `display_status`.
    function statusLabel(order) {
        return (PAGE_DATA.i18n.status_labels || {})[order.display_status] ||
            capitalizeFirst(order.display_status || order.status);
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

            return '<div class="order-card" data-action="show-order-detail" data-id="' + order.id + '">' +
                '<div class="order-header">' +
                '<div class="row align-items-center">' +
                '<div class="col-md-3"><strong>' + escapeHtml(PAGE_DATA.i18n.order) + ' #' + escapeHtml(order.order_number) + '</strong></div>' +
                '<div class="col-md-3">' +
                '<small class="text-muted"><i class="far fa-calendar-alt mr-1"></i>' + escapeHtml(PAGE_DATA.i18n.order_date) + ': ' + escapeHtml(formatDate(order.created_at)) + '</small>' +
                '</div>' +
                '<div class="col-md-3">' +
                '<span class="order-status status-' + escapeHtml(order.display_status) + '">' + escapeHtml(statusLabel(order)) + '</span>' +
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
                reorderBtn +
                '<div class="order-actions d-inline-block align-top" data-order-id="' + order.id + '"></div>' +
                '</div></div></div></div>';
        }).join('');

        ordersListElement.innerHTML = html;
        orders.forEach(drawActions);
    }

    // Pay, Skip/Cancel/Decline and Confirm: the backend's `customer_actions`, drawn by the one script every order
    // surface shares (order-actions.js). Nothing here decides what an order may do.
    function drawActions(order) {
        var container = document.querySelector('.order-actions[data-order-id="' + order.id + '"]');
        if (!container) return;
        window.OrderActions.render(container, order, {
            words: PAGE_DATA.order_actions,
            subscriptionsUrl: PAGE_DATA.subscriptions_url,
            small: true,
            onChanged: function () { return loadOrders(currentPage); }
        });
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

    // An order's details open in the shared order modal (partials/order_detail_modal.html), the one detail
    // view /my-orders, /my-account and /my-subscriptions share (spec 2026-10-26 §12).
    function showOrderDetail(orderId) {
        if (typeof window.showOrderDetail === 'function') window.showOrderDetail(orderId);
    }

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

        // The order modal changed an order (a skip, a cancel, a Pay): the card behind it is out of date too.
        document.addEventListener('orders:changed', function () { loadOrders(currentPage); });

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
                case 'page':
                    e.preventDefault();
                    loadOrders(parseInt(target.dataset.page, 10));
                    break;
            }
        });
    });
})();
