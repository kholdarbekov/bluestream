// Checkout (/checkout; templates/frontend/checkout.html lists the hooks).
//
// 🔴 THE PAGE PRICES NOTHING. Every figure is read off `POST /api/v1/orders/cart/estimate`, which runs the same
// CartService the order is built from and already knows the rail, the reward and the tier. A refused quote is the
// server refusing the basket: below the minimum order (`ORDER_MIN_AMOUNT`, with the server's own figures in
// `details`) or otherwise; the page then holds the order back and says why. Product names and pictures come from
// `POST /products/bulk` (in the visitor's language); its prices are never used.
//
// The address list is the server's; "Add address" opens the shared address wizard (address-wizard.js) and the saved
// address joins the list, selected. Ways to pay are GET /payments/methods' answer, named in the page's words.
//
// The day and the window are the server's too: the date field's min/max and the windows (with whether each is still
// open today) are page data `schedule` (delivery_window.schedule_date_bounds / checkout_windows). The quote carries
// the day and window and refuses a schedule the order would refuse (`error_code` SCHEDULE_*); the page says it at
// the field and holds the order back. It keeps no clock and no window table of its own.
(function () {
    var PAGE_DATA = getPageData();
    var I18N = PAGE_DATA.i18n || {};
    var MIN_ORDER_AMOUNT = PAGE_DATA.min_order_amount;
    var SCHEDULE = PAGE_DATA.schedule || { windows: [] };

    var cartItems = [];
    var catalogue = [];               // products, for names and IMAGES only — never prices
    var currentEstimate = null;       // the server's quote; the only money source
    var quoteRefusal = null;          // the refused quote's answer ({error_code, details}) when there is one
    var scheduleProblem = null;       // {field: 'date'|'window', text}: why the day or window cannot be ordered
    var estimateSeq = 0;              // only the latest quote is drawn
    var selectedAddress = null;
    var selectedWindow = 'anytime';
    var deliveryWindowStart = null;   // "HH:MM" or null (open)
    var deliveryWindowEnd = null;     // "HH:MM" or null (open)
    var selectedPaymentMethod = 'cash';
    var deliveryDate = null;
    var orderAllowed = false;

    function $(id) { return document.getElementById(id); }

    // base.js (loaded first) escapes and words money ("18 000 сум" / "18 000 so‘m").
    var escapeHtml = window.AE.escapeHtml;
    var formatPrice = window.AE.formatMoney;

    // `{name}` placeholders, filled by base.js (AE.fill, the one fill every page script shares).
    var fill = window.AE.fill;

    function showOverlay(on) {
        var overlay = $('loading-overlay');
        if (overlay) overlay.style.display = on ? 'flex' : 'none';
    }

    // Every place-order button (the summary's and the phone bar's) follows the same gate.
    function placeButtons() {
        return Array.prototype.slice.call(document.querySelectorAll('[data-action="place-order"]'));
    }

    function setPlaceDisabled(disabled) {
        placeButtons().forEach(function (button) { button.disabled = disabled; });
    }

    async function clearCartAfterOrder() {
        localStorage.removeItem('cart');
        document.querySelectorAll('.cart-count, .cart-badge').forEach(function (el) {
            el.textContent = '0';
            el.style.display = 'none';
        });
    }

    // ------------------------------------------------------------------ the quote

    function estimatePayload() {
        return {
            items: cartItems.map(function (item) {
                return { product_id: item.product_id, quantity: item.quantity };
            }),
            delivery_address_id: selectedAddress,
            delivery_date: deliveryDate,
            delivery_window_start: deliveryWindowStart,
            delivery_window_end: deliveryWindowEnd,
            payment_method: selectedPaymentMethod
        };
    }

    async function refreshEstimate() {
        if (!deliveryDate) return;   // no day, no quote: the field says so (onDateChange)
        var seq = ++estimateSeq;
        $('summary-total').textContent = I18N.calculating || '…';
        var estimate = null;
        var refusal = null;
        try {
            var response = await apiRequest('/orders/cart/estimate', {
                method: 'POST',
                body: JSON.stringify(estimatePayload())
            });
            var body = null;
            try { body = await response.json(); } catch (parseError) { body = null; }
            if (response.ok && body && body.success) estimate = body.data;
            else refusal = body || {};
        } catch (error) {
            refusal = {};
        }
        if (seq !== estimateSeq) return;   // a newer quote is on its way
        currentEstimate = estimate;
        quoteRefusal = refusal;
        showScheduleProblem(scheduleRefusal(refusal));
        renderEstimate();
    }

    // ------------------------------------------------------------------ schedule refusals

    // A refusal's code: the quote answers at the top level (handle_api_exception), the order under `data`.
    function refusalCode(body) {
        if (!body) return null;
        return body.error_code || (body.data && body.data.error_code) || null;
    }

    // The schedule rule's refusal (SCHEDULE_*), worded for the field it is about; null for any other answer.
    function scheduleRefusal(body) {
        var code = refusalCode(body);
        if (!code || code.indexOf('SCHEDULE_') !== 0) return null;
        if (code === 'SCHEDULE_DATE_IN_PAST') return { field: 'date', text: I18N.schedule_date_past };
        if (code === 'SCHEDULE_BEYOND_HORIZON') {
            return { field: 'date', text: fill(I18N.schedule_date_far, { date: SCHEDULE.max_label || '' }) };
        }
        if (code === 'SCHEDULE_WINDOW_PASSED') return { field: 'window', text: I18N.schedule_window_passed };
        if (code === 'SCHEDULE_MALFORMED') return { field: 'date', text: I18N.schedule_other };
        return { field: 'window', text: I18N.schedule_other };
    }

    // Say (or clear) the schedule problem at its field: the day's error under the date, the window's under the tiles.
    function showScheduleProblem(problem) {
        scheduleProblem = problem;
        var onDate = !!problem && problem.field === 'date';
        var onWindow = !!problem && problem.field === 'window';
        markField('delivery-date', 'delivery-date-error', onDate ? problem.text : '');
        markField('time-slots-container', 'delivery-window-error', onWindow ? problem.text : '');
    }

    // A field's error line and its aria-invalid, set together (an empty text clears both).
    function markField(fieldId, errorId, text) {
        var field = $(fieldId);
        var error = $(errorId);
        if (error) error.textContent = text;
        if (!field) return;
        if (text) field.setAttribute('aria-invalid', 'true');
        else field.removeAttribute('aria-invalid');
    }

    function catalogueProduct(productId) {
        return catalogue.find(function (p) { return p.id === productId; }) || null;
    }

    function minQtyViolations() {
        var violations = [];
        cartItems.forEach(function (cartItem) {
            var product = catalogueProduct(cartItem.product_id);
            if (!product) return;
            var minOrderQty = parseInt(
                (product.inventory && product.inventory.min_order_quantity) ||
                product.min_order_quantity || 1,
                10
            ) || 1;
            if (cartItem.quantity < minOrderQty) {
                violations.push({
                    name: product.name,
                    min_qty: minOrderQty,
                    remaining: minOrderQty - cartItem.quantity
                });
            }
        });
        return violations;
    }

    // The quote's lines; with no quote (refused), the cart's own lines with their quantities and no money.
    function renderSummaryItems() {
        var container = $('summary-items');
        var quoted = (currentEstimate && currentEstimate.items) || null;
        var lines = quoted || cartItems.map(function (item) {
            return { product_id: item.product_id, quantity: item.quantity, product_name: '' };
        });
        container.innerHTML = lines.map(function (line) {
            var product = catalogueProduct(line.product_id) || {};
            var productImages = (product.media && product.media.images) || product.images || [];
            var productImage = productImages.length > 0 ? productImages[0] : PAGE_DATA.default_image;
            var name = product.name || line.product_name;
            var qty = quoted ? line.quantity + ' × ' + formatPrice(line.unit_price) : '× ' + line.quantity;
            return '<li class="ae-co-line">' +
                '<img class="ae-co-line__thumb" src="' + escapeHtml(productImage) + '" alt="" width="56" height="56" loading="lazy" data-fallback>' +
                '<span class="ae-co-line__body">' +
                '<span class="ae-co-line__name">' + escapeHtml(name) + '</span>' +
                '<span class="ae-co-line__qty">' + escapeHtml(qty) + '</span>' +
                '</span>' +
                '<span class="ae-co-line__total">' + (quoted ? escapeHtml(formatPrice(line.subtotal)) : '') + '</span>' +
                '</li>';
        }).join('');
        container.removeAttribute('aria-busy');
    }

    function renderDiscountRow(id, label, amount) {
        var row = $(id + '-row');
        var value = Number(amount || 0);
        row.style.display = value > 0 ? '' : 'none';
        if (value > 0) {
            $(id + '-label').textContent = label;
            $(id).textContent = '−' + formatPrice(value);
        }
    }

    function tierLabel(pricing) {
        // The RATE is never held by this page — it is echoed from live config.
        return String(I18N.tier_discount_line || '')
            .replace('{tier}', pricing.tier_name || '')
            .replace('{percent}', pricing.tier_discount_percentage);
    }

    function renderEstimate() {
        renderSummaryItems();

        var pricing = (currentEstimate && currentEstimate.pricing) || null;
        var unknown = '—';

        $('summary-subtotal').textContent = pricing ? formatPrice(pricing.items_subtotal) : unknown;

        var deliveryEl = $('summary-delivery');
        deliveryEl.classList.toggle('is-free', !!pricing && Number(pricing.delivery_fee) === 0);
        if (!pricing) deliveryEl.textContent = unknown;
        else if (Number(pricing.delivery_fee) === 0) deliveryEl.textContent = I18N.free;
        else deliveryEl.textContent = formatPrice(pricing.delivery_fee);

        renderDiscountRow('summary-discount', I18N.discount, pricing && pricing.discount_amount);
        renderDiscountRow('summary-reward', I18N.reward_discount, pricing && pricing.loyalty_discount);
        renderDiscountRow('summary-tier', pricing ? tierLabel(pricing) : '', pricing && pricing.tier_discount);

        var total = pricing ? formatPrice(pricing.final_total) : unknown;
        $('summary-total').textContent = total;
        var barTotal = $('checkout-bar-total');
        if (barTotal) barTotal.textContent = total;

        var savings = pricing ? Number(pricing.cod_savings || 0) : 0;
        $('summary-cod-savings-row').style.display = savings > 0 ? '' : 'none';
        if (savings > 0) {
            $('summary-cod-savings').textContent = String(I18N.cod_savings || '').replace('{amount}', formatPrice(savings));
        }

        renderOrderGate(pricing);
    }

    // Why the order cannot be placed yet, in the server's terms. A quote IS the server accepting this basket:
    // no client-side threshold decides whether the order may be placed.
    function gateMessage(pricing) {
        // The day or window first: the server judges it before the basket, and the field already says what to do.
        if (scheduleProblem) return { title: I18N.schedule_title, text: scheduleProblem.text };
        var violations = minQtyViolations();
        if (violations.length) {
            return {
                title: I18N.min_qty_title,
                text: violations.map(function (v) {
                    return fill(I18N.min_qty_line, { name: v.name, min: v.min_qty, remaining: v.remaining });
                }).join('\n')
            };
        }
        if (pricing) return null;
        var refusal = quoteRefusal || {};
        if (refusal.error_code === 'ORDER_MIN_AMOUNT') {
            // The server's own figures: the floor it applies and the basket it priced.
            var details = refusal.details || {};
            var minimum = Number(details.min_amount || MIN_ORDER_AMOUNT || 0);
            var text = '';
            if (details.items_subtotal !== undefined && details.items_subtotal !== null) {
                text = fill(I18N.min_amount_text, { remaining: formatPrice(minimum - Number(details.items_subtotal)) });
            }
            return { title: fill(I18N.min_amount_title, { minimum: formatPrice(minimum) }), text: text };
        }
        return { title: I18N.quote_failed_title, text: I18N.quote_failed_text };
    }

    function renderOrderGate(pricing) {
        var warningEl = $('min-order-warning');
        var message = gateMessage(pricing);
        orderAllowed = !message;
        if (message) {
            warningEl.style.display = 'flex';
            $('min-order-title').textContent = message.title || '';
            $('min-order-text').textContent = message.text || '';
        } else {
            warningEl.style.display = 'none';
        }
        setPlaceDisabled(!orderAllowed);
    }

    async function loadCatalogue() {
        try {
            var response = await apiRequest('/products/bulk', {
                method: 'POST',
                body: JSON.stringify({
                    product_ids: cartItems.map(function (item) { return item.product_id; }),
                    language: PAGE_DATA.current_language
                })
            });
            if (response.ok) {
                var data = await response.json();
                // Names, pictures and per-product minimums only. The prices in this payload are contract-blind
                // over cookie auth.
                if (data.success && data.data.products) catalogue = data.data.products;
            }
        } catch (error) {
            catalogue = [];
        }
    }

    // ------------------------------------------------------------------ address

    function addressCards() {
        return Array.prototype.slice.call(document.querySelectorAll('.address-card[data-address-id]'))
            .filter(function (card) { return card.dataset.addressId; });
    }

    function clearError(id) {
        var error = $(id);
        if (error) error.textContent = '';
    }

    async function selectAddress(addressId, options) {
        var selectedCard = null;
        addressCards().forEach(function (card) {
            var match = parseInt(card.dataset.addressId, 10) === addressId;
            card.classList.toggle('selected', match);
            var radio = card.querySelector('input[type="radio"]');
            if (radio) radio.checked = match;
            if (match) selectedCard = card;
        });
        if (!selectedCard) return;
        selectedAddress = addressId;
        clearError('address-error');
        // The estimate computes the fee through CartService._calculate_delivery_fee.
        // `/delivery/calculate-fee` was a second surface that could disagree.
        if (!options || options.quote !== false) await refreshEstimate();
    }

    // A saved address from the wizard: a card like the server's (the page's own <template>), selected.
    function addAddressCard(address) {
        if (!address || !address.id) return;
        var grid = $('address-grid');
        var template = $('address-card-template');
        var card = document.querySelector('.address-card[data-address-id="' + address.id + '"]');
        if (!card && template && grid) {
            card = template.content.firstElementChild.cloneNode(true);
            card.dataset.addressId = String(address.id);
            card.querySelector('input[type="radio"]').value = String(address.id);
            grid.insertBefore(card, grid.querySelector('[data-action="show-add-address"]'));
        }
        if (!card) return;
        card.querySelector('.ae-co-addr__name').textContent = address.title || address.full_address || I18N.untitled_address || '';
        var line = card.querySelector('.ae-co-addr__line');
        line.textContent = address.title ? (address.full_address || '') : '';
        line.hidden = !address.title;
        var note = card.querySelector('.ae-co-addr__note');
        note.querySelector('span').textContent = address.delivery_instructions || '';
        note.hidden = !address.delivery_instructions;
        if (address.is_default) {
            // One main address: the backend unset the others.
            document.querySelectorAll('.ae-co-addr__main').forEach(function (badge) { badge.hidden = true; });
        }
        card.querySelector('.ae-co-addr__main').hidden = !address.is_default;

        if (grid) grid.hidden = false;
        var empty = $('address-empty');
        if (empty) empty.hidden = true;
        selectAddress(address.id);
    }

    function openAddressWizard() {
        if (!window.AddressWizard) {
            window.location.href = PAGE_DATA.addresses_url;
            return;
        }
        window.AddressWizard.open({
            makeDefault: addressCards().length === 0,
            onSaved: addAddressCard
        });
    }

    // ------------------------------------------------------------------ when

    // The windows the server publishes (key, start, end, open_today), named by key in the page's words.
    var WINDOW_WORDS = {
        anytime: I18N.window_anytime,
        morning: I18N.window_morning,
        afternoon: I18N.window_afternoon,
        evening: I18N.window_evening
    };

    function windowWord(win) { return WINDOW_WORDS[win.key] || win.key; }

    // The word plus the published bounds: `Morning (09:00–12:00)`. Anytime has none.
    // The tile draws the word and the bounds on two lines; this full label is what a screen reader hears.
    function windowLabel(win) {
        return win.start ? windowWord(win) + ' (' + win.start + '–' + win.end + ')' : windowWord(win);
    }

    function renderDeliveryWindows() {
        var container = $('time-slots-container');
        container.innerHTML = (SCHEDULE.windows || []).map(function (win, index) {
            return '<label class="ae-radio-card time-slot' + (index === 0 ? ' selected' : '') + '"' +
                ' data-action="select-window" data-window-key="' + escapeHtml(win.key) + '">' +
                '<input class="ae-co-radio" type="radio" name="delivery_window" value="' + escapeHtml(win.key) + '"' +
                (index === 0 ? ' checked' : '') + ' aria-label="' + escapeHtml(windowLabel(win)) + '">' +
                '<span class="ae-radio-card__title time-slot-time">' + escapeHtml(windowWord(win)) + '</span>' +
                (win.start ? '<span class="ae-radio-card__text">' + escapeHtml(win.start + '–' + win.end) + '</span>' : '') +
                '</label>';
        }).join('');
        selectWindow((SCHEDULE.windows && SCHEDULE.windows[0] && SCHEDULE.windows[0].key) || 'anytime');
        markClosedWindows();
    }

    // A window the server already counts as over today is offered for later days only. Today is the server's day.
    function markClosedWindows() {
        var isToday = deliveryDate === SCHEDULE.today;
        (SCHEDULE.windows || []).forEach(function (win) {
            var tile = document.querySelector('.time-slot[data-window-key="' + win.key + '"]');
            var radio = tile && tile.querySelector('input[type="radio"]');
            if (radio) radio.disabled = isToday && win.open_today === false;
        });
    }

    function selectWindow(key) {
        document.querySelectorAll('.time-slot').forEach(function (el) {
            var match = el.dataset.windowKey === key;
            el.classList.toggle('selected', match);
            var radio = el.querySelector('input[type="radio"]');
            if (radio) radio.checked = match;
        });
        var win = (SCHEDULE.windows || []).filter(function (w) { return w.key === key; })[0] || { start: null, end: null };
        selectedWindow = key;
        deliveryWindowStart = win.start || null;
        deliveryWindowEnd = win.end || null;
    }

    function onWindowChange(key) {
        selectWindow(key);
        // The quote judges the window as the order will: re-quote.
        refreshEstimate();
    }

    function onDateChange() {
        deliveryDate = $('delivery-date').value || null;
        markClosedWindows();
        if (!deliveryDate) {
            // A cleared field (the date pickers' Clear): say it at the field and hold the order; no quote is asked.
            estimateSeq += 1;   // an answer still on its way is for a day that is no longer chosen
            showScheduleProblem({ field: 'date', text: I18N.select_delivery_date });
            renderOrderGate((currentEstimate && currentEstimate.pricing) || null);
            return;
        }
        showScheduleProblem(null);
        // The day can change the delivery fee, and whether the window is still open: re-quote.
        refreshEstimate();
    }

    // ------------------------------------------------------------------ payment

    var METHOD_ICONS = {
        cash: 'far fa-money-bill-wave',
        click: 'far fa-credit-card',
        business_account: 'far fa-building'
    };

    function selectPaymentMethod(method) {
        document.querySelectorAll('.payment-method').forEach(function (pm) {
            var match = pm.dataset.method === method;
            pm.classList.toggle('selected', match);
            var radio = pm.querySelector('input[type="radio"]');
            if (radio) radio.checked = match;
        });
        selectedPaymentMethod = method;
        $('selected-payment-method').value = method;
        // The rail changes the price (bot/website perk, paid on delivery).
        // Re-quote rather than adjust the number on screen.
        refreshEstimate();
    }

    async function loadPaymentMethods() {
        var container = $('payment-methods-container');
        var labels = PAGE_DATA.method_labels || {};
        var notes = PAGE_DATA.method_notes || {};
        var methods = [];
        try {
            var response = await apiRequest('/payments/methods?context=order');
            var payload = await response.json();
            methods = (payload.data && payload.data.available_methods) || [];
        } catch (error) {
            methods = [];
        }
        if (!methods.length) methods = [{ method: 'cash' }];

        container.innerHTML = methods.map(function (m, index) {
            var name = labels[m.method] || m.display_name || m.method;
            var note = notes[m.method] || '';
            return '<label class="ae-radio-card payment-method' + (index === 0 ? ' selected' : '') + '"' +
                ' data-action="select-payment-method" data-method="' + escapeHtml(m.method) + '">' +
                '<input class="ae-co-radio" type="radio" name="payment_method_choice" value="' + escapeHtml(m.method) + '"' +
                (index === 0 ? ' checked' : '') + '>' +
                '<span class="ae-icon-tile ae-icon-tile--sm" aria-hidden="true"><i class="' + (METHOD_ICONS[m.method] || 'far fa-wallet') + '"></i></span>' +
                '<span class="ae-co-method__body">' +
                '<span class="ae-radio-card__title payment-method-name">' + escapeHtml(name) + '</span>' +
                (note ? '<span class="ae-radio-card__text payment-method-desc">' + escapeHtml(note) + '</span>' : '') +
                '</span>' +
                '</label>';
        }).join('');
        container.removeAttribute('aria-busy');

        selectedPaymentMethod = methods[0].method;
        $('selected-payment-method').value = selectedPaymentMethod;
    }

    // ------------------------------------------------------------------ place the order

    function orderBody(method) {
        return {
            items: cartItems,
            delivery_address_id: selectedAddress,
            delivery_date: deliveryDate,
            delivery_window_start: deliveryWindowStart,
            delivery_window_end: deliveryWindowEnd,
            payment_method: method,
            delivery_notes: $('order-notes').value || null,
            source: 'web'
        };
    }

    function releaseButton() {
        showOverlay(false);
        setPlaceDisabled(!orderAllowed);
    }

    async function placeOrderDirect(method) {
        showOverlay(true);
        setPlaceDisabled(true);

        try {
            var response = await apiRequest('/orders/', {
                method: 'POST',
                body: JSON.stringify(orderBody(method))
            });

            var data = await response.json();

            if (data.success) {
                await clearCartAfterOrder();
                window.location.href = '/order-confirmation?order_id=' + data.data.order.id;
            } else if (!refusedSchedule(data)) {
                throw new Error(refusalMessage(data));
            }
        } catch (error) {
            showNotification(error.message || I18N.place_order_failed, 'error');
            releaseButton();
        }
    }

    // The order refused the day or window (it ended since the quote): said at the field, the order held back.
    function refusedSchedule(data) {
        var problem = scheduleRefusal(data);
        if (!problem) return false;
        showOverlay(false);
        showScheduleProblem(problem);
        renderOrderGate(null);
        var field = problem.field === 'date' ? $('delivery-date') : document.querySelector('#time-slots-container input:not(:disabled)');
        refuse('delivery-time-section', problem.field === 'date' ? 'delivery-date-error' : 'delivery-window-error', problem.text, field);
        return true;
    }

    // A refusal's own sentence when the server words one; the generic "Validation failed" envelope (it lists
    // `errors`, in English) gets the page's words instead.
    function refusalMessage(data) {
        if (!data || (data.errors && data.errors.length)) return I18N.place_order_failed;
        return data.message || I18N.place_order_failed;
    }

    async function placeOrderWithClick() {
        var stateEl = $('checkoutPaymentState');
        stateEl.textContent = '';
        stateEl.hidden = true;
        showOverlay(true);
        setPlaceDisabled(true);

        try {
            var response = await apiRequest('/orders/', {
                method: 'POST',
                body: JSON.stringify(orderBody('click'))
            });

            var data = await response.json();

            if (data.success && data.data.payment_url) {
                await clearCartAfterOrder();
                // The backend says how long Click needs (the items are being registered with the tax authority).
                // The overlay hides so the wait is explained in the page; the button stays disabled until we leave.
                showOverlay(false);
                window.OrderActions.openPaymentWhenReady(data.data, { words: PAGE_DATA.order_actions, statusEl: stateEl });
            } else if (data.data && data.data.error_code === 'ASL_BELGISI_UNAVAILABLE') {
                // The tax system is down: nothing was charged, the cart is kept. Cash or another try.
                releaseButton();
                window.OrderActions.showCashRescue(stateEl, data, {
                    words: PAGE_DATA.order_actions,
                    onRetry: placeOrderWithClick
                });
            } else if (!refusedSchedule(data)) {
                throw new Error(refusalMessage(data));
            }
        } catch (error) {
            showNotification(error.message || I18N.place_order_failed, 'error');
            releaseButton();
        }
    }

    // A missing choice is said next to it, and the page scrolls there.
    function refuse(sectionId, errorId, message, focusEl) {
        var error = $(errorId);
        if (error) error.textContent = message || '';
        var section = $(sectionId);
        if (section && section.scrollIntoView) section.scrollIntoView({ behavior: 'smooth', block: 'start' });
        if (focusEl && focusEl.focus) focusEl.focus({ preventScroll: true });
    }

    async function placeOrder() {
        if (!orderAllowed) return;

        if (!selectedAddress) {
            refuse('address-section', 'address-error', I18N.select_delivery_address,
                document.querySelector('#address-empty:not([hidden]) [data-action="show-add-address"]') ||
                document.querySelector('#address-grid input[type="radio"]'));
            return;
        }

        if (!deliveryDate) {
            var dateInput = $('delivery-date');
            dateInput.setAttribute('aria-invalid', 'true');
            refuse('delivery-time-section', 'delivery-date-error', I18N.select_delivery_date, dateInput);
            return;
        }

        var terms = $('agree-terms');
        if (!terms.checked) {
            terms.setAttribute('aria-invalid', 'true');
            refuse('notes-section', 'agree-terms-error', I18N.agree_terms, terms);
            return;
        }

        if (selectedPaymentMethod === 'click') {
            await placeOrderWithClick();
        } else {
            // cash and business_account both settle without a redirect.
            await placeOrderDirect(selectedPaymentMethod);
        }
    }

    function bind() {
        var form = $('checkout-form');
        // Enter in a field must not submit (and reload) the page: the order goes through placeOrder() only.
        if (form) form.addEventListener('submit', function (e) { e.preventDefault(); });

        $('delivery-date').addEventListener('change', onDateChange);

        // Native radios inside the option cards: a click, a tap or the arrow keys all arrive as `change`.
        document.addEventListener('change', function (e) {
            var target = e.target;
            if (!target || target.type !== 'radio') return;
            if (target.name === 'delivery_address') selectAddress(parseInt(target.value, 10));
            else if (target.name === 'delivery_window') onWindowChange(target.value);
            else if (target.name === 'payment_method_choice') selectPaymentMethod(target.value);
        });

        $('agree-terms').addEventListener('change', function () {
            this.removeAttribute('aria-invalid');
            clearError('agree-terms-error');
        });

        document.body.addEventListener('click', function (e) {
            var target = e.target.closest('[data-action]');
            if (!target) return;

            switch (target.dataset.action) {
                case 'show-add-address':
                    e.preventDefault();
                    openAddressWizard();
                    break;
                case 'place-order':
                    e.preventDefault();
                    placeOrder();
                    break;
            }
        });
    }

    // ------------------------------------------------------------------ start

    document.addEventListener('DOMContentLoaded', async function () {
        if (typeof CURRENT_USER === 'undefined' || !CURRENT_USER) {
            window.location.href = PAGE_DATA.login_url + '?next=' + encodeURIComponent(PAGE_DATA.checkout_url);
            return;
        }

        if (window.cartSyncPromise) await window.cartSyncPromise;

        cartItems = JSON.parse(localStorage.getItem('cart') || '[]');
        if (cartItems.length === 0) {
            window.location.href = PAGE_DATA.cart_url;
            return;
        }

        // The day defaults to the server's today (Tashkent), with its min and max, as the template drew them. Not
        // tomorrow: an order dated tomorrow is invisible to every driver until then, and this page has no "deliver
        // now" escape. Not the browser's day: in another time zone it disagrees with the rule the order is judged by.
        deliveryDate = $('delivery-date').value || null;

        // The pre-selected address (the main one comes first), without a quote of its own.
        var firstAddress = document.querySelector('.address-card.selected[data-address-id]') ||
            addressCards()[0] || null;
        if (firstAddress && firstAddress.dataset.addressId) {
            await selectAddress(parseInt(firstAddress.dataset.addressId, 10), { quote: false });
        }

        renderDeliveryWindows();
        setPlaceDisabled(true);
        bind();
        await Promise.all([loadCatalogue(), loadPaymentMethods()]);
        await refreshEstimate();
    });
})();
