/**
 * Subscription Constructor - Enhanced Version
 * Multi-step subscription creation with authentication flow
 * Steps: Products -> Schedule -> Address -> Payment -> Review
 */

class SubscriptionConstructor {
    constructor() {
        this.currentStep = 1;
        this.totalSteps = 5;

        // Subscription data
        this.selectedProducts = new Map(); // productId -> {product, quantity}
        this.selectedFrequency = null;
        this.selectedDay = null;
        this.selectedTimeSlot = null;
        this.selectedAddress = null;
        this.selectedPaymentMethod = null;
        this.subscriptionName = '';  // User-editable subscription name

        // Available options
        this.products = [];
        this.timeSlots = [];
        this.addresses = [];
        this.paymentMethods = [];

        // State management
        this.isAuthenticated = !!CURRENT_USER;
        this.stateKey = 'subscription_constructor_state';

        this.init();
    }

    async init() {
        try {
            // Restore saved state if exists
            this.restoreState();

            // Load initial data
            await this.loadProducts();

            // Setup event listeners
            this.setupEventListeners();

            // Update UI
            this.updateStepIndicators();
            this.showStep(this.currentStep);
        } catch (error) {
            console.error('Failed to initialize subscription constructor:', error);
            this.showError('Failed to load subscription constructor');
        }
    }

    // ============================================
    // STATE MANAGEMENT
    // ============================================

    saveState() {
        const state = {
            currentStep: this.currentStep,
            selectedProducts: Array.from(this.selectedProducts.entries()),
            selectedFrequency: this.selectedFrequency,
            selectedDay: this.selectedDay,
            selectedTimeSlot: this.selectedTimeSlot,
            selectedAddress: this.selectedAddress,
            selectedPaymentMethod: this.selectedPaymentMethod,
            subscriptionName: this.subscriptionName,
            timestamp: Date.now()
        };
        sessionStorage.setItem(this.stateKey, JSON.stringify(state));
    }

    restoreState() {
        const savedState = sessionStorage.getItem(this.stateKey);
        if (!savedState) return;

        try {
            const state = JSON.parse(savedState);

            // Only restore if saved within last hour
            if (Date.now() - state.timestamp > 3600000) {
                sessionStorage.removeItem(this.stateKey);
                return;
            }

            this.currentStep = state.currentStep || 1;
            this.selectedProducts = new Map(state.selectedProducts || []);
            this.selectedFrequency = state.selectedFrequency;
            this.selectedDay = state.selectedDay;
            this.selectedTimeSlot = state.selectedTimeSlot;
            this.selectedAddress = state.selectedAddress;
            this.selectedPaymentMethod = state.selectedPaymentMethod;
            this.subscriptionName = state.subscriptionName || '';
        } catch (error) {
            console.error('Failed to restore state:', error);
            sessionStorage.removeItem(this.stateKey);
        }
    }

    clearState() {
        sessionStorage.removeItem(this.stateKey);

        // Also reset all instance variables
        this.currentStep = 1;
        this.selectedProducts = new Map();
        this.selectedFrequency = null;
        this.selectedDay = null;
        this.selectedTimeSlot = null;
        this.selectedAddress = null;
        this.selectedPaymentMethod = null;
        this.subscriptionName = '';

        // Reset UI to initial state
        this.resetUI();
    }

    resetUI() {
        // Reset step indicators
        this.updateStepIndicators();

        // Go back to step 1
        this.showStep(1);

        // Re-render products with no selections
        if (this.products.length > 0) {
            this.renderProducts();
        }
    }

    // ============================================
    // DATA LOADING
    // ============================================

    async loadProducts() {
        try {
            const response = await fetch(`${API_BASE_URL}/products?per_page=50&is_active=true`, {
                method: 'GET',
                headers: { 'Content-Type': 'application/json' },
                credentials: 'include'
            });

            if (!response.ok) throw new Error('Failed to fetch products');

            const result = await response.json();
            if (result.success && result.data) {
                this.products = result.data.items || [];
                this.renderProducts();
            }
        } catch (error) {
            console.error('Error loading products:', error);
            throw error;
        }
    }

    async loadTimeSlots() {
        try {
            // Get today's date in YYYY-MM-DD format
            const today = new Date().toISOString().split('T')[0];

            const response = await apiRequest(`/delivery/time-slots?date=${today}`, {
                method: 'GET'
            });

            // Parse JSON from the response
            const data = await response.json();

            if (data && data.time_slots) {
                // Map backend response to our format
                this.timeSlots = data.time_slots.map(slot => ({
                    id: slot.id,
                    label: `${slot.start_time} - ${slot.end_time}`,
                    value: slot.time_range || `${slot.start_time}-${slot.end_time}`,
                    available: slot.is_available,
                    fee: slot.total_fee
                }));
                this.renderTimeSlots();
            } else {
                // Fallback to default time slots
                this.timeSlots = this.getDefaultTimeSlots();
                this.renderTimeSlots();
            }
        } catch (error) {
            console.error('Error loading time slots:', error);
            this.timeSlots = this.getDefaultTimeSlots();
            this.renderTimeSlots();
        }
    }

