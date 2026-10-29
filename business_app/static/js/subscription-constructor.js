/**
 * Subscription Constructor - Enhanced Version
 * Multi-step subscription creation with authentication flow
 * Steps: Products -> Schedule -> Address -> Payment -> Review
 */

// Everything the builder takes from the page, rendered by index.html (#subscription-constructor-data) in the
// visitor's language: its words (`i18n`), the four frequency names and the seven weekday names (0 = Monday).
const CONSTRUCTOR_PAGE = (typeof getPageData === 'function' && getPageData('subscription-constructor-data')) || {};
const CONSTRUCTOR_I18N = CONSTRUCTOR_PAGE.i18n || {};

// Fills the `{name}` holes of one of the page's sentences.
const constructorWords = (sentence, values) => String(sentence).replace(/\{(\w+)\}/g, (hole, name) => (name in values ? values[name] : hole));

class SubscriptionConstructor {
    constructor() {
        this.currentStep = 1;
        this.totalSteps = 5;

        // Subscription data
        this.selectedProducts = new Map(); // productId -> {product, quantity}
        this.selectedFrequency = null;
        this.selectedWeekday = null;      // backend convention: 0 = Monday (see weekdayFromJsDay)
        this.selectedDayOfMonth = null;   // 1-31, monthly only
        // null (not chosen), SubscriptionConstructor.ANY_TIME (no slot, any time of day), or a slot id.
        this.selectedTimeSlot = null;
        // Whether the visitor never chose that: it was held for them because the backend offered no slot.
        this.anyTimeAssumed = false;
        // The delivery days (0 = Monday) the last slot answer published; null until one arrived.
        this.deliveryWeekdays = null;
        this.selectedAddress = null;
        this.selectedPaymentMethod = null;
        this.subscriptionName = '';  // User-editable subscription name
        this.preview = null;         // the backend's quote for the review step (deliveries_per_month, monthly cost)

        // Available options
        this.products = [];
        this.timeSlots = [];
        // Whether `timeSlots` is the backend's answer for the schedule held now (a failed, pending or older request
        // is not: an empty list then does not mean "no slot offered"), whether that answer is still on its way,
        // and the latest slot question asked.
        this.timeSlotsAnswered = false;
        this.timeSlotsLoading = false;
        this.timeSlotsRequest = 0;
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

            // A restored schedule: step 2 shows it, and its slot list is asked, so step 2 and the review read the
            // backend's answer for it; every step from 3 to the restored one loads its data, as Next would have,
            // so the review names the address and method the POST sends.
            if (this.selectedFrequency) {
                this.syncScheduleControls();
                await this.loadTimeSlots();
            }
            for (let step = 3; step <= this.currentStep; step++) {
                await this.loadStepData(step);
            }

            // Update UI
            this.updateStepIndicators();
            this.showStep(this.currentStep);
        } catch (error) {
            console.error('Failed to initialize subscription constructor:', error);
            this.showError(CONSTRUCTOR_I18N.error_load_failed);
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
            selectedWeekday: this.selectedWeekday,
            selectedDayOfMonth: this.selectedDayOfMonth,
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
            this.selectedWeekday = state.selectedWeekday ?? null;
            this.selectedDayOfMonth = state.selectedDayOfMonth ?? null;
            // A state saved before this choice existed holds null: not chosen, asked again.
            this.selectedTimeSlot = state.selectedTimeSlot ?? null;
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
        this.selectedWeekday = null;
        this.selectedDayOfMonth = null;
        this.selectedTimeSlot = null;
        this.anyTimeAssumed = false;
        this.selectedAddress = null;
        this.selectedPaymentMethod = null;
        this.subscriptionName = '';
        this.preview = null;

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

    // The subscription catalog (spec 2026-10-26 B10): every product a subscription can take, unpaged, each with
    // the price label the subscription will use (never the products API's checkout price, another rule). It is
    // the same for every visitor, so it is asked without cookies (an expired session cookie would turn it into
    // a 401) and in the page's language.
    async loadProducts() {
        try {
            const language = document.documentElement.lang;
            const response = await fetch(`${API_BASE_URL}/subscriptions/products`, {
                method: 'GET',
                headers: language ? { 'Accept-Language': language } : {},
                credentials: 'omit'
            });

            if (!response.ok) throw new Error('Failed to fetch products');

            const result = await response.json();
            if (result.success && result.data) {
                this.products = result.data.items || [];
                // A basket restored from this tab's session keeps the rows it was saved with: take the
                // catalog's current row, and drop a product the catalog no longer lists.
                this.selectedProducts.forEach((entry, productId) => {
                    const current = this.products.find(p => p.id === productId);
                    if (current) entry.product = current;
                    else this.selectedProducts.delete(productId);
                });
                this.renderProducts();
            }
        } catch (error) {
            console.error('Error loading products:', error);
            throw error;
        }
    }

    // JS Date.getDay() counts from Sunday (0 = Sunday ... 6 = Saturday); the backend's
    // delivery_day_of_week counts from Monday (0 = Monday ... 6 = Sunday). The day buttons carry the JS
    // number in data-day, and this is the ONE place it becomes the backend's.
    static weekdayFromJsDay(jsDay) {
        return (jsDay + 6) % 7;
    }

    usesWeekday() {
        return this.selectedFrequency === 'weekly' || this.selectedFrequency === 'biweekly';
    }

    usesDayOfMonth() {
        return this.selectedFrequency === 'monthly';
    }

    // The slots a recurring delivery can pick: the ones offered on the chosen weekday (any weekday for daily
    // and monthly, or a weekday not chosen yet), filtered by the backend's own rule for the frequency, the
    // rule creation enforces (spec 2026-10-26 §8.2: monthly gets only the slots offered on every day). No date
    // exists yet, so no capacity or fee is shown: a subscription order never pays a slot fee.
    timeSlotsQuery() {
        const weekday = this.usesWeekday() && this.selectedWeekday !== null ? this.selectedWeekday : 'any';
        const query = `/delivery/time-slots?weekday=${weekday}`;
        return this.selectedFrequency ? `${query}&frequency=${this.selectedFrequency}` : query;
    }

    // The slots for the schedule held now. Only a 200 carrying a list, for the latest question, is the backend's
    // answer: an answer to an older question (the customer changed the day meanwhile) is dropped, and a failure
    // leaves the list unanswered, never "no slot offered" (validateStep asks again).
    async loadTimeSlots() {
        const request = (this.timeSlotsRequest || 0) + 1;
        this.timeSlotsRequest = request;
        this.timeSlotsAnswered = false;
        this.timeSlotsLoading = true;
        let offered = null;
        let deliveryWeekdays = null;
        try {
            const response = await apiRequest(this.timeSlotsQuery(), { method: 'GET' });
            const data = await response.json();
            if (response.ok && data && Array.isArray(data.time_slots)) {
                offered = data.time_slots;
                deliveryWeekdays = Array.isArray(data.delivery_weekdays) ? data.delivery_weekdays : null;
            }
        } catch (error) {
            console.error('Error loading time slots:', error);
        }
        if (request !== this.timeSlotsRequest) return;
        this.timeSlotsLoading = false;
        if (offered === null) {
            // No invented slot ids: a made-up id would be refused at creation.
            this.timeSlots = [];
            this.showError(CONSTRUCTOR_I18N.error_slots_failed);
            this.renderTimeSlots();
            return;
        }
        this.timeSlots = offered.map(slot => ({
            id: slot.id,
            label: `${slot.start_time} - ${slot.end_time}`,
            value: slot.time_range || `${slot.start_time}-${slot.end_time}`
        }));
        this.timeSlotsAnswered = true;
        this.reconcileTimeChoice();
        this.deliveryWeekdays = deliveryWeekdays;
        // The delivery days are the backend's answer; a weekday held that is not one any more is asked for again.
        if (deliveryWeekdays && this.selectedWeekday !== null && !deliveryWeekdays.includes(this.selectedWeekday)) {
            this.selectedWeekday = null;
            this.saveState();
        }
        this.syncScheduleControls();
        this.renderTimeSlots();
    }

    // The time held against the slots the backend just offered. any-time is always valid, so it stays, except when
    // it was only held because no slot was offered and slots are offered now (the visitor never chose it). With
    // no slot offered, any-time is the only choice and is held. A slot the new schedule does not offer is dropped:
    // the visitor chooses again, and nothing is picked for them.
    reconcileTimeChoice() {
        const any = SubscriptionConstructor.ANY_TIME;
        const before = this.selectedTimeSlot;
        if (this.timeSlots.length === 0) {
            this.selectedTimeSlot = any;
            this.anyTimeAssumed = before !== any || this.anyTimeAssumed;
        } else if (this.selectedTimeSlot === any) {
            if (this.anyTimeAssumed) this.selectedTimeSlot = null;
        } else if (this.selectedTimeSlot !== null && !this.timeSlots.some(slot => slot.id === this.selectedTimeSlot)) {
            this.selectedTimeSlot = null;
        }
        if (this.selectedTimeSlot !== any) this.anyTimeAssumed = false;
        if (this.selectedTimeSlot !== before) this.saveState();
    }

    // The visitor's pick in the time list: '' (the placeholder), 'any' or a slot id.
    static parseSlotChoice(value) {
        if (value === SubscriptionConstructor.ANY_TIME) return SubscriptionConstructor.ANY_TIME;
        const id = parseInt(value, 10);
        return Number.isNaN(id) ? null : id;
    }

    keepTimeSlot(event) {
        this.selectedTimeSlot = SubscriptionConstructor.parseSlotChoice(event.target.value);
        this.anyTimeAssumed = false;
        this.saveState();
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

    // The menu for the chosen address (step 3 comes before this step): creation accepts only a method the
    // backend offers for that address, so the menu is asked the same question (the cash limit's place arm).
    paymentMethodsQuery() {
        const query = '/payments/methods?context=subscription';
        return this.selectedAddress ? `${query}&delivery_address_id=${this.selectedAddress.id}` : query;
    }

    async loadPaymentMethods() {
        // The ways to pay come from the backend SSOT so this menu can never
        // diverge from checkout. Payme is not offered; `click` is labelled "Card".
        const ICONS = { cash: '💵', click: '💳', business_account: '🏢' };
        try {
            const response = await apiRequest(this.paymentMethodsQuery());
            if (!response.ok) throw new Error('payment methods refused');
            const payload = await response.json();
            const methods = (payload.data && payload.data.available_methods) || [];
            this.paymentMethods = methods.map((m) => ({
                id: m.method,
                name: m.display_name,
                icon: ICONS[m.method] || '💳',
                description: m.description || ''
            }));
        } catch (error) {
            // No method of our own: one the backend did not offer would be refused at creation.
            console.error('Error loading payment methods:', error);
            this.paymentMethods = [];
            this.showError(CONSTRUCTOR_I18N.error_methods_failed);
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
            const minQty = this.minOrderQuantity(product.id);
            // A quantity restored from sessionStorage may predate the minimum.
            const quantity = Math.max(isSelected ? this.selectedProducts.get(product.id).quantity : minQty, minQty);
            if (isSelected) this.selectedProducts.get(product.id).quantity = quantity;

            const imageUrl = product.images?.[0] || '/static/images/product-placeholder.svg';

            // The price is the catalog's label, the one the subscription will use.
            return `
                <div class="product-card ${isSelected ? 'selected' : ''}" data-product-id="${product.id}">
                    <img src="${imageUrl}"
                         alt="${this.escapeHtml(product.product_name)}"
                         class="product-image">
                    <div class="product-name">${this.escapeHtml(product.product_name)}</div>
                    <div class="product-price">${this.escapeHtml(product.unit_price_label || '')}</div>
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

        // any-time is always a choice, next to the slots and never chosen for the visitor. With none offered it is the
        // only choice. A list the backend has not answered (failed, or still on its way) offers only the placeholder.
        const any = SubscriptionConstructor.ANY_TIME;
        const anyOption = `<option value="${any}">${this.escapeHtml(CONSTRUCTOR_I18N.any_time)}</option>`;
        const placeholder = `<option value="">${this.escapeHtml(CONSTRUCTOR_I18N.select_time_slot)}</option>`;
        const slotOptions = this.timeSlots.map(slot => `<option value="${slot.id}">${this.escapeHtml(slot.label)}</option>`);
        if (!this.timeSlotsAnswered) {
            select.innerHTML = placeholder;
        } else if (this.timeSlots.length === 0) {
            select.innerHTML = anyOption;
        } else {
            select.innerHTML = [placeholder, anyOption, ...slotOptions].join('');
        }

        // Show what is held (the placeholder when nothing is)
        select.value = this.selectedTimeSlot === null ? '' : String(this.selectedTimeSlot);

        // Setup change listener if not already done
        // 3. Update Nice Select and Attach jQuery Listener
        if (typeof $ !== 'undefined' && $(select).next().hasClass('nice-select')) {
            // Refresh the visual dropdown to show new options
            $(select).niceSelect('update');

            // CRITICAL FIX: Use jQuery .on() to catch the event from Nice Select
            // We use .off() first to ensure we don't add multiple listeners if this runs twice
            $(select).off('change.subscription').on('change.subscription', (e) => this.keepTimeSlot(e));
        } else {
            // Fallback for vanilla JS if jQuery fails to load
            if (!select.dataset.listenerAttached) {
                select.addEventListener('change', (e) => this.keepTimeSlot(e));
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
                    <h3 style="margin: 0 0 8px 0; color: #262626;">${this.escapeHtml(CONSTRUCTOR_I18N.address_none_title)}</h3>
                    <p style="color: #8c8c8c; margin: 0 0 20px 0;">${this.escapeHtml(CONSTRUCTOR_I18N.address_none_body)}</p>
                    <button type="button" class="theme-btn btn-one" onclick="subscriptionConstructor.showAddressForm()">
                        ${this.escapeHtml(CONSTRUCTOR_I18N.address_add)}
                    </button>
                </div>
            `;
            return;
        }

        const addressesHTML = this.addresses.map(address => `
            <div class="address-card ${this.selectedAddress?.id === address.id ? 'selected' : ''}"
                 data-address-id="${address.id}">
                <div class="address-header">
                    <div class="address-type">${this.escapeHtml(address.title || '')}</div>
                    ${this.selectedAddress?.id === address.id ? '<div class="selected-badge">✓</div>' : ''}
                </div>
                <div class="address-details">
                    <div class="address-line">${this.escapeHtml(address.full_address || address.street_address)}</div>
                    ${address.apartment_number ? `<div class="address-line">${this.escapeHtml(this.apartmentLabel(address))}</div>` : ''}
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
                    <div style="font-weight: 500; color: #1890ff;">${this.escapeHtml(CONSTRUCTOR_I18N.address_add)}</div>
                </div>
            </div>
        `;
    }

    apartmentLabel(address) {
        return constructorWords(CONSTRUCTOR_I18N.address_apartment, { number: address.apartment_number });
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

        // Every figure is the backend's create preview (loadPreview), at the subscription's one price rule
        // (spec 2026-10-26 B10): each line's labels, the per-delivery amount and the monthly estimate. Nothing is
        // multiplied or summed here. Without a preview (not signed in) no money is shown.
        const quote = this.preview;
        const quoteLines = (quote && quote.items) || [];
        const productsList = Array.from(this.selectedProducts.values()).map(({ product, quantity }) => {
            const line = quoteLines.find(item => item.product_id === product.id) || {};
            return {
                name: product.product_name,
                quantity: quantity,
                priceLabel: line.unit_price_label || '',
                totalLabel: line.line_total_label || ''
            };
        });

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
                        <h3 style="margin: 0; font-size: 18px; font-weight: 600; color: #262626;">${this.escapeHtml(CONSTRUCTOR_I18N.review_name_title)}</h3>
                    </div>
                    <div>
                        <label for="subscription-name-input" style="display: block; margin-bottom: 8px; font-size: 14px; color: #595959; font-weight: 500;">
                            ${this.escapeHtml(CONSTRUCTOR_I18N.review_name_label)}
                        </label>
                        <input
                            type="text"
                            id="subscription-name-input"
                            value="${this.escapeHtml(this.subscriptionName)}"
                            maxlength="200"
                            style="width: 100%; padding: 12px 16px; border: 2px solid #d9d9d9; border-radius: 8px; font-size: 16px; font-weight: 500; color: #262626; transition: all 0.3s ease;"
                            placeholder="${this.escapeHtml(CONSTRUCTOR_I18N.review_name_placeholder)}"
                        />
                        <div style="margin-top: 8px; font-size: 13px; color: #8c8c8c;">
                            <span style="margin-right: 8px;">💡</span>
                            ${this.escapeHtml(CONSTRUCTOR_I18N.review_name_hint)}
                        </div>
                    </div>
                </div>

                <!-- Selected Products Card -->
                <div style="background: white; border-radius: 12px; padding: 24px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); border: 1px solid #e8e8e8;">
                    <div style="display: flex; align-items: center; margin-bottom: 20px; padding-bottom: 16px; border-bottom: 2px solid #f0f0f0;">
                        <div style="width: 40px; height: 40px; background: linear-gradient(135deg, #1890ff 0%, #096dd9 100%); border-radius: 10px; display: flex; align-items: center; justify-content: center; margin-right: 12px;">
                            <span style="color: white; font-size: 20px;">🛒</span>
                        </div>
                        <h3 style="margin: 0; font-size: 18px; font-weight: 600; color: #262626;">${this.escapeHtml(CONSTRUCTOR_I18N.review_products_title)}</h3>
                    </div>
                    <div>
                        ${productsList.map((item, index) => `
                            <div style="display: flex; justify-content: space-between; align-items: center; padding: 16px; background: #fafafa; border-radius: 8px; margin-bottom: ${index < productsList.length - 1 ? '12px' : '0'};">
                                <div style="flex: 1;">
                                    <div style="font-size: 15px; font-weight: 500; color: #262626; margin-bottom: 6px;">
                                        ${this.escapeHtml(item.name)}
                                    </div>
                                    <div style="display: flex; align-items: center; gap: 8px;">
                                        <span style="font-size: 13px; color: #8c8c8c;" data-review="line-price">
                                            ${this.escapeHtml(item.priceLabel)}
                                        </span>
                                        <span style="color: #d9d9d9;">×</span>
                                        <span style="font-size: 13px; font-weight: 500; color: #595959; background: white; padding: 2px 8px; border-radius: 4px;">
                                            ${this.escapeHtml(constructorWords(CONSTRUCTOR_I18N.review_quantity, { quantity: item.quantity }))}
                                        </span>
                                    </div>
                                </div>
                                <div style="font-size: 16px; font-weight: 600; color: #52c41a; margin-left: 16px;" data-review="line-total">
                                    ${this.escapeHtml(item.totalLabel)}
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
                        <h3 style="margin: 0; font-size: 18px; font-weight: 600; color: #262626;">${this.escapeHtml(CONSTRUCTOR_I18N.review_schedule_title)}</h3>
                    </div>
                    <div style="background: #f6ffed; border: 1px solid #b7eb8f; border-radius: 8px; padding: 16px;">
                        <div style="display: grid; gap: 12px;">
                            <div style="display: flex; justify-content: space-between; align-items: center;">
                                <span style="font-size: 14px; color: #595959; font-weight: 500;">${this.escapeHtml(CONSTRUCTOR_I18N.review_frequency)}</span>
                                <span style="font-size: 15px; font-weight: 600; color: #262626;">${this.escapeHtml(this.getFrequencyName(this.selectedFrequency))}</span>
                            </div>
                            ${this.deliveryDayLabel() ? `
                                <div style="display: flex; justify-content: space-between; align-items: center; padding-top: 12px; border-top: 1px solid #d9f7be;">
                                    <span style="font-size: 14px; color: #595959; font-weight: 500;">${this.escapeHtml(CONSTRUCTOR_I18N.review_day)}</span>
                                    <span style="font-size: 15px; font-weight: 600; color: #262626;">${this.escapeHtml(this.deliveryDayLabel())}</span>
                                </div>
                            ` : ''}
                            <div style="display: flex; justify-content: space-between; align-items: center; padding-top: 12px; border-top: 1px solid #d9f7be;">
                                <span style="font-size: 14px; color: #595959; font-weight: 500;">${this.escapeHtml(CONSTRUCTOR_I18N.review_time)}</span>
                                <span style="font-size: 15px; font-weight: 600; color: #262626;" data-review="time-slot">${this.escapeHtml(this.timeSlotReviewLabel())}</span>
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
                        <h3 style="margin: 0; font-size: 18px; font-weight: 600; color: #262626;">${this.escapeHtml(CONSTRUCTOR_I18N.review_address_title)}</h3>
                    </div>
                    <div style="background: #fff7e6; border: 1px solid #ffd591; border-radius: 8px; padding: 16px;">
                        <div style="font-weight: 500; color: #262626; margin-bottom: 8px;">${this.escapeHtml(address?.title || '')}</div>
                        <div style="color: #595959; line-height: 1.6;">
                            ${this.escapeHtml(address?.full_address || '')}
                            ${address?.apartment_number ? `<br>${this.escapeHtml(this.apartmentLabel(address))}` : ''}
                        </div>
                    </div>
                </div>

                <!-- Payment Method Card -->
                <div style="background: white; border-radius: 12px; padding: 24px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); border: 1px solid #e8e8e8;">
                    <div style="display: flex; align-items: center; margin-bottom: 20px; padding-bottom: 16px; border-bottom: 2px solid #f0f0f0;">
                        <div style="width: 40px; height: 40px; background: linear-gradient(135deg, #13c2c2 0%, #08979c 100%); border-radius: 10px; display: flex; align-items: center; justify-content: center; margin-right: 12px;">
                            <span style="color: white; font-size: 20px;">💳</span>
                        </div>
                        <h3 style="margin: 0; font-size: 18px; font-weight: 600; color: #262626;">${this.escapeHtml(CONSTRUCTOR_I18N.review_payment_title)}</h3>
                    </div>
                    <div style="background: #e6fffb; border: 1px solid #87e8de; border-radius: 8px; padding: 16px; display: flex; align-items: center; gap: 12px;">
                        <div style="font-size: 32px;">${paymentMethod?.icon || '💳'}</div>
                        <div>
                            <div style="font-weight: 600; color: #262626;">${paymentMethod?.name || ''}</div>
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
                        <h3 style="margin: 0; font-size: 18px; font-weight: 600; color: #262626;">${this.escapeHtml(CONSTRUCTOR_I18N.review_cost_title)}</h3>
                    </div>

                    <!-- Per Delivery Total -->
                    <div style="background: #fafafa; border-radius: 8px; padding: 16px; margin-bottom: 16px;">
                        <div style="display: flex; justify-content: space-between; align-items: center;">
                            <div>
                                <div style="font-size: 14px; color: #8c8c8c; margin-bottom: 4px;">${this.escapeHtml(CONSTRUCTOR_I18N.review_per_delivery)}</div>
                                <div style="font-size: 13px; color: #bfbfbf;">${this.escapeHtml(CONSTRUCTOR_I18N.review_per_delivery_hint)}</div>
                            </div>
                            <div style="font-size: 22px; font-weight: 700; color: #262626;" data-review="per-delivery">
                                ${quote ? this.escapeHtml(quote.total_amount_label) : '&mdash;'}
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
                                    <div style="font-size: 15px; color: rgba(255,255,255,0.9); margin-bottom: 6px; font-weight: 500;">${this.escapeHtml(CONSTRUCTOR_I18N.review_per_month)}</div>
                                    <div style="font-size: 12px; color: rgba(255,255,255,0.7);">
                                        ${quote ? this.escapeHtml(quote.deliveries_per_month_label) : this.escapeHtml(CONSTRUCTOR_I18N.preview_after_sign_in)}
                                    </div>
                                </div>
                                <div style="font-size: 28px; font-weight: 700; color: white; text-align: right;" data-review="per-month">
                                    ${quote ? this.escapeHtml(quote.estimated_monthly_cost_label) : '&mdash;'}
                                </div>
                            </div>
                        </div>
                    </div>

                    <!-- Info Note -->
                    <div style="margin-top: 16px; padding: 12px; background: #e6f7ff; border: 1px solid #91d5ff; border-radius: 6px; display: flex; align-items: start; gap: 8px;">
                        <span style="color: #1890ff; font-size: 16px; flex-shrink: 0;">ℹ️</span>
                        <p style="margin: 0; font-size: 13px; color: #595959; line-height: 1.6;">
                            ${this.escapeHtml(CONSTRUCTOR_I18N.review_note)}
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

            // Way-to-pay selection
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

        // Day of the month (monthly). The theme turns this <select> into a nice-select, which reports the pick
        // with jQuery's .trigger('change'): only a jQuery handler hears it (a native listener never did, so the
        // day was never kept). jQuery's .on() hears a native change too.
        const monthDaySelect = document.getElementById('month-day-select');
        if (monthDaySelect) {
            const keepMonthDay = (e) => {
                this.selectedDayOfMonth = e.target.value ? parseInt(e.target.value, 10) : null;
                this.saveState();
            };
            if (typeof $ !== 'undefined') {
                $(monthDaySelect).off('change.subscription').on('change.subscription', keepMonthDay);
            } else {
                monthDaySelect.addEventListener('change', keepMonthDay);
            }
        }

        // Time slot selection (dropdown)
        const timeSlotSelect = document.getElementById('time-slots-select');
        if (timeSlotSelect) {
            timeSlotSelect.addEventListener('change', (e) => this.keepTimeSlot(e));
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
        } else if (action === 'decrease' && currentQty > this.minOrderQuantity(productId)) {
            currentQty--;
        }

        displayEl.textContent = currentQty;

        if (this.selectedProducts.has(productId)) {
            const item = this.selectedProducts.get(productId);
            item.quantity = currentQty;
            this.saveState();
        }
    }

    // The product's minimum order quantity, as the subscription catalog publishes it. A
    // subscription line below it is one billing's create_order refuses every cycle.
    minOrderQuantity(productId) {
        const product = this.products.find(p => p.id === productId);
        return parseInt(product?.min_order_quantity, 10) || 1;
    }

    selectFrequency(option) {
        this.selectedFrequency = option.dataset.frequency;
        // Forget the day this frequency does not use
        if (!this.usesWeekday()) this.selectedWeekday = null;
        if (!this.usesDayOfMonth()) this.selectedDayOfMonth = null;
        this.syncScheduleControls();

        this.saveState();
        // The slots offered depend on the weekday
        return this.loadTimeSlots();
    }

    // Step 2 shows exactly the schedule the builder holds (a restored one too), so it never shows a day that is not
    // held, nor hides one that is: the frequency, the day chooser it uses, the weekday button, the day of the month.
    syncScheduleControls() {
        document.querySelectorAll('.frequency-option').forEach(el => {
            el.classList.toggle('selected', el.dataset.frequency === this.selectedFrequency);
        });
        const daySelector = document.getElementById('day-selector');
        const monthDaySelector = document.getElementById('month-day-selector');
        if (daySelector) daySelector.style.display = this.usesWeekday() ? 'block' : 'none';
        if (monthDaySelector) monthDaySelector.style.display = this.usesDayOfMonth() ? 'block' : 'none';
        document.querySelectorAll('.day-btn').forEach(el => {
            const weekday = SubscriptionConstructor.weekdayFromJsDay(parseInt(el.dataset.day, 10));
            el.classList.toggle('selected', this.selectedWeekday !== null && weekday === this.selectedWeekday);
            // Only the delivery days the backend published are offered (spec 2026-10-27 §6.4).
            el.style.display = this.deliveryWeekdays && !this.deliveryWeekdays.includes(weekday) ? 'none' : '';
        });
        this.showMonthDay(this.selectedDayOfMonth);
    }

    // The day-of-month <select> on `day` (its placeholder for none), and, under the theme, its nice-select, which
    // shows the select's value only once told to redraw ('update').
    showMonthDay(day) {
        const select = document.getElementById('month-day-select');
        if (!select) return;
        select.value = day === null || day === undefined ? '' : String(day);
        if (typeof $ !== 'undefined' && $(select).next().hasClass('nice-select')) {
            $(select).niceSelect('update');
        }
    }

    selectDay(btn) {
        document.querySelectorAll('.day-btn').forEach(el => el.classList.remove('selected'));
        btn.classList.add('selected');
        this.selectedWeekday = SubscriptionConstructor.weekdayFromJsDay(parseInt(btn.dataset.day, 10));
        this.saveState();
        return this.loadTimeSlots();
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
        // No address form here: addresses are added in My addresses.
        if (typeof showNotification === 'function') {
            showNotification(CONSTRUCTOR_I18N.address_add_elsewhere, 'info');
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

            this.showError(CONSTRUCTOR_I18N.error_sign_in);
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
                await this.loadPreview();
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
                    this.showError(CONSTRUCTOR_I18N.error_products);
                    return false;
                }
                return true;

            case 2: // Schedule
                if (!this.selectedFrequency) {
                    this.showError(CONSTRUCTOR_I18N.error_frequency);
                    return false;
                }
                if (this.usesWeekday() && this.selectedWeekday === null) {
                    this.showError(CONSTRUCTOR_I18N.error_day);
                    return false;
                }
                if (this.usesDayOfMonth() && this.selectedDayOfMonth === null) {
                    this.showError(CONSTRUCTOR_I18N.select_day_of_month);
                    return false;
                }
                // The slot list must be the backend's answer for this schedule: one still on its way, or one that
                // failed, is not "no slot offered". Stay until it arrives; after a failure, say so and ask again.
                if (!this.timeSlotsAnswered) {
                    if (!this.timeSlotsLoading) {
                        this.showError(CONSTRUCTOR_I18N.error_slots_failed);
                        this.loadTimeSlots();
                    }
                    return false;
                }
                // A time is always chosen: a slot, or any-time (held for the visitor only when no slot is offered).
                if (this.selectedTimeSlot === null) {
                    this.showError(CONSTRUCTOR_I18N.error_time);
                    return false;
                }
                return true;

            case 3: // Address
                if (!this.selectedAddress) {
                    this.showError(CONSTRUCTOR_I18N.error_address);
                    return false;
                }
                return true;

            case 4: // Payment
                if (!this.selectedPaymentMethod) {
                    this.showError(CONSTRUCTOR_I18N.error_payment);
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

            const subscriptionData = this.buildSubscriptionBody(finalName);

            const response = await apiRequest('/subscriptions/', {
                method: 'POST',
                body: JSON.stringify(subscriptionData)
            });

            const result = await response.json();

            if (response.ok && result.success) {
                this.clearState();
                this.showSuccess(CONSTRUCTOR_I18N.created);
                setTimeout(() => {
                    window.location.href = '/my-subscriptions';
                }, 2000);
            } else {
                // Only a backend answer's own sentence (it is in the page language) is shown as it is.
                this.showError(result.message || CONSTRUCTOR_I18N.error_create_failed);
            }
        } catch (error) {
            // A network failure, a non-JSON 5xx page or an expired session carries English or technical text.
            console.error('Error creating subscription:', error);
            this.showError(CONSTRUCTOR_I18N.error_create_failed);
        } finally {
            this.hideLoading();
        }
    }

    // The exact body POST /subscriptions/ gets: ONE `frequency`, the day that frequency uses
    // (delivery_day_of_week 0 = Monday, or delivery_day_of_month), and the chosen slot, if any (none for any-time).
    buildSubscriptionBody(name) {
        const body = {
            name: name,
            frequency: this.selectedFrequency,
            delivery_address_id: this.selectedAddress.id,
            payment_method: this.selectedPaymentMethod,
            auto_renew: true,
            items: Array.from(this.selectedProducts.values()).map(({ product, quantity }) => ({
                product_id: product.id,
                quantity: quantity
            }))
        };
        // any-time is no slot: the key is left out.
        if (typeof this.selectedTimeSlot === 'number') body.delivery_time_slot_id = this.selectedTimeSlot;
        if (this.usesWeekday()) body.delivery_day_of_week = this.selectedWeekday;
        if (this.usesDayOfMonth()) body.delivery_day_of_month = this.selectedDayOfMonth;
        return body;
    }

    // The backend's quote for this basket on this frequency and slot (deliveries per month, monthly cost): the slot
    // is asked too, because a daily delivery counts only its slot's days, as the saved subscription will. It runs
    // each time the review step is opened, so a slot changed on step 2 is always the slot quoted.
    // It needs a signed-in customer; without one the review shows no estimate.
    async loadPreview() {
        this.preview = null;
        if (!this.isAuthenticated) return;
        try {
            const response = await apiRequest('/subscriptions/preview', {
                method: 'POST',
                body: JSON.stringify({
                    frequency: this.selectedFrequency,
                    ...(typeof this.selectedTimeSlot === 'number' ? { delivery_time_slot_id: this.selectedTimeSlot } : {}),
                    items: Array.from(this.selectedProducts.values()).map(({ product, quantity }) => ({
                        product_id: product.id,
                        quantity: quantity
                    }))
                })
            });
            const result = await response.json();
            if (response.ok && result.success && result.data && result.data.preview) {
                this.preview = result.data.preview;
            }
        } catch (error) {
            console.error('Error loading subscription preview:', error);
        }
    }

    deliveryDayLabel() {
        if (this.usesWeekday() && this.selectedWeekday !== null) return this.getDayName(this.selectedWeekday);
        if (this.usesDayOfMonth() && this.selectedDayOfMonth !== null) {
            return String(CONSTRUCTOR_I18N.day_of_month).replace('{day}', this.selectedDayOfMonth);
        }
        return '';
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
                    productNames.push(`${quantity}x ${product.product_name}`);
                } else {
                    productNames.push(product.product_name);
                }
            });

            // If more than 2 products, summarize
            if (productNames.length > 2) {
                const more = constructorWords(CONSTRUCTOR_I18N.name_more, { count: productNames.length - 2 });
                parts.push(`${productNames.slice(0, 2).join(', ')} ${more}`);
            } else {
                parts.push(productNames.join(', '));
            }
        }

        // Add address type if available
        if (this.selectedAddress && this.selectedAddress.title) {
            parts.push(constructorWords(CONSTRUCTOR_I18N.name_to, { place: this.selectedAddress.title }));
        }

        return parts.join(' - ') || CONSTRUCTOR_I18N.name_default;
    }

    // The page's name for the frequency (rendered in the visitor's language).
    getFrequencyName(frequency) {
        return (CONSTRUCTOR_PAGE.frequency_names || {})[frequency] || frequency;
    }

    // The review's time row: the chosen slot as the backend's list names it, or any-time. A chosen slot the list
    // cannot name (it has not loaded) is left blank, never called any-time while the POST sends it.
    timeSlotReviewLabel() {
        if (this.selectedTimeSlot === SubscriptionConstructor.ANY_TIME) return CONSTRUCTOR_I18N.any_time;
        const slot = this.timeSlots.find(s => s.id === this.selectedTimeSlot);
        return slot ? slot.label : '';
    }

    // Takes the backend's weekday (0 = Monday); the names are the page's, in the visitor's language.
    getDayName(weekday) {
        return (CONSTRUCTOR_PAGE.weekday_names || [])[weekday] || '';
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

// The time choice that names no slot ("any-time"). A slot is its id, a number; not chosen is null.
SubscriptionConstructor.ANY_TIME = 'any';

// Global reference for inline handlers
let subscriptionConstructor;

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', function () {
    if (document.getElementById('subscription-constructor')) {
        subscriptionConstructor = new SubscriptionConstructor();
    }
});
