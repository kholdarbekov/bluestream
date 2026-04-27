(function () {
    var PAGE_DATA = getPageData();
    var addresses = [];

    function escapeHtml(str) {
        if (str === null || str === undefined) return '';
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    async function loadAddresses() {
        try {
            var response = await apiRequest('/auth/addresses');
            var result = await response.json();

            if (response.ok && result.success) {
                addresses = result.data.addresses;
            }
        } catch (error) {
            console.error('Error loading addresses:', error);
        }
        renderAddresses();
    }

    function renderAddresses() {
        var addressesList = document.getElementById('addressesList');

        if (addresses.length === 0) {
            addressesList.innerHTML =
                '<div class="text-center py-5" id="noAddressesMessage">' +
                '<i class="far fa-map-marker-alt" style="font-size: 4rem; color: #ccc;"></i>' +
                '<h4 class="mt-3">' + escapeHtml(PAGE_DATA.i18n.no_addresses) + '</h4>' +
                '<p class="text-muted">' + escapeHtml(PAGE_DATA.i18n.add_first_hint) + '</p>' +
                '<button class="btn btn-primary" data-action="show-add-address">' +
                '<i class="far fa-plus"></i> ' + escapeHtml(PAGE_DATA.i18n.add_first_button) +
                '</button></div>';
            return;
        }

        var html = addresses.map(function (address) {
            var addressLine = address.full_address || address.street_address || '';
            var cityLine = address.city + (address.district ? ', ' + address.district : '');
            var apartment = address.apartment_number
                ? '<p><strong>' + escapeHtml(PAGE_DATA.i18n.apartment) + ':</strong> <span>' + escapeHtml(address.apartment_number) + '</span></p>'
                : '';
            var landmark = address.landmark
                ? '<p><strong>' + escapeHtml(PAGE_DATA.i18n.landmark) + ':</strong> <span>' + escapeHtml(address.landmark) + '</span></p>'
                : '';
            var instructions = address.delivery_instructions
                ? '<p><strong>' + escapeHtml(PAGE_DATA.i18n.instructions) + ':</strong> <span>' + escapeHtml(address.delivery_instructions) + '</span></p>'
                : '';
            var defaultBadge = address.is_default
                ? '<span class="default-badge">' + escapeHtml(PAGE_DATA.i18n.default_badge) + '</span>' : '';
            var businessBadge = address.is_business
                ? '<span class="business-badge">' + escapeHtml(PAGE_DATA.i18n.business_badge) + '</span>' : '';
            var nonDefaultActions = !address.is_default
                ? '<button class="btn btn-outline-success btn-sm" data-action="set-default" data-id="' + address.id + '">' +
                  '<i class="far fa-check-circle"></i> ' + escapeHtml(PAGE_DATA.i18n.set_default) + '</button>' +
                  '<button class="btn btn-outline-danger btn-sm" data-action="delete-address" data-id="' + address.id + '">' +
                  '<i class="far fa-trash-alt"></i> ' + escapeHtml(PAGE_DATA.i18n.delete) + '</button>'
                : '';

            return '<div class="address-card ' + (address.is_default ? 'default' : '') + '">' +
                '<div class="address-card-content">' +
                '<div class="address-icon">' +
                '<i class="fas ' + (address.is_business ? 'fa-building' : 'fa-map-marker-alt') + '"></i>' +
                '</div>' +
                '<div class="address-info">' +
                '<div class="address-header">' +
                '<div class="address-title-wrapper">' +
                '<h5 class="address-title">' + escapeHtml(address.title) + '</h5>' +
                defaultBadge + businessBadge +
                '</div>' +
                '<div class="address-actions">' +
                '<button class="btn btn-outline-primary btn-sm" data-action="edit-address" data-id="' + address.id + '">' +
                '<i class="far fa-edit"></i> ' + escapeHtml(PAGE_DATA.i18n.edit) + '</button>' +
                nonDefaultActions +
                '</div></div>' +
                '<div class="address-details">' +
                '<p><strong>' + escapeHtml(PAGE_DATA.i18n.address_label) + ':</strong> <span>' + escapeHtml(addressLine) + '</span></p>' +
                '<p><strong>' + escapeHtml(PAGE_DATA.i18n.city) + ':</strong> <span>' + escapeHtml(cityLine) + '</span></p>' +
                apartment + landmark + instructions +
                '</div></div></div></div>';
        }).join('');

        addressesList.innerHTML = html;
    }

    function editAddress(addressId) {
        var address = addresses.find(function (a) { return a.id === addressId; });
        if (!address) return;

        if (typeof editAddressWizard === 'function') {
            editAddressWizard(address);
        } else {
            console.error('Address wizard not loaded');
        }
    }

    async function setDefaultAddress(addressId) {
        try {
            var response = await apiRequest('/auth/addresses/' + addressId, {
                method: 'PUT',
                body: JSON.stringify({ is_default: true })
            });

            var result = await response.json();

            if (response.ok) {
                showNotification(PAGE_DATA.i18n.default_updated, 'success');
                await loadAddresses();
            } else {
                showNotification(result.message || PAGE_DATA.i18n.set_default_failed, 'error');
            }
        } catch (error) {
            showNotification(PAGE_DATA.i18n.network_error, 'error');
        }
    }

    async function deleteAddress(addressId) {
        if (!confirm(PAGE_DATA.i18n.delete_confirm)) return;

        try {
            var response = await apiRequest('/auth/addresses/' + addressId, {
                method: 'DELETE'
            });

            var result = await response.json();

            if (response.ok) {
                showNotification(PAGE_DATA.i18n.delete_success, 'success');
                await loadAddresses();
            } else {
                showNotification(result.message || PAGE_DATA.i18n.delete_failed, 'error');
            }
        } catch (error) {
            showNotification(PAGE_DATA.i18n.network_error, 'error');
        }
    }

    document.addEventListener('DOMContentLoaded', function () {
        loadAddresses();

        document.body.addEventListener('click', function (e) {
            var target = e.target.closest('[data-action]');
            if (!target) return;
            var action = target.dataset.action;
            var id = parseInt(target.dataset.id, 10);

            switch (action) {
                case 'show-add-address':
                    if (typeof window.showAddAddressModal === 'function') {
                        window.showAddAddressModal();
                    }
                    break;
                case 'edit-address':
                    editAddress(id);
                    break;
                case 'set-default':
                    setDefaultAddress(id);
                    break;
                case 'delete-address':
                    deleteAddress(id);
                    break;
            }
        });
    });
})();