    getDefaultTimeSlots() {
        return [
            { id: 1, label: '09:00 - 12:00', value: '09:00-12:00' },
            { id: 2, label: '12:00 - 15:00', value: '12:00-15:00' },
            { id: 3, label: '15:00 - 18:00', value: '15:00-18:00' },
            { id: 4, label: '18:00 - 21:00', value: '18:00-21:00' }
        ];
    }

    async loadAddresses() {
        if (!this.isAuthenticated) {
            this.addresses = [];
            return;
        }

        try {
            const response = await apiRequest('/auth/addresses', {
                method: 'GET'
            });

            // Parse JSON from the response
            const data = await response.json();

            if (data.success && data.data) {
                this.addresses = data.data.addresses || [];
                this.renderAddresses();
            }
        } catch (error) {
            console.error('Error loading addresses:', error);
            this.addresses = [];
            this.renderAddresses();
        }
    }

    async loadPaymentMethods() {
        // Payment methods come from the backend SSOT so this menu can never
        // diverge from checkout. Payme is not offered; `click` is labelled "Card".
        const ICONS = { cash: '💵', click: '💳', business_account: '🏢' };
        try {
            const response = await apiRequest('/payments/methods?context=subscription');
            const payload = await response.json();
            const methods = (payload.data && payload.data.available_methods) || [];
            this.paymentMethods = methods.map((m) => ({
                id: m.method,
                name: m.display_name,
                icon: ICONS[m.method] || '💳',
                description: m.description || ''
            }));
        } catch (error) {
            // Cash is always available offline; degrade rather than block signup.
            this.paymentMethods = [
                { id: 'cash', name: 'Cash on Delivery', icon: '💵', description: 'Pay when you receive' }
            ];
        }
        this.renderPaymentMethods();
    }

    // ============================================
    // RENDERING
    // ============================================

    renderProducts() {
        const container = document.getElementById('products-grid');
        if (!container || !this.products) return;

        const productsHTML = this.products.map(product => {
            const isSelected = this.selectedProducts.has(product.id);
            const quantity = isSelected ? this.selectedProducts.get(product.id).quantity : 1;

            // Get price from nested structure
            const price = product.pricing?.base_price || product.base_price || 0;

            // Get image from nested media structure
            const imageUrl = product.media?.images?.[0] || product.image_url || '/static/images/product-placeholder.svg';

            return `
                <div class="product-card ${isSelected ? 'selected' : ''}" data-product-id="${product.id}">
                    <img src="${imageUrl}"
                         alt="${this.escapeHtml(product.name)}"
                         class="product-image">
                    <div class="product-name">${this.escapeHtml(product.name)}</div>
                    <div class="product-price">${this.formatPrice(price)} UZS</div>
                    <div class="quantity-control">
                        <button type="button" class="quantity-btn" data-action="decrease">-</button>
                        <span class="quantity-display">${quantity}</span>
                        <button type="button" class="quantity-btn" data-action="increase">+</button>
                    </div>
                </div>
            `;
        }).join('');

        container.innerHTML = productsHTML;
    }

    renderTimeSlots() {
        const select = document.getElementById('time-slots-select');
        if (!select) {
            console.error('Time slots select element not found');
            return;
        }

        // Build all options including placeholder
        const allOptionsHTML = `
            <option value="">Select a time slot (optional)</option>
            ${this.timeSlots.map(slot => `
                <option value="${slot.id}" ${this.selectedTimeSlot === slot.id ? 'selected' : ''}>
                    ${slot.label}${slot.fee ? ` (+${this.formatPrice(slot.fee)} UZS)` : ''}
                </option>
            `).join('')}
        `;

        select.innerHTML = allOptionsHTML;

        // Set the selected value if we have one
        if (this.selectedTimeSlot) {
            select.value = this.selectedTimeSlot;
        }

        // Setup change listener if not already done
        // 3. Update Nice Select and Attach jQuery Listener
        if (typeof $ !== 'undefined' && $(select).next().hasClass('nice-select')) {
            // Refresh the visual dropdown to show new options
            $(select).niceSelect('update');

            // CRITICAL FIX: Use jQuery .on() to catch the event from Nice Select
            // We use .off() first to ensure we don't add multiple listeners if this runs twice
            $(select).off('change.subscription').on('change.subscription', (e) => {
                this.selectedTimeSlot = e.target.value;
                this.saveState();
            });
        } else {
            // Fallback for vanilla JS if jQuery fails to load
            if (!select.dataset.listenerAttached) {
                select.addEventListener('change', (e) => {
                    this.selectedTimeSlot = e.target.value;
                    this.saveState();
                });
                select.dataset.listenerAttached = 'true';
            }
        }
    }

