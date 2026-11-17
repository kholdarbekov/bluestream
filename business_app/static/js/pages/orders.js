(function () {
    var PAGE_DATA = getPageData();
    var I18N = PAGE_DATA.i18n || {};
    var currentPage = 1;
    var requestedPage = 1; // the page the last read asked for: Try again asks for it again
    var currentFilters = {};
    var PER_PAGE = 10;
    var DAY_MS = 24 * 60 * 60 * 1000;

    // base.js's one copy (window.AE, loaded before every page script).
    var escapeHtml = window.AE.escapeHtml;

    // `{name}` placeholders, filled by base.js (AE.fill, the one fill every page script shares).
    var fill = window.AE.fill;

    // Money and dates through base.js (AE.formatMoney mirrors the server's money label, AE.formatDate prints the
    // Tashkent day in the page's language): this script keeps no format of its own.
    function money(amount) {
        return window.AE ? window.AE.formatMoney(amount) : String(amount);
    }

    function day(iso) {
        return window.AE ? window.AE.formatDate(iso) : String(iso || '').slice(0, 10);
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
        // `status-<display_status>`: the hook the order modal and the tests key on.
        return pill.replace('class="ae-status', 'class="order-status status-' + escapeHtml(order.display_status) + ' ae-status');
    }

    function thumb(item) {
        var placeholder = PAGE_DATA.image_placeholder || '/static/images/product-placeholder.svg';
        var src = item.product_image || placeholder;
        return '<img class="ae-thumb" src="' + escapeHtml(src) + '" alt="" width="48" height="48" loading="lazy" data-fallback="' +
            escapeHtml(placeholder) + '">';
    }

    function itemLine(item) {
        var name = item.product_display_name || item.product_name || '';
        // A courtesy line prints the split the API published (AE.linePriceText), never quantity x unit price beside
        // the net total.
        var detail = item.is_reward
            ? '<span class="ae-badge ae-badge--gold">' + escapeHtml(I18N.reward) + '</span>'
            : item.courtesy_quantity
                ? escapeHtml(window.AE.linePriceText(item, { courtesy_part: I18N.courtesy_part }))
                : escapeHtml(fill(I18N.qty_price, { quantity: item.quantity, price: money(item.unit_price) }));
        return '<li class="ae-line">' + thumb(item) +
            '<span class="ae-line__text"><span class="ae-line__name">' + escapeHtml(name) + '</span>' +
            '<span class="ae-line__meta">' + detail + '</span></span>' +
            '<span class="ae-line__sum">' + escapeHtml(money(item.total_price)) + '</span></li>';
    }

    // The address as the customer names it, published by GET /orders (`delivery_address_label`: its title, else its
    // street line, the rule the subscription page uses). The title is the deploy-window fallback only.
    function addressLine(order) {
        var label = order.delivery_address_label || (order.delivery_address && order.delivery_address.title);
        if (!label) return '';
        return '<p class="ae-order__addr"><i class="far fa-map-marker-alt" aria-hidden="true"></i><span>' + escapeHtml(label) + '</span></p>';
    }

    function displayOrders(orders) {
        var ordersListElement = document.getElementById('ordersList');

        if (orders.length === 0) {
            ordersListElement.innerHTML = '';
            return;
        }

        var html = orders.map(function (order) {
            var items = order.order_items || [];
            var itemsHtml = items.slice(0, 2).map(itemLine).join('');
            var moreItems = items.length > 2
                ? '<li class="ae-order__more">' + escapeHtml(fill(I18N.more_items, { count: items.length - 2 })) + '</li>'
                : '';

            // Reorder is the backend's `customer_actions.reorder` (the one reorder rule), with its label.
            var reorder = (order.customer_actions && order.customer_actions.reorder) || {};
            var reorderBtn = reorder.allowed
                ? '<button type="button" class="ae-btn ae-btn--secondary ae-btn--sm" data-action="reorder" data-id="' + order.id + '">' +
                  '<i class="far fa-redo" aria-hidden="true"></i>' + escapeHtml(reorder.label) + '</button>'
                : '';

            return '<article class="order-card ae-order" data-action="show-order-detail" data-id="' + order.id + '" ' +
                'aria-labelledby="order-' + order.id + '-title">' +
                '<header class="ae-order__head">' +
                '<div class="ae-order__id">' +
                '<h2 class="ae-order__num" id="order-' + order.id + '-title">' + escapeHtml(fill(I18N.order_number, { number: order.order_number })) + '</h2>' +
                '<time class="ae-order__date" datetime="' + escapeHtml(order.created_at) + '">' + escapeHtml(day(order.created_at)) + '</time>' +
                '</div>' +
                '<div class="ae-order__state">' + statusPill(order) +
                '<span class="ae-order__total">' + escapeHtml(money(order.total_amount)) + '</span></div>' +
                '</header>' +
                '<ul class="ae-order__items">' + itemsHtml + moreItems + '</ul>' +
                '<footer class="ae-order__foot">' +
                addressLine(order) +
                '<div class="order-actions ae-order__actions" data-order-id="' + order.id + '"></div>' +
                '<div class="ae-order__buttons">' + reorderBtn +
                '<button type="button" class="ae-btn ae-btn--secondary ae-btn--sm" data-action="show-order-detail" data-id="' + order.id + '" data-stop-propagation="1">' +
                escapeHtml(I18N.view_details) + '</button></div>' +
                '</footer></article>';
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

    // A pager link is the true address of that page (filters included), so opening it in a new tab shows that page.
    function pageLink(page, label, ariaLabel) {
        return '<li><a href="' + escapeHtml(stateUrl(page)) + '" data-action="page" data-page="' + page + '"' +
            (ariaLabel ? ' aria-label="' + escapeHtml(ariaLabel) + '"' : '') + '>' + label + '</a></li>';
    }

    // GET /orders publishes {page, pages, per_page, total, has_next, has_prev}.
    function updatePagination(pagination) {
        var paginationSection = document.getElementById('paginationSection');
        var paginationElement = document.getElementById('ordersPagination');
        var info = document.getElementById('ordersPageInfo');
        if (!paginationSection || !paginationElement) return;

        var page = Number(pagination && pagination.page) || 1;
        var pages = Number(pagination && pagination.pages) || 0;
        var total = Number(pagination && pagination.total) || 0;
        currentPage = page;

        if (pages <= 1) {
            paginationSection.hidden = true;
            paginationElement.innerHTML = '';
            if (info) info.textContent = '';
            return;
        }

        var html = '';
        if (page > 1) html += pageLink(page - 1, '<i class="far fa-chevron-left" aria-hidden="true"></i>', I18N.prev);

        // Every page when there are few; otherwise the first, the last and the two around the current one.
        var shown = {};
        for (var i = 1; i <= pages; i++) {
            if (pages <= 7 || i === 1 || i === pages || Math.abs(i - page) <= 1) shown[i] = true;
        }
        var last = 0;
        for (var p = 1; p <= pages; p++) {
            if (!shown[p]) continue;
            if (last && p - last > 1) html += '<li><span class="ae-pagination__gap" aria-hidden="true">…</span></li>';
            html += p === page
                ? '<li><span aria-current="page">' + p + '</span></li>'
                : pageLink(p, String(p), fill(I18N.page, { page: p }));
            last = p;
        }

        if (page < pages) html += pageLink(page + 1, '<i class="far fa-chevron-right" aria-hidden="true"></i>', I18N.next);

        paginationElement.innerHTML = html;
        if (info) {
            var from = (page - 1) * (Number(pagination.per_page) || PER_PAGE) + 1;
            var to = Math.min(total, from + (Number(pagination.per_page) || PER_PAGE) - 1);
            info.textContent = fill(I18N.showing, { from: from, to: to, total: total });
        }
        paginationSection.hidden = false;
    }

    function showEmpty(kind) {
        var empty = document.getElementById('emptyState');
        if (!empty) return;
        empty.hidden = !kind;
        empty.querySelectorAll('[data-empty]').forEach(function (block) {
            block.hidden = block.getAttribute('data-empty') !== kind;
        });
    }

    // A failed read: the cards and the pager of the page shown before would no longer match, so only the failed
    // state (with Try again) is left.
    function showFailed() {
        displayOrders([]);
        var section = document.getElementById('paginationSection');
        if (section) section.hidden = true;
        showEmpty('failed');
    }

    function setLoading(loading) {
        var loadingElement = document.getElementById('loadingOrders');
        if (loadingElement) loadingElement.hidden = !loading;
        var list = document.getElementById('ordersList');
        if (list) list.setAttribute('aria-busy', loading ? 'true' : 'false');
    }

    function hasFilters() {
        return Object.keys(currentFilters).length > 0;
    }

    async function loadOrders(page) {
        page = page || 1;
        requestedPage = page;
        setLoading(true);
        showEmpty(null);

        try {
            var queryParams = new URLSearchParams(Object.assign(
                { page: page, per_page: 10 },
                currentFilters
            ));

            var response = await apiRequest('/orders?' + queryParams);
            var result = await response.json();

            if (response.ok && result.success) {
                var pages = Number(result.data.pagination && result.data.pagination.pages) || 0;
                if (!result.data.orders.length && pages && page > pages) {
                    // An address past the last page (an old link, a list that shrank): show the last page.
                    remember(pages, true);
                    return await loadOrders(pages);
                }
                displayOrders(result.data.orders);
                updatePagination(result.data.pagination);

                if (result.data.orders.length === 0) {
                    showEmpty(hasFilters() ? 'filtered' : 'first');
                }
            } else {
                // The failed state in the list says so and offers Try again (a toast on top repeated it).
                showFailed();
            }
        } catch (error) {
            console.error('Failed to load orders:', error);
            showFailed();
        } finally {
            setLoading(false);
        }
    }

    // The period is the start of the window, sent as `start_date` (an instant, which GET /orders compares with the
    // order's creation time). The endpoint filters by nothing else the page could offer, so nothing else is offered.
    function periodStart(days) {
        var since = new Date(Date.now() - Number(days) * DAY_MS);
        return since.toISOString().replace(/\.\d{3}Z$/, '+00:00');
    }

    // The query GET /orders gets from what the two selects show.
    function filtersFromSelects() {
        var statusFilter = document.getElementById('statusFilter');
        var dateFilter = document.getElementById('dateFilter');
        var filters = {};
        if (statusFilter && statusFilter.value) filters.status = statusFilter.value;
        if (dateFilter && dateFilter.value) filters.start_date = periodStart(dateFilter.value);
        return filters;
    }

    // ------------------------------------------------------------------ the list's address
    // The page, the status and the period live in the address (?page=2&status=delivered&period=30; other parameters,
    // such as lang, are kept), so a pager link, a reload, Back and a link opened in a new tab show the same list.

    function setParam(params, name, value) {
        if (value) params.set(name, value);
        else params.delete(name);
    }

    function stateUrl(page) {
        var params = new URLSearchParams(window.location.search);
        var statusFilter = document.getElementById('statusFilter');
        var dateFilter = document.getElementById('dateFilter');
        setParam(params, 'status', statusFilter && statusFilter.value);
        setParam(params, 'period', dateFilter && dateFilter.value);
        setParam(params, 'page', page > 1 ? String(page) : '');
        var query = params.toString();
        return window.location.pathname + (query ? '?' + query : '');
    }

    function remember(page, replace) {
        var url = stateUrl(page);
        if (!window.history || !window.history.pushState || url === window.location.pathname + window.location.search) return;
        window.history[replace ? 'replaceState' : 'pushState']({ page: page }, '', url);
    }

    // The selects take the address's status and period (only a value they offer) and the page number is returned.
    function readAddress() {
        var params = new URLSearchParams(window.location.search);
        [['statusFilter', 'status'], ['dateFilter', 'period']].forEach(function (pair) {
            var select = document.getElementById(pair[0]);
            if (!select) return;
            var value = params.get(pair[1]) || '';
            var offered = Array.prototype.some.call(select.options, function (option) { return option.value === value; });
            select.value = offered ? value : '';
        });
        var page = parseInt(params.get('page'), 10);
        return page > 0 ? page : 1;
    }

    function showAddress() {
        var page = readAddress();
        currentFilters = filtersFromSelects();
        loadOrders(page);
    }

    function applyFilters() {
        currentFilters = filtersFromSelects();
        currentPage = 1;
        remember(1);
        loadOrders(currentPage);
    }

    function resetFilters() {
        ['statusFilter', 'dateFilter'].forEach(function (id) {
            var select = document.getElementById(id);
            if (select) select.value = '';
        });
        applyFilters();
    }

    function reorderItems(orderId) {
        return window.OrderActions.reorder(orderId, { cartUrl: PAGE_DATA.cart_url, words: PAGE_DATA.order_actions });
    }

    // An order's details open in the shared order modal (partials/order_detail_modal.html), the one detail
    // view /my-orders, /my-account and /my-subscriptions share (spec 2026-10-26 §12).
    function showOrderDetail(orderId) {
        if (typeof window.showOrderDetail === 'function') window.showOrderDetail(orderId);
    }

    document.addEventListener('DOMContentLoaded', function () {
        showAddress();
        window.addEventListener('popstate', showAddress);

        var statusFilter = document.getElementById('statusFilter');
        if (statusFilter) statusFilter.addEventListener('change', applyFilters);

        var dateFilter = document.getElementById('dateFilter');
        if (dateFilter) dateFilter.addEventListener('change', applyFilters);

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
                    // A tap on a card's text opens it too; a selection in progress is not a tap.
                    if (target.tagName === 'ARTICLE' && window.getSelection && String(window.getSelection())) return;
                    showOrderDetail(id);
                    break;
                case 'reorder':
                    e.stopPropagation();
                    reorderItems(id);
                    break;
                case 'page':
                    e.preventDefault();
                    remember(parseInt(target.dataset.page, 10));
                    loadOrders(parseInt(target.dataset.page, 10));
                    var list = document.getElementById('ordersList');
                    if (list && list.scrollIntoView) list.scrollIntoView({ block: 'start' });
                    break;
                case 'reset-filters':
                    resetFilters();
                    break;
                case 'retry-orders':
                    loadOrders(requestedPage);
                    break;
            }
        });
    });
})();
