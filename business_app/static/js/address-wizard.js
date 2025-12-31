/**
 * Address Wizard - Enhanced address creation with map-based location selection
 * Uses Leaflet + OpenStreetMap for free map functionality
 * Loads geographic configuration from backend API (single source of truth)
 */

// Wizard state
const AddressWizard = {
    map: null,
    marker: null,
    boundaryLayer: null,
    currentStep: 1,
    totalSteps: 3,
    editingAddressId: null,
    selectedLocation: null,

    // Geographic config (loaded from API)
    geoConfig: {
        center: { latitude: 41.2995, longitude: 69.2401 },  // Default fallback
        bounds: { min_lat: 41.15, max_lat: 41.45, min_lng: 69.05, max_lng: 69.45 },
        districts: []
    },
    configLoaded: false,

    // Initialize the wizard
    init: function() {
        this.bindEvents();
        this.loadGeoConfig();
    },

    // Bind event handlers
    bindEvents: function() {
        // Step navigation
        document.getElementById('wizardNextBtn')?.addEventListener('click', () => this.nextStep());
        document.getElementById('wizardPrevBtn')?.addEventListener('click', () => this.prevStep());
        document.getElementById('wizardSaveBtn')?.addEventListener('click', () => this.saveAddress());

        // Map controls
        document.getElementById('useMyLocationBtn')?.addEventListener('click', () => this.useMyLocation());
        document.getElementById('searchAddressBtn')?.addEventListener('click', () => this.searchAddress());

        // Address search on enter
        document.getElementById('addressSearchInput')?.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                this.searchAddress();
            }
        });

        // Quick title buttons
        document.querySelectorAll('.quick-title-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                const title = e.target.dataset.title || e.currentTarget.dataset.title;
                document.getElementById('addressTitle').value = title;
                document.querySelectorAll('.quick-title-btn').forEach(b => b.classList.remove('active'));
                e.currentTarget.classList.add('active');
            });
        });

        // Modal events - critical for proper Leaflet map rendering
        const modal = document.getElementById('addressWizardModal');
        if (modal) {
            $(modal).on('shown.bs.modal', () => {
                // Initialize map ONLY after modal is fully visible
                // This ensures container has proper dimensions (Leaflet needs non-zero dimensions)
                if (!this.map) {
                    this.initMap();
                }

                // Set marker if we have a pending location (from edit mode)
                if (this.pendingMarkerLocation && this.map) {
                    this.setMarkerPosition(this.pendingMarkerLocation.lat, this.pendingMarkerLocation.lng);
                    this.pendingMarkerLocation = null;
                }

                // Force invalidateSize after a small delay to ensure proper tile rendering
                setTimeout(() => {
                    if (this.map) {
                        this.map.invalidateSize();
                    }
                }, 100);
            });

            $(modal).on('hidden.bs.modal', () => {
                // Clean up MutationObserver
                if (this.tileObserver) {
                    this.tileObserver.disconnect();
                    this.tileObserver = null;
                }
                // Clean up map when modal closes
                if (this.map) {
                    this.map.remove();
                    this.map = null;
                    this.marker = null;
                    this.boundaryLayer = null;
                }
                this.pendingMarkerLocation = null;
            });
        }
    },

    // Load geographic configuration from API
    loadGeoConfig: async function() {
        try {
            const lang = document.documentElement.lang || 'en';
            const response = await apiRequest(`/addresses/geo-config?lang=${lang}`);
            const result = await response.json();

            if (response.ok && result.success) {
                this.geoConfig = result.data;
                this.configLoaded = true;
                this.populateDistrictDropdown();
            }
        } catch (error) {
            console.error('Failed to load geo config:', error);
            // Use fallback values
        }
    },

    // Populate district dropdown
    populateDistrictDropdown: function() {
        const select = document.getElementById('districtSelect');
        if (!select || !this.geoConfig.districts) return;

        select.innerHTML = '<option value="">-- ' + this.getTranslation('select_district') + ' --</option>';
        this.geoConfig.districts.forEach(district => {
            const option = document.createElement('option');
            option.value = district.key;
            option.textContent = district.name;
            select.appendChild(option);
        });
    },

    // Open wizard for new address
    openForNew: function() {
        this.editingAddressId = null;
        this.selectedLocation = null;
        this.pendingMarkerLocation = null;
        this.resetForm();
        this.goToStep(1);
        this.showModal();
        // Map will be initialized by the shown.bs.modal event handler
    },

    // Open wizard for editing existing address
    openForEdit: function(address) {
        this.editingAddressId = address.id;
        this.selectedLocation = (address.latitude && address.longitude) ? {
            lat: address.latitude,
            lng: address.longitude
        } : null;

        // Store location to set marker after map init
        this.pendingMarkerLocation = this.selectedLocation;

        // Pre-fill form fields
        this.fillForm(address);

        // Start at step 2 if we have coordinates, otherwise step 1
        if (this.selectedLocation) {
            this.goToStep(2);
        } else {
            this.goToStep(1);
        }

        this.showModal();
        // Map will be initialized by the shown.bs.modal event handler
    },

    // Initialize Leaflet map
    initMap: function() {
        const mapContainer = document.getElementById('addressWizardMap');
        if (!mapContainer) {
            console.error('AddressWizard: Map container not found');
            return;
        }

        // Debug: Log container dimensions
        const rect = mapContainer.getBoundingClientRect();
        console.log('AddressWizard: Map container dimensions:', {
            width: rect.width,
            height: rect.height,
            offsetWidth: mapContainer.offsetWidth,
            offsetHeight: mapContainer.offsetHeight
        });

        // Ensure container has dimensions before initializing map
        if (rect.width === 0 || rect.height === 0) {
            console.warn('AddressWizard: Map container has zero dimensions, retrying...');
            setTimeout(() => this.initMap(), 100);
            return;
        }

        // Destroy existing map if any
        if (this.map) {
            this.map.remove();
            this.map = null;
            this.marker = null;
            this.boundaryLayer = null;
        }

        // Fix Leaflet default icon path issue
        // This fixes the missing marker icon 404 errors
        delete L.Icon.Default.prototype._getIconUrl;
        L.Icon.Default.mergeOptions({
            iconRetinaUrl: 'https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon-2x.png',
            iconUrl: 'https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon.png',
            shadowUrl: 'https://unpkg.com/leaflet@1.9.4/dist/images/marker-shadow.png',
        });

        const center = this.geoConfig.center;
        const bounds = this.geoConfig.bounds;

        // Create map centered on Tashkent
        // Disable fadeAnimation to prevent conflicts with global CSS transitions
        this.map = L.map('addressWizardMap', {
            fadeAnimation: false,
            zoomAnimation: true
        }).setView(
            [center.latitude, center.longitude],
            12
        );

        // Add OpenStreetMap tiles
        // Use crossOrigin to ensure tiles load properly
        const tileLayer = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
            maxZoom: 19,
            crossOrigin: true
        }).addTo(this.map);

        // Debug: Listen for tile events
        tileLayer.on('tileload', (e) => {
            console.log('AddressWizard: Tile loaded', e.coords);
            // Force visibility on the loaded tile with inline styles
            if (e.tile) {
                // Log tile's current computed styles for debugging
                const computed = window.getComputedStyle(e.tile);
                console.log('AddressWizard: Tile computed styles:', {
                    visibility: computed.visibility,
                    opacity: computed.opacity,
                    display: computed.display,
                    transform: computed.transform,
                    width: computed.width,
                    height: computed.height,
                    position: computed.position,
                    left: computed.left,
                    top: computed.top
                });

                // Apply all styles to override CSS transitions
                e.tile.style.setProperty('visibility', 'visible', 'important');
                e.tile.style.setProperty('opacity', '1', 'important');
                e.tile.style.setProperty('transition', 'none', 'important');
                e.tile.style.setProperty('transition-property', 'none', 'important');
                e.tile.style.setProperty('transition-duration', '0s', 'important');
                e.tile.style.setProperty('transition-delay', '0s', 'important');
                e.tile.style.setProperty('max-width', 'none', 'important');
                e.tile.style.setProperty('max-height', 'none', 'important');
            }
        });

        tileLayer.on('tileerror', (e) => {
            console.error('AddressWizard: Tile error', e);
        });

        tileLayer.on('load', () => {
            console.log('AddressWizard: All tiles loaded');
            // Force refresh styles on all tiles after load
            this.forceRefreshTileStyles();

            // Debug: Check parent elements
            const tilePane = mapContainer.querySelector('.leaflet-tile-pane');
            const mapPane = mapContainer.querySelector('.leaflet-map-pane');
            const tileContainer = mapContainer.querySelector('.leaflet-tile-container');
            const leafletContainer = mapContainer;

            if (leafletContainer) {
                const lcComputed = window.getComputedStyle(leafletContainer);
                console.log('AddressWizard: Leaflet container styles:', {
                    visibility: lcComputed.visibility,
                    opacity: lcComputed.opacity,
                    background: lcComputed.background,
                    backgroundColor: lcComputed.backgroundColor,
                    overflow: lcComputed.overflow,
                    position: lcComputed.position
                });
            }

            if (tilePane) {
                const tpComputed = window.getComputedStyle(tilePane);
                console.log('AddressWizard: Tile pane styles:', {
                    visibility: tpComputed.visibility,
                    opacity: tpComputed.opacity,
                    display: tpComputed.display,
                    transform: tpComputed.transform,
                    zIndex: tpComputed.zIndex,
                    background: tpComputed.background
                });
                // Check tile pane's bounding rect
                const tpRect = tilePane.getBoundingClientRect();
                console.log('AddressWizard: Tile pane rect:', tpRect);
            }

            if (tileContainer) {
                const tcComputed = window.getComputedStyle(tileContainer);
                console.log('AddressWizard: Tile container styles:', {
                    visibility: tcComputed.visibility,
                    opacity: tcComputed.opacity,
                    transform: tcComputed.transform,
                    position: tcComputed.position
                });
                // Check bounding rect
                const tcRect = tileContainer.getBoundingClientRect();
                console.log('AddressWizard: Tile container rect:', tcRect);
            }

            if (mapPane) {
                const mpComputed = window.getComputedStyle(mapPane);
                console.log('AddressWizard: Map pane styles:', {
                    visibility: mpComputed.visibility,
                    opacity: mpComputed.opacity,
                    display: mpComputed.display,
                    transform: mpComputed.transform
                });
            }

            // Check first tile's actual position
            const firstTile = mapContainer.querySelector('.leaflet-tile');
            if (firstTile) {
                const ftRect = firstTile.getBoundingClientRect();
                console.log('AddressWizard: First tile bounding rect:', ftRect);
            }

            // Check if there's anything covering tiles
            const overlayPane = mapContainer.querySelector('.leaflet-overlay-pane');
            if (overlayPane) {
                const opComputed = window.getComputedStyle(overlayPane);
                console.log('AddressWizard: Overlay pane styles:', {
                    visibility: opComputed.visibility,
                    background: opComputed.background,
                    zIndex: opComputed.zIndex
                });
            }
        });

        // Force invalidateSize after creation to ensure proper rendering
        setTimeout(() => {
            if (this.map) {
                this.map.invalidateSize();
                // Also force refresh tile styles
                this.forceRefreshTileStyles();
            }
        }, 100);

        // Additional delayed refresh for modal animation completion
        setTimeout(() => {
            if (this.map) {
                this.map.invalidateSize();
                this.forceRefreshTileStyles();
            }
        }, 500);

        // Use MutationObserver to catch dynamically added tiles
        this.setupTileObserver(mapContainer);

        // Draw Tashkent boundary (visual indicator)
        const boundaryCoords = [
            [bounds.min_lat, bounds.min_lng],
            [bounds.min_lat, bounds.max_lng],
            [bounds.max_lat, bounds.max_lng],
            [bounds.max_lat, bounds.min_lng],
            [bounds.min_lat, bounds.min_lng]
        ];

        this.boundaryLayer = L.polyline(boundaryCoords, {
            color: '#1890ff',
            weight: 2,
            opacity: 0.5,
            dashArray: '5, 10'
        }).addTo(this.map);

        // Map click handler - place marker
        this.map.on('click', (e) => this.onMapClick(e));

        // If editing and has location, show marker
        if (this.selectedLocation) {
            this.setMarkerPosition(this.selectedLocation.lat, this.selectedLocation.lng);
        }
    },

    // Handle map click
    onMapClick: function(e) {
        const { lat, lng } = e.latlng;

        // Validate within Tashkent bounds
        if (!this.isWithinBounds(lat, lng)) {
            this.showError(this.getTranslation('location_outside_tashkent'));
            return;
        }

        this.setMarkerPosition(lat, lng);
    },

    // Check if coordinates are within service area
    isWithinBounds: function(lat, lng) {
        const bounds = this.geoConfig.bounds;
        return lat >= bounds.min_lat &&
               lat <= bounds.max_lat &&
               lng >= bounds.min_lng &&
               lng <= bounds.max_lng;
    },

    // Set marker position and reverse geocode
    setMarkerPosition: function(lat, lng) {
        if (this.marker) {
            this.marker.setLatLng([lat, lng]);
        } else {
            // Create draggable marker with default Leaflet icon
            this.marker = L.marker([lat, lng], {
                draggable: true
            }).addTo(this.map);

            // Handle marker drag
            this.marker.on('dragend', (e) => {
                const pos = e.target.getLatLng();

                // Validate new position
                if (!this.isWithinBounds(pos.lat, pos.lng)) {
                    // Reset to previous valid position
                    if (this.selectedLocation) {
                        this.marker.setLatLng([this.selectedLocation.lat, this.selectedLocation.lng]);
                    } else {
                        const center = this.geoConfig.center;
                        this.marker.setLatLng([center.latitude, center.longitude]);
                    }
                    this.showError(this.getTranslation('location_outside_tashkent'));
                    return;
                }

                this.updateSelectedLocation(pos.lat, pos.lng);
            });
        }

        // Center map on marker
        this.map.setView([lat, lng], 16);

        // Update selected location
        this.updateSelectedLocation(lat, lng);
    },

    // Update selected location and reverse geocode
    updateSelectedLocation: async function(lat, lng) {
        this.selectedLocation = { lat, lng };

        // Show loading indicator
        const statusEl = document.getElementById('locationStatus');
        const statusTextEl = document.getElementById('locationStatusText');

        if (statusEl) statusEl.classList.remove('d-none');
        if (statusTextEl) {
            statusTextEl.textContent = this.getTranslation('getting_address');
            statusTextEl.classList.remove('text-success');
        }

        // Update hidden fields
        document.getElementById('selectedLatitude').value = lat;
        document.getElementById('selectedLongitude').value = lng;

        // Reverse geocode to get address
        try {
            const response = await apiRequest('/addresses/reverse-geocode', {
                method: 'POST',
                body: JSON.stringify({ latitude: lat, longitude: lng })
            });
            const result = await response.json();

            if (response.ok && result.success) {
                const data = result.data;

                // Update form fields with geocoded address
                document.getElementById('fullAddress').value = data.formatted_address || '';

                if (data.district) {
                    const districtKey = this.findDistrictKey(data.district);
                    if (districtKey) {
                        document.getElementById('districtSelect').value = districtKey;
                    }
                }

                // Update status
                if (statusTextEl) {
                    statusTextEl.textContent = this.getTranslation('location_selected');
                    statusTextEl.classList.add('text-success');
                }

                // Enable next button
                this.updateNavigationButtons();
            } else {
                if (statusTextEl) {
                    statusTextEl.textContent = this.getTranslation('address_lookup_failed');
                }
            }
        } catch (error) {
            console.error('Reverse geocoding failed:', error);
            if (statusTextEl) {
                statusTextEl.textContent = this.getTranslation('location_selected_no_address');
            }
        }
    },

    // Find district key from name
    findDistrictKey: function(districtName) {
        if (!districtName || !this.geoConfig.districts) return null;

        const normalized = districtName.toLowerCase();
        for (const district of this.geoConfig.districts) {
            if (district.name.toLowerCase().includes(normalized) ||
                normalized.includes(district.name.toLowerCase())) {
                return district.key;
            }
        }
        return null;
    },

    // Use browser geolocation
    useMyLocation: function() {
        if (!navigator.geolocation) {
            this.showError(this.getTranslation('geolocation_not_supported'));
            return;
        }

        const btn = document.getElementById('useMyLocationBtn');
        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + this.getTranslation('getting_location');

        navigator.geolocation.getCurrentPosition(
            (position) => {
                const { latitude, longitude } = position.coords;

                // Validate within bounds
                if (!this.isWithinBounds(latitude, longitude)) {
                    this.showError(this.getTranslation('location_outside_tashkent'));
                    this.resetLocationButton();
                    return;
                }

                this.setMarkerPosition(latitude, longitude);
                this.resetLocationButton();
            },
            (error) => {
                console.error('Geolocation error:', error);
                let errorMsg = this.getTranslation('geolocation_error');
                if (error.code === error.PERMISSION_DENIED) {
                    errorMsg = this.getTranslation('geolocation_denied');
                }
                this.showError(errorMsg);
                this.resetLocationButton();
            },
            {
                enableHighAccuracy: true,
                timeout: 10000,
                maximumAge: 0
            }
        );
    },

    // Reset location button state
    resetLocationButton: function() {
        const btn = document.getElementById('useMyLocationBtn');
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-location-arrow"></i> ' + this.getTranslation('use_my_location');
        }
    },

    // Search address and place marker
    searchAddress: async function() {
        const input = document.getElementById('addressSearchInput');
        const address = input?.value?.trim();

        if (!address) {
            this.showError(this.getTranslation('enter_address_to_search'));
            return;
        }

        const btn = document.getElementById('searchAddressBtn');
        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';

        try {
            const response = await apiRequest('/addresses/geocode', {
                method: 'POST',
                body: JSON.stringify({ address: address + ', Tashkent' })
            });
            const result = await response.json();

            if (response.ok && result.success) {
                const { latitude, longitude, formatted_address } = result.data;

                // Validate within bounds
                if (!this.isWithinBounds(latitude, longitude)) {
                    this.showError(this.getTranslation('location_outside_tashkent'));
                    return;
                }

                this.setMarkerPosition(latitude, longitude);

                // Update full address field
                if (formatted_address) {
                    document.getElementById('fullAddress').value = formatted_address;
                }
            } else {
                this.showError(result.message || this.getTranslation('address_not_found'));
            }
        } catch (error) {
            console.error('Address search failed:', error);
            this.showError(this.getTranslation('search_failed'));
        } finally {
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-search"></i>';
        }
    },

    // Navigation methods
    goToStep: function(step) {
        if (step < 1 || step > this.totalSteps) return;

        // Hide all steps
        document.querySelectorAll('.wizard-step').forEach(el => {
            el.classList.remove('active');
        });

        // Show current step
        document.getElementById(`wizardStep${step}`)?.classList.add('active');

        // Update step indicators
        document.querySelectorAll('.step-indicator').forEach((el, index) => {
            el.classList.remove('active', 'completed');
            if (index + 1 < step) {
                el.classList.add('completed');
            } else if (index + 1 === step) {
                el.classList.add('active');
            }
        });

        this.currentStep = step;
        this.updateNavigationButtons();

        // If going back to step 1 and map exists, invalidate size
        // (Map initialization is handled by shown.bs.modal event)
        if (step === 1 && this.map) {
            setTimeout(() => {
                this.map.invalidateSize();
            }, 50);
        }
    },

    nextStep: function() {
        // Validate current step before proceeding
        if (!this.validateStep(this.currentStep)) {
            return;
        }

        if (this.currentStep < this.totalSteps) {
            this.goToStep(this.currentStep + 1);
        }
    },

    prevStep: function() {
        if (this.currentStep > 1) {
            this.goToStep(this.currentStep - 1);
        }
    },

    // Validate step data
    validateStep: function(step) {
        switch(step) {
            case 1:
                // Must have selected location
                if (!this.selectedLocation || !this.selectedLocation.lat || !this.selectedLocation.lng) {
                    this.showError(this.getTranslation('select_location_on_map'));
                    return false;
                }
                return true;

            case 2:
                // Title is required
                const title = document.getElementById('addressTitle')?.value?.trim();
                if (!title) {
                    this.showError(this.getTranslation('title_required'));
                    return false;
                }
                return true;

            case 3:
                return true;

            default:
                return true;
        }
    },

    // Update navigation buttons state
    updateNavigationButtons: function() {
        const prevBtn = document.getElementById('wizardPrevBtn');
        const nextBtn = document.getElementById('wizardNextBtn');
        const saveBtn = document.getElementById('wizardSaveBtn');

        // Previous button
        if (prevBtn) {
            prevBtn.style.display = this.currentStep > 1 ? 'inline-block' : 'none';
        }

        // Next button
        if (nextBtn) {
            nextBtn.style.display = this.currentStep < this.totalSteps ? 'inline-block' : 'none';
            // Disable if no location selected on step 1
            if (this.currentStep === 1) {
                nextBtn.disabled = !this.selectedLocation || !this.selectedLocation.lat;
            } else {
                nextBtn.disabled = false;
            }
        }

        // Save button (only on last step)
        if (saveBtn) {
            saveBtn.style.display = this.currentStep === this.totalSteps ? 'inline-block' : 'none';
        }
    },

    // Fill form with address data (for editing)
    fillForm: function(address) {
        const setVal = (id, val) => {
            const el = document.getElementById(id);
            if (el) el.value = val || '';
        };
        const setChecked = (id, val) => {
            const el = document.getElementById(id);
            if (el) el.checked = val || false;
        };

        setVal('addressTitle', address.title);
        setVal('fullAddress', address.full_address);
        setVal('streetAddress', address.street_address);
        setVal('districtSelect', address.district);
        setVal('buildingNumber', address.building_number);
        setVal('apartmentNumber', address.apartment_number);
        setVal('floorNumber', address.floor_number);
        setVal('entranceNumber', address.entrance_number);
        setVal('deliveryInstructions', address.delivery_instructions);
        setVal('landmark', address.landmark);
        setVal('selectedLatitude', address.latitude);
        setVal('selectedLongitude', address.longitude);
        setChecked('isDefault', address.is_default);
        setChecked('isBusiness', address.is_business);
    },

    // Reset form
    resetForm: function() {
        document.getElementById('addressWizardForm')?.reset();

        const setVal = (id, val) => {
            const el = document.getElementById(id);
            if (el) el.value = val || '';
        };

        setVal('selectedLatitude', '');
        setVal('selectedLongitude', '');

        const statusEl = document.getElementById('locationStatus');
        if (statusEl) statusEl.classList.add('d-none');

        // Reset quick title buttons
        document.querySelectorAll('.quick-title-btn').forEach(btn => btn.classList.remove('active'));

        // Reset marker
        if (this.marker && this.map) {
            this.map.removeLayer(this.marker);
            this.marker = null;
        }
    },

    // Save address to API
    saveAddress: async function() {
        // Final validation
        if (!this.selectedLocation || !this.selectedLocation.lat || !this.selectedLocation.lng) {
            this.showError(this.getTranslation('location_required'));
            return;
        }

        const title = document.getElementById('addressTitle')?.value?.trim();
        if (!title) {
            this.showError(this.getTranslation('title_required'));
            this.goToStep(2);
            return;
        }

        const saveBtn = document.getElementById('wizardSaveBtn');
        saveBtn.disabled = true;
        saveBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + this.getTranslation('saving');

        const getVal = (id) => document.getElementById(id)?.value?.trim() || '';
        const getChecked = (id) => document.getElementById(id)?.checked || false;

        const data = {
            title: title,
            full_address: getVal('fullAddress'),
            street_address: getVal('streetAddress'),
            district: getVal('districtSelect'),
            city: 'Tashkent',
            country: 'Uzbekistan',
            building_number: getVal('buildingNumber'),
            apartment_number: getVal('apartmentNumber'),
            floor_number: getVal('floorNumber'),
            entrance_number: getVal('entranceNumber'),
            delivery_instructions: getVal('deliveryInstructions'),
            landmark: getVal('landmark'),
            latitude: this.selectedLocation.lat,
            longitude: this.selectedLocation.lng,
            is_default: getChecked('isDefault'),
            is_business: getChecked('isBusiness')
        };

        try {
            let response;
            if (this.editingAddressId) {
                response = await apiRequest(`/auth/addresses/${this.editingAddressId}`, {
                    method: 'PUT',
                    body: JSON.stringify(data)
                });
            } else {
                response = await apiRequest('/auth/addresses', {
                    method: 'POST',
                    body: JSON.stringify(data)
                });
            }

            const result = await response.json();

            if (response.ok) {
                this.hideModal();
                showNotification(this.getTranslation('address_saved'), 'success');

                // Reload addresses list
                if (typeof loadAddresses === 'function') {
                    loadAddresses();
                }
            } else {
                this.showError(result.message || this.getTranslation('save_failed'));
            }
        } catch (error) {
            console.error('Failed to save address:', error);
            this.showError(this.getTranslation('network_error'));
        } finally {
            saveBtn.disabled = false;
            saveBtn.innerHTML = '<i class="fas fa-check"></i> ' + this.getTranslation('save_address');
        }
    },

    // Modal methods
    showModal: function() {
        $('#addressWizardModal').modal('show');
    },

    hideModal: function() {
        $('#addressWizardModal').modal('hide');
    },

    // Show error message
    showError: function(message) {
        if (typeof showNotification === 'function') {
            showNotification(message, 'error');
        } else {
            alert(message);
        }
    },

    // Get translation (with fallback)
    getTranslation: function(key) {
        const translations = {
            'en': {
                'select_district': 'Select District',
                'location_outside_tashkent': 'Selected location is outside Tashkent. Please select a location within the city.',
                'getting_address': 'Getting address...',
                'location_selected': 'Location selected!',
                'address_lookup_failed': 'Could not get address details',
                'location_selected_no_address': 'Location selected (address lookup unavailable)',
                'geolocation_not_supported': 'Geolocation is not supported by your browser',
                'getting_location': 'Getting location...',
                'geolocation_error': 'Could not get your location',
                'geolocation_denied': 'Location access was denied. Please enable location or select on map.',
                'use_my_location': 'Use My Location',
                'enter_address_to_search': 'Please enter an address to search',
                'address_not_found': 'Address not found. Try a different search.',
                'search_failed': 'Search failed. Please try again.',
                'select_location_on_map': 'Please select a location on the map',
                'title_required': 'Please enter a title for this address',
                'location_required': 'Location is required. Please go back and select on map.',
                'saving': 'Saving...',
                'address_saved': 'Address saved successfully!',
                'save_failed': 'Failed to save address',
                'network_error': 'Network error. Please try again.',
                'save_address': 'Save Address'
            },
            'ru': {
                'select_district': 'Выберите район',
                'location_outside_tashkent': 'Выбранное местоположение находится за пределами Ташкента. Пожалуйста, выберите местоположение в пределах города.',
                'getting_address': 'Получение адреса...',
                'location_selected': 'Местоположение выбрано!',
                'address_lookup_failed': 'Не удалось получить данные адреса',
                'location_selected_no_address': 'Местоположение выбрано (адрес недоступен)',
                'geolocation_not_supported': 'Геолокация не поддерживается вашим браузером',
                'getting_location': 'Определение местоположения...',
                'geolocation_error': 'Не удалось определить местоположение',
                'geolocation_denied': 'Доступ к местоположению был запрещен. Включите геолокацию или выберите на карте.',
                'use_my_location': 'Мое местоположение',
                'enter_address_to_search': 'Введите адрес для поиска',
                'address_not_found': 'Адрес не найден. Попробуйте другой запрос.',
                'search_failed': 'Ошибка поиска. Попробуйте снова.',
                'select_location_on_map': 'Пожалуйста, выберите местоположение на карте',
                'title_required': 'Введите название для этого адреса',
                'location_required': 'Местоположение обязательно. Вернитесь и выберите на карте.',
                'saving': 'Сохранение...',
                'address_saved': 'Адрес успешно сохранен!',
                'save_failed': 'Не удалось сохранить адрес',
                'network_error': 'Ошибка сети. Попробуйте снова.',
                'save_address': 'Сохранить адрес'
            },
            'uz': {
                'select_district': 'Tumanni tanlang',
                'location_outside_tashkent': 'Tanlangan joy Toshkent tashqarisida. Iltimos, shahar ichidagi joyni tanlang.',
                'getting_address': 'Manzil olinmoqda...',
                'location_selected': 'Joy tanlandi!',
                'address_lookup_failed': 'Manzil maʼlumotlarini olishda xatolik',
                'location_selected_no_address': 'Joy tanlandi (manzil mavjud emas)',
                'geolocation_not_supported': 'Brauzeringiz joylashuvni aniqlashtni qoʻllab-quvvatlamaydi',
                'getting_location': 'Joylashuv aniqlanmoqda...',
                'geolocation_error': 'Joylashuvni aniqlab boʻlmadi',
                'geolocation_denied': 'Joylashuvga ruxsat berilmadi. Geolokatsiyani yoqing yoki xaritadan tanlang.',
                'use_my_location': 'Joylashuvimni aniqlash',
                'enter_address_to_search': 'Qidirish uchun manzilni kiriting',
                'address_not_found': 'Manzil topilmadi. Boshqa soʻrov bilan urinib koʻring.',
                'search_failed': 'Qidiruv xatosi. Qaytadan urinib koʻring.',
                'select_location_on_map': 'Iltimos, xaritadan joyni tanlang',
                'title_required': 'Iltimos, manzil uchun nom kiriting',
                'location_required': 'Joy talab qilinadi. Orqaga qaytib, xaritadan tanlang.',
                'saving': 'Saqlanmoqda...',
                'address_saved': 'Manzil muvaffaqiyatli saqlandi!',
                'save_failed': 'Manzilni saqlashda xatolik',
                'network_error': 'Tarmoq xatosi. Qaytadan urinib koʻring.',
                'save_address': 'Manzilni saqlash'
            }
        };

        const lang = document.documentElement.lang || 'en';
        const langTranslations = translations[lang] || translations['en'];
        return langTranslations[key] || translations['en'][key] || key;
    },

    // Setup MutationObserver to fix styles on dynamically added tiles
    setupTileObserver: function(mapContainer) {
        if (this.tileObserver) {
            this.tileObserver.disconnect();
        }

        this.tileObserver = new MutationObserver((mutations) => {
            mutations.forEach((mutation) => {
                mutation.addedNodes.forEach((node) => {
                    if (node.nodeType === Node.ELEMENT_NODE) {
                        // Check if it's a tile image
                        if (node.tagName === 'IMG' && (node.classList.contains('leaflet-tile') || node.closest('.leaflet-tile-pane'))) {
                            this.fixTileStyle(node);
                        }
                        // Also check children
                        const tiles = node.querySelectorAll ? node.querySelectorAll('img.leaflet-tile, .leaflet-tile-pane img') : [];
                        tiles.forEach((tile) => this.fixTileStyle(tile));
                    }
                });
            });
        });

        this.tileObserver.observe(mapContainer, {
            childList: true,
            subtree: true
        });
    },

    // Fix styles on a single tile
    fixTileStyle: function(tile) {
        tile.style.setProperty('visibility', 'visible', 'important');
        tile.style.setProperty('opacity', '1', 'important');
        tile.style.setProperty('transition', 'none', 'important');
        tile.style.setProperty('transition-property', 'none', 'important');
        tile.style.setProperty('transition-duration', '0s', 'important');
        tile.style.setProperty('transition-delay', '0s', 'important');
        tile.style.setProperty('max-width', 'none', 'important');
        tile.style.setProperty('max-height', 'none', 'important');
    },

    // Force refresh tile styles to override any CSS transition issues
    forceRefreshTileStyles: function() {
        if (!this.map) return;

        const mapContainer = document.getElementById('addressWizardMap');
        if (!mapContainer) return;

        // Get all tile images and force their styles
        const tiles = mapContainer.querySelectorAll('.leaflet-tile, .leaflet-tile-container img, img.leaflet-tile');
        console.log('AddressWizard: Forcing styles on', tiles.length, 'tiles');

        tiles.forEach((tile) => {
            // IMPORTANT: Use setProperty to ADD styles without overwriting Leaflet's transform positioning
            // DO NOT use style.cssText as it replaces ALL inline styles including position transforms
            this.fixTileStyle(tile);
        });

        // Also check for any images that might have been missed
        const allImages = mapContainer.querySelectorAll('img');
        allImages.forEach((img) => {
            if (img.classList.contains('leaflet-tile') || img.closest('.leaflet-tile-pane')) {
                this.fixTileStyle(img);
            }
        });
    }
};

// Initialize on DOM ready
document.addEventListener('DOMContentLoaded', function() {
    AddressWizard.init();
});

// Global function to open wizard (called from addresses.html)
function showAddAddressModal() {
    AddressWizard.openForNew();
}

// Global function to edit address with wizard
function editAddressWizard(address) {
    AddressWizard.openForEdit(address);
}