    renderAddresses() {
        const container = document.getElementById('addresses-list');
        if (!container) return;

        if (this.addresses.length === 0) {
            container.innerHTML = `
                <div style="text-align: center; padding: 40px; background: #fafafa; border-radius: 8px; border: 2px dashed #d9d9d9;">
                    <div style="font-size: 48px; margin-bottom: 16px;">📍</div>
                    <h3 style="margin: 0 0 8px 0; color: #262626;">No Addresses Yet</h3>
                    <p style="color: #8c8c8c; margin: 0 0 20px 0;">Add your first delivery address to continue</p>
                    <button type="button" class="theme-btn btn-one" onclick="subscriptionConstructor.showAddressForm()">
                        Add New Address
                    </button>
                </div>
            `;
            return;
        }

        const addressesHTML = this.addresses.map(address => `
            <div class="address-card ${this.selectedAddress?.id === address.id ? 'selected' : ''}"
                 data-address-id="${address.id}">
                <div class="address-header">
                    <div class="address-type">${address.title || 'Home'}</div>
                    ${this.selectedAddress?.id === address.id ? '<div class="selected-badge">✓</div>' : ''}
                </div>
                <div class="address-details">
                    <div class="address-line">${this.escapeHtml(address.full_address || address.street_address)}</div>
                    ${address.apartment_number ? `<div class="address-line">Apt: ${this.escapeHtml(address.apartment_number)}</div>` : ''}
                    <div class="address-line">${this.escapeHtml(address.city || '')}, ${this.escapeHtml(address.district || '')}</div>
                    ${address.delivery_instructions ? `<div class="address-notes">${this.escapeHtml(address.delivery_instructions)}</div>` : ''}
                </div>
            </div>
        `).join('');

        container.innerHTML = `
            ${addressesHTML}
            <div class="address-card add-new-address" onclick="subscriptionConstructor.showAddressForm()">
                <div style="text-align: center; padding: 20px;">
                    <div style="font-size: 32px; margin-bottom: 8px;">➕</div>
                    <div style="font-weight: 500; color: #1890ff;">Add New Address</div>
                </div>
            </div>
        `;
    }

    renderPaymentMethods() {
        const container = document.getElementById('payment-methods-grid');
        if (!container) return;

        const methodsHTML = this.paymentMethods.map(method => `
            <div class="payment-method-card ${this.selectedPaymentMethod === method.id ? 'selected' : ''}"
                 data-payment-method="${method.id}">
                <div class="payment-method-icon">${method.icon}</div>
                <div class="payment-method-name">${method.name}</div>
                <div class="payment-method-description">${method.description}</div>
                ${this.selectedPaymentMethod === method.id ? '<div class="selected-indicator">✓</div>' : ''}
            </div>
        `).join('');

        container.innerHTML = methodsHTML;
    }

