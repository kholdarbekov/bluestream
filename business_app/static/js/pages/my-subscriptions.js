(function () {
    var PAGE_DATA = getPageData();
    var currentSubscription = null;

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
        var numAmount = parseFloat(amount) || 0;
        return new Intl.NumberFormat('en-US', {
            minimumFractionDigits: 0,
            maximumFractionDigits: 0
        }).format(numAmount) + ' UZS';
    }

    function formatDate(dateString) {
        var date = new Date(dateString);
        return date.toLocaleDateString('en-US', {
            year: 'numeric',
            month: 'short',
            day: 'numeric'
        });
    }

    async function loadSubscriptionOverview() {
        try {
            var response = await apiRequest('/subscriptions?status=active');
            var result = await response.json();

            if (response.ok && result.success) {
                var subscriptions = result.data.items || result.data.subscriptions || [];
                var activeCount = subscriptions.length;

                var now = new Date();
                var currentMonth = now.getMonth();
                var currentYear = now.getFullYear();

                var deliveriesThisMonth = subscriptions.filter(function (sub) {
                    if (!sub.next_delivery_date) return false;
                    var deliveryDate = new Date(sub.next_delivery_date);
                    return deliveryDate.getMonth() === currentMonth &&
                        deliveryDate.getFullYear() === currentYear;
                }).length;

                document.getElementById('activeSubscriptionsCount').textContent = activeCount;
                document.getElementById('nextDeliveryCount').textContent = deliveriesThisMonth;
                document.getElementById('monthlySavings').textContent = formatPrice(0);
            }
        } catch (error) {
            console.error('Failed to load subscription overview:', error);
            document.getElementById('activeSubscriptionsCount').textContent = '0';
            document.getElementById('nextDeliveryCount').textContent = '0';
            document.getElementById('monthlySavings').textContent = '0 UZS';
        }
    }

    async function buildProductsDisplay(subscription) {
        if (subscription.subscription_items && subscription.subscription_items.length > 0) {
            return subscription.subscription_items.map(function (item) {
                return '<li>' + item.quantity + 'x ' + escapeHtml(item.product_name || 'Product') +
                    ' (' + formatPrice(item.unit_price) + ' each)</li>';
            }).join('');
        }

        try {
            var response = await apiRequest('/subscriptions/' + subscription.id);
            var result = await response.json();
            if (response.ok && result.success && result.data.subscription_items) {
                return result.data.subscription_items.map(function (item) {
                    return '<li>' + item.quantity + 'x ' + escapeHtml(item.product_name || 'Product') +
                        ' (' + formatPrice(item.unit_price) + ' each)</li>';
                }).join('');
            }
            return '<li class="text-muted">' + escapeHtml(PAGE_DATA.i18n.no_products) + '</li>';
        } catch (error) {
            return '<li class="text-muted">' + escapeHtml(PAGE_DATA.i18n.products_failed) + '</li>';
        }
    }

    async function displaySubscriptions(subscriptions, container) {
        var html = await Promise.all(subscriptions.map(async function (subscription) {
            var productsDisplay = await buildProductsDisplay(subscription);

            var statusBtns = '';
            if (subscription.status === 'active') {
                statusBtns = '<button class="btn btn-sm btn-outline-warning ml-2" data-action="pause-subscription" data-id="' + subscription.id + '">' +
                    escapeHtml(PAGE_DATA.i18n.pause) + '</button>';
            } else if (subscription.status === 'paused') {
                statusBtns = '<button class="btn btn-sm btn-outline-success ml-2" data-action="resume-subscription" data-id="' + subscription.id + '">' +
                    escapeHtml(PAGE_DATA.i18n.resume) + '</button>';
            }

            return '<div class="subscription-card" data-action="manage-subscription" data-id="' + subscription.id + '">' +
                '<div class="subscription-header">' +
                '<div class="row align-items-center">' +
                '<div class="col-md-6">' +
                '<h5 class="mb-1">' + escapeHtml(subscription.name || 'Subscription') + '</h5>' +
                '<p class="text-muted mb-0">' + escapeHtml(subscription.description || '') + '</p>' +
                '</div>' +
                '<div class="col-md-3">' +
                '<span class="subscription-status status-' + escapeHtml(subscription.status) + '">' +
                escapeHtml(subscription.status.toUpperCase()) + '</span>' +
                '</div>' +
                '<div class="col-md-3 text-right">' +
                '<h5 class="mb-0">' + formatPrice(subscription.billing_amount) + '</h5>' +
                '<small class="text-muted">/' + escapeHtml(subscription.delivery_frequency) + '</small>' +
                '</div></div></div>' +
                '<div class="subscription-body">' +
                '<div class="row">' +
                '<div class="col-md-8">' +
                '<div class="mb-3">' +
                '<small class="text-muted">' + escapeHtml(PAGE_DATA.i18n.products) + ':</small>' +
                '<ul class="mb-0 mt-1" style="padding-left: 20px; list-style: none;">' +
                productsDisplay + '</ul></div>' +
                '<div class="subscription-details">' +
                '<div class="row">' +
                '<div class="col-6">' +
                '<small class="text-muted">' + escapeHtml(PAGE_DATA.i18n.next_delivery) + '</small>' +
                '<div><strong>' + (subscription.next_delivery_date ? escapeHtml(formatDate(subscription.next_delivery_date)) : 'N/A') + '</strong></div>' +
                '</div>' +
                '<div class="col-6">' +
                '<small class="text-muted">' + escapeHtml(PAGE_DATA.i18n.next_billing) + '</small>' +
                '<div><strong>' + (subscription.next_billing_date ? escapeHtml(formatDate(subscription.next_billing_date)) : 'N/A') + '</strong></div>' +
                '</div></div></div></div>' +
                '<div class="col-md-4">' +
                '<div class="subscription-meta">' +
                '<div class="mb-2">' +
                '<small class="text-muted">' + escapeHtml(PAGE_DATA.i18n.started) + '</small>' +
                '<div><strong>' + escapeHtml(formatDate(subscription.created_at)) + '</strong></div>' +
                '</div>' +
                '<div class="mb-2">' +
                '<small class="text-muted">' + escapeHtml(PAGE_DATA.i18n.payment_method) + '</small>' +
                '<div><strong>' + escapeHtml((subscription.payment_method || '').toUpperCase()) + '</strong></div>' +
                '</div>' +
                (subscription.auto_renew ? '<div><span class="badge badge-success">' + escapeHtml(PAGE_DATA.i18n.auto_renew) + '</span></div>' : '') +
                '</div></div></div></div>' +
                '<div class="subscription-footer">' +
                '<div class="d-flex justify-content-between align-items-center">' +
                '<div><small class="text-muted">' + escapeHtml(PAGE_DATA.i18n.subscription) + ' #' + subscription.id + '</small></div>' +
                '<div>' +
                '<button class="btn btn-sm btn-outline-primary" data-action="manage-subscription" data-id="' + subscription.id + '" data-stop-propagation="1">' +
                escapeHtml(PAGE_DATA.i18n.manage) + '</button>' +
                statusBtns +
                '</div></div></div></div>';
        }));

        container.innerHTML = html.join('');
    }

    function displayDeliveries(deliveries, container) {
        var html = deliveries.map(function (delivery) {
            var statusBadge = delivery.is_urgent ? PAGE_DATA.i18n.urgent : PAGE_DATA.i18n.scheduled;
            var addressLine = escapeHtml(delivery.delivery_address.street + ', ' + delivery.delivery_address.city);
            return '<div class="delivery-item ' + (delivery.is_urgent ? 'urgent' : 'upcoming') + '">' +
                '<div class="delivery-details">' +
                '<div>' +
                '<div class="delivery-date">' + escapeHtml(formatDate(delivery.scheduled_date)) + '</div>' +
                '<small class="text-muted">' + escapeHtml(delivery.subscription.plan.name) +
                ' - ' + delivery.quantity + ' bottles</small><br>' +
                '<small class="text-muted">' + addressLine + '</small>' +
                '</div>' +
                '<div>' +
                '<span class="delivery-status badge badge-' + (delivery.is_urgent ? 'danger' : 'primary') + '">' +
                escapeHtml(statusBadge) + '</span><br>' +
                '<button class="btn btn-sm btn-outline-primary mt-2" data-action="reschedule-delivery" data-id="' + delivery.id + '">' +
                escapeHtml(PAGE_DATA.i18n.reschedule) + '</button>' +
                '</div></div></div>';
        }).join('');

        container.innerHTML = html;
    }

    async function loadActiveSubscriptions() {
        var list = document.getElementById('activeSubscriptionsList');

        try {
            var response = await apiRequest('/subscriptions?status=active');
            var result = await response.json();

            if (response.ok && result.success) {
                var subscriptions = result.data.items || result.data.subscriptions || [];
                if (subscriptions.length === 0) {
                    list.innerHTML =
                        '<div class="text-center py-4">' +
                        '<i class="far fa-sync-alt fa-3x text-muted mb-3"></i>' +
                        '<h5>' + escapeHtml(PAGE_DATA.i18n.no_active) + '</h5>' +
                        '<p class="text-muted">' + escapeHtml(PAGE_DATA.i18n.no_active_hint) + '</p>' +
                        '<a href="' + PAGE_DATA.subscriptions_url + '" class="btn btn-primary">' +
                        '<i class="far fa-plus"></i> ' + escapeHtml(PAGE_DATA.i18n.browse_plans) + '</a>' +
                        '</div>';
                } else {
                    displaySubscriptions(subscriptions, list);
                }
            } else {
                list.innerHTML = '<div class="text-center py-4"><p class="text-muted">' + escapeHtml(PAGE_DATA.i18n.load_failed) + '</p></div>';
            }
        } catch (error) {
            console.error('Failed to load active subscriptions:', error);
            list.innerHTML = '<div class="text-center py-4"><p class="text-muted">' + escapeHtml(PAGE_DATA.i18n.load_failed) + '</p></div>';
        }
    }

    async function loadUpcomingDeliveries() {
        var list = document.getElementById('upcomingDeliveriesList');

        try {
            var response = await apiRequest('/subscriptions/upcoming-deliveries');
            var result = await response.json();

            if (response.ok && result.success) {
                if (result.data.deliveries.length === 0) {
                    list.innerHTML = '<div class="text-center py-4"><p class="text-muted">' + escapeHtml(PAGE_DATA.i18n.no_upcoming) + '</p></div>';
                } else {
                    displayDeliveries(result.data.deliveries, list);
                }
            } else {
                list.innerHTML = '<div class="text-center py-4"><p class="text-muted">' + escapeHtml(PAGE_DATA.i18n.load_deliveries_failed) + '</p></div>';
            }
        } catch (error) {
            console.error('Failed to load upcoming deliveries:', error);
            list.innerHTML = '<div class="text-center py-4"><p class="text-muted">' + escapeHtml(PAGE_DATA.i18n.load_deliveries_failed) + '</p></div>';
        }
    }

    async function loadInactiveSubscriptions() {
        var list = document.getElementById('inactiveSubscriptionsList');

        try {
            var response = await apiRequest('/subscriptions?status=inactive');
            var result = await response.json();

            if (response.ok && result.success) {
                var subscriptions = result.data.items || result.data.subscriptions || [];
                if (subscriptions.length === 0) {
                    list.innerHTML = '<div class="text-center py-4"><p class="text-muted">' + escapeHtml(PAGE_DATA.i18n.no_inactive) + '</p></div>';
                } else {
                    displaySubscriptions(subscriptions, list);
                }
            } else {
                list.innerHTML = '<div class="text-center py-4"><p class="text-muted">' + escapeHtml(PAGE_DATA.i18n.load_inactive_failed) + '</p></div>';
            }
        } catch (error) {
            console.error('Failed to load inactive subscriptions:', error);
            list.innerHTML = '<div class="text-center py-4"><p class="text-muted">' + escapeHtml(PAGE_DATA.i18n.load_inactive_failed) + '</p></div>';
        }
    }

    function showSubscriptionModal(data) {
        var subscription = data.subscription;
        var recentOrders = data.recent_orders || [];

        var modalContent = document.getElementById('subscriptionModalContent');
        var pauseBtn = document.getElementById('pauseSubscriptionBtn');
        var resumeBtn = document.getElementById('resumeSubscriptionBtn');
        var modifyBtn = document.getElementById('modifySubscriptionBtn');
        var cancelBtn = document.getElementById('cancelSubscriptionBtn');

        pauseBtn.style.display = subscription.status === 'active' ? 'inline-block' : 'none';
        resumeBtn.style.display = subscription.status === 'paused' ? 'inline-block' : 'none';
        modifyBtn.style.display = ['active', 'paused'].includes(subscription.status) ? 'inline-block' : 'none';
        cancelBtn.style.display = ['active', 'paused'].includes(subscription.status) ? 'inline-block' : 'none';

        pauseBtn.onclick = function () { pauseSubscription(subscription.id); };
        resumeBtn.onclick = function () { resumeSubscription(subscription.id); };
        modifyBtn.onclick = function () { showModifyModal(subscription); };
        cancelBtn.onclick = function () { cancelSubscription(subscription.id); };

        var productsHtml = '';
        if (subscription.subscription_items && subscription.subscription_items.length > 0) {
            productsHtml = subscription.subscription_items.map(function (item) {
                return '<li>' + item.quantity + 'x ' + escapeHtml(item.product_name || 'Product') +
                    ' - ' + formatPrice(item.unit_price) + ' each</li>';
            }).join('');
        } else {
            productsHtml = '<li class="text-muted">' + escapeHtml(PAGE_DATA.i18n.no_products) + '</li>';
        }

        var recentOrdersHtml = recentOrders.length > 0
            ? '<ul style="padding-left: 20px;">' +
              recentOrders.slice(0, 5).map(function (order) {
                  return '<li>' + escapeHtml(order.order_number) + ' - ' +
                      escapeHtml(order.status.toUpperCase()) + ' - ' + formatPrice(order.total_amount) + '</li>';
              }).join('') + '</ul>'
            : '<div class="text-muted">' + escapeHtml(PAGE_DATA.i18n.no_orders) + '</div>';

        modalContent.innerHTML =
            '<div class="subscription-details">' +
            '<div class="row mb-4">' +
            '<div class="col-md-6">' +
            '<h4>' + escapeHtml(subscription.name || 'Subscription') + '</h4>' +
            '<p class="text-muted">' + escapeHtml(subscription.description || '') + '</p>' +
            '<div class="subscription-meta">' +
            '<div class="meta-item"><strong>' + escapeHtml(PAGE_DATA.i18n.status) + ':</strong> ' +
            '<span class="subscription-status status-' + escapeHtml(subscription.status) + '">' +
            escapeHtml(subscription.status.toUpperCase()) + '</span></div>' +
            '<div class="meta-item"><strong>' + escapeHtml(PAGE_DATA.i18n.subscription_id) + ':</strong> #' + subscription.id + '</div>' +
            '<div class="meta-item"><strong>' + escapeHtml(PAGE_DATA.i18n.started) + ':</strong> ' + escapeHtml(formatDate(subscription.created_at)) + '</div>' +
            '</div></div>' +
            '<div class="col-md-6">' +
            '<div class="subscription-pricing">' +
            '<h3>' + formatPrice(subscription.billing_amount) + '<small>/' + escapeHtml(subscription.delivery_frequency) + '</small></h3>' +
            '<div class="pricing-details">' +
            '<div><strong>' + escapeHtml(PAGE_DATA.i18n.products) + ':</strong></div>' +
            '<ul style="padding-left: 20px; margin-top: 5px;">' + productsHtml + '</ul>' +
            '<div>' + escapeHtml(PAGE_DATA.i18n.delivery_frequency) + ': <strong>' + escapeHtml(subscription.delivery_frequency) + '</strong></div>' +
            '<div>' + escapeHtml(PAGE_DATA.i18n.next_billing) + ': <strong>' + (subscription.next_billing_date ? escapeHtml(formatDate(subscription.next_billing_date)) : 'N/A') + '</strong></div>' +
            '</div></div></div></div>' +
            '<div class="delivery-info mb-4">' +
            '<h5>' + escapeHtml(PAGE_DATA.i18n.delivery_information) + '</h5>' +
            '<div class="row">' +
            '<div class="col-md-6">' +
            '<div class="info-group"><strong>' + escapeHtml(PAGE_DATA.i18n.next_delivery) + ':</strong>' +
            '<div>' + (subscription.next_delivery_date ? escapeHtml(formatDate(subscription.next_delivery_date)) : 'N/A') + '</div></div>' +
            '<div class="info-group"><strong>' + escapeHtml(PAGE_DATA.i18n.time_slot) + ':</strong>' +
            '<div>' + escapeHtml(subscription.delivery_time_slot || 'N/A') + '</div></div>' +
            '<div class="info-group"><strong>' + escapeHtml(PAGE_DATA.i18n.payment_method) + ':</strong>' +
            '<div>' + escapeHtml(subscription.payment_method ? subscription.payment_method.toUpperCase() : 'N/A') + '</div></div>' +
            '</div>' +
            '<div class="col-md-6">' +
            '<div class="info-group"><strong>' + escapeHtml(PAGE_DATA.i18n.recent_orders) + ':</strong>' +
            recentOrdersHtml + '</div></div>' +
            '</div></div></div>';

        $('#subscriptionModal').modal('show');
    }

    async function manageSubscription(subscriptionId) {
        try {
            var response = await apiRequest('/subscriptions/' + subscriptionId);
            var result = await response.json();

            if (response.ok && result.success) {
                currentSubscription = result.data.subscription;
                showSubscriptionModal(result.data);
            } else {
                showNotification(result.message || PAGE_DATA.i18n.details_failed, 'error');
            }
        } catch (error) {
            console.error('Failed to load subscription details:', error);
            showNotification(PAGE_DATA.i18n.details_failed, 'error');
        }
    }

    function showModifyModal(subscription) {
        document.getElementById('subscriptionIdToModify').value = subscription.id;

        var tomorrow = new Date();
        tomorrow.setDate(tomorrow.getDate() + 1);
        var tomorrowStr = tomorrow.toISOString().split('T')[0];
        document.getElementById('effectiveDate').min = tomorrowStr;
        document.getElementById('effectiveDate').value = tomorrowStr;

        $('#subscriptionModal').modal('hide');
        $('#modifyModal').modal('show');
    }

    async function confirmModifySubscription() {
        var subscriptionId = document.getElementById('subscriptionIdToModify').value;
        var formData = {
            plan: document.getElementById('newPlan').value,
            quantity: parseInt(document.getElementById('newQuantity').value),
            frequency: document.getElementById('newFrequency').value,
            effective_date: document.getElementById('effectiveDate').value
        };

        try {
            var response = await apiRequest('/subscriptions/' + subscriptionId + '/modify', {
                method: 'PUT',
                body: JSON.stringify(formData)
            });

            var result = await response.json();

            if (response.ok && result.success) {
                showNotification(PAGE_DATA.i18n.update_success, 'success');
                $('#modifyModal').modal('hide');
                loadActiveSubscriptions();
                loadSubscriptionOverview();
            } else {
                showNotification(result.message || PAGE_DATA.i18n.update_failed, 'error');
            }
        } catch (error) {
            console.error('Failed to modify subscription:', error);
            showNotification(PAGE_DATA.i18n.update_failed, 'error');
        }
    }

    async function pauseSubscription(subscriptionId) {
        if (!confirm(PAGE_DATA.i18n.pause_confirm)) return;

        try {
            var response = await apiRequest('/subscriptions/' + subscriptionId + '/pause', {
                method: 'POST'
            });

            var result = await response.json();

            if (response.ok && result.success) {
                showNotification(PAGE_DATA.i18n.pause_success, 'success');
                $('#subscriptionModal').modal('hide');
                loadActiveSubscriptions();
                loadInactiveSubscriptions();
                loadSubscriptionOverview();
            } else {
                showNotification(result.message || PAGE_DATA.i18n.pause_failed, 'error');
            }
        } catch (error) {
            console.error('Failed to pause subscription:', error);
            showNotification(PAGE_DATA.i18n.pause_failed, 'error');
        }
    }

    async function resumeSubscription(subscriptionId) {
        try {
            var response = await apiRequest('/subscriptions/' + subscriptionId + '/resume', {
                method: 'POST'
            });

            var result = await response.json();

            if (response.ok && result.success) {
                showNotification(PAGE_DATA.i18n.resume_success, 'success');
                $('#subscriptionModal').modal('hide');
                loadActiveSubscriptions();
                loadInactiveSubscriptions();
                loadSubscriptionOverview();
            } else {
                showNotification(result.message || PAGE_DATA.i18n.resume_failed, 'error');
            }
        } catch (error) {
            console.error('Failed to resume subscription:', error);
            showNotification(PAGE_DATA.i18n.resume_failed, 'error');
        }
    }

    async function cancelSubscription(subscriptionId) {
        if (!confirm(PAGE_DATA.i18n.cancel_confirm)) return;

        var reason = prompt(PAGE_DATA.i18n.cancel_reason_prompt);

        try {
            var response = await apiRequest('/subscriptions/' + subscriptionId + '/cancel', {
                method: 'POST',
                body: JSON.stringify({ reason: reason || 'User requested cancellation' })
            });

            var result = await response.json();

            if (response.ok && result.success) {
                showNotification(PAGE_DATA.i18n.cancel_success, 'success');
                $('#subscriptionModal').modal('hide');
                loadActiveSubscriptions();
                loadInactiveSubscriptions();
                loadSubscriptionOverview();
            } else {
                showNotification(result.message || PAGE_DATA.i18n.cancel_failed, 'error');
            }
        } catch (error) {
            console.error('Failed to cancel subscription:', error);
            showNotification(PAGE_DATA.i18n.cancel_failed, 'error');
        }
    }

    function rescheduleDelivery(deliveryId) {
        showNotification(PAGE_DATA.i18n.reschedule_coming_soon, 'info');
    }

    document.addEventListener('DOMContentLoaded', function () {
        loadSubscriptionOverview();
        loadActiveSubscriptions();
        loadUpcomingDeliveries();
        loadInactiveSubscriptions();

        var confirmBtn = document.getElementById('confirmModifyBtn');
        if (confirmBtn) confirmBtn.addEventListener('click', confirmModifySubscription);

        document.body.addEventListener('click', function (e) {
            var target = e.target.closest('[data-action]');
            if (!target) return;

            var action = target.dataset.action;
            if (target.dataset.stopPropagation === '1') e.stopPropagation();
            var id = parseInt(target.dataset.id, 10);

            switch (action) {
                case 'manage-subscription':
                    manageSubscription(id);
                    break;
                case 'pause-subscription':
                    e.stopPropagation();
                    pauseSubscription(id);
                    break;
                case 'resume-subscription':
                    e.stopPropagation();
                    resumeSubscription(id);
                    break;
                case 'reschedule-delivery':
                    rescheduleDelivery(id);
                    break;
            }
        });
    });
})();
