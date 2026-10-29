// The ONE renderer and write path for what a customer may do with an order: Pay, Skip/Cancel/Decline and the
// store's Confirm of an order its sales agent placed (spec 2026-10-27 §5.3, §8, D1, D10-D13). The My orders cards,
// the order modal and the tracking page all draw through `OrderActions.render`, so the three can never disagree.
//
// This script decides nothing. Every button, its label, the sentence its inline confirm asks, the reasons a skip
// offers, the amount a Pay charges and the "Pay by" line are the backend's `order.customer_actions` as the API
// publishes it; the writes echo back what the screen showed (`expected_amount`, `expected_via`), and the backend
// answers 409 when that moved. It reads no status, rail, date or amount, and it opens no native dialog.
(function (window) {
    'use strict';
    // The modal's partial and the tracking page may both ask for this script; one copy serves the page.
    if (window.OrderActions) return;

    var document = window.document;
    // One write at a time on a page: a double click, or a second button, never sends a second request.
    var inFlight = false;

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    // Send one write through the shared helper (cookies, CSRF header and X-Platform) and read its answer. Never
    // throws. `send` is the apiRequest call itself, so each route below is spelled out where it is made.
    async function answer(send) {
        var payload = {};
        var status = 0;
        try {
            var response = await send();
            status = response.status;
            try { payload = await response.json(); } catch (err) { payload = {}; }
            return { ok: response.ok && payload.success !== false, status: status, message: payload.message || null, payload: payload };
        } catch (err) {
            return { ok: false, status: status, message: null, payload: payload };
        }
    }

    function outcome(result) {
        return { ok: result.ok, status: result.status, message: result.message };
    }

    // Open a Click link the backend has published: at once, or, when the answer says Click is not ready yet
    // (`payment_ready_in_seconds`, the wait while the items are registered with the tax authority), after that many
    // seconds, saying so in `options.statusEl` (when the surface has one). The wait is the backend's; this only keeps it.
    function openPaymentWhenReady(payload, options) {
        options = options || {};
        var seconds = Number(payload && payload.payment_ready_in_seconds) || 0;
        var url = payload && payload.payment_url;
        if (seconds > 0) {
            if (options.statusEl) {
                options.statusEl.textContent = (options.words && options.words.preparing_payment) || '';
                options.statusEl.hidden = false;
            }
            setTimeout(function () { window.OrderActions.navigate(url); }, seconds * 1000);
        } else {
            window.OrderActions.navigate(url);
        }
    }

    // Pay: the Click link for the order. Posts what the screen showed (`expected_amount`).
    async function pay(orderId, pay, options) {
        var result = await answer(function () {
            return window.apiRequest('/orders/' + orderId + '/retry-payment', {
                method: 'POST',
                body: JSON.stringify({ channel: 'web', expected_amount: pay.expected_amount })
            });
        });
        var data = result.payload && result.payload.data;
        if (result.ok && data && data.payment_url) {
            openPaymentWhenReady(data, { words: options && options.words, statusEl: options && options.statusEl });
        } else if (result.ok) {
            result.ok = false;
            result.message = (options && options.words && options.words.failed) || result.message;
        }
        return outcome(result);
    }

    // Skip, cancel or decline: posts the `via` the screen showed, and for a skip the reason picked.
    async function remove(orderId, cancel, reason) {
        var body = { expected_via: cancel.via };
        reason = reason || {};
        if (reason.reason_code) body.reason_code = reason.reason_code;
        if (reason.reason_text) body.reason_text = reason.reason_text;
        return outcome(await answer(function () {
            return window.apiRequest('/orders/' + orderId + '/cancel', { method: 'POST', body: JSON.stringify(body) });
        }));
    }

    // The store's Confirm of an agent's order (the bot's Confirm, through the same route). Its answer carries no
    // sentence of its own, so a success has no message and the page says its own.
    async function confirmOrder(orderId) {
        var result = await answer(function () {
            return window.apiRequest('/orders/' + orderId + '/agent-confirmation', {
                method: 'POST',
                body: JSON.stringify({ action: 'confirm', channel: 'web' })
            });
        });
        if (result.ok) result.message = null;
        return outcome(result);
    }

    function navigate(url) {
        window.location.href = url;
    }

    function notify(message, type) {
        if (message && typeof window.showNotification === 'function') window.showNotification(message, type);
    }

    // The error line that is a direct child of `host` (a panel's own, or the container's for Pay and Confirm).
    function errorOf(host) {
        for (var i = 0; i < host.children.length; i += 1) {
            if (host.children[i].classList.contains('order-actions__error')) return host.children[i];
        }
        return null;
    }

    // Made when first needed, so the first one found in a surface is the one that applies.
    function showError(host, message) {
        var error = errorOf(host);
        if (!error) {
            error = el('p', 'order-actions__error text-danger mt-2 mb-0');
            error.setAttribute('role', 'alert');
            host.appendChild(error);
        }
        error.textContent = message;
        error.hidden = false;
    }

    function clearError(host) {
        var error = errorOf(host);
        if (error) error.hidden = true;
    }

    // A confirm panel left open, or a write in flight: the page must not reload under the customer.
    function isBusy() {
        return inFlight || document.querySelector('.order-actions__confirm:not([hidden])') !== null;
    }

    // Run one write behind `button`: aria-disabled and "working" while it is out, then its answer.
    // 200: the message (a toast), then onChanged. 409: the message (the refresh line, or the refusal's sentence)
    // and onChanged, so the surface redraws from the new answer. Anything else: the message inline in `errorHost`.
    async function run(button, errorHost, task, ctx) {
        if (inFlight) return;
        inFlight = true;
        var label = button.textContent;
        button.setAttribute('aria-disabled', 'true');
        button.textContent = ctx.words.working;
        clearError(errorHost);
        var result;
        try {
            result = await task();
        } catch (err) {
            result = { ok: false, status: 0, message: null };
        }
        // A Pay that succeeded has already started leaving the page (the link may open up to ~45 s later): the button
        // stays "working" and busy, so no second tap sends a second payment and the 30 s reload does not fire first.
        var leaving = result.ok && ctx.redraw === false;
        if (!leaving) {
            inFlight = false;
            button.removeAttribute('aria-disabled');
            button.textContent = label;
        }
        if (result.ok) {
            if (ctx.toast !== false) notify(result.message || ctx.words.confirmed, 'success');
            if (ctx.redraw !== false && ctx.onChanged) ctx.onChanged(result);
        } else if (result.status === 409 && !ctx.refusalInline) {
            notify(result.message || ctx.words.failed, 'error');
            if (ctx.onChanged) ctx.onChanged(result);
        } else {
            showError(errorHost, result.message || ctx.words.failed);
        }
    }

    function actionButton(kind, className, label, small) {
        var button = el('button', 'btn ' + className + (small ? ' btn-sm' : ''), label);
        button.type = 'button';
        button.dataset.orderAction = kind;
        return button;
    }

    // The inline confirm of a skip, cancel or decline: its sentence, a skip's reasons, Yes and Keep.
    function buildPanel(order, cancel, ctx, trigger) {
        var words = ctx.words;
        var panel = el('div', 'order-actions__confirm mt-2');
        panel.setAttribute('role', 'group');
        panel.setAttribute('aria-label', cancel.label);
        panel.hidden = true;
        panel.appendChild(el('p', 'mb-2', cancel.confirm_text));

        var other = null;
        var reasons = cancel.reasons || [];
        if (reasons.length) {
            panel.appendChild(el('p', 'mb-1 small', words.reason_prompt));
            reasons.forEach(function (reason) {
                var row = el('div', 'mb-1');
                var label = el('label', 'mb-0');
                var radio = el('input', 'mr-2');
                radio.type = 'radio';
                radio.name = 'order-skip-reason-' + order.id;
                radio.value = reason.code;
                label.appendChild(radio);
                label.appendChild(document.createTextNode(reason.label));
                row.appendChild(label);
                panel.appendChild(row);
            });
            other = el('textarea', 'order-actions__reason-text form-control mb-2');
            other.rows = 2;
            other.maxLength = 500;
            other.placeholder = words.reason_text;
            other.hidden = true;
            panel.appendChild(other);
        }

        var yes = actionButton('cancel-confirm', 'btn-danger', cancel.label, false);
        yes.removeAttribute('data-order-action');
        var keep = actionButton('cancel-keep', 'btn-secondary', words.keep, false);
        keep.removeAttribute('data-order-action');
        panel.appendChild(yes);
        panel.appendChild(keep);

        function picked() {
            var radios = panel.querySelectorAll('input[type="radio"]');
            for (var i = 0; i < radios.length; i += 1) if (radios[i].checked) return radios[i].value;
            return null;
        }

        function syncReasonText() {
            if (other) other.hidden = picked() !== 'other';
        }
        panel.addEventListener('change', syncReasonText);
        panel.addEventListener('click', syncReasonText);

        keep.addEventListener('click', function () {
            panel.hidden = true;
            clearError(panel);
            trigger.setAttribute('aria-expanded', 'false');
            trigger.focus();
        });
        yes.addEventListener('click', function () {
            var code = picked();
            var reason = {};
            if (code) reason.reason_code = code;
            if (code === 'other' && other && other.value.trim()) reason.reason_text = other.value.trim();
            run(yes, panel, function () { return remove(order.id, cancel, reason); }, ctx);
        });
        return panel;
    }

    // Draw `order.customer_actions` into `container`. options: {words, onChanged(result), subscriptionsUrl, small}.
    function render(container, order, options) {
        options = options || {};
        var ctx = { words: options.words || {}, onChanged: options.onChanged };
        var actions = (order && order.customer_actions) || {};
        var pay_ = actions.pay || {};
        var confirm_ = actions.confirm || {};
        var cancel = actions.cancel || {};
        var small = options.small === true;

        container.textContent = '';
        if (!container.dataset.orderActionsBound) {
            container.dataset.orderActionsBound = '1';
            // The cards are themselves clickable (they open the order); a tap inside the actions is not that.
            container.addEventListener('click', function (event) { event.stopPropagation(); });
        }

        var row = el('div', 'order-actions__buttons d-flex flex-wrap justify-content-end align-items-start');
        container.appendChild(row);

        if (pay_.allowed) {
            var payButton = actionButton('pay', 'btn-primary ml-2', pay_.label, small);
            payButton.addEventListener('click', function () {
                run(payButton, container, function () { return pay(order.id, pay_, { words: ctx.words }); },
                    Object.assign({}, ctx, { toast: false, redraw: false }));
            });
            row.appendChild(payButton);
        }
        if (confirm_.allowed) {
            var confirmButton = actionButton('confirm', 'btn-success ml-2', confirm_.label, small);
            confirmButton.addEventListener('click', function () {
                run(confirmButton, container, function () { return confirmOrder(order.id); }, ctx);
            });
            row.appendChild(confirmButton);
        }
        var panel = null;
        if (cancel.allowed) {
            var cancelButton = actionButton('cancel', 'btn-outline-danger ml-2', cancel.label, small);
            cancelButton.setAttribute('aria-expanded', 'false');
            panel = buildPanel(order, cancel, ctx, cancelButton);
            cancelButton.addEventListener('click', function () {
                if (inFlight) return;
                clearError(panel);
                panel.hidden = false;
                cancelButton.setAttribute('aria-expanded', 'true');
                panel.querySelectorAll('button')[1].focus();
            });
            row.appendChild(cancelButton);
        }

        if (pay_.allowed && pay_.deadline_label) {
            container.appendChild(el('small', 'order-actions__deadline text-muted d-block text-right mt-1', pay_.deadline_label));
        }
        if (panel) container.appendChild(panel);
        if (cancel.subscription_id && options.subscriptionsUrl) {
            var link = el('a', 'order-actions__subscription d-block text-right small mt-1', ctx.words.open_subscription);
            link.href = options.subscriptionsUrl + '?sub=' + encodeURIComponent(cancel.subscription_id) + '#subscription';
            container.appendChild(link);
        }
        // Nothing published and no link: hide the container, so a layout cell is not left blank.
        container.hidden = !row.hasChildNodes() && container.children.length === 1;
    }

    // The checkout's tax-system outage (503 ASL_BELGISI_UNAVAILABLE): the order was cancelled and nothing was
    // charged. Offers Pay with cash (unless the backend published the customer as cash-restricted) and Try again.
    // `failure` is the 503 answer; Pay with cash opens the new order, a refusal (400, 409) is shown in place.
    function showCashRescue(container, failure, options) {
        options = options || {};
        var words = options.words || {};
        var data = (failure && failure.data) || {};
        var restricted = !!(data.payment_restrictions && data.payment_restrictions.cod_restricted);
        container.textContent = '';
        container.hidden = false;
        container.appendChild(el('p', 'order-actions__rescue mb-2', failure && failure.message));
        var row = el('div', 'order-actions__buttons');
        container.appendChild(row);

        if (!restricted) {
            var cash = actionButton('switch-to-cash', 'btn-primary mr-2', words.switch_to_cash, false);
            cash.addEventListener('click', function () {
                run(cash, container, async function () {
                    var result = await answer(function () {
                        return window.apiRequest('/orders/' + data.cancelled_order_id + '/retry-with-cash', {
                            method: 'POST',
                            body: JSON.stringify({})
                        });
                    });
                    var order = result.ok && result.payload.data && result.payload.data.order;
                    if (order) window.OrderActions.navigate('/order-confirmation?order_id=' + order.id);
                    else result.ok = false;
                    return outcome(result);
                }, { words: words, toast: false, redraw: false, refusalInline: true });
            });
            row.appendChild(cash);
        }
        var again = actionButton('try-again', 'btn-outline-secondary', words.try_again, false);
        again.addEventListener('click', function () {
            if (inFlight) return;
            container.textContent = '';
            container.hidden = true;
            if (options.onRetry) options.onRetry();
        });
        row.appendChild(again);
    }

    window.OrderActions = {
        render: render,
        isBusy: isBusy,
        pay: pay,
        remove: remove,
        confirm: confirmOrder,
        navigate: navigate,
        openPaymentWhenReady: openPaymentWhenReady,
        showCashRescue: showCashRescue
    };
})(window);