    renderReview() {
        const preview = document.getElementById('review-container');
        if (!preview) return;

        // Generate subscription name if not already set by user
        if (!this.subscriptionName) {
            this.subscriptionName = this.generateSubscriptionName();
        }

        // Calculate totals
        let subtotal = 0;
        const productsList = [];

        this.selectedProducts.forEach(({ product, quantity }) => {
            const price = product.pricing?.base_price || product.base_price || 0;
            const productTotal = price * quantity;
            subtotal += productTotal;
            productsList.push({
                name: product.name,
                quantity: quantity,
                price: price,
                total: productTotal
            });
        });

        // Calculate monthly estimate
        let monthlyMultiplier = 1;
        if (this.selectedFrequency === 'daily') monthlyMultiplier = 30;
        else if (this.selectedFrequency === 'weekly') monthlyMultiplier = 4;
        else if (this.selectedFrequency === 'biweekly') monthlyMultiplier = 2;
        else if (this.selectedFrequency === 'monthly') monthlyMultiplier = 1;

        const estimatedMonthlyCost = subtotal * monthlyMultiplier;

        // Get selected address and payment method
        const address = this.selectedAddress;
        const paymentMethod = this.paymentMethods.find(pm => pm.id === this.selectedPaymentMethod);

        // Render comprehensive review
        preview.innerHTML = `
            <div style="max-width: 800px; margin: 0 auto;">
                <!-- Subscription Name Card -->
                <div style="background: white; border-radius: 12px; padding: 24px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); border: 1px solid #e8e8e8;">
                    <div style="display: flex; align-items: center; margin-bottom: 20px; padding-bottom: 16px; border-bottom: 2px solid #f0f0f0;">
                        <div style="width: 40px; height: 40px; background: linear-gradient(135deg, #722ed1 0%, #531dab 100%); border-radius: 10px; display: flex; align-items: center; justify-content: center; margin-right: 12px;">
                            <span style="color: white; font-size: 20px;">✏️</span>
                        </div>
                        <h3 style="margin: 0; font-size: 18px; font-weight: 600; color: #262626;">Subscription Name</h3>
                    </div>
                    <div>
                        <label for="subscription-name-input" style="display: block; margin-bottom: 8px; font-size: 14px; color: #595959; font-weight: 500;">
                            Give your subscription a memorable name
                        </label>
                        <input
                            type="text"
                            id="subscription-name-input"
                            value="${this.escapeHtml(this.subscriptionName)}"
                            maxlength="200"
                            style="width: 100%; padding: 12px 16px; border: 2px solid #d9d9d9; border-radius: 8px; font-size: 16px; font-weight: 500; color: #262626; transition: all 0.3s ease;"
                            placeholder="e.g., Weekly Water Delivery - Home"
                        />
                        <div style="margin-top: 8px; font-size: 13px; color: #8c8c8c;">
                            <span style="margin-right: 8px;">💡</span>
                            Auto-generated based on your selections, but feel free to customize it!
                        </div>
                    </div>
                </div>

                <!-- Selected Products Card -->
                <div style="background: white; border-radius: 12px; padding: 24px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); border: 1px solid #e8e8e8;">
                    <div style="display: flex; align-items: center; margin-bottom: 20px; padding-bottom: 16px; border-bottom: 2px solid #f0f0f0;">
                        <div style="width: 40px; height: 40px; background: linear-gradient(135deg, #1890ff 0%, #096dd9 100%); border-radius: 10px; display: flex; align-items: center; justify-content: center; margin-right: 12px;">
                            <span style="color: white; font-size: 20px;">🛒</span>
                        </div>
                        <h3 style="margin: 0; font-size: 18px; font-weight: 600; color: #262626;">Selected Products</h3>
                    </div>
                    <div>
                        ${productsList.map((item, index) => `
                            <div style="display: flex; justify-content: space-between; align-items: center; padding: 16px; background: #fafafa; border-radius: 8px; margin-bottom: ${index < productsList.length - 1 ? '12px' : '0'};">
                                <div style="flex: 1;">
                                    <div style="font-size: 15px; font-weight: 500; color: #262626; margin-bottom: 6px;">
                                        ${this.escapeHtml(item.name)}
                                    </div>
                                    <div style="display: flex; align-items: center; gap: 8px;">
                                        <span style="font-size: 13px; color: #8c8c8c;">
                                            ${this.formatPrice(item.price)} UZS
                                        </span>
                                        <span style="color: #d9d9d9;">×</span>
                                        <span style="font-size: 13px; font-weight: 500; color: #595959; background: white; padding: 2px 8px; border-radius: 4px;">
                                            Qty: ${item.quantity}
                                        </span>
                                    </div>
                                </div>
                                <div style="font-size: 16px; font-weight: 600; color: #52c41a; margin-left: 16px;">
                                    ${this.formatPrice(item.total)} UZS
                                </div>
                            </div>
                        `).join('')}
                    </div>
                </div>

                <!-- Delivery Schedule Card -->
                <div style="background: white; border-radius: 12px; padding: 24px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); border: 1px solid #e8e8e8;">
                    <div style="display: flex; align-items: center; margin-bottom: 20px; padding-bottom: 16px; border-bottom: 2px solid #f0f0f0;">
                        <div style="width: 40px; height: 40px; background: linear-gradient(135deg, #52c41a 0%, #389e0d 100%); border-radius: 10px; display: flex; align-items: center; justify-content: center; margin-right: 12px;">
                            <span style="color: white; font-size: 20px;">📅</span>
                        </div>
                        <h3 style="margin: 0; font-size: 18px; font-weight: 600; color: #262626;">Delivery Schedule</h3>
                    </div>
                    <div style="background: #f6ffed; border: 1px solid #b7eb8f; border-radius: 8px; padding: 16px;">
                        <div style="display: grid; gap: 12px;">
                            <div style="display: flex; justify-content: space-between; align-items: center;">
                                <span style="font-size: 14px; color: #595959; font-weight: 500;">Frequency</span>
                                <span style="font-size: 15px; font-weight: 600; color: #262626;">${this.getFrequencyName(this.selectedFrequency)}</span>
                            </div>
                            ${this.selectedDay !== null ? `
                                <div style="display: flex; justify-content: space-between; align-items: center; padding-top: 12px; border-top: 1px solid #d9f7be;">
                                    <span style="font-size: 14px; color: #595959; font-weight: 500;">Delivery Day</span>
                                    <span style="font-size: 15px; font-weight: 600; color: #262626;">${this.getDayName(this.selectedDay)}</span>
                                </div>
                            ` : ''}
                            <div style="display: flex; justify-content: space-between; align-items: center; padding-top: 12px; border-top: 1px solid #d9f7be;">
                                <span style="font-size: 14px; color: #595959; font-weight: 500;">Time Slot</span>
                                <span style="font-size: 15px; font-weight: 600; color: #262626;">${this.getSelectedTimeSlotLabel() || 'Not selected (flexible)'}</span>
                            </div>
                        </div>
                    </div>
                </div>

                <!-- Delivery Address Card -->
                <div style="background: white; border-radius: 12px; padding: 24px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); border: 1px solid #e8e8e8;">
                    <div style="display: flex; align-items: center; margin-bottom: 20px; padding-bottom: 16px; border-bottom: 2px solid #f0f0f0;">
                        <div style="width: 40px; height: 40px; background: linear-gradient(135deg, #fa8c16 0%, #d46b08 100%); border-radius: 10px; display: flex; align-items: center; justify-content: center; margin-right: 12px;">
                            <span style="color: white; font-size: 20px;">📍</span>
                        </div>
                        <h3 style="margin: 0; font-size: 18px; font-weight: 600; color: #262626;">Delivery Address</h3>
                    </div>
                    <div style="background: #fff7e6; border: 1px solid #ffd591; border-radius: 8px; padding: 16px;">
                        <div style="font-weight: 500; color: #262626; margin-bottom: 8px;">${address?.type || 'Home'}</div>
                        <div style="color: #595959; line-height: 1.6;">
                            ${address?.street || address?.address_line1 || ''}<br>
                            ${address?.apartment ? `Apt: ${address.apartment}<br>` : ''}
                            ${address?.city || ''}, ${address?.region || ''}
                        </div>
                    </div>
                </div>

                <!-- Payment Method Card -->
                <div style="background: white; border-radius: 12px; padding: 24px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); border: 1px solid #e8e8e8;">
                    <div style="display: flex; align-items: center; margin-bottom: 20px; padding-bottom: 16px; border-bottom: 2px solid #f0f0f0;">
                        <div style="width: 40px; height: 40px; background: linear-gradient(135deg, #13c2c2 0%, #08979c 100%); border-radius: 10px; display: flex; align-items: center; justify-content: center; margin-right: 12px;">
                            <span style="color: white; font-size: 20px;">💳</span>
                        </div>
                        <h3 style="margin: 0; font-size: 18px; font-weight: 600; color: #262626;">Payment Method</h3>
                    </div>
                    <div style="background: #e6fffb; border: 1px solid #87e8de; border-radius: 8px; padding: 16px; display: flex; align-items: center; gap: 12px;">
                        <div style="font-size: 32px;">${paymentMethod?.icon || '💵'}</div>
                        <div>
                            <div style="font-weight: 600; color: #262626;">${paymentMethod?.name || 'Cash on Delivery'}</div>
                            <div style="font-size: 13px; color: #595959;">${paymentMethod?.description || ''}</div>
                        </div>
                    </div>
                </div>

                <!-- Cost Summary Card -->
                <div style="background: white; border-radius: 12px; padding: 24px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); border: 1px solid #e8e8e8;">
                    <div style="display: flex; align-items: center; margin-bottom: 20px; padding-bottom: 16px; border-bottom: 2px solid #f0f0f0;">
                        <div style="width: 40px; height: 40px; background: linear-gradient(135deg, #722ed1 0%, #531dab 100%); border-radius: 10px; display: flex; align-items: center; justify-content: center; margin-right: 12px;">
                            <span style="color: white; font-size: 20px;">💰</span>
                        </div>
                        <h3 style="margin: 0; font-size: 18px; font-weight: 600; color: #262626;">Cost Summary</h3>
                    </div>

                    <!-- Per Delivery Total -->
                    <div style="background: #fafafa; border-radius: 8px; padding: 16px; margin-bottom: 16px;">
                        <div style="display: flex; justify-content: space-between; align-items: center;">
                            <div>
                                <div style="font-size: 14px; color: #8c8c8c; margin-bottom: 4px;">Per Delivery</div>
                                <div style="font-size: 13px; color: #bfbfbf;">Single delivery cost</div>
                            </div>
                            <div style="font-size: 22px; font-weight: 700; color: #262626;">
                                ${this.formatPrice(subtotal)} <span style="font-size: 16px; color: #8c8c8c;">UZS</span>
                            </div>
                        </div>
                    </div>

                    <!-- Monthly Estimate -->
                    <div style="background: linear-gradient(135deg, #722ed1 0%, #531dab 100%); border-radius: 10px; padding: 20px; position: relative; overflow: hidden;">
                        <div style="position: absolute; top: -20px; right: -20px; width: 100px; height: 100px; background: rgba(255,255,255,0.1); border-radius: 50%;"></div>
                        <div style="position: absolute; bottom: -30px; left: -30px; width: 120px; height: 120px; background: rgba(255,255,255,0.08); border-radius: 50%;"></div>
                        <div style="position: relative; z-index: 1;">
                            <div style="display: flex; justify-content: space-between; align-items: center;">
                                <div>
                                    <div style="font-size: 15px; color: rgba(255,255,255,0.9); margin-bottom: 6px; font-weight: 500;">Estimated Monthly Cost</div>
                                    <div style="font-size: 12px; color: rgba(255,255,255,0.7);">
                                        Based on ${monthlyMultiplier} ${monthlyMultiplier === 1 ? 'delivery' : 'deliveries'}/month
                                    </div>
                                </div>
                                <div style="font-size: 28px; font-weight: 700; color: white; text-align: right;">
                                    ${this.formatPrice(estimatedMonthlyCost)}
                                    <div style="font-size: 14px; font-weight: 500; color: rgba(255,255,255,0.8); margin-top: 2px;">UZS</div>
                                </div>
                            </div>
                        </div>
                    </div>

                    <!-- Info Note -->
                    <div style="margin-top: 16px; padding: 12px; background: #e6f7ff; border: 1px solid #91d5ff; border-radius: 6px; display: flex; align-items: start; gap: 8px;">
                        <span style="color: #1890ff; font-size: 16px; flex-shrink: 0;">ℹ️</span>
                        <p style="margin: 0; font-size: 13px; color: #595959; line-height: 1.6;">
                            Your subscription will start after confirmation. You can modify or cancel anytime from your account dashboard.
                        </p>
                    </div>
                </div>
            </div>
        `;
    }

