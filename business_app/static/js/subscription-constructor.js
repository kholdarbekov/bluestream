/**
 * Subscription Constructor - Enhanced Version
 * Multi-step subscription creation with authentication flow
 * Steps: Products -> Schedule -> Address -> Payment -> Review
 */

// Everything the builder takes from the page, rendered by index.html (#subscription-constructor-data) in the
// visitor's language: its words (`i18n`), the stepper and quantity labels (`ui`), each way to pay's label and note
// (`method_labels`, `method_notes`), where addresses are added (`addresses_url`), the four frequency names and the
// seven weekday names (0 = Monday).
const CONSTRUCTOR_PAGE = (typeof getPageData === 'function' && getPageData('subscription-constructor-data')) || {};
const CONSTRUCTOR_I18N = CONSTRUCTOR_PAGE.i18n || {};
const CONSTRUCTOR_UI = CONSTRUCTOR_PAGE.ui || {};

// Fills the `{name}` holes of one of the page's sentences.
const constructorWords = (sentence, values) => String(sentence).replace(/\{(\w+)\}/g, (hole, name) => (name in values ? values[name] : hole));

// The icon of each way to pay (Font Awesome); any other method gets the card.
const PAYMENT_ICONS = { cash: 'far fa-money-bill-wave', click: 'far fa-credit-card', business_account: 'far fa-building' };
const PRODUCT_PLACEHOLDER = '/static/images/product-placeholder.svg';

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

            // The controls answer from the start, whatever the catalog does: Next says what is missing while the
            // water list loads, and a failed list offers its own retry.
            this.setupEventListeners();

            // The water list. Until it answers, two placeholder cards hold its place; if it fails, the list says so
            // with a retry and the builder waits on step 1 (nothing after it means anything without the catalog).
            if (!(await this.loadCatalog())) {
                this.currentStep = 1;
                this.updateStepIndicators();
                this.showStep(1);
                return;
            }

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
            } else {
                throw new Error('The subscription catalog answered without a list');
            }
        } catch (error) {
            console.error('Error loading products:', error);
            throw error;
        }
    }

    // The catalog with its two visible states: placeholder cards while it is asked, and on failure a message with a
    // "Try again" button that asks again. True once the list is shown.
    async loadCatalog() {
        const container = document.getElementById('products-grid');
        if (container) {
            container.setAttribute('aria-busy', 'true');
            if (!this.products.length) this.renderProductsLoading();
        }
        try {
            await this.loadProducts();
            return true;
        } catch (error) {
            this.renderProductsFailed();
            return false;
        } finally {
            if (container) container.removeAttribute('aria-busy');
        }
    }

    renderProductsLoading() {
        const container = document.getElementById('products-grid');
        if (!container) return;
        const placeholder = `
            <div class="sc-product-skeleton" aria-hidden="true">
                <span class="ae-skeleton sc-product-skeleton__img"></span>
                <span class="ae-skeleton ae-skeleton--text sc-product-skeleton__name"></span>
                <span class="ae-skeleton ae-skeleton--title sc-product-skeleton__price"></span>
            </div>`;
        container.innerHTML = placeholder + placeholder;
    }

    renderProductsFailed() {
        const container = document.getElementById('products-grid');
        if (!container) return;
        container.innerHTML = `
            <div class="ae-alert ae-alert--danger sc-catalog-failed" role="alert">
                <i class="far fa-exclamation-circle ae-alert__icon" aria-hidden="true"></i>
                <p class="sc-catalog-failed__text">${this.escapeHtml(CONSTRUCTOR_UI.catalog_failed || CONSTRUCTOR_I18N.error_load_failed || '')}</p>
                <button type="button" class="ae-btn ae-btn--secondary ae-btn--sm sc-catalog-retry">${this.escapeHtml(CONSTRUCTOR_UI.retry || '')}</button>
            </div>
        `;
    }

    // "Try again" on a failed list: ask the catalog once more.
    async retryCatalog() {
        this.clearInlineError();
        await this.loadCatalog();
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
            label: `${slot.start_time}–${slot.end_time}`,
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
        this.clearInlineError();
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
        // The ways to pay come from the backend SSOT so this menu can never diverge from checkout. Each is named
        // with the page's label for that method in the visitor's language (the backend's display_name is English),
        // the backend's name only for a method the page has no label for.
        const labels = CONSTRUCTOR_PAGE.method_labels || {};
        const notes = CONSTRUCTOR_PAGE.method_notes || {};
        try {
            const response = await apiRequest(this.paymentMethodsQuery());
            if (!response.ok) throw new Error('payment methods refused');
            const payload = await response.json();
            const methods = (payload.data && payload.data.available_methods) || [];
            this.paymentMethods = methods.map((m) => ({
                id: m.method,
                name: labels[m.method] || m.display_name || m.method,
                icon: PAYMENT_ICONS[m.method] || 'far fa-credit-card',
                description: notes[m.method] || ''
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

            const imageUrl = product.images?.[0] || PRODUCT_PLACEHOLDER;
            const name = this.escapeHtml(product.product_name);
            const hint = minQty > 1 && CONSTRUCTOR_UI.min_quantity
                ? `<span class="ae-qty__hint">${this.escapeHtml(constructorWords(CONSTRUCTOR_UI.min_quantity, { count: minQty }))}</span>`
                : '';

            // The card picks the product (a button, so a keyboard can too); the quantity works once it is picked.
            // The price is the catalog's label, the one the subscription will use.
            return `
                <div class="product-card ae-radio-card ${isSelected ? 'selected' : ''}" data-product-id="${product.id}">
                    <button type="button" class="product-card__pick" aria-pressed="${isSelected}">
                        <img src="${this.escapeHtml(imageUrl)}" alt="" class="product-image" width="160" height="160"
                             loading="lazy" decoding="async" data-fallback="${PRODUCT_PLACEHOLDER}">
                        <span class="product-name">${name}</span>
                        <span class="product-price">${this.escapeHtml(product.unit_price_label || '')}</span>
                    </button>
                    <div class="product-card__qty">
                        <div class="quantity-control ae-qty" role="group" aria-label="${this.escapeHtml(CONSTRUCTOR_UI.quantity || '')} ${name}">
                            <button type="button" class="quantity-btn ae-qty__btn" data-action="decrease" aria-label="${this.escapeHtml(CONSTRUCTOR_UI.fewer || '')}"${!isSelected || quantity <= minQty ? ' disabled' : ''}><i class="far fa-minus" aria-hidden="true"></i></button>
                            <span class="quantity-display ae-qty__input">${quantity}</span>
                            <button type="button" class="quantity-btn ae-qty__btn" data-action="increase" aria-label="${this.escapeHtml(CONSTRUCTOR_UI.more || '')}"${isSelected ? '' : ' disabled'}><i class="far fa-plus" aria-hidden="true"></i></button>
                        </div>
                        ${hint}
                    </div>
                </div>
            `;
        }).join('');

        container.innerHTML = productsHTML;
    }

    // A product card shows what the builder holds: picked or not, and a quantity it can lower only above the minimum.
    syncProductCard(card) {
        const productId = parseInt(card.dataset.productId, 10);
        const picked = this.selectedProducts.has(productId);
        const quantity = parseInt(card.querySelector('.quantity-display').textContent, 10);
        card.classList.toggle('selected', picked);
        const pick = card.querySelector('.product-card__pick');
        if (pick) pick.setAttribute('aria-pressed', String(picked));
        const decrease = card.querySelector('.quantity-btn[data-action="decrease"]');
        const increase = card.querySelector('.quantity-btn[data-action="increase"]');
        if (decrease) decrease.disabled = !picked || quantity <= this.minOrderQuantity(productId);
        if (increase) increase.disabled = !picked;
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

        // The change listener is bound once, in setupEventListeners: the select is a native one (no nice-select).
    }

    renderAddresses() {
        const container = document.getElementById('addresses-list');
        if (!container) return;

        if (this.addresses.length === 0) {
            container.innerHTML = `
                <div class="ae-empty sc-empty">
                    <span class="ae-empty__icon" aria-hidden="true"><i class="far fa-map-marker-alt"></i></span>
                    <h4 class="ae-empty__title">${this.escapeHtml(CONSTRUCTOR_I18N.address_none_title)}</h4>
                    <p class="ae-empty__text">${this.escapeHtml(CONSTRUCTOR_I18N.address_none_body)}</p>
                    <div class="ae-empty__actions">
                        <a class="ae-btn ae-btn--primary" href="${this.escapeHtml(this.addressesUrl())}">
                            <i class="far fa-plus" aria-hidden="true"></i>${this.escapeHtml(CONSTRUCTOR_I18N.address_add)}
                        </a>
                    </div>
                </div>
            `;
            return;
        }

        const addressesHTML = this.addresses.map(address => {
            const selected = this.selectedAddress?.id === address.id;
            // The address as checkout and My addresses show it: its title, the full address (which already names the
            // city and district) and the apartment. The API's raw `city` / `district` fields are never printed: the
            // district is a key ("shaykhontohur") and the city is stored in English.
            return `
                <button type="button" class="address-card ae-radio-card ${selected ? 'selected' : ''}"
                        data-address-id="${address.id}" aria-pressed="${selected}">
                    <span class="address-type ae-radio-card__title">${this.escapeHtml(address.title || '')}</span>
                    <span class="address-line ae-radio-card__text">${this.escapeHtml(address.full_address || address.street_address || '')}</span>
                    ${address.apartment_number ? `<span class="address-line ae-radio-card__text">${this.escapeHtml(this.apartmentLabel(address))}</span>` : ''}
                    ${address.delivery_instructions ? `<span class="address-notes ae-radio-card__text">${this.escapeHtml(address.delivery_instructions)}</span>` : ''}
                </button>
            `;
        }).join('');

        // A new address is added in My addresses; the builder keeps this tab's choices for when the visitor is back.
        container.innerHTML = `
            ${addressesHTML}
            <a class="address-card add-new-address sc-add" href="${this.escapeHtml(this.addressesUrl())}">
                <span class="ae-icon-tile ae-icon-tile--sm" aria-hidden="true"><i class="far fa-plus"></i></span>
                <span class="sc-add__text">
                    <span class="sc-add__title">${this.escapeHtml(CONSTRUCTOR_I18N.address_add)}</span>
                    <span class="sc-add__hint">${this.escapeHtml(CONSTRUCTOR_I18N.address_add_elsewhere)}</span>
                </span>
            </a>
        `;
    }

    // My addresses, where a new address is added (the page publishes the link).
    addressesUrl() {
        return CONSTRUCTOR_PAGE.addresses_url || '/addresses';
    }

    apartmentLabel(address) {
        return constructorWords(CONSTRUCTOR_I18N.address_apartment, { number: address.apartment_number });
    }

    renderPaymentMethods() {
        const container = document.getElementById('payment-methods-grid');
        if (!container) return;

        const methodsHTML = this.paymentMethods.map(method => {
            const selected = this.selectedPaymentMethod === method.id;
            return `
                <button type="button" class="payment-method-card ae-radio-card ${selected ? 'selected' : ''}"
                        data-payment-method="${this.escapeHtml(method.id)}" aria-pressed="${selected}">
                    <span class="payment-method-icon ae-icon-tile ae-icon-tile--sm" aria-hidden="true"><i class="${method.icon}"></i></span>
                    <span class="payment-method-name ae-radio-card__title">${this.escapeHtml(method.name)}</span>
                    ${method.description ? `<span class="payment-method-description ae-radio-card__text">${this.escapeHtml(method.description)}</span>` : ''}
                </button>
            `;
        }).join('');

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

        // One summary card: the name, the products with the preview's labels, the schedule, the address and the way
        // to pay, then the cost (the backend's figures only). The data-review hooks are what the tests read.
        const esc = (text) => this.escapeHtml(text);
        const dayLabel = this.deliveryDayLabel();
        const productRows = productsList.map(item => `
            <li class="sc-review__line">
                <span class="sc-review__item">
                    <span class="sc-review__name">${esc(item.name)}</span>
                    <span class="sc-review__qty">${esc(constructorWords(CONSTRUCTOR_I18N.review_quantity, { quantity: item.quantity }))}${item.priceLabel ? ' × ' : ''}<span class="ae-num" data-review="line-price">${esc(item.priceLabel)}</span></span>
                </span>
                <span class="sc-review__amount ae-num" data-review="line-total">${esc(item.totalLabel)}</span>
            </li>
        `).join('');
        preview.innerHTML = `
            <div class="sc-review">
                <div class="sc-review__main">
                    <section class="sc-review__block">
                        <h4 class="sc-review__title"><i class="far fa-pen" aria-hidden="true"></i>${esc(CONSTRUCTOR_I18N.review_name_title)}</h4>
                        <div class="ae-field sc-review__field">
                            <label class="ae-field__label" for="subscription-name-input">${esc(CONSTRUCTOR_I18N.review_name_label)}</label>
                            <input type="text" id="subscription-name-input" class="ae-input" maxlength="200"
                                   value="${esc(this.subscriptionName)}" placeholder="${esc(CONSTRUCTOR_I18N.review_name_placeholder)}"
                                   aria-describedby="subscription-name-hint">
                            <p class="ae-field__help" id="subscription-name-hint">${esc(CONSTRUCTOR_I18N.review_name_hint)}</p>
                        </div>
                    </section>

                    <section class="sc-review__block">
                        <h4 class="sc-review__title"><i class="far fa-tint" aria-hidden="true"></i>${esc(CONSTRUCTOR_I18N.review_products_title)}</h4>
                        <ul class="sc-review__lines">${productRows}</ul>
                    </section>

                    <section class="sc-review__block">
                        <h4 class="sc-review__title"><i class="far fa-calendar-alt" aria-hidden="true"></i>${esc(CONSTRUCTOR_I18N.review_schedule_title)}</h4>
                        <dl class="ae-dl sc-review__dl">
                            <div class="ae-dl__row"><dt>${esc(CONSTRUCTOR_I18N.review_frequency)}</dt><dd>${esc(this.getFrequencyName(this.selectedFrequency))}</dd></div>
                            ${dayLabel ? `<div class="ae-dl__row"><dt>${esc(CONSTRUCTOR_I18N.review_day)}</dt><dd class="sc-review__cap">${esc(dayLabel)}</dd></div>` : ''}
                            <div class="ae-dl__row"><dt>${esc(CONSTRUCTOR_I18N.review_time)}</dt><dd data-review="time-slot">${esc(this.timeSlotReviewLabel())}</dd></div>
                        </dl>
                    </section>

                    <section class="sc-review__block sc-review__block--split">
                        <div>
                            <h4 class="sc-review__title"><i class="far fa-map-marker-alt" aria-hidden="true"></i>${esc(CONSTRUCTOR_I18N.review_address_title)}</h4>
                            <p class="sc-review__strong">${esc(address?.title || '')}</p>
                            <p class="sc-review__text">${esc(address?.full_address || '')}${address?.apartment_number ? `<br>${esc(this.apartmentLabel(address))}` : ''}</p>
                        </div>
                        <div>
                            <h4 class="sc-review__title"><i class="far fa-wallet" aria-hidden="true"></i>${esc(CONSTRUCTOR_I18N.review_payment_title)}</h4>
                            <p class="sc-review__strong">${paymentMethod ? `<i class="${paymentMethod.icon} sc-review__pay-icon" aria-hidden="true"></i>${esc(paymentMethod.name)}` : ''}</p>
                            ${paymentMethod && paymentMethod.description ? `<p class="sc-review__text">${esc(paymentMethod.description)}</p>` : ''}
                        </div>
                    </section>
                </div>

                <aside class="sc-review__cost">
                    <h4 class="sc-review__title">${esc(CONSTRUCTOR_I18N.review_cost_title)}</h4>
                    <div class="sc-review__sum">
                        <span class="sc-review__sum-label">${esc(CONSTRUCTOR_I18N.review_per_delivery)}<small>${esc(CONSTRUCTOR_I18N.review_per_delivery_hint)}</small></span>
                        <strong class="sc-review__sum-value ae-num" data-review="per-delivery">${quote ? esc(quote.total_amount_label) : '&mdash;'}</strong>
                    </div>
                    <div class="sc-review__month">
                        <span class="sc-review__sum-label">${esc(CONSTRUCTOR_I18N.review_per_month)}<small>${quote ? esc(quote.deliveries_per_month_label) : esc(CONSTRUCTOR_I18N.preview_after_sign_in)}</small></span>
                        <strong class="sc-review__month-value ae-num" data-review="per-month">${quote ? esc(quote.estimated_monthly_cost_label) : '&mdash;'}</strong>
                    </div>
                    <p class="sc-review__note"><i class="far fa-info-circle" aria-hidden="true"></i><span>${esc(CONSTRUCTOR_I18N.review_note)}</span></p>
                </aside>
            </div>
        `;
    }

    // ============================================
    // EVENT HANDLERS
    // ============================================

    setupEventListeners() {
        document.addEventListener('click', (e) => {
            // "Try again" on a water list that failed to load
            if (e.target.closest('.sc-catalog-retry')) {
                this.retryCatalog();
                return;
            }

            // Product selection: anywhere on the card but its quantity control
            const productCard = e.target.closest('.product-card');
            if (productCard && !e.target.closest('.quantity-control')) {
                this.toggleProduct(productCard);
            }

            // Quantity buttons (a disabled one: below the minimum, or the product is not picked)
            const quantityBtn = e.target.closest('.quantity-btn');
            if (quantityBtn && !quantityBtn.disabled) {
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

            // Navigation buttons (a click on the icon inside a button is the button's click)
            if (e.target.closest('.nav-btn-prev')) {
                this.previousStep();
            }
            if (e.target.closest('.nav-btn-next')) {
                this.nextStep();
            }
            if (e.target.closest('.nav-btn-submit')) {
                this.submitSubscription();
            }
        });

        // Day of the month (monthly): a native <select>. jQuery's .on() hears its native change, as a plain listener
        // does when jQuery is missing.
        const monthDaySelect = document.getElementById('month-day-select');
        if (monthDaySelect) {
            const keepMonthDay = (e) => {
                this.clearInlineError();
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

        this.syncProductCard(card);
        this.clearInlineError();
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
        this.syncProductCard(card);
    }

    // The product's minimum order quantity, as the subscription catalog publishes it. A
    // subscription line below it is one billing's create_order refuses every cycle.
    minOrderQuantity(productId) {
        const product = this.products.find(p => p.id === productId);
        return parseInt(product?.min_order_quantity, 10) || 1;
    }

    selectFrequency(option) {
        this.clearInlineError();
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
            const picked = el.dataset.frequency === this.selectedFrequency;
            el.classList.toggle('selected', picked);
            el.setAttribute('aria-pressed', String(picked));
        });
        const daySelector = document.getElementById('day-selector');
        const monthDaySelector = document.getElementById('month-day-selector');
        if (daySelector) daySelector.style.display = this.usesWeekday() ? 'block' : 'none';
        if (monthDaySelector) monthDaySelector.style.display = this.usesDayOfMonth() ? 'block' : 'none';
        document.querySelectorAll('.day-btn').forEach(el => {
            const weekday = SubscriptionConstructor.weekdayFromJsDay(parseInt(el.dataset.day, 10));
            const picked = this.selectedWeekday !== null && weekday === this.selectedWeekday;
            el.classList.toggle('selected', picked);
            el.setAttribute('aria-pressed', String(picked));
            // Only the delivery days the backend published are offered (spec 2026-10-27 §6.4).
            el.style.display = this.deliveryWeekdays && !this.deliveryWeekdays.includes(weekday) ? 'none' : '';
        });
        this.showMonthDay(this.selectedDayOfMonth);
    }

    // The day-of-month <select> on `day` (its placeholder for none).
    showMonthDay(day) {
        const select = document.getElementById('month-day-select');
        if (!select) return;
        select.value = day === null || day === undefined ? '' : String(day);
    }

    selectDay(btn) {
        this.clearInlineError();
        this.selectedWeekday = SubscriptionConstructor.weekdayFromJsDay(parseInt(btn.dataset.day, 10));
        this.syncScheduleControls();
        this.saveState();
        return this.loadTimeSlots();
    }

    selectAddress(card) {
        this.clearInlineError();
        const addressId = parseInt(card.dataset.addressId);
        const hadFocus = document.activeElement === card;
        this.selectedAddress = this.addresses.find(a => a.id === addressId);
        this.renderAddresses();
        if (hadFocus) this.refocus(`#addresses-list .address-card[data-address-id="${addressId}"]`);
        this.saveState();
    }

    selectPaymentMethod(card) {
        this.clearInlineError();
        const hadFocus = document.activeElement === card;
        this.selectedPaymentMethod = card.dataset.paymentMethod;
        this.renderPaymentMethods();
        if (hadFocus) this.refocus(`#payment-methods-grid .payment-method-card[data-payment-method="${this.selectedPaymentMethod}"]`);
        this.saveState();
    }

    // A list re-rendered under the visitor's focus: the focus goes back to the choice just picked.
    refocus(selector) {
        const el = document.querySelector(selector);
        if (el) el.focus();
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
                window.location.href = this.signInUrl();
            }, 1500);
            return;
        }

        if (this.currentStep < this.totalSteps) {
            this.clearInlineError();
            this.currentStep++;

            // Load data for the next step
            await this.loadStepData(this.currentStep);

            this.showStep(this.currentStep, true); // Scroll when navigating to next step
            this.saveState();
        }
    }

    previousStep() {
        if (this.currentStep > 1) {
            this.clearInlineError();
            this.currentStep--;
            this.showStep(this.currentStep, true); // Scroll when navigating to previous step
            this.saveState();
        }
    }

    // Sign-in that brings the visitor back to this builder (the page publishes it: login's `next`, back to
    // /#subscription-constructor), where this tab's saved choices are restored.
    signInUrl() {
        return CONSTRUCTOR_PAGE.sign_in_url || '/login';
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
                // The name the visitor types is the name posted
                const nameInput = document.getElementById('subscription-name-input');
                if (nameInput) {
                    nameInput.addEventListener('input', (e) => {
                        this.subscriptionName = e.target.value;
                        this.saveState();
                    });
                }
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

        // Only on Next/Back: the builder's top comes into view (below the sticky header, the card's scroll-margin)
        // and focus moves to the new step's title, so a keyboard or screen-reader user lands on the step.
        if (shouldScroll) {
            const card = document.querySelector('#subscription-constructor .constructor-container');
            const reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
            if (card) card.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'start' });
            const title = currentPanel && currentPanel.querySelector('.sc-panel-title');
            if (title) title.focus({ preventScroll: true });
        }
    }

    // The stepper: the shared .ae-stepper states (is-done / is-current, aria-current) next to the builder's own
    // active / completed, and on phones the compact "Step N of 5: label" line with its progress bar.
    updateStepIndicators() {
        let currentLabel = '';
        for (let i = 1; i <= this.totalSteps; i++) {
            const step = document.querySelector(`.constructor-step[data-step="${i}"]`);
            if (!step) continue;
            const done = i < this.currentStep;
            const current = i === this.currentStep;
            step.classList.toggle('completed', done);
            step.classList.toggle('is-done', done);
            step.classList.toggle('active', current);
            step.classList.toggle('is-current', current);
            if (current) {
                step.setAttribute('aria-current', 'step');
                const label = step.querySelector('.step-label');
                currentLabel = label ? label.textContent.trim() : '';
            } else {
                step.removeAttribute('aria-current');
            }
        }
        const compact = document.querySelector('#subscription-constructor .sc-compact');
        if (compact && CONSTRUCTOR_UI.step_of) {
            const position = constructorWords(CONSTRUCTOR_UI.step_of, { current: this.currentStep, total: this.totalSteps });
            compact.textContent = currentLabel ? `${position}: ${currentLabel}` : position;
        }
        const bar = document.querySelector('#subscription-constructor .sc-progress');
        if (bar) bar.style.width = `${Math.round((this.currentStep / this.totalSteps) * 100)}%`;
    }

    // Back from step 2, Next until the review, Create on the review. '' hands the display back to the stylesheet.
    updateNavigation() {
        const prevBtn = document.querySelector('.nav-btn-prev');
        const nextBtn = document.querySelector('.nav-btn-next');
        const submitBtn = document.querySelector('.nav-btn-submit');

        if (prevBtn) prevBtn.style.display = this.currentStep === 1 ? 'none' : '';
        if (nextBtn) nextBtn.style.display = this.currentStep === this.totalSteps ? 'none' : '';
        if (submitBtn) submitBtn.style.display = this.currentStep === this.totalSteps ? '' : 'none';
    }

    // ============================================
    // VALIDATION
    // ============================================

    validateStep(stepNumber) {
        switch (stepNumber) {
            case 1: // Products
                if (this.selectedProducts.size === 0) {
                    // A water list that failed to load already says so, next to its "Try again": Next takes the
                    // visitor there instead of asking for a product there is none of.
                    const retry = document.querySelector('#products-grid .sc-catalog-retry');
                    if (retry) {
                        retry.focus();
                        return false;
                    }
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
            window.location.href = this.signInUrl();
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
                    productNames.push(`${quantity} × ${product.product_name}`);
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

        return parts.join(', ') || CONSTRUCTOR_I18N.name_default;
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

    // A problem is said once, inline above Back / Next, where the visitor is looking (the alert is announced). No
    // toast as well: it repeated the same words, piled up over the builder, and was announced a second time.
    showError(message) {
        const inline = document.getElementById('sc-error');
        if (inline && message) {
            const text = inline.querySelector('.sc-error__text');
            // Emptied first, so the same message after another miss is announced again.
            if (text) {
                text.textContent = '';
                text.textContent = message;
            }
            inline.hidden = false;
        }
    }

    clearInlineError() {
        const inline = document.getElementById('sc-error');
        if (inline) inline.hidden = true;
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
