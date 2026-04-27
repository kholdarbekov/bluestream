(function () {
    var PAGE_DATA = getPageData();
    var MIN_ORDER_AMOUNT = PAGE_DATA.min_order_amount || 20000;

    var cartItems = [];
    var selectedAddress = null;
    var selectedTimeSlot = null;
    var selectedPaymentMethod = 'cash';
    var deliveryDate = null;
    var currentOrderId = null;
    var currentCardToken = null;

    var verificationState = {
        token: null,
        maskedPhone: null,
        waitSeconds: 60,
        attemptsRemaining: 3,
        timerInterval: null,
        cardMetadata: null
    };

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
        return new Intl.NumberFormat('uz-UZ').format(Math.round(amount)) + ' UZS';
    }

    async function clearCartAfterOrder() {
        localStorage.removeItem('cart');
        document.querySelectorAll('.cart-count, .cart-badge').forEach(function (el) {
            el.textContent = '0';
            el.style.display = 'none';
        });
    }

    function renderOrderSummary(products) {
        var container = document.getElementById('summary-items');
        var html = '';
        var subtotal = 0;

        cartItems.forEach(function (cartItem) {
            var product = products.find(function (p) { return p.id === cartItem.product_id; });
            if (!product) return;

            var pricing = product.pricing || {};
            var price = parseFloat(pricing.current_price || pricing.base_price || product.base_price || 0);
            var itemTotal = price * cartItem.quantity;
            subtotal += itemTotal;

            var productImages = (product.media && product.media.images) || product.images || [];
            var productImage = productImages.length > 0 ? productImages[0] : PAGE_DATA.default_image;

            html += '<div class="summary-item">' +
                '<img src="' + escapeHtml(productImage) + '" alt="' + escapeHtml(product.name) + '" class="summary-item-image">' +
                '<div class="summary-item-details">' +
                '<div class="summary-item-name">' + escapeHtml(product.name) + '</div>' +
                '<div class="summary-item-qty">' + escapeHtml(PAGE_DATA.i18n.qty) + ': ' + cartItem.quantity + '</div>' +
                '</div>' +
                '<div class="summary-item-price">' + formatPrice(itemTotal) + '</div>' +
                '</div>';
        });

        container.innerHTML = html;

        window.currentSubtotal = subtotal;
        updateSummaryTotals(subtotal, null);
    }

    function updateSummaryTotals(subtotal, deliveryFee) {
        document.getElementById('summary-subtotal').textContent = formatPrice(subtotal);

        var deliveryEl = document.getElementById('summary-delivery');
        var totalEl = document.getElementById('summary-total');

        if (deliveryFee === null) {
            deliveryEl.textContent = PAGE_DATA.i18n.select_address;
            deliveryEl.style.color = '#999';
            totalEl.textContent = formatPrice(subtotal);
        } else if (deliveryFee === 0) {
            deliveryEl.textContent = PAGE_DATA.i18n.free;
            deliveryEl.style.color = '#28a745';
            totalEl.textContent = formatPrice(subtotal);
        } else {
            deliveryEl.textContent = formatPrice(deliveryFee);
            deliveryEl.style.color = '';
            totalEl.textContent = formatPrice(subtotal + deliveryFee);
        }

        window.currentDeliveryFee = deliveryFee;

        var warningEl = document.getElementById('min-order-warning');
        var titleEl = document.getElementById('min-order-title');
        var textEl = document.getElementById('min-order-text');
        var placeOrderBtn = document.getElementById('place-order-btn');

        if (subtotal < MIN_ORDER_AMOUNT) {
            var remaining = MIN_ORDER_AMOUNT - subtotal;

            warningEl.style.display = 'flex';
            titleEl.textContent = PAGE_DATA.i18n.min_not_met;
            textEl.textContent = PAGE_DATA.i18n.add + ' ' + formatPrice(remaining) + ' ' +
                PAGE_DATA.i18n.more_to_place + '. ' + PAGE_DATA.i18n.min_order_amount + ': ' + formatPrice(MIN_ORDER_AMOUNT);

            placeOrderBtn.disabled = true;
            placeOrderBtn.style.opacity = '0.6';
            placeOrderBtn.style.cursor = 'not-allowed';
            placeOrderBtn.title = PAGE_DATA.i18n.min_warning + ' ' + formatPrice(MIN_ORDER_AMOUNT);
        } else {
            warningEl.style.display = 'none';
            placeOrderBtn.disabled = false;
            placeOrderBtn.style.opacity = '';
            placeOrderBtn.style.cursor = '';
            placeOrderBtn.title = '';
        }
    }

    async function loadCartSummary() {
        cartItems = JSON.parse(localStorage.getItem('cart') || '[]');

        if (cartItems.length === 0) {
            window.location.href = PAGE_DATA.cart_url;
            return;
        }

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
                if (data.success && data.data.products) {
                    renderOrderSummary(data.data.products);
                }
            }
        } catch (error) {
            console.error('Error loading cart summary:', error);
        }
    }

    async function calculateDeliveryFee(addressId) {
        if (!addressId || window.currentSubtotal === undefined) return;

        var deliveryEl = document.getElementById('summary-delivery');
        deliveryEl.textContent = PAGE_DATA.i18n.calculating;
        deliveryEl.style.color = '#999';

        try {
            var response = await apiRequest('/delivery/calculate-fee', {
                method: 'POST',
                body: JSON.stringify({
                    address_id: addressId,
                    order_total: window.currentSubtotal
                })
            });

            if (response.ok) {
                var data = await response.json();
                if (data.success) {
                    var deliveryFee = data.data.delivery_fee || 0;
                    updateSummaryTotals(window.currentSubtotal, deliveryFee);
                } else {
                    updateSummaryTotals(window.currentSubtotal, null);
                    console.error('Delivery fee calculation failed:', data.message);
                }
            } else {
                updateSummaryTotals(window.currentSubtotal, null);
            }
        } catch (error) {
            console.error('Error calculating delivery fee:', error);
            updateSummaryTotals(window.currentSubtotal, null);
        }
    }

    async function selectAddress(addressId) {
        document.querySelectorAll('.address-card').forEach(function (card) {
            card.classList.remove('selected');
        });

        var selectedCard = document.querySelector('.address-card[data-address-id="' + addressId + '"]');
        if (selectedCard) {
            selectedCard.classList.add('selected');
            selectedCard.querySelector('input[type="radio"]').checked = true;
            selectedAddress = addressId;
            await calculateDeliveryFee(addressId);
        }
    }

    function renderTimeSlots(slots) {
        var container = document.getElementById('time-slots-container');

        if (slots.length === 0) {
            renderDefaultTimeSlots();
            return;
        }

        var html = '';
        slots.forEach(function (slot, index) {
            var isAvailable = slot.available !== false;
            html += '<div class="time-slot ' + (!isAvailable ? 'unavailable' : '') + ' ' + (index === 0 ? 'selected' : '') + '"' +
                (isAvailable
                    ? ' data-action="select-time-slot" data-slot-id="' + slot.id + '" data-start-time="' + escapeHtml(slot.start_time) + '" data-end-time="' + escapeHtml(slot.end_time) + '"'
                    : '') +
                '>' +
                '<div class="time-slot-time">' + escapeHtml(slot.start_time) + ' - ' + escapeHtml(slot.end_time) + '</div>' +
                '<div class="time-slot-info">' + escapeHtml(isAvailable ? PAGE_DATA.i18n.available : PAGE_DATA.i18n.full) + '</div>' +
                '</div>';
        });

        container.innerHTML = html;

        if (slots.length > 0 && slots[0].available !== false) {
            selectedTimeSlot = slots[0].id;
        }
    }

    function renderDefaultTimeSlots() {
        var defaultSlots = [
            { time: '09:00 - 12:00', label: PAGE_DATA.i18n.morning },
            { time: '12:00 - 15:00', label: PAGE_DATA.i18n.afternoon },
            { time: '15:00 - 18:00', label: PAGE_DATA.i18n.evening },
            { time: '18:00 - 21:00', label: PAGE_DATA.i18n.night }
        ];

        var container = document.getElementById('time-slots-container');
        var html = '';

        defaultSlots.forEach(function (slot, index) {
            html += '<div class="time-slot ' + (index === 0 ? 'selected' : '') + '" ' +
                'data-action="select-time-slot-manual" data-slot-time="' + escapeHtml(slot.time) + '">' +
                '<div class="time-slot-time">' + escapeHtml(slot.time) + '</div>' +
                '<div class="time-slot-info">' + escapeHtml(slot.label) + '</div>' +
                '</div>';
        });

        container.innerHTML = html;
        selectedTimeSlot = defaultSlots[0].time;
    }

    async function loadTimeSlots() {
        deliveryDate = document.getElementById('delivery-date').value;
        if (!deliveryDate) return;

        var container = document.getElementById('time-slots-container');
        container.innerHTML = '<div style="grid-column: 1 / -1; text-align: center; padding: 20px;"><div class="loading-spinner" style="width: 30px; height: 30px;"></div></div>';

        try {
            var response = await apiRequest('/delivery/time-slots?date=' + deliveryDate, { method: 'GET' });

            if (response.ok) {
                var data = await response.json();
                renderTimeSlots((data.data && data.data.time_slots) || []);
            } else {
                renderDefaultTimeSlots();
            }
        } catch (error) {
            console.error('Error loading time slots:', error);
            renderDefaultTimeSlots();
        }
    }

    function selectTimeSlot(slotId, startTime, endTime, targetEl) {
        document.querySelectorAll('.time-slot').forEach(function (slot) {
            slot.classList.remove('selected');
        });
        targetEl.classList.add('selected');
        selectedTimeSlot = slotId;
    }

    function selectTimeSlotManual(time, targetEl) {
        document.querySelectorAll('.time-slot').forEach(function (slot) {
            slot.classList.remove('selected');
        });
        targetEl.classList.add('selected');
        selectedTimeSlot = time;
    }

    function selectPaymentMethod(method, targetEl) {
        document.querySelectorAll('.payment-method').forEach(function (pm) {
            pm.classList.remove('selected');
        });
        targetEl.classList.add('selected');
        selectedPaymentMethod = method;
        document.getElementById('selected-payment-method').value = method;

        var cardContainer = document.getElementById('card-input-container');
        if (cardContainer) cardContainer.style.display = 'none';
    }

    function formatCardNumber(input) {
        var value = input.value.replace(/\D/g, '');
        var formattedValue = '';
        for (var i = 0; i < value.length; i++) {
            if (i > 0 && i % 4 === 0) formattedValue += ' ';
            formattedValue += value[i];
        }
        input.value = formattedValue;
    }

    function formatCardExpiry(input) {
        var value = input.value.replace(/\D/g, '');
        if (value.length >= 2) value = value.substring(0, 2) + '/' + value.substring(2, 4);
        input.value = value;
    }

    function showAddAddressModal() {
        showNotification(PAGE_DATA.i18n.address_coming_soon, 'info');
    }

    function closeAddressModal() {
        var modal = document.getElementById('address-modal');
        if (modal) modal.style.display = 'none';
    }

    function saveAddress(event) {
        if (event) event.preventDefault();
        closeAddressModal();
    }

    // ==========================================
    // PAYME VERIFICATION FLOW
    // ==========================================

    function showVerificationError(message) {
        var errorEl = document.getElementById('verification-error');
        errorEl.textContent = message;
        errorEl.style.display = 'block';
    }

    function closeVerificationModal() {
        document.getElementById('verification-modal').style.display = 'none';
        if (verificationState.timerInterval) {
            clearInterval(verificationState.timerInterval);
        }
    }

    function cancelVerification() {
        closeVerificationModal();
        document.getElementById('loading-overlay').style.display = 'none';
        document.getElementById('place-order-btn').disabled = false;
    }

    function startVerificationCountdown(seconds) {
        var remaining = seconds;
        var resendBtn = document.getElementById('resend-btn');
        var timerEl = document.getElementById('verification-timer');

        if (verificationState.timerInterval) clearInterval(verificationState.timerInterval);

        resendBtn.disabled = true;

        timerEl.innerHTML = '';
        var codeExpiresText = document.createTextNode(PAGE_DATA.i18n.code_expires_in + ' ');
        var countdownSpan = document.createElement('span');
        countdownSpan.id = 'countdown';
        countdownSpan.textContent = remaining;
        var suffix = document.createTextNode('s');
        timerEl.appendChild(codeExpiresText);
        timerEl.appendChild(countdownSpan);
        timerEl.appendChild(suffix);

        verificationState.timerInterval = setInterval(function () {
            remaining--;
            var cd = document.getElementById('countdown');
            if (cd) cd.textContent = remaining;

            if (remaining <= 0) {
                clearInterval(verificationState.timerInterval);
                if (cd) cd.textContent = '0';
                resendBtn.disabled = false;

                timerEl.innerHTML = '';
                var expiredText = document.createTextNode(PAGE_DATA.i18n.code_expired + ' ');
                var requestNew = document.createElement('a');
                requestNew.href = '#';
                requestNew.className = 'timer-expired-link';
                requestNew.textContent = PAGE_DATA.i18n.request_new_code;
                requestNew.addEventListener('click', function (e) {
                    e.preventDefault();
                    resendVerificationCode();
                });
                timerEl.appendChild(expiredText);
                timerEl.appendChild(requestNew);
            }
        }, 1000);
    }

    function showVerificationModal(data) {
        verificationState.token = data.token;
        verificationState.maskedPhone = data.masked_phone;
        verificationState.waitSeconds = data.wait_seconds || 60;
        verificationState.attemptsRemaining = 3;
        verificationState.cardMetadata = {
            masked_number: data.masked_number,
            expire: data.expire,
            cardholder_name: document.getElementById('card-holder').value,
            recurrent: data.recurrent || false
        };

        document.getElementById('verification-phone').textContent = data.masked_phone;
        document.getElementById('verification-code').value = '';
        document.getElementById('verification-error').style.display = 'none';
        document.getElementById('attempts-remaining').style.display = 'none';

        startVerificationCountdown(verificationState.waitSeconds);

        document.getElementById('verification-modal').style.display = 'flex';
        document.getElementById('verification-code').focus();
    }

    function onVerificationCodeInput(input) {
        input.value = input.value.replace(/[^a-zA-Z0-9]/g, '').toUpperCase();
        if (input.value.length >= 6) {
            submitVerificationCode();
        }
    }

    async function resendVerificationCode() {
        var resendBtn = document.getElementById('resend-btn');
        resendBtn.disabled = true;
        resendBtn.textContent = PAGE_DATA.i18n.sending;

        try {
            var response = await apiRequest('/payments/cards/resend-code', {
                method: 'POST',
                body: JSON.stringify({ token: verificationState.token })
            });

            var data = await response.json();

            if (data.success) {
                verificationState.waitSeconds = data.data.wait_seconds || 60;
                verificationState.attemptsRemaining = 3;

                startVerificationCountdown(verificationState.waitSeconds);

                document.getElementById('verification-error').style.display = 'none';
                document.getElementById('attempts-remaining').style.display = 'none';
                showNotification(PAGE_DATA.i18n.new_code_sent, 'success');
            } else {
                showVerificationError(data.message || PAGE_DATA.i18n.resend_failed);
            }
        } catch (error) {
            showVerificationError(PAGE_DATA.i18n.resend_failed);
        } finally {
            resendBtn.textContent = PAGE_DATA.i18n.resend_code;
        }
    }

    async function submitVerificationCode() {
        var code = document.getElementById('verification-code').value.trim();

        if (!code || code.length < 4) {
            showVerificationError(PAGE_DATA.i18n.enter_code);
            return;
        }

        var verifyBtn = document.getElementById('verify-btn');
        verifyBtn.disabled = true;
        verifyBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + PAGE_DATA.i18n.verifying;

        try {
            var response = await apiRequest('/payments/cards/verify', {
                method: 'POST',
                body: JSON.stringify({
                    token: verificationState.token,
                    code: code
                })
            });

            var data = await response.json();

            if (data.success && data.data.verified) {
                closeVerificationModal();
                await processPaymentWithVerifiedCard();
            } else {
                verificationState.attemptsRemaining =
                    (data.data && data.data.attempts_remaining !== undefined) ?
                        data.data.attempts_remaining :
                        (verificationState.attemptsRemaining - 1);

                if (verificationState.attemptsRemaining <= 0 || (data.data && data.data.request_new_code)) {
                    showVerificationError(PAGE_DATA.i18n.too_many_attempts);
                    document.getElementById('resend-btn').disabled = false;
                } else {
                    showVerificationError(data.message || PAGE_DATA.i18n.invalid_code);
                    document.getElementById('attempts-remaining').style.display = 'block';
                    document.getElementById('attempts-count').textContent = verificationState.attemptsRemaining;
                }

                document.getElementById('verification-code').value = '';
                document.getElementById('verification-code').focus();
            }
        } catch (error) {
            showVerificationError(PAGE_DATA.i18n.verify_failed);
        } finally {
            verifyBtn.disabled = false;
            verifyBtn.innerHTML = PAGE_DATA.i18n.verify;
        }
    }

    async function processPaymentWithVerifiedCard() {
        document.getElementById('payment-processing-modal').style.display = 'flex';

        try {
            var response = await apiRequest('/payments/process-card-payment', {
                method: 'POST',
                body: JSON.stringify({
                    order_id: currentOrderId,
                    token: verificationState.token,
                    save_card: true,
                    card_metadata: verificationState.cardMetadata
                })
            });

            var data = await response.json();

            if (data.success) {
                await clearCartAfterOrder();
                window.location.href = data.data.redirect_url ||
                    '/my-orders?order_id=' + data.data.order_id + '&payment=success';
            } else {
                throw new Error(data.message || 'Payment failed');
            }
        } catch (error) {
            document.getElementById('payment-processing-modal').style.display = 'none';
            showNotification(error.message || PAGE_DATA.i18n.payment_failed, 'error');
            document.getElementById('place-order-btn').disabled = false;
        }
    }

    // ==========================================
    // MAIN PLACE ORDER
    // ==========================================

    async function createOrderForPayme() {
        var orderData = {
            items: cartItems,
            delivery_address_id: selectedAddress,
            delivery_date: deliveryDate,
            delivery_time_slot: selectedTimeSlot,
            payment_method: 'payme',
            delivery_notes: document.getElementById('order-notes').value || null,
            source: 'web'
        };

        var orderResponse = await apiRequest('/orders/', {
            method: 'POST',
            body: JSON.stringify(orderData)
        });

        var orderResult = await orderResponse.json();

        if (!orderResult.success) throw new Error(orderResult.message || 'Order creation failed');

        currentOrderId = orderResult.data.order.id;
    }

    async function placeOrderWithCash() {
        document.getElementById('loading-overlay').style.display = 'flex';
        document.getElementById('place-order-btn').disabled = true;

        try {
            var orderData = {
                items: cartItems,
                delivery_address_id: selectedAddress,
                delivery_date: deliveryDate,
                delivery_time_slot: selectedTimeSlot,
                payment_method: 'cash',
                delivery_notes: document.getElementById('order-notes').value || null,
                source: 'web'
            };

            var response = await apiRequest('/orders/', {
                method: 'POST',
                body: JSON.stringify(orderData)
            });

            var data = await response.json();

            if (data.success) {
                await clearCartAfterOrder();
                window.location.href = '/order-confirmation?order_id=' + data.data.order.id;
            } else {
                throw new Error(data.message || 'Order creation failed');
            }
        } catch (error) {
            showNotification(error.message || PAGE_DATA.i18n.place_order_failed, 'error');
            document.getElementById('loading-overlay').style.display = 'none';
            document.getElementById('place-order-btn').disabled = false;
        }
    }

    async function placeOrderWithClick() {
        document.getElementById('loading-overlay').style.display = 'flex';
        document.getElementById('place-order-btn').disabled = true;

        try {
            var orderData = {
                items: cartItems,
                delivery_address_id: selectedAddress,
                delivery_date: deliveryDate,
                delivery_time_slot: selectedTimeSlot,
                payment_method: 'click',
                delivery_notes: document.getElementById('order-notes').value || null,
                source: 'web'
            };

            var response = await apiRequest('/orders/', {
                method: 'POST',
                body: JSON.stringify(orderData)
            });

            var data = await response.json();

            if (data.success && data.data.payment_url) {
                await clearCartAfterOrder();
                window.location.href = data.data.payment_url;
            } else {
                throw new Error(data.message || 'Order creation failed');
            }
        } catch (error) {
            showNotification(error.message || PAGE_DATA.i18n.place_order_failed, 'error');
            document.getElementById('loading-overlay').style.display = 'none';
            document.getElementById('place-order-btn').disabled = false;
        }
    }

    async function placeOrder() {
        if (!selectedAddress) {
            showNotification(PAGE_DATA.i18n.select_delivery_address, 'warning');
            return;
        }

        if (!deliveryDate) {
            showNotification(PAGE_DATA.i18n.select_delivery_date, 'warning');
            return;
        }

        if (!selectedTimeSlot) {
            showNotification(PAGE_DATA.i18n.select_delivery_time, 'warning');
            return;
        }

        var agreedToTerms = document.getElementById('agree-terms').checked;
        if (!agreedToTerms) {
            showNotification(PAGE_DATA.i18n.agree_terms, 'warning');
            return;
        }

        if (selectedPaymentMethod === 'click') {
            await placeOrderWithClick();
        } else {
            await placeOrderWithCash();
        }
    }

    document.addEventListener('DOMContentLoaded', async function () {
        if (typeof CURRENT_USER === 'undefined' || !CURRENT_USER) {
            window.location.href = PAGE_DATA.login_url + '?next=' + encodeURIComponent(PAGE_DATA.checkout_url);
            return;
        }

        if (window.cartSyncPromise) await window.cartSyncPromise;

        var today = new Date().toISOString().split('T')[0];
        document.getElementById('delivery-date').min = today;

        var tomorrow = new Date();
        tomorrow.setDate(tomorrow.getDate() + 1);
        document.getElementById('delivery-date').value = tomorrow.toISOString().split('T')[0];

        await loadCartSummary();
        loadTimeSlots();

        var firstAddress = document.querySelector('.address-card[data-address-id]');
        if (firstAddress) {
            selectedAddress = parseInt(firstAddress.dataset.addressId, 10);
            await calculateDeliveryFee(selectedAddress);
        }

        var dateInput = document.getElementById('delivery-date');
        if (dateInput) dateInput.addEventListener('change', loadTimeSlots);

        var cardNumInput = document.getElementById('card-number');
        if (cardNumInput) cardNumInput.addEventListener('input', function () { formatCardNumber(this); });

        var cardExpInput = document.getElementById('card-expiry');
        if (cardExpInput) cardExpInput.addEventListener('input', function () { formatCardExpiry(this); });

        var verificationCodeInput = document.getElementById('verification-code');
        if (verificationCodeInput) verificationCodeInput.addEventListener('input', function () { onVerificationCodeInput(this); });

        var addressForm = document.getElementById('address-form');
        if (addressForm) addressForm.addEventListener('submit', saveAddress);

        document.body.addEventListener('click', function (e) {
            var target = e.target.closest('[data-action]');
            if (!target) return;

            var action = target.dataset.action;

            switch (action) {
                case 'select-address':
                    selectAddress(parseInt(target.dataset.addressId, 10));
                    break;
                case 'show-add-address':
                    showAddAddressModal();
                    break;
                case 'close-address-modal':
                    closeAddressModal();
                    break;
                case 'select-payment-method':
                    selectPaymentMethod(target.dataset.method, target);
                    break;
                case 'select-time-slot':
                    selectTimeSlot(
                        parseInt(target.dataset.slotId, 10),
                        target.dataset.startTime,
                        target.dataset.endTime,
                        target
                    );
                    break;
                case 'select-time-slot-manual':
                    selectTimeSlotManual(target.dataset.slotTime, target);
                    break;
                case 'place-order':
                    placeOrder();
                    break;
                case 'cancel-verification':
                    cancelVerification();
                    break;
                case 'resend-verification':
                    resendVerificationCode();
                    break;
                case 'submit-verification':
                    submitVerificationCode();
                    break;
            }
        });
    });
})();