    // ============================================
    // EVENT HANDLERS
    // ============================================

    setupEventListeners() {
        document.addEventListener('click', (e) => {
            // Product selection
            const productCard = e.target.closest('.product-card');
            if (productCard && !e.target.closest('.quantity-btn')) {
                this.toggleProduct(productCard);
            }

            // Quantity buttons
            const quantityBtn = e.target.closest('.quantity-btn');
            if (quantityBtn) {
                const action = quantityBtn.dataset.action;
                const card = quantityBtn.closest('.product-card');
                this.updateQuantity(card, action);
            }

            // Frequency selection
            const frequencyOption = e.target.closest('.frequency-option');
            if (frequencyOption) {
                this.selectFrequency(frequencyOption);
            }

            // Day selection
            const dayBtn = e.target.closest('.day-btn');
            if (dayBtn) {
                this.selectDay(dayBtn);
            }

            // Address selection
            const addressCard = e.target.closest('.address-card');
            if (addressCard && !addressCard.classList.contains('add-new-address')) {
                this.selectAddress(addressCard);
            }

            // Payment method selection
            const paymentCard = e.target.closest('.payment-method-card');
            if (paymentCard) {
                this.selectPaymentMethod(paymentCard);
            }

            // Navigation buttons
            if (e.target.classList.contains('nav-btn-prev')) {
                this.previousStep();
            }
            if (e.target.classList.contains('nav-btn-next')) {
                this.nextStep();
            }
            if (e.target.classList.contains('nav-btn-submit')) {
                this.submitSubscription();
            }
        });

        // Time slot selection (dropdown)
        const timeSlotSelect = document.getElementById('time-slots-select');
        if (timeSlotSelect) {
            timeSlotSelect.addEventListener('change', (e) => {
                // Store time slot ID (integer) instead of string value
                this.selectedTimeSlot = e.target.value ? parseInt(e.target.value) : null;
                this.saveState();
            });
        }
    }

