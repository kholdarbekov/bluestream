/**
 * Address Management Modal
 * Handles create, edit, delete, and set default operations for user addresses
 */

// Modal state
let editingAddressId = null;

// Show add address modal
function showAddAddressModal() {
    editingAddressId = null;
    document.getElementById('modal-title').textContent = window.translations?.add_new_address || 'Add New Address';
    document.getElementById('address-form').reset();
    document.getElementById('address-modal').style.display = 'flex';
}

// Show edit address modal
function showEditAddressModal(addressId) {
    editingAddressId = addressId;
    document.getElementById('modal-title').textContent = window.translations?.edit_address || 'Edit Address';

    // Fetch address details
    apiRequest(`/addresses/${addressId}`, { method: 'GET' })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                const address = data.data.address;
                document.getElementById('address-label').value = address.label || '';
                document.getElementById('address-line1').value = address.address_line1;
                document.getElementById('address-line2').value = address.address_line2 || '';
                document.getElementById('address-city').value = address.city;
                document.getElementById('address-state').value = address.state || '';
                document.getElementById('address-postal-code').value = address.postal_code || '';
                document.getElementById('address-phone').value = address.phone_number || '';
                document.getElementById('delivery-instructions').value = address.delivery_instructions || '';
                document.getElementById('is-default').checked = address.is_default || false;

                document.getElementById('address-modal').style.display = 'flex';
            }
        })
        .catch(error => {
            console.error('Error loading address:', error);
            showNotification(window.translations?.error_loading || 'Error loading address', 'error');
        });
}

// Close modal
function closeAddressModal() {
    document.getElementById('address-modal').style.display = 'none';
    editingAddressId = null;
}

// Save address (create or update)
async function saveAddress(event) {
    event.preventDefault();

    const formData = {
        label: document.getElementById('address-label').value,
        address_line1: document.getElementById('address-line1').value,
        address_line2: document.getElementById('address-line2').value,
        city: document.getElementById('address-city').value,
        state: document.getElementById('address-state').value,
        postal_code: document.getElementById('address-postal-code').value,
        country: 'Uzbekistan',
        phone_number: document.getElementById('address-phone').value,
        delivery_instructions: document.getElementById('delivery-instructions').value,
        is_default: document.getElementById('is-default').checked
    };

    try {
        const method = editingAddressId ? 'PUT' : 'POST';
        const url = editingAddressId ? `/addresses/${editingAddressId}` : '/addresses/';

        const response = await apiRequest(url, {
            method: method,
            body: JSON.stringify(formData)
        });

        const data = await response.json();

        if (data.success) {
            showNotification(
                editingAddressId
                    ? (window.translations?.address_updated || 'Address updated successfully')
                    : (window.translations?.address_created || 'Address created successfully'),
                'success'
            );
            closeAddressModal();
            // Reload addresses
            loadUserAddresses();
        } else {
            throw new Error(data.message || 'Failed to save address');
        }
    } catch (error) {
        console.error('Error saving address:', error);
        showNotification(error.message || (window.translations?.error_saving || 'Error saving address'), 'error');
    }
}

// Delete address
async function deleteAddressConfirm(addressId) {
    if (!confirm(window.translations?.confirm_delete || 'Are you sure you want to delete this address?')) {
        return;
    }

    try {
        const response = await apiRequest(`/addresses/${addressId}`, {
            method: 'DELETE'
        });

        const data = await response.json();

        if (data.success) {
            showNotification(window.translations?.address_deleted || 'Address deleted successfully', 'success');
            // Reload addresses
            loadUserAddresses();
        } else {
            throw new Error(data.message || 'Failed to delete address');
        }
    } catch (error) {
        console.error('Error deleting address:', error);
        showNotification(error.message || (window.translations?.error_deleting || 'Error deleting address'), 'error');
    }
}

// Set default address
async function setDefaultAddress(addressId) {
    try {
        const response = await apiRequest(`/addresses/${addressId}/set-default`, {
            method: 'POST'
        });

        const data = await response.json();

        if (data.success) {
            showNotification(window.translations?.default_updated || 'Default address updated', 'success');
            // Reload addresses
            loadUserAddresses();
        } else {
            throw new Error(data.message || 'Failed to set default address');
        }
    } catch (error) {
        console.error('Error setting default address:', error);
        showNotification(error.message || (window.translations?.error_setting_default || 'Error setting default'), 'error');
    }
}

// Load user addresses
async function loadUserAddresses() {
    try {
        const response = await apiRequest('/addresses/', {
            method: 'GET'
        });

        const data = await response.json();

        if (data.success) {
            renderAddresses(data.data.addresses);
        }
    } catch (error) {
        console.error('Error loading addresses:', error);
    }
}

// Render addresses in grid
function renderAddresses(addresses) {
    const grid = document.getElementById('address-grid');

    // Keep only the "Add New" card
    const addNewCard = grid.querySelector('.add-address-card');
    grid.innerHTML = '';

    if (addresses && addresses.length > 0) {
        addresses.forEach((address, index) => {
            const card = createAddressCard(address, index === 0);
            grid.appendChild(card);
        });
    }

    // Re-add "Add New" card at the end
    if (addNewCard) {
        grid.appendChild(addNewCard);
    }

    // Auto-select first address if none selected
    if (addresses && addresses.length > 0 && !selectedAddress) {
        selectedAddress = addresses[0].id;
    }
}

// Create address card element
function createAddressCard(address, isFirst) {
    const card = document.createElement('div');
    card.className = `address-card ${isFirst && !selectedAddress ? 'selected' : ''}`;
    card.dataset.addressId = address.id;
    card.onclick = () => selectAddress(address.id);

    card.innerHTML = `
        <input type="radio" name="delivery_address" value="${address.id}" ${isFirst && !selectedAddress ? 'checked' : ''}>
        <div class="address-name">
            ${address.label || `Address ${address.id}`}
            ${address.is_default ? '<span style="color: #00d1f9; font-size: 12px; margin-left: 5px;">(Default)</span>' : ''}
        </div>
        <div class="address-details">
            ${address.address_line1}<br>
            ${address.address_line2 ? address.address_line2 + '<br>' : ''}
            ${address.city}${address.postal_code ? ', ' + address.postal_code : ''}
        </div>
        ${address.phone_number ? `
        <div class="address-phone">
            <i class="far fa-phone"></i> ${address.phone_number}
        </div>` : ''}
        <div class="address-actions" style="margin-top: 10px; display: flex; gap: 10px;">
            <button onclick="event.stopPropagation(); showEditAddressModal(${address.id})"
                    class="btn-sm btn-edit"
                    style="padding: 5px 10px; font-size: 12px; border: 1px solid #00d1f9; background: none; color: #00d1f9; border-radius: 3px; cursor: pointer;">
                <i class="far fa-edit"></i> Edit
            </button>
            ${!address.is_default ? `
            <button onclick="event.stopPropagation(); deleteAddressConfirm(${address.id})"
                    class="btn-sm btn-delete"
                    style="padding: 5px 10px; font-size: 12px; border: 1px solid #dc3545; background: none; color: #dc3545; border-radius: 3px; cursor: pointer;">
                <i class="far fa-trash"></i> Delete
            </button>` : ''}
        </div>
    `;

    return card;
}

// Close modal when clicking outside
window.onclick = function(event) {
    const modal = document.getElementById('address-modal');
    if (event.target === modal) {
        closeAddressModal();
    }
};
