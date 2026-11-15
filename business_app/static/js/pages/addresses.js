/**
 * My addresses (/addresses): lists the customer's saved addresses (GET /auth/addresses) and runs the card actions.
 * Adding and editing open the address wizard (static/js/address-wizard.js); the list reloads on its
 * 'ae:address-saved' event. "Make main" uses PATCH /auth/addresses/<id>/set-default, delete asks in a modal first.
 */
(function () {
    'use strict';

    var PAGE_DATA = getPageData();
    var T = PAGE_DATA.i18n || {};
    var addresses = [];
    var pendingDeleteId = null;

    function el(id) { return document.getElementById(id); }
    function esc(value) { return window.AE.escapeHtml(value); }

    async function readJson(response) {
        try { return await response.json(); } catch (error) { return {}; }
    }

    function wizardReady() {
        var ready = window.AddressWizard && window.AddressWizard.ready;
        return ready ? ready.catch(function () { return null; }) : Promise.resolve(null);
    }

    function districtName(key) {
        return window.AddressWizard ? window.AddressWizard.districtName(key) : '';
    }

    async function loadAddresses() {
        var list = el('addressesList');
        list.setAttribute('aria-busy', 'true');
        var ok = false;
        try {
            var response = await apiRequest('/auth/addresses');
            var result = await readJson(response);
            if (response.ok && result.success) {
                addresses = (result.data && result.data.addresses) || [];
                ok = true;
            }
        } catch (error) {
            ok = false;
        }
        await wizardReady();
        list.setAttribute('aria-busy', 'false');
        if (ok) render();
        else showError();
    }

    function showError() {
        el('addressesList').innerHTML = '';
        el('addressesHead').hidden = true;
        el('noAddressesMessage').hidden = true;
        el('addressesError').hidden = false;
    }

    function render() {
        var list = el('addressesList');
        var empty = addresses.length === 0;
        el('addressesError').hidden = true;
        el('noAddressesMessage').hidden = !empty;
        el('addressesHead').hidden = empty;
        var sorted = addresses.slice().sort(function (a, b) {
            return (b.is_default ? 1 : 0) - (a.is_default ? 1 : 0) || a.id - b.id;
        });
        list.innerHTML = sorted.map(cardHtml).join('');
    }

    function fact(label, value) {
        if (!value) return '';
        return '<li><span class="ae-addr-card__fact-label">' + esc(label) + '</span> ' + esc(value) + '</li>';
    }

    function cardHtml(address) {
        var id = Number(address.id);
        var text = address.full_address || address.street_address || '';
        // A district saved by key shows its localized name; free text from older rows shows as it was typed.
        var district = address.district ? (districtName(address.district) || address.district) : '';
        var facts = [
            fact(T.district, district),
            address.full_address ? fact(T.street, address.street_address) : '',
            fact(T.floor, address.floor_number),
            fact(T.apartment, address.apartment_number)
        ].join('');
        var notes = '';
        if (address.landmark) {
            notes += '<p class="ae-addr-card__note"><i class="far fa-flag" aria-hidden="true"></i>' +
                '<span><span class="visually-hidden">' + esc(T.landmark) + ': </span>' + esc(address.landmark) + '</span></p>';
        }
        if (address.delivery_instructions) {
            notes += '<p class="ae-addr-card__note"><i class="far fa-comment-alt" aria-hidden="true"></i>' +
                '<span><span class="visually-hidden">' + esc(T.instructions) + ': </span>' + esc(address.delivery_instructions) + '</span></p>';
        }
        var badges = (address.is_default ? '<span class="ae-badge ae-badge--main">' + esc(T.default_badge) + '</span>' : '') +
            (address.is_business ? '<span class="ae-badge ae-badge--muted">' + esc(T.business_badge) + '</span>' : '');
        var actions = '<button type="button" class="ae-btn ae-btn--secondary ae-btn--sm" data-action="edit-address" data-id="' + id + '">' +
            '<i class="far fa-pen" aria-hidden="true"></i><span>' + esc(T.edit) + '</span></button>';
        if (!address.is_default) {
            actions += '<button type="button" class="ae-btn ae-btn--ghost ae-btn--sm" data-action="set-default" data-id="' + id + '">' +
                '<i class="far fa-check-circle" aria-hidden="true"></i><span>' + esc(T.make_default) + '</span></button>' +
                '<button type="button" class="ae-btn ae-btn--ghost ae-btn--sm ae-addr-card__delete" data-action="delete-address" data-id="' + id + '">' +
                '<i class="far fa-trash-alt" aria-hidden="true"></i><span>' + esc(T.delete) + '</span></button>';
        }
        return '<article class="ae-addr-card' + (address.is_default ? ' is-default' : '') + '" role="listitem" data-address-id="' + id + '">' +
            '<span class="ae-icon-tile ae-icon-tile--sm ae-addr-card__icon" aria-hidden="true"><i class="far ' +
            (address.is_business ? 'fa-building' : 'fa-map-marker-alt') + '"></i></span>' +
            '<div class="ae-addr-card__body">' +
            '<div class="ae-addr-card__head"><h2 class="ae-addr-card__title">' + esc(address.title) + '</h2>' + badges + '</div>' +
            (text ? '<p class="ae-addr-card__address">' + esc(text) + '</p>' : '') +
            (facts ? '<ul class="ae-addr-card__facts">' + facts + '</ul>' : '') +
            notes +
            '<div class="ae-addr-card__actions">' + actions + '</div>' +
            '</div></article>';
    }

    function find(id) {
        return addresses.find(function (a) { return a.id === id; });
    }

    async function setDefault(id, button) {
        if (button) button.disabled = true;
        try {
            var response = await apiRequest('/auth/addresses/' + id + '/set-default', { method: 'PATCH' });
            var result = await readJson(response);
            if (response.ok && result.success !== false) {
                showNotification(T.default_updated, 'success');
                await loadAddresses();
            } else {
                showNotification(result.message || T.default_failed, 'error');
            }
        } catch (error) {
            showNotification(T.network_error, 'error');
        } finally {
            if (button && button.isConnected) button.disabled = false;
        }
    }

    function askDelete(id) {
        var address = find(id);
        if (!address) return;
        pendingDeleteId = id;
        el('addressDeleteText').textContent = String(T.delete_text || '').replace('{title}', address.title || '');
        $('#addressDeleteModal').modal('show');
    }

    async function confirmDelete() {
        var id = pendingDeleteId;
        if (!id) return;
        var button = el('addressDeleteConfirm');
        button.disabled = true;
        button.setAttribute('aria-busy', 'true');
        try {
            var response = await apiRequest('/auth/addresses/' + id, { method: 'DELETE' });
            var result = await readJson(response);
            $('#addressDeleteModal').modal('hide');
            if (response.ok && result.success !== false) {
                showNotification(T.deleted, 'success');
                await loadAddresses();
            } else {
                showNotification(result.message || T.delete_failed, 'error');
            }
        } catch (error) {
            showNotification(T.network_error, 'error');
        } finally {
            button.disabled = false;
            button.removeAttribute('aria-busy');
            pendingDeleteId = null;
        }
    }

    function openWizard(address) {
        if (!window.AddressWizard) return;
        if (address) window.AddressWizard.open({ address: address });
        else window.AddressWizard.open({ makeDefault: addresses.length === 0 });
    }

    function init() {
        // Bootstrap modals must sit directly in <body>, outside the cabinet's layout boxes.
        var confirmModal = el('addressDeleteModal');
        if (confirmModal && confirmModal.parentNode !== document.body) document.body.appendChild(confirmModal);
        el('addressDeleteConfirm').addEventListener('click', confirmDelete);
        document.addEventListener('ae:address-saved', loadAddresses);

        el('addressesPage').addEventListener('click', function (event) {
            var target = event.target.closest('[data-action]');
            if (!target) return;
            var id = parseInt(target.dataset.id, 10);
            switch (target.dataset.action) {
                case 'show-add-address': openWizard(null); break;
                case 'edit-address': openWizard(find(id)); break;
                case 'set-default': setDefault(id, target); break;
                case 'delete-address': askDelete(id); break;
                case 'reload-addresses': loadAddresses(); break;
            }
        });
        loadAddresses();
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
})();