    toggleProduct(card) {
        const productId = parseInt(card.dataset.productId);

        if (card.classList.contains('selected')) {
            card.classList.remove('selected');
            this.selectedProducts.delete(productId);
        } else {
            card.classList.add('selected');
            const product = this.products.find(p => p.id === productId);
            const quantityDisplay = card.querySelector('.quantity-display');
            const quantity = parseInt(quantityDisplay.textContent);
            this.selectedProducts.set(productId, { product, quantity });
        }

        this.saveState();
    }

    updateQuantity(card, action) {
        const productId = parseInt(card.dataset.productId);
        const displayEl = card.querySelector('.quantity-display');
        let currentQty = parseInt(displayEl.textContent);

        if (action === 'increase') {
            currentQty++;
        } else if (action === 'decrease' && currentQty > 1) {
            currentQty--;
        }

        displayEl.textContent = currentQty;

        if (this.selectedProducts.has(productId)) {
            const item = this.selectedProducts.get(productId);
            item.quantity = currentQty;
            this.saveState();
        }
    }

    selectFrequency(option) {
        document.querySelectorAll('.frequency-option').forEach(el => el.classList.remove('selected'));
        option.classList.add('selected');
        this.selectedFrequency = option.dataset.frequency;

        // Show/hide day selector
        const daySelector = document.getElementById('day-selector');
        if (this.selectedFrequency === 'weekly' || this.selectedFrequency === 'biweekly') {
            daySelector.style.display = 'block';
        } else {
            daySelector.style.display = 'none';
            this.selectedDay = null;
        }

        this.saveState();
    }

    selectDay(btn) {
        document.querySelectorAll('.day-btn').forEach(el => el.classList.remove('selected'));
        btn.classList.add('selected');
        this.selectedDay = parseInt(btn.dataset.day);
        this.saveState();
    }

    selectTimeSlot(slot) {
        document.querySelectorAll('.time-slot-option').forEach(el => el.classList.remove('selected'));
        slot.classList.add('selected');
        this.selectedTimeSlot = slot.dataset.slot;
        this.saveState();
    }

    selectAddress(card) {
        const addressId = parseInt(card.dataset.addressId);
        this.selectedAddress = this.addresses.find(a => a.id === addressId);
        this.renderAddresses();
        this.saveState();
    }

    selectPaymentMethod(card) {
        this.selectedPaymentMethod = card.dataset.paymentMethod;
        this.renderPaymentMethods();
        this.saveState();
    }

    showAddressForm() {
        // TODO: Implement address form modal
        if (typeof showNotification === 'function') {
            showNotification('Address form will be implemented. For now, please add addresses from your profile page.', 'info');
        }
    }

    // ============================================
    // NAVIGATION
    // ============================================

    async nextStep() {
        // Validate current step
        if (!this.validateStep(this.currentStep)) {
            return;
        }

        // Check authentication for steps that require it
        // If user is on Step 2 (Schedule) and trying to go to Step 3 (Address)
        if (this.currentStep === 2 && !this.isAuthenticated) {

            // A. Fast-forward state to Step 3
            // This ensures when they return, 'restoreState' puts them on the Address step
            this.currentStep = 3;

            // B. Save the data (Products, Schedule, and the new Step 3 position)
            this.saveState();

            this.showError('Please sign in to continue');
            setTimeout(() => {
                window.location.href = '/login?redirect=' + encodeURIComponent(window.location.pathname);
            }, 1500);
            return;
        }

        if (this.currentStep < this.totalSteps) {
            this.currentStep++;

            // Load data for the next step
            await this.loadStepData(this.currentStep);

            this.showStep(this.currentStep, true); // Scroll when navigating to next step
            this.saveState();
        }
    }

    previousStep() {
        if (this.currentStep > 1) {
            this.currentStep--;
            this.showStep(this.currentStep, true); // Scroll when navigating to previous step
            this.saveState();
        }
    }

    async loadStepData(step) {
        try {
            if (step === 2) {
                await this.loadTimeSlots();
            } else if (step === 3) {
                await this.loadAddresses();
            } else if (step === 4) {
                await this.loadPaymentMethods();
            } else if (step === 5) {
                this.renderReview();
                // Attach event listener for subscription name input
                setTimeout(() => {
                    const nameInput = document.getElementById('subscription-name-input');
                    if (nameInput) {
                        nameInput.addEventListener('input', (e) => {
                            this.subscriptionName = e.target.value;
                            this.saveState();
                        });
                        // Add focus styling
                        nameInput.addEventListener('focus', (e) => {
                            e.target.style.borderColor = '#1890ff';
                            e.target.style.boxShadow = '0 0 0 2px rgba(24, 144, 255, 0.2)';
                        });
                        nameInput.addEventListener('blur', (e) => {
                            e.target.style.borderColor = '#d9d9d9';
                            e.target.style.boxShadow = 'none';
                        });
                    }
                }, 100);
            }
        } catch (error) {
            console.error(`Error loading data for step ${step}:`, error);
        }
    }

    showStep(stepNumber, shouldScroll = false) {
        // Hide all panels
        document.querySelectorAll('.constructor-panel').forEach(panel => {
            panel.classList.remove('active');
        });

        // Show current panel
        const currentPanel = document.getElementById(`step-${stepNumber}`);
        if (currentPanel) {
            currentPanel.classList.add('active');
        }

        // Update step indicators
        this.updateStepIndicators();

        // Update navigation buttons
        this.updateNavigation();

        // Only scroll to constructor when explicitly requested (e.g., when clicking Next/Previous)
        if (shouldScroll) {
            const constructorSection = document.getElementById('subscription-constructor');
            if (constructorSection) {
                const offsetTop = constructorSection.offsetTop - 100;
                window.scrollTo({ top: offsetTop, behavior: 'smooth' });
            }
        }
    }

    updateStepIndicators() {
        for (let i = 1; i <= this.totalSteps; i++) {
            const step = document.querySelector(`.constructor-step[data-step="${i}"]`);
            if (!step) continue;

            if (i < this.currentStep) {
                step.classList.add('completed');
                step.classList.remove('active');
            } else if (i === this.currentStep) {
                step.classList.add('active');
                step.classList.remove('completed');
            } else {
                step.classList.remove('active', 'completed');
            }
        }
    }

    updateNavigation() {
        const prevBtn = document.querySelector('.nav-btn-prev');
        const nextBtn = document.querySelector('.nav-btn-next');
        const submitBtn = document.querySelector('.nav-btn-submit');

        if (prevBtn) prevBtn.style.display = this.currentStep === 1 ? 'none' : 'block';
        if (nextBtn) nextBtn.style.display = this.currentStep === this.totalSteps ? 'none' : 'block';
        if (submitBtn) submitBtn.style.display = this.currentStep === this.totalSteps ? 'block' : 'none';
    }

    // ============================================
    // VALIDATION
    // ============================================

    validateStep(stepNumber) {
        switch (stepNumber) {
            case 1: // Products
                if (this.selectedProducts.size === 0) {
                    this.showError('Please select at least one product');
                    return false;
                }
                return true;

            case 2: // Schedule
                if (!this.selectedFrequency) {
                    this.showError('Please select a delivery frequency');
                    return false;
                }
                if ((this.selectedFrequency === 'weekly' || this.selectedFrequency === 'biweekly') && this.selectedDay === null) {
                    this.showError('Please select a delivery day');
                    return false;
                }
                if (!this.selectedTimeSlot) {
                    this.showError('Please select a time slot');
                    return false;
                }
                return true;

            case 3: // Address
                if (!this.selectedAddress) {
                    this.showError('Please select a delivery address');
                    return false;
                }
                return true;

            case 4: // Payment
                if (!this.selectedPaymentMethod) {
                    this.showError('Please select a payment method');
                    return false;
                }
                return true;

            case 5: // Review
                return true;

            default:
                return true;
        }
    }

    // ============================================
    // SUBMISSION
    // ============================================

    async submitSubscription() {
        // Final validation
        for (let i = 1; i <= this.totalSteps; i++) {
            if (!this.validateStep(i)) {
                this.currentStep = i;
                this.showStep(i);
                return;
            }
        }

        if (!this.isAuthenticated) {
            this.saveState();
            window.location.href = '/login?redirect=' + encodeURIComponent(window.location.pathname);
            return;
        }

        this.showLoading();

        try {
            // Use the subscription name (either user-edited or auto-generated)
            const finalName = this.subscriptionName || this.generateSubscriptionName();

            const subscriptionData = {
                name: finalName,
                description: `Custom subscription with ${this.selectedProducts.size} product(s)`,
                billing_cycle: this.selectedFrequency,
                delivery_frequency: this.selectedFrequency,
                delivery_day_of_week: this.selectedDay,
                delivery_time_slot_id: this.selectedTimeSlot,  // Now sends integer ID (or null)
                delivery_address_id: this.selectedAddress.id,
                payment_method: this.selectedPaymentMethod,
                auto_renew: true,
                discount_percentage: 0,
                items: Array.from(this.selectedProducts.values()).map(({ product, quantity }) => ({
                    product_id: product.id,
                    quantity: quantity
                }))
            };

            const response = await apiRequest('/subscriptions/', {
                method: 'POST',
                body: JSON.stringify(subscriptionData)
            });

            const result = await response.json();

            if (response.ok && result.success) {
                this.clearState();
                this.showSuccess('Subscription created successfully!');
                setTimeout(() => {
                    window.location.href = '/my-subscriptions';
                }, 2000);
            } else {
                throw new Error(result.message || 'Failed to create subscription');
            }
        } catch (error) {
            console.error('Error creating subscription:', error);
            this.showError(error.message || 'Failed to create subscription');
        } finally {
            this.hideLoading();
        }
    }

    // ============================================
    // UTILITIES
    // ============================================

    generateSubscriptionName() {
        /**
         * Generate a descriptive subscription name based on:
         * - Delivery frequency
         * - Products and quantities
         * - Address (optional)
         */
        const parts = [];

        // Add frequency
        if (this.selectedFrequency) {
            parts.push(this.getFrequencyName(this.selectedFrequency));
        }

        // Add products summary
        if (this.selectedProducts.size > 0) {
            const productNames = [];
            this.selectedProducts.forEach(({ product, quantity }) => {
                if (quantity > 1) {
                    productNames.push(`${quantity}x ${product.name}`);
                } else {
                    productNames.push(product.name);
                }
            });

            // If more than 2 products, summarize
            if (productNames.length > 2) {
                parts.push(`${productNames.slice(0, 2).join(', ')} +${productNames.length - 2} more`);
            } else {
                parts.push(productNames.join(', '));
            }
        }

        // Add address type if available
        if (this.selectedAddress && this.selectedAddress.title) {
            parts.push(`to ${this.selectedAddress.title}`);
        }

        return parts.join(' - ') || 'My Subscription';
    }

    getFrequencyName(frequency) {
        const names = {
            'daily': 'Daily',
            'weekly': 'Weekly',
            'biweekly': 'Bi-Weekly',
            'monthly': 'Monthly'
        };
        return names[frequency] || frequency;
    }

    getSelectedTimeSlotLabel() {
        if (!this.selectedTimeSlot) {
            return null;
        }
        const slot = this.timeSlots.find(s => s.id === this.selectedTimeSlot);
        return slot ? slot.label : null;
    }

    getDayName(dayNumber) {
        const days = {
            0: 'Sunday',
            1: 'Monday',
            2: 'Tuesday',
            3: 'Wednesday',
            4: 'Thursday',
            5: 'Friday',
            6: 'Saturday'
        };
        return days[dayNumber] || '';
    }

    formatPrice(price) {
        return parseFloat(price).toLocaleString('en-US', {
            minimumFractionDigits: 0,
            maximumFractionDigits: 0
        });
    }

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    showLoading() {
        const overlay = document.getElementById('loading-overlay');
        if (overlay) overlay.classList.add('active');
    }

    hideLoading() {
        const overlay = document.getElementById('loading-overlay');
        if (overlay) overlay.classList.remove('active');
    }

    showError(message) {
        // Use modern toast notification
        if (typeof showNotification === 'function') {
            showNotification(message, 'error');
        }
    }

    showSuccess(message) {
        if (typeof showNotification === 'function') {
            showNotification(message, 'success');
        }
    }
}

// Global reference for inline handlers
let subscriptionConstructor;

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', function () {
    if (document.getElementById('subscription-constructor')) {
        subscriptionConstructor = new SubscriptionConstructor();
    }
});
