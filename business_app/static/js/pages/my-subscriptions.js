/*
 * /my-subscriptions (spec 2026-10-26 §4, §12; UX brief). ONE renderer over the API's published fields.
 *
 * The page decides nothing: every date, window, amount, sentence and chip is a backend label; what the
 * customer may do now is `allowed_actions` (with each action's consequences); the next-delivery card's state
 * is `upcoming_delivery.state`; the waterline is `upcoming_dates`; the Orders rows are `/history`; a drafted
 * change is checked and worded by `POST /subscriptions/<id>/preview` before it can be saved. No status rules,
 * date maths or money maths live here, and no date string is ever parsed. The only words this script adds are
 * the page-data island's (`cabinet.subs.*` and the backend's display labels, seeded en/uz/ru).
 *
 * Read side (plan Task 4a): strip, body header, next-delivery card, waterline, Items / Schedule / Payment /
 * Orders in read mode, footer, empty state.
 * Write side (plan Task 4b; UX brief interaction rules): Pay (one function, the card's and every Orders
 * row's), Skip and Pause as inline two-step confirms, Resume, the Cancel modal, an Orders row's Skip or Cancel,
 * and the read-then-edit sections, one at a time, each with its sticky save bar: Items (stepper, remove, the
 * product picker, one atomic save), Schedule (frequency, day and slot tiles with a debounced backend preview)
 * and Payment (the methods the backend offers). Every request goes through base.js `apiRequest`; an order's Pay
 * and Skip/Cancel go through order-actions.js, the write path every order surface shares. The exact requests are
 * the contract in tests/integration/test_web_subscription_management_contract.py.
 *
 * Spec 2026-10-27 §16 M2, M3: every sentence and button label is the backend's (`allowed_actions`, an order's
 * `customer_actions`, `upcoming_delivery`), and every write echoes what its screen showed. A 409 means that screen
 * changed meanwhile: the page shows the backend's sentence for it and reads the latest (`refreshWhenChanged`).
 */
(function ($) {
    'use strict';

    var PAGE = getPageData();
    var I18N = PAGE.i18n || {};

    var LIST_URL = '/subscriptions/?per_page=50';
    // The payment menu of the subscription shown; its delivery address completes the URL.
    var METHODS_URL = '/payments/methods?context=subscription&delivery_address_id=';
    // The product picker: every product a subscription can take, at its subscription price (unpaged).
    var PRODUCTS_URL = '/subscriptions/products';
    var SLOTS_URL = '/delivery/time-slots';       // the slots a weekday (and frequency) offers, and the delivery days
    var HISTORY_PAGE_SIZE = 5;   // rows shown first (UX brief: 5, then "Show more" appends 10)
    var HISTORY_MORE_PAGES = 2;  // pages of HISTORY_PAGE_SIZE that "Show more orders" appends
    var SKELETON_DELAY_MS = 300; // the skeleton appears only on a slow load (UX brief "SWITCH LOADING")
    var PREVIEW_DELAY_MS = 300;  // a drafted change is previewed once the customer pauses (UX brief)

    // The Schedule editor's fields: which frequencies choose a weekday, which a day of the month (the PUT's).
    var FREQUENCIES = ['daily', 'weekly', 'biweekly', 'monthly'];
    var WEEKDAY_FREQUENCIES = { weekly: true, biweekly: true };
    var DAYS_OF_MONTH = 31;
    // The Any time tile's value. A draft's slot is a number (a slot), `null` (Any time) or `undefined` (not chosen yet).
    var ANY_TIME = 'any';

    // The one chip palette (UX brief tokens): tone -> class, code -> Font Awesome icon.
    var CHIP_ICONS = {
        paid: 'fa-check', delivered: 'fa-check',
        awaiting_payment: 'fa-clock', awaiting_new_date: 'fa-clock', paused: 'fa-pause',
        placed: 'fa-box', on_the_way: 'fa-truck', pay_on_delivery: 'fa-money-bill', bank_transfer: 'fa-university',
        cancelled: 'fa-times', returned: 'fa-undo', skipped: 'fa-forward',
        payment_failed: 'fa-exclamation', not_offered: 'fa-exclamation-triangle'
    };
    var TONE_ICONS = { success: 'fa-check', warning: 'fa-clock', info: 'fa-box', neutral: 'fa-times', danger: 'fa-exclamation' };

    var SECTION_HEADINGS = {
        items: 'section.items', schedule: 'section.schedule', address: 'section.address', payment: 'section.payment',
        orders: 'section.orders'
    };
    // After a card action focus stays in the card: its first action, else the subscription's name.
    var HERO_FOCUS = ['#subscription .subs-hero__actions .subs-btn', '#sub-h'];

    var state = {
        subs: [],
        selectedId: null,
        showCancelled: false,
        history: null,  // {subId, rows, page, total, hasNext, loading, failed, token}
        methods: null,  // the payment menu: {subId, list, loading, failed}
        addresses: null,  // the saved addresses, read when the Address editor opens: {list, loading, failed}
        status: null,   // the last action's result line: {subId, where: 'hero'|section, text, error}
        // The one open inline confirm, the card's or an Orders row's: {subId, kind: 'skip'|'pause'|'order', orderId
        // (an Orders row's order, else null), reason, other, error}.
        confirm: null,
        edit: null      // the one open section form (see openEdit)
    };
    var historyToken = 0;
    var methodsToken = 0;
    var addressesToken = 0;
    var previewTimer = null;
    var modalReturn = null;  // what a modal gives focus back to
    var afterModal = null;   // what runs once a modal has closed (Pause instead, Deliver less often, Add, a cancel's answer)

    // ------------------------------------------------------------------ words and markup

    function esc(value) {
        if (value === null || value === undefined) return '';
        return String(value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function fill(template, values) {
        return String(template).replace(/\{(\w+)\}/g, function (whole, name) {
            return Object.prototype.hasOwnProperty.call(values, name) ? values[name] : whole;
        });
    }

    // Plain text of an island string, its {placeholders} filled with plain text.
    function text(key, values) {
        var template = I18N[key];
        if (template === undefined || template === null) return '';
        return values ? fill(template, values) : template;
    }

    // Markup of an island string: the string escaped, its {placeholders} filled with ready markup.
    function html(key, markup) {
        var template = I18N[key];
        if (template === undefined || template === null) return '';
        return fill(esc(template), markup || {});
    }

    function icon(name) {
        return '<i class="far ' + name + '" aria-hidden="true"></i>';
    }

    // A published `{code, label, tone}` chip (or an island chip), never coloured without its icon and text.
    function chip(published) {
        if (!published || !published.label) return '';
        var glyph = CHIP_ICONS[published.code] || TONE_ICONS[published.tone] || TONE_ICONS.neutral;
        return '<span class="subs-chip subs-chip--' + esc(published.tone || 'neutral') + '">' +
            icon(glyph) + esc(published.label) + '</span>';
    }

    function bottle(variant) {
        return '<svg class="subs-bottle subs-bottle--' + variant + '" viewBox="0 0 22 34" aria-hidden="true" ' +
            'focusable="false"><use href="#subs-bottle"></use></svg>';
    }

    function allowed(sub, action) {
        var actions = sub.allowed_actions || {};
        return Boolean(actions[action] && actions[action].allowed);
    }

    // The subscription's own published lifecycle value, read only to group the strip and name the state;
    // what may be done to it is always `allowed_actions`.
    function lifecycle(sub) {
        return sub.status === 'paused' || sub.status === 'cancelled' ? sub.status : 'active';
    }

    function lifecycleChip(sub) {
        var phase = lifecycle(sub);
        if (phase === 'paused') return chip({ code: 'paused', label: text('chip.paused'), tone: 'warning' });
        if (phase === 'cancelled') return chip({ code: 'cancelled', label: text('chip.cancelled'), tone: 'neutral' });
        return '';
    }

    function actionButton(variant, action, label, extra) {
        return '<button type="button" class="subs-btn subs-btn--' + variant + '" data-action="' + action + '"' +
            (extra || '') + '>' + esc(label) + '</button>';
    }

    // "Thursday, 29 October, 10:00–12:00" from a published delivery or order reference.
    function dateWindow(ref) {
        if (!ref || !ref.label_long) return '';
        return ref.window_label ? text('hero.date_window', { date: ref.label_long, window: ref.window_label }) : ref.label_long;
    }

    function weekdayName(day) {
        return (PAGE.weekday_names || [])[day] || '';
    }

    function methodLabel(method) {
        return (PAGE.method_labels || {})[method] || method;
    }

    async function readJson(response) {
        try {
            return (await response.json()) || {};
        } catch (error) {
            return {};
        }
    }

    // A refused request's own translated message (a 4xx), else the page's fallback line.
    function refusal(response, result, fallbackKey) {
        if (response && response.status >= 400 && response.status < 500 && result && result.message) return result.message;
        return text(fallbackKey);
    }

    function reducedMotion() {
        return Boolean(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
    }

    function scrollIntoSight(element, block) {
        if (element && element.scrollIntoView) element.scrollIntoView({ behavior: reducedMotion() ? 'auto' : 'smooth', block: block });
    }

    // Re-draw part of the page and give focus back to the control that had it (same `data-focus`).
    function keepFocus(draw) {
        var active = document.activeElement;
        var key = active && active.getAttribute ? active.getAttribute('data-focus') : null;
        draw();
        if (key) {
            var again = document.querySelector('#subs [data-focus="' + key + '"]');
            if (again && again !== document.activeElement) again.focus();
        }
    }

    // ------------------------------------------------------------------ the strip

    // Needs payment, then active, then paused; cancelled last (UX brief "SUBSCRIPTION STRIP"). Within a
    // group the backend's order (newest first) stands.
    function strip(subs) {
        var groups = { pay: [], active: [], paused: [], cancelled: [] };
        subs.forEach(function (sub) {
            var phase = lifecycle(sub);
            if (phase !== 'cancelled' && allowed(sub, 'pay')) groups.pay.push(sub);
            else groups[phase].push(sub);
        });
        return {
            live: groups.pay.concat(groups.active, groups.paused),
            cancelled: groups.cancelled
        };
    }

    function defaultSelection(subs) {
        var groups = strip(subs);
        var first = groups.live[0] || groups.cancelled[0];
        return first ? first.id : null;
    }

    function subHref(id) {
        var params = new URLSearchParams(window.location.search);
        params.set('sub', String(id));
        return '?' + params.toString() + '#subscription';
    }

    function cardLine(sub) {
        var phase = lifecycle(sub);
        var upcoming = sub.upcoming_delivery;
        if (phase === 'cancelled') {
            return sub.cancelled_at_label
                ? '<span class="subs-card__meta">' + esc(text('strip.cancelled_on', { date: sub.cancelled_at_label })) + '</span>'
                : lifecycleChip(sub);
        }
        if (phase === 'paused') return lifecycleChip(sub);
        if (allowed(sub, 'pay') && upcoming && upcoming.payment_status) return chip(upcoming.payment_status);
        if (upcoming && upcoming.label_short) {
            return '<span class="subs-card__meta">' + esc(text('strip.next', { date: upcoming.label_short })) + '</span>';
        }
        return '';
    }

    function cardGlyph(sub) {
        if (lifecycle(sub) !== 'active') return bottle('muted');
        var upcoming = sub.upcoming_delivery;
        return bottle(upcoming && upcoming.order_id ? 'placed' : 'planned');
    }

    function card(sub, hidden) {
        var current = sub.id === state.selectedId;
        return '<li class="subs-strip__item' + (lifecycle(sub) === 'cancelled' ? ' subs-strip__item--cancelled' : '') + '"' +
            (hidden ? ' hidden' : '') + '>' +
            '<a class="subs-card' + (lifecycle(sub) === 'cancelled' ? ' subs-card--muted' : '') + '" href="' + esc(subHref(sub.id)) +
            '" data-sub-id="' + esc(sub.id) + '"' + (current ? ' aria-current="true"' : '') + '>' +
            cardGlyph(sub) +
            '<span class="subs-card__text"><b class="subs-card__name">' + esc(sub.name) + '</b>' + cardLine(sub) + '</span>' +
            '</a></li>';
    }

    function renderStrip() {
        var groups = strip(state.subs);
        var parts = groups.live.map(function (sub) { return card(sub, false); });
        if (groups.cancelled.length) {
            if (groups.live.length) {
                var label = state.showCancelled
                    ? text('strip.hide_cancelled')
                    : text('strip.show_cancelled', { count: String(groups.cancelled.length) });
                parts.push('<li class="subs-strip__item subs-strip__item--toggle">' +
                    '<button type="button" class="subs-card subs-card--toggle" data-action="toggle-cancelled" aria-expanded="' +
                    (state.showCancelled ? 'true' : 'false') + '">' + esc(label) + '</button></li>');
            }
            var hidden = groups.live.length > 0 && !state.showCancelled;
            groups.cancelled.forEach(function (sub) { parts.push(card(sub, hidden)); });
        }
        parts.push('<li class="subs-strip__item subs-strip__item--new"><a class="subs-card subs-card--new" href="' +
            esc(PAGE.builder_url) + '">' + esc(text('strip.new')) + '</a></li>');
        $('#subs-strip').html(parts.join(''));
        $('#subs-strip-nav').prop('hidden', false);
    }

    // ------------------------------------------------------------------ the next-delivery card

    function orderLink(upcoming) {
        if (!upcoming || !upcoming.order_id) return '';
        var chips = [];
        if (upcoming.state === 'on_the_way') {
            chips.push(chip({ code: 'on_the_way', label: text('chip.on_the_way'), tone: 'info' }));
        }
        chips.push(chip(upcoming.payment_status));
        return '<div class="subs-hero__order">' +
            '<button type="button" class="subs-link" data-action="open-order" data-order-id="' + esc(upcoming.order_id) + '">' +
            esc(text('hero.order', { number: upcoming.order_number })) + '</button>' +
            (upcoming.amount_label ? '<span class="subs-money">' + esc(upcoming.amount_label) + '</span>' : '') +
            chips.join('') + '</div>';
    }

    // "10:00–12:00, in 3 days", with the backend's own time words (`time_label`: the window, "Any time" or "Time to
    // be confirmed"). A date already past has no days label (the backend publishes None), and then the line is the
    // time alone.
    function windowLine(upcoming) {
        if (!upcoming.days_away_label) return upcoming.time_label;
        return text('hero.window_days', { window: upcoming.time_label, days: upcoming.days_away_label });
    }

    function unavailableNames(sub) {
        return (sub.subscription_items || []).filter(function (item) { return item.available === false; })
            .map(function (item) { return item.product_name || text('items.unknown_product'); });
    }

    // Review Focus 2: a discontinued product, on the card as on its row.
    function availabilityWarning(sub) {
        if (sub.items_availability === 'none') {
            return '<p class="subs-warning" role="note">' + icon('fa-exclamation-triangle') + esc(text('hero.none_available')) + '</p>';
        }
        if (sub.items_availability === 'some') {
            return '<p class="subs-warning" role="note">' + icon('fa-exclamation-triangle') +
                esc(text('hero.not_included', { products: unavailableNames(sub).join(', ') })) + '</p>';
        }
        return '';
    }

    // What a paused or cancelled card still has coming (Review Focus 4; spec §12): the order it keeps, under the
    // backend's sentence for it (`kept_line`: paid, or the driver already has it; None for any other order).
    function keptOrder(upcoming) {
        if (!upcoming || !upcoming.order_id) return '';
        return (upcoming.kept_line ? '<p class="subs-hero__note">' + esc(upcoming.kept_line) + '</p>' : '') + orderLink(upcoming);
    }

    // One bottle on a waterline. A preview draws the rhythm not saved yet as dashed `draft` bottles (UX brief).
    function waterlineItem(day, draft) {
        var variant = day.state === 'on_the_way' ? 'placed' : day.state;
        if (draft && day.state === 'planned') variant = 'draft';
        return '<li class="subs-wl__item subs-wl__item--' + esc(day.state) + '">' +
            bottle(variant) +
            '<span class="subs-wl__date" aria-hidden="true">' + esc(day.label_short) + '</span>' +
            (day.state === 'skipped' ? '<span class="subs-wl__note" aria-hidden="true">' + esc(text('wl.legend.skipped')) + '</span>' : '') +
            '<span class="sr-only">' + esc(text('wl.item', { date: day.label_long, state: text('wl.state.' + day.state) })) + '</span>' +
            '</li>';
    }

    function waterlineList(days, draft) {
        return '<ol class="subs-wl" aria-label="' + esc(text('wl.label')) + '">' +
            days.map(function (day) { return waterlineItem(day, draft); }).join('') + '</ol>';
    }

    function waterline(sub) {
        var days = sub.upcoming_dates || [];
        if (!days.length) return '';
        if (allowed(sub, 'resume')) {
            // Paused: the one delivery a resume today would give.
            return '<div class="subs-wl-band subs-wl-band--single">' +
                '<ol class="subs-wl subs-wl--single" aria-label="' + esc(text('wl.label')) + '">' + waterlineItem(days[0], false) + '</ol>' +
                '<p class="subs-wl__caption">' + esc(text('wl.if_resume', { date: days[0].label_short })) + '</p></div>';
        }
        var seen = {};
        days.forEach(function (day) { seen[day.state === 'on_the_way' ? 'on_the_way' : day.state] = true; });
        var legend = ['placed', 'on_the_way', 'planned', 'skipped'].filter(function (s) { return seen[s]; })
            .map(function (s) {
                return '<span class="subs-wl__key subs-wl__key--' + s + '"><i aria-hidden="true"></i>' + esc(text('wl.legend.' + s)) + '</span>';
            }).join('');
        return '<div class="subs-wl-band">' + waterlineList(days, false) +
            '<p class="subs-wl__legend" aria-hidden="true">' + legend + '</p></div>';
    }

    // Which card the hero is: the next delivery, or the subscription's paused or cancelled state with what it
    // still has coming. Paused is read from the published answers (Resume is offered to a paused one only).
    function heroKind(sub) {
        if (lifecycle(sub) === 'cancelled') return 'cancelled';
        return allowed(sub, 'resume') ? 'paused' : 'active';
    }

    // The last action's result, where it was taken: it stays until the next action (UX brief "FEEDBACK").
    function statusLine(sub, where, id, className) {
        var status = state.status && state.status.subId === sub.id && state.status.where === where ? state.status : null;
        var error = Boolean(status && status.error);
        return '<p class="' + className + (error ? ' is-error' : '') + '" id="' + id + '"' + (error ? ' role="alert"' : '') + '>' +
            (status ? esc(status.text) : '') + '</p>';
    }

    function heroActions(sub) {
        var actions = sub.allowed_actions || {};
        var upcoming = sub.upcoming_delivery;
        var pay = allowed(sub, 'pay');
        var open = cardConfirm(sub);
        var parts = [];
        // Exactly one primary, from the published answers: Pay, else Resume, else none. What a resume today
        // gives is the one-bottle waterline under the card. Every button's words are the action's own `label`.
        if (pay) {
            parts.push(actionButton('primary', 'pay', actions.pay.label,
                ' data-order-id="' + esc(actions.pay.order_id) + '" data-amount="' + esc(actions.pay.expected_amount) + '"'));
        } else if (allowed(sub, 'resume')) {
            parts.push(actionButton('primary', 'resume', actions.resume.label));
        }
        // The payment line: Pay's own note (the order it pays, and until when), else the active card's order's. A
        // paused or cancelled card's kept order says how it stands in its own sentence and chips.
        var note = pay ? actions.pay.note : (heroKind(sub) === 'active' && upcoming ? upcoming.payment_note : null);
        if (note) parts.push('<p class="subs-hint">' + esc(note) + '</p>');
        var secondary = [];
        // A paused subscription can still owe for the order it kept: Resume stays reachable beside Pay.
        if (pay && allowed(sub, 'resume')) secondary.push(actionButton('secondary', 'resume', actions.resume.label));
        ['skip', 'pause'].forEach(function (action) {
            if (!allowed(sub, action)) return;
            secondary.push(actionButton('secondary', action, actions[action].label,
                ' aria-controls="subs-confirm" aria-expanded="' + (open === action ? 'true' : 'false') + '"'));
        });
        if (secondary.length) parts.push('<div class="subs-hero__row">' + secondary.join('') + '</div>');
        parts.push('<div class="subs-hero__confirm" id="subs-confirm" tabindex="-1"' + (open ? '' : ' hidden') + '>' +
            (open ? confirmPanel(sub) : '') + '</div>');
        parts.push(statusLine(sub, 'hero', 'subs-hero-status', 'subs-hero__status'));
        return '<div class="subs-hero__actions">' + parts.join('') + '</div>';
    }

    function heroMain(sub) {
        var upcoming = sub.upcoming_delivery;
        var kind = heroKind(sub);
        if (kind === 'cancelled') {
            return '<p class="subs-hero__date subs-hero__date--state">' + esc(sub.cancelled_at_label
                ? text('hero.cancelled_on', { date: sub.cancelled_at_label }) : text('hero.cancelled')) + '</p>' +
                keptOrder(upcoming) + '<p class="subs-hero__note">' + esc(text('hero.cancelled_note')) + '</p>';
        }
        if (kind === 'paused') {
            return '<p class="subs-hero__date subs-hero__date--state">' + esc(sub.paused_at_label
                ? text('hero.paused_since', { date: sub.paused_at_label }) : text('hero.paused')) + '</p>' +
                '<p class="subs-hero__note">' + esc(text('hero.paused_note')) + '</p>' + keptOrder(upcoming) +
                availabilityWarning(sub);
        }
        if (!upcoming) {
            return '<p class="subs-hero__label">' + esc(text('hero.label')) + '</p>' +
                '<p class="subs-hero__note">' + esc(text('hero.no_date')) + '</p>' + availabilityWarning(sub);
        }
        return '<p class="subs-hero__label">' + esc(text('hero.label')) + '</p>' +
            '<p class="subs-hero__date">' + esc(upcoming.label_long) + '</p>' +
            '<p class="subs-hero__win">' + esc(windowLine(upcoming)) + '</p>' +
            orderLink(upcoming) +
            (upcoming.state === 'on_the_way' ? '<p class="subs-hero__note">' + esc(text('hero.on_the_way_note')) + '</p>' : '') +
            // Nothing deliverable: the warning below replaces "we'll place this order".
            (upcoming.will_place_on_label && sub.items_availability !== 'none'
                ? '<p class="subs-hero__note">' + esc(text('hero.will_place', { date: upcoming.will_place_on_label })) + '</p>' : '') +
            availabilityWarning(sub);
    }

    function hero(sub) {
        var kind = heroKind(sub);
        var modifier = kind === 'active' ? '' : ' subs-hero--' + kind;
        // A cancelled card has no actions and no waterline; only the order the driver still has can be paid.
        var withActions = kind !== 'cancelled' || allowed(sub, 'pay');
        var main = heroMain(sub) + (withActions ? '' : statusLine(sub, 'hero', 'subs-hero-status', 'subs-hero__status'));
        return '<div class="subs-hero' + modifier + '">' +
            '<div class="subs-hero__top"><div class="subs-hero__main">' + main + '</div>' +
            (withActions ? heroActions(sub) : '') + '</div>' +
            (kind === 'cancelled' ? '' : waterline(sub)) +
            '</div>';
    }

    // ------------------------------------------------------------------ the inline confirms (Skip, Pause, an Orders row)

    // The card's open confirm ('skip' or 'pause'), else null.
    function cardConfirm(sub) {
        var open = state.confirm;
        return open && open.subId === sub.id && open.kind !== 'order' ? open.kind : null;
    }

    // Is this Orders row's confirm the one open?
    function rowConfirmOpen(orderId) {
        var open = state.confirm;
        return Boolean(open && open.subId === state.selectedId && open.kind === 'order' && open.orderId === orderId);
    }

    // A skip's reasons, the backend's codes in its own words (`reasons`: [{code, label}]).
    function reasonChips(reasons, current) {
        return '<fieldset class="subs-reasons"><legend class="subs-hint">' + esc(text('skip.reasons')) + '</legend>' +
            '<div class="subs-reasons__chips">' + reasons.map(function (reason) {
                var code = esc(reason.code);
                return '<button type="button" class="subs-reason" data-action="skip-reason" data-reason="' + code + '" ' +
                    'data-focus="reason-' + code + '" aria-pressed="' + (current === reason.code ? 'true' : 'false') + '">' +
                    (current === reason.code ? icon('fa-check') : '') + esc(reason.label) + '</button>';
            }).join('') + '</div></fieldset>';
    }

    // One inline confirm, the card's or an Orders row's: the backend's sentences, a skip's reasons (and a few
    // words for "other"), the two buttons, then the last error.
    function confirmBody(lines, reasons, open, buttons) {
        var parts = lines.map(function (line) { return '<p class="subs-confirm__text">' + esc(line) + '</p>'; });
        if (reasons && reasons.length) {
            parts.push(reasonChips(reasons, open.reason));
            if (open.reason === 'other') {
                parts.push('<label class="subs-label" for="subs-skip-other">' + esc(text('skip.reason_text')) + '</label>' +
                    '<input type="text" class="form-control subs-input" id="subs-skip-other" data-focus="skip-other" maxlength="500" ' +
                    'value="' + esc(open.other) + '">');
            }
        }
        parts.push('<div class="subs-confirm__row">' + buttons + '</div>');
        if (open.error) parts.push('<p class="subs-error-line" role="alert">' + esc(open.error) + '</p>');
        return parts.join('');
    }

    // The card's Skip or Pause, as published: its sentence, then the orders it keeps and cancels.
    function confirmPanel(sub) {
        var open = state.confirm;
        var plan = sub.allowed_actions[open.kind];
        return confirmBody([plan.confirm_text].concat(plan.kept_lines || [], plan.cancel_lines || []), plan.reasons, open,
            actionButton('primary', open.kind + '-confirm', plan.confirm_label) +
            actionButton('secondary', 'confirm-keep', plan.keep_label));
    }

    // An Orders row's Skip or Cancel, in that order's own published words (`customer_actions.cancel`).
    function rowConfirmPanel(row) {
        var cancel = row.customer_actions.cancel;
        return confirmBody([cancel.confirm_text], cancel.reasons, state.confirm,
            actionButton('primary', 'order-remove-confirm', cancel.label) +
            actionButton('secondary', 'confirm-keep', text('orders.keep')));
    }

    // Draw the one open confirm and close every other; the buttons that open them say which is expanded.
    function renderConfirm(focusPanel) {
        var sub = selected();
        if (!sub) return;
        var open = cardConfirm(sub);
        var $panel = $('#subs-confirm');
        keepFocus(function () {
            $panel.html(open ? confirmPanel(sub) : '').prop('hidden', !open);
            $('#subs-orders .subs-orow__confirm').each(function () {
                var row = shownRow(parseInt(this.getAttribute('data-order-id'), 10));
                var mine = Boolean(row) && rowConfirmOpen(row.order_id);
                $(this).html(mine ? rowConfirmPanel(row) : '').prop('hidden', !mine);
            });
        });
        $('#subscription [data-action="skip"]').attr('aria-expanded', open === 'skip' ? 'true' : 'false');
        $('#subscription [data-action="pause"]').attr('aria-expanded', open === 'pause' ? 'true' : 'false');
        $('#subs-orders [data-action="order-remove"]').each(function () {
            this.setAttribute('aria-expanded', rowConfirmOpen(parseInt(this.getAttribute('data-order-id'), 10)) ? 'true' : 'false');
        });
        if (!focusPanel) return;
        var target = open ? $panel[0] : null;
        if (!target && state.confirm && state.confirm.kind === 'order') {
            target = document.getElementById('subs-orow-confirm-' + state.confirm.orderId);
        }
        if (target) target.focus();
    }

    // ------------------------------------------------------------------ the sections (read mode)

    function editing(sub, name) {
        return Boolean(state.edit && state.edit.subId === sub.id && state.edit.section === name);
    }

    // The payment menu offers nothing to change to: no method, or only the current one (UX brief "PAYMENT METHOD").
    function nothingElseToPick(sub) {
        var methods = state.methods;
        if (!methods || methods.subId !== sub.id || !methods.list) return false;
        var list = methods.list;
        return !list.length || (list.length === 1 && list[0].method === sub.payment_method);
    }

    function changeButton(sub, name) {
        if (!allowed(sub, 'edit')) return '';
        var emphasis = '';
        if (name === 'payment') {
            if (nothingElseToPick(sub)) return '';
            // The backend's B8 answer: the current method is no longer offered, so Change is the way on.
            if (sub.payment_method_warning) emphasis = ' subs-change--emphasis';
        }
        return '<button type="button" class="subs-change' + emphasis + '" data-action="change" data-section="' + name + '" ' +
            'data-focus="change-' + name + '" aria-controls="subs-edit-' + name + '" ' +
            'aria-expanded="' + (editing(sub, name) ? 'true' : 'false') + '">' +
            '<span aria-hidden="true">' + esc(text('change')) + '</span>' +
            '<span class="sr-only">' + esc(text('change_label.' + name)) + '</span></button>';
    }

    function sectionValue(sub, name) {
        if (editing(sub, name)) return editForm(sub, name);
        if (name === 'items') return itemsValue(sub);
        if (name === 'schedule') return '<p class="subs-val">' + esc(sub.schedule_sentence) + '</p>';
        if (name === 'address') return '<p class="subs-val">' + esc(sub.delivery_address_label) + '</p>';
        if (name === 'payment') return paymentValue(sub);
        return ordersValue();
    }

    function sectionInner(sub, name) {
        return '<h3 id="subs-sec-' + name + '-h">' + esc(text(SECTION_HEADINGS[name])) + '</h3>' +
            '<div class="subs-sec__value">' + sectionValue(sub, name) +
            statusLine(sub, name, 'subs-sec-' + name + '-status', 'subs-sec__status') + '</div>' +
            '<div class="subs-sec__action">' + (name === 'orders' ? '' : changeButton(sub, name)) + '</div>';
    }

    function section(sub, name) {
        return '<section class="subs-sec subs-sec--' + name + (editing(sub, name) ? ' is-editing' : '') + '" id="subs-sec-' + name +
            '" aria-labelledby="subs-sec-' + name + '-h">' + sectionInner(sub, name) + '</section>';
    }

    // Re-draw one section in place (its form opening or closing), keeping focus where it was.
    function renderSection(name) {
        var sub = selected();
        if (!sub) return;
        keepFocus(function () {
            $('#subs-sec-' + name).toggleClass('is-editing', editing(sub, name)).html(sectionInner(sub, name));
        });
        followSavebar();
    }

    function thumb(item) {
        var images = (item.product && item.product.images) || [];
        var image = images.length ? images[0] : null;
        return '<span class="subs-item__thumb">' +
            (image ? '<img src="' + esc(image) + '" alt="" loading="lazy">' : bottle('thumb')) + '</span>';
    }

    function itemRow(sub, item) {
        var unavailable = item.available === false;
        var note = unavailable
            ? '<small class="subs-item__warn">' + icon('fa-exclamation-triangle') +
                esc(text(sub.items_availability === 'none' ? 'items.unavailable_none' : 'items.unavailable')) + '</small>'
            : '';
        var line = item.unit_price_label
            ? '<small>' + esc(text('items.qty_price', { quantity: String(item.quantity), price: item.unit_price_label })) + '</small>'
            : '';
        return '<li class="subs-item' + (unavailable ? ' is-unavailable' : '') + '">' + thumb(item) +
            '<div class="subs-item__text"><b>' + esc(item.product_name || text('items.unknown_product')) + '</b>' + line + note + '</div>' +
            '<span class="subs-money">' + esc(unavailable ? text('items.not_included') : item.line_total_label) + '</span></li>';
    }

    function totals(perDelivery, perMonth) {
        return '<dl class="subs-totals">' +
            '<dt>' + esc(text('items.per_delivery')) + '</dt><dd class="subs-money">' + esc(perDelivery) + '</dd>' +
            '<dt>' + esc(text('items.per_month')) + '</dt><dd class="subs-money">' + esc(perMonth) + '</dd>' +
            '</dl>';
    }

    function itemsValue(sub) {
        var rows = (sub.subscription_items || []).map(function (item) { return itemRow(sub, item); }).join('');
        return '<ul class="subs-items">' + rows + '</ul>' +
            totals(sub.estimated_per_delivery_label, sub.estimated_per_month_label) +
            '<p class="subs-benefit">' + html('items.benefit', { free: '<b>' + esc(text('items.benefit_free')) + '</b>' }) + '</p>';
    }

    function paymentValue(sub) {
        var note = text('payment.note.' + sub.payment_method);
        // Spec 2026-10-27 §7 (B8): the backend's warning that the current method is no longer offered here (the cash
        // limit, a lapsed contract), shown as it is published.
        var warning = sub.payment_method_warning
            ? '<p class="subs-val__chip">' + chip({ code: 'not_offered', label: sub.payment_method_warning, tone: 'warning' }) + '</p>'
            : '';
        return '<p class="subs-val">' + esc(sub.payment_method_label) + (note ? '<small>' + esc(note) + '</small>' : '') + '</p>' + warning;
    }

    function ordersValue() {
        return '<ul class="subs-orders" id="subs-orders" tabindex="-1" aria-busy="true"></ul>' +
            '<p class="subs-orders__empty" id="subs-orders-empty" hidden></p>' +
            '<button type="button" class="subs-btn subs-btn--secondary subs-orders__more" id="subs-orders-more" data-action="more-orders" hidden>' +
            esc(text('orders.more')) + '</button>';
    }

    function footer(sub) {
        var link = '<a href="' + esc(PAGE.help_url) + '" target="_blank" rel="noopener noreferrer">' + esc(text('footer.help_link')) + '</a>';
        // Cancel opens the one Cancel modal through Bootstrap's data API, which gives focus back to it.
        return '<div class="subs-foot">' +
            (allowed(sub, 'cancel')
                ? '<button type="button" class="subs-danger" data-action="cancel" data-toggle="modal" data-target="#subs-cancel-modal">' +
                    esc(sub.allowed_actions.cancel.label) + '</button>'
                : '') +
            '<p class="subs-help">' + html('footer.help', { link: link }) + '</p></div>';
    }

    function jumpLinks() {
        var links = [['items', text('section.items')], ['schedule', text('section.schedule')],
            ['address', text('section.address')], ['payment', text('section.payment')], ['orders', text('jump.orders')]];
        return '<nav class="subs-jump" aria-label="' + esc(text('jump.label')) + '">' + links.map(function (link) {
            return '<a href="#subs-sec-' + link[0] + '">' + esc(link[1]) + '</a>';
        }).join('') + '</nav>';
    }

    function body(sub) {
        return '<div class="subs-bh">' +
            '<div class="subs-bh__title"><h2 id="sub-h" tabindex="-1">' + esc(sub.name) + '</h2>' + lifecycleChip(sub) + '</div>' +
            (sub.summary_sentence ? '<p>' + esc(sub.summary_sentence) + '</p>' : '') +
            '</div>' + jumpLinks() + hero(sub) +
            section(sub, 'items') + section(sub, 'schedule') + section(sub, 'address') + section(sub, 'payment') +
            section(sub, 'orders') +
            footer(sub);
    }

    function emptyState() {
        return '<div class="subs-empty">' + bottle('planned') +
            '<h2 id="sub-h" tabindex="-1">' + esc(text('empty.title')) + '</h2>' +
            '<p>' + esc(text('empty.body')) + '</p>' +
            '<a class="subs-btn subs-btn--primary" href="' + esc(PAGE.builder_url) + '">' + esc(text('empty.cta')) + '</a></div>';
    }

    // ------------------------------------------------------------------ the sections (edit mode)

    function editForm(sub, name) {
        var forms = { items: itemsForm, schedule: scheduleForm, address: addressForm, payment: paymentForm };
        var form = forms[name](sub);
        return '<div class="subs-edit" id="subs-edit-' + name + '">' + form +
            '<div class="subs-savebar-slot" id="subs-savebar-slot">' + savebar() + '</div></div>';
    }

    // The segmented stepper (product-detail.css idiom): 44px buttons, a text box with a numeric keypad.
    function stepper(row, minimum, prefix, name) {
        var atMinimum = row.quantity <= minimum;
        return '<div class="subs-stepper">' +
            '<button type="button" class="subs-stepper__btn" data-action="' + prefix + 'dec" data-product-id="' + row.product_id + '" ' +
            'data-focus="' + prefix + 'dec-' + row.product_id + '" aria-label="' + esc(text('items.fewer', { name: name })) + '"' +
            (atMinimum ? ' aria-disabled="true"' : '') + '>' + icon('fa-minus') + '</button>' +
            '<input type="text" class="subs-stepper__input" inputmode="numeric" pattern="[0-9]*" autocomplete="off" ' +
            'data-action="' + prefix + 'qty" data-product-id="' + row.product_id + '" data-focus="' + prefix + 'qty-' + row.product_id + '" ' +
            'aria-label="' + esc(text('items.quantity', { name: name })) + '" value="' + esc(row.quantity) + '">' +
            '<button type="button" class="subs-stepper__btn" data-action="' + prefix + 'inc" data-product-id="' + row.product_id + '" ' +
            'data-focus="' + prefix + 'inc-' + row.product_id + '" aria-label="' + esc(text('items.more', { name: name })) + '">' +
            icon('fa-plus') + '</button></div>';
    }

    function lineText(row) {
        return row.unit_price_label ? text('items.qty_price', { quantity: String(row.quantity), price: row.unit_price_label }) : '';
    }

    function editRow(sub, row, only) {
        var name = row.product_name || text('items.unknown_product');
        var unavailable = row.available === false;
        var remove = '<button type="button" class="subs-remove" data-action="remove-item" data-product-id="' + row.product_id + '" ' +
            'data-focus="remove-' + row.product_id + '" aria-label="' + esc(text('items.remove', { name: name })) + '"' +
            (only ? ' aria-disabled="true" aria-describedby="subs-last-item"' : '') + '>' + icon('fa-trash-alt') + '</button>';
        return '<li class="subs-item subs-item--edit' + (unavailable ? ' is-unavailable' : '') + '" data-product-id="' + row.product_id + '">' +
            thumb(row) +
            '<div class="subs-item__text"><b>' + esc(name) + '</b>' +
            '<small class="subs-item__line">' + esc(lineText(row)) + '</small>' +
            (unavailable ? '<small class="subs-item__warn">' + icon('fa-exclamation-triangle') +
                esc(text(sub.items_availability === 'none' ? 'items.unavailable_none' : 'items.unavailable')) + '</small>' : '') +
            '</div>' +
            '<span class="subs-money subs-item__total" aria-live="off">' +
            esc(unavailable ? text('items.not_included') : (row.line_total_label || '')) + '</span>' +
            '<div class="subs-item__controls">' + stepper(row, row.min_order_quantity || 1, '', name) + remove + '</div></li>';
    }

    function itemsTotals(sub) {
        var edit = state.edit;
        return edit.preview && edit.previewState === 'ok'
            ? totals(edit.preview.estimated_per_delivery_label, edit.preview.estimated_per_month_label)
            : totals(sub.estimated_per_delivery_label, sub.estimated_per_month_label);
    }

    function itemsNote() {
        var preview = state.edit.preview;
        return text('items.edit_note') + (preview && preview.kept_order_note ? ' ' + preview.kept_order_note : '');
    }

    function itemsForm(sub) {
        var rows = state.edit.draft.items;
        var only = rows.length === 1;
        return '<ul class="subs-items subs-items--edit" id="subs-edit-rows">' +
            rows.map(function (row) { return editRow(sub, row, only); }).join('') + '</ul>' +
            (only ? '<p class="subs-hint" id="subs-last-item">' + esc(text('items.last_item')) + '</p>' : '') +
            '<button type="button" class="subs-add" data-action="open-picker" data-focus="add-product">' +
            icon('fa-plus') + esc(text('items.add')) + '</button>' +
            '<p class="subs-hint subs-edit__note" id="subs-edit-note">' + esc(itemsNote()) + '</p>' +
            '<div id="subs-edit-totals">' + itemsTotals(sub) + '</div>';
    }

    function radioTile(group, value, checked, label, hint, focusKey, extraClass) {
        return '<label class="subs-tile' + (extraClass ? ' ' + extraClass : '') + (checked ? ' is-checked' : '') + '">' +
            '<input type="radio" class="subs-tile__input" name="' + group + '" value="' + esc(value) + '" data-focus="' + focusKey + '"' +
            (checked ? ' checked' : '') + '>' +
            '<span class="subs-tile__name">' + (checked ? icon('fa-check') : '') + esc(label) + '</span>' +
            (hint ? '<small>' + esc(hint) + '</small>' : '') + '</label>';
    }

    // One payment-method tile rule for the Address editor's menu and the Payment editor: label, note and card styling.
    function methodTiles(group, list, chosenMethod, focusPrefix) {
        return '<div class="subs-tiles subs-tiles--methods">' + list.map(function (method) {
            return radioTile(group, method.method, chosenMethod === method.method, methodLabel(method.method),
                text('payment.note.' + method.method) || method.description, focusPrefix + method.method, 'subs-tile--card');
        }).join('') + '</div>';
    }

    function frequencyFieldset() {
        var draft = state.edit.draft;
        return '<fieldset class="subs-fieldset"><legend>' + esc(text('schedule.how_often')) + '</legend>' +
            '<div class="subs-tiles subs-tiles--freq">' + FREQUENCIES.map(function (frequency) {
                return radioTile('subs-frequency', frequency, draft.frequency === frequency, text('frequency.' + frequency),
                    text('schedule.hint.' + frequency), 'freq-' + frequency);
            }).join('') + '</div></fieldset>';
    }

    function dayFieldset() {
        var draft = state.edit.draft;
        if (WEEKDAY_FREQUENCIES[draft.frequency]) {
            var chips = [];
            // Only the backend's delivery days (the time-slots answer); all seven until it has answered.
            var offered = state.edit.deliveryWeekdays;
            for (var day = 0; day < 7; day += 1) {
                if (offered && offered.indexOf(day) === -1) continue;
                var checked = draft.delivery_day_of_week === day;
                chips.push('<label class="subs-day' + (checked ? ' is-checked' : '') + '">' +
                    '<input type="radio" class="subs-tile__input" name="subs-weekday" value="' + day + '" data-focus="day-' + day + '"' +
                    (checked ? ' checked' : '') + '>' +
                    '<span aria-hidden="true">' + esc((PAGE.weekday_short_names || [])[day]) + '</span>' +
                    '<span class="sr-only">' + esc(weekdayName(day)) + '</span></label>');
            }
            return '<fieldset class="subs-fieldset"><legend>' + esc(text('schedule.which_day')) + '</legend>' +
                '<div class="subs-days">' + chips.join('') + '</div></fieldset>';
        }
        if (draft.frequency === 'monthly') {
            var options = ['<option value="">' + esc(text('schedule.choose_day')) + '</option>'];
            for (var number = 1; number <= DAYS_OF_MONTH; number += 1) {
                options.push('<option value="' + number + '"' + (draft.delivery_day_of_month === number ? ' selected' : '') + '>' + number + '</option>');
            }
            return '<fieldset class="subs-fieldset"><legend>' + esc(text('schedule.which_day')) + '</legend>' +
                '<label class="subs-label" for="subs-dom">' + esc(text('schedule.day_of_month')) + '</label>' +
                '<select class="form-control ignore subs-select" id="subs-dom" data-focus="dom" aria-describedby="subs-dom-note">' +
                options.join('') + '</select>' +
                '<p class="subs-hint" id="subs-dom-note">' + esc(text('schedule.short_months')) + '</p></fieldset>';
        }
        return '';
    }

    function slotTiles() {
        var edit = state.edit;
        var parts = [];
        if (edit.slotsState === 'loading') parts.push('<p class="subs-hint">' + esc(text('busy.loading')) + '</p>');
        if (edit.slotsState === 'failed') parts.push('<p class="subs-error-line" role="alert">' + esc(text('schedule.slots_failed')) + '</p>');
        // Any time is always a choice, first, even when no time is offered (spec 2026-10-27 §6.2).
        var tiles = [radioTile('subs-slot', ANY_TIME, edit.draft.delivery_time_slot_id === null, text('slot.any'), '', 'slot-any')];
        if (edit.slotsState === 'ok') {
            tiles = tiles.concat(edit.slots.map(function (slot) {
                return radioTile('subs-slot', slot.id, edit.draft.delivery_time_slot_id === slot.id,
                    text('slot.window', { start: slot.start_time, end: slot.end_time }), '', 'slot-' + slot.id);
            }));
        }
        parts.push('<div class="subs-tiles subs-tiles--slots">' + tiles.join('') + '</div>');
        if (edit.slotMessage) parts.push('<p class="subs-edit__msg" role="alert">' + esc(edit.slotMessage) + '</p>');
        return parts.join('');
    }

    // The backend's preview of the new rhythm: the placed order stays filled, the new dates dashed.
    function schedulePreview() {
        var edit = state.edit;
        if (!edit.dirty || !edit.preview || edit.previewState !== 'ok') return '';
        return '<div class="subs-preview">' + waterlineList(edit.preview.upcoming_dates || [], true) +
            '<p class="subs-preview__sentence">' + esc(edit.preview.sentence) + '</p></div>';
    }

    function scheduleForm() {
        return '<div id="subs-edit-freq">' + frequencyFieldset() + '</div>' +
            '<div id="subs-edit-days">' + dayFieldset() + '</div>' +
            '<fieldset class="subs-fieldset"><legend>' + esc(text('schedule.time')) + '</legend>' +
            '<div id="subs-edit-slots">' + slotTiles() + '</div></fieldset>' +
            '<div id="subs-edit-preview">' + schedulePreview() + '</div>';
    }

    // The saved addresses as tiles, the current one checked, then the way to add one (spec 2026-10-27 §7).
    function addressFieldset() {
        var saved = state.addresses;
        if (!saved || saved.loading) return '<p class="subs-hint">' + esc(text('busy.loading')) + '</p>';
        if (saved.failed) return '<p class="subs-error-line" role="alert">' + esc(text('address.load_failed')) + '</p>';
        var draft = state.edit.draft;
        return '<fieldset class="subs-fieldset"><legend>' + esc(text('address.legend')) + '</legend>' +
            '<div class="subs-tiles subs-tiles--addresses">' + saved.list.map(function (address) {
                return radioTile('subs-address', address.id, draft.delivery_address_id === address.id,
                    address.title || address.full_address, address.title ? address.full_address : '', 'address-' + address.id);
            }).join('') + '</div>' +
            '<a class="subs-add" href="' + esc(PAGE.addresses_url) + '">' + icon('fa-plus') + esc(text('address.add')) + '</a></fieldset>';
    }

    // When the new address does not offer the subscription's method, the backend's menu for it: choose one before Save.
    function addressMethods() {
        var edit = state.edit;
        if (!edit.methodChoices) return '';
        return '<fieldset class="subs-fieldset"><legend>' + esc(text('address.method_legend')) + '</legend>' +
            methodTiles('subs-address-method', edit.methodChoices, edit.draft.payment_method, 'address-method-') + '</fieldset>';
    }

    // The backend's confirm sentences for the move: where deliveries go, and what happens to each placed order.
    function addressPreview() {
        var edit = state.edit;
        if (!edit.dirty || !edit.preview || edit.previewState !== 'ok') return '';
        return '<div class="subs-preview"><ul class="subs-preview__list">' + edit.preview.consequences.map(function (line) {
            return '<li>' + esc(line) + '</li>';
        }).join('') + '</ul></div>';
    }

    function addressForm() {
        return '<div id="subs-edit-addresses">' + addressFieldset() + '</div>' +
            '<div id="subs-edit-address-methods">' + addressMethods() + '</div>' +
            '<div id="subs-edit-address-preview">' + addressPreview() + '</div>';
    }

    function methodsFieldset(sub) {
        var methods = state.methods && state.methods.subId === sub.id ? state.methods : null;
        if (!methods || methods.loading) return '<p class="subs-hint">' + esc(text('busy.loading')) + '</p>';
        if (methods.failed) return '<p class="subs-error-line" role="alert">' + esc(text('payment.methods_failed')) + '</p>';
        var draft = state.edit.draft;
        return '<fieldset class="subs-fieldset"><legend>' + esc(text('payment.legend')) + '</legend>' +
            methodTiles('subs-method', methods.list, draft.payment_method, 'method-') + '</fieldset>';
    }

    // The order already placed keeps the method it was placed with: the backend's note says so before the
    // save (UX brief "SCHEDULE AND PAYMENT SAVE").
    function paymentForm(sub) {
        return '<div id="subs-edit-methods">' + methodsFieldset(sub) + '</div>' +
            (sub.kept_order_note ? '<p class="subs-hint subs-edit__note" id="subs-edit-note">' + esc(sub.kept_order_note) + '</p>' : '');
    }

    // Save waits for the latest backend preview of exactly this draft (UX brief "PREVIEWS COME FROM THE BACKEND").
    function canSave() {
        var edit = state.edit;
        if (!edit || !edit.dirty || edit.saving) return false;
        if (edit.section === 'payment') return true;
        if (edit.previewState !== 'ok') return false;
        if (edit.section === 'address') return edit.preview.address_change.payment_method_offered || edit.draft.payment_method !== null;
        return true;
    }

    function savebar() {
        var edit = state.edit;
        if (!edit || !edit.dirty) return '';
        var figure = '';
        if (edit.previewState === 'waiting' || edit.previewState === 'loading') figure = text('edit.checking');
        else if (edit.section === 'items' && edit.previewState === 'ok' && edit.preview) {
            figure = text('edit.new_total', { amount: edit.preview.estimated_per_delivery_label });
        }
        var message = edit.saveError || (edit.previewState === 'refused' || edit.previewState === 'failed' ? edit.previewMessage : '');
        return '<div class="subs-savebar" id="subs-savebar" tabindex="-1" role="region" aria-label="' + esc(text('edit.unsaved')) + '">' +
            '<div class="subs-savebar__text"><p class="subs-savebar__title">' + esc(text('edit.unsaved')) + '</p>' +
            (figure ? '<p class="subs-savebar__figure">' + esc(figure) + '</p>' : '') +
            (edit.guard ? '<p class="subs-savebar__guard" role="alert">' + esc(text('edit.dirty_guard')) + '</p>' : '') +
            (message ? '<p class="subs-error-line" role="alert">' + esc(message) + '</p>' : '') + '</div>' +
            '<div class="subs-savebar__buttons">' +
            '<button type="button" class="subs-btn subs-btn--secondary" data-action="discard" data-focus="discard">' + esc(text('edit.discard')) + '</button>' +
            '<button type="button" class="subs-btn subs-btn--primary" data-action="save" data-focus="save"' +
            (canSave() ? '' : ' aria-disabled="true"') + '>' +
            (edit.saving ? '<span class="spinner-border spinner-border-sm" aria-hidden="true"></span>' + esc(text('busy.saving')) : esc(text('edit.save'))) +
            '</button></div></div>';
    }

    // The parts of the open form that follow the draft and its preview, each re-drawn in place.
    function renderEditParts() {
        var sub = selected();
        var edit = state.edit;
        if (!sub || !edit) return;
        keepFocus(function () {
            $('#subs-savebar-slot').html(savebar());
            if (edit.section === 'items') {
                var lines = {};
                ((edit.previewState === 'ok' && edit.preview && edit.preview.items) || []).forEach(function (line) {
                    lines[line.product_id] = line;
                });
                edit.draft.items.forEach(function (row) {
                    var line = lines[row.product_id];
                    if (line) {
                        row.unit_price_label = line.unit_price_label;
                        row.line_total_label = line.line_total_label;
                    }
                    var $row = $('#subs-edit-rows > li[data-product-id="' + row.product_id + '"]');
                    $row.find('.subs-item__line').text(lineText(row));
                    if (row.available !== false) $row.find('.subs-item__total').text(row.line_total_label || '');
                    // The box follows the draft, except while the customer has emptied it to type a new number.
                    var $input = $row.find('.subs-stepper__input');
                    var emptied = document.activeElement === $input[0] && $input.val() === '';
                    if (!emptied && String($input.val()) !== String(row.quantity)) $input.val(row.quantity);
                    var $dec = $row.find('[data-action="dec"]');
                    if (row.quantity <= (row.min_order_quantity || 1)) $dec.attr('aria-disabled', 'true');
                    else $dec.removeAttr('aria-disabled');
                });
                $('#subs-edit-totals').html(itemsTotals(sub));
                $('#subs-edit-note').text(itemsNote());
            }
            if (edit.section === 'schedule') {
                $('#subs-edit-slots').html(slotTiles());
                $('#subs-edit-preview').html(schedulePreview());
            }
            if (edit.section === 'address') {
                $('#subs-edit-addresses').html(addressFieldset());
                $('#subs-edit-address-methods').html(addressMethods());
                $('#subs-edit-address-preview').html(addressPreview());
            }
        });
        followSavebar();
    }

    // While the save bar shows, hide the fixed mobile bars it would collide with and keep a focused control
    // from scrolling under it (UX brief "SAVE BAR"; WCAG 2.4.11).
    function followSavebar() {
        var bar = document.getElementById('subs-savebar');
        document.body.classList.toggle('subs-editing', Boolean(bar));
        document.documentElement.style.scrollPaddingBottom = bar ? bar.offsetHeight + 'px' : '';
    }

    // ------------------------------------------------------------------ opening, changing and closing a form

    function itemDraft(sub) {
        return {
            items: (sub.subscription_items || []).map(function (item) {
                return {
                    product_id: item.product_id,
                    quantity: item.quantity,
                    product_name: item.product_name,
                    product: item.product,
                    available: item.available,
                    min_order_quantity: item.min_order_quantity || 1,
                    unit_price_label: item.unit_price_label,
                    line_total_label: item.line_total_label
                };
            })
        };
    }

    function orNull(value) {
        return value === undefined ? null : value;
    }

    function scheduleDraft(sub) {
        return {
            frequency: sub.frequency,
            delivery_day_of_week: orNull(sub.delivery_day_of_week),
            delivery_day_of_month: orNull(sub.delivery_day_of_month),
            delivery_time_slot_id: orNull(sub.delivery_time_slot_id)
        };
    }

    // The Address editor's draft: the address chosen, and the method asked for only when the new address does not
    // offer the subscription's (spec 2026-10-27 §7).
    function addressDraft(sub) {
        return { delivery_address_id: sub.delivery_address_id, payment_method: null };
    }

    function addressBody(draft) {
        var body = { delivery_address_id: draft.delivery_address_id };
        if (draft.payment_method !== null) body.payment_method = draft.payment_method;
        return body;
    }

    // What the Schedule save and its preview send: the frequency, its own day field and the slot (a number, or an
    // explicit null for Any time; not sent while undecided). The PUT carries only the section's own fields; the
    // payment method has its own route (spec §12, B8).
    function scheduleBody(draft) {
        var body = { frequency: draft.frequency };
        if (WEEKDAY_FREQUENCIES[draft.frequency] && draft.delivery_day_of_week !== null) body.delivery_day_of_week = draft.delivery_day_of_week;
        if (draft.frequency === 'monthly' && draft.delivery_day_of_month !== null) body.delivery_day_of_month = draft.delivery_day_of_month;
        if (draft.delivery_time_slot_id !== undefined) body.delivery_time_slot_id = draft.delivery_time_slot_id;
        return body;
    }

    // A write's body with the `expected_state` its screen showed, echoed as the backend published it.
    function withState(body, expectedState) {
        body.expected_state = expectedState;
        return body;
    }

    function itemsBody(draft) {
        return { items: draft.items.map(function (row) { return { product_id: row.product_id, quantity: row.quantity }; }) };
    }

    function itemsKey(items) {
        return items.map(function (row) { return row.product_id + 'x' + row.quantity; }).sort().join(',');
    }

    function isDirty(sub, edit) {
        if (edit.section === 'items') return itemsKey(edit.draft.items) !== itemsKey(itemDraft(sub).items);
        if (edit.section === 'payment') return edit.draft.payment_method !== sub.payment_method;
        if (edit.section === 'address') return JSON.stringify(addressBody(edit.draft)) !== JSON.stringify(addressBody(addressDraft(sub)));
        return JSON.stringify(scheduleBody(edit.draft)) !== JSON.stringify(scheduleBody(scheduleDraft(sub)));
    }

    function dirty() {
        return Boolean(state.edit && state.edit.dirty);
    }

    // Another Change, a strip link or a card action while a section has unsaved changes: send the customer to
    // its save bar instead (UX brief "READ-THEN-EDIT SECTIONS").
    function guardDirty() {
        if (!dirty()) return false;
        state.edit.guard = true;
        renderEditParts();
        var bar = document.getElementById('subs-savebar');
        if (bar) {
            scrollIntoSight(bar, 'nearest');
            bar.focus();
        }
        return true;
    }

    function firstField(name) {
        var form = document.getElementById('subs-edit-' + name);
        if (!form) return null;
        return form.querySelector('input[type="radio"]:checked') || form.querySelector('input:not([type="hidden"]), select') ||
            form.querySelector('button:not([aria-disabled="true"])');
    }

    function clearStatus() {
        state.status = null;
        $('#subscription .subs-sec__status, #subs-hero-status').text('').removeAttr('role').removeClass('is-error');
    }

    function openEdit(name) {
        var sub = selected();
        if (!sub || !allowed(sub, 'edit')) return;
        clearStatus();
        state.confirm = null;
        renderConfirm(false);
        var drafts = {
            items: itemDraft, schedule: scheduleDraft, address: addressDraft,
            payment: function () { return { payment_method: sub.payment_method }; }
        };
        var draft = drafts[name](sub);
        state.edit = {
            subId: sub.id, section: name, draft: draft, dirty: false, guard: false, saving: false, saveError: '',
            preview: null, previewState: 'idle', previewMessage: '', previewToken: 0,
            slots: [], slotsState: 'idle', slotsQuery: null, slotsToken: 0, slotMessage: '', deliveryWeekdays: null,
            methodChoices: null,
            candidates: [], picks: {}
        };
        renderSection(name);
        var field = firstField(name);
        if (field) field.focus();
        if (name === 'schedule') loadSlots();
        if (name === 'address') loadAddresses();
        if (name === 'payment' && (!state.methods || state.methods.subId !== sub.id || state.methods.failed)) loadMethods(sub);
    }

    function closeEdit(options) {
        var edit = state.edit;
        if (!edit) return;
        window.clearTimeout(previewTimer);
        state.edit = null;
        renderSection(edit.section);
        if (!options || options.focus !== false) $('#subs-sec-' + edit.section + ' [data-action="change"]').trigger('focus');
    }

    // The draft changed: is it different from what is saved, and what does the backend say about it?
    function draftChanged(options) {
        var sub = selected();
        var edit = state.edit;
        edit.dirty = isDirty(sub, edit);
        edit.guard = false;
        edit.saveError = '';
        window.clearTimeout(previewTimer);
        edit.previewToken += 1;
        if (edit.section === 'payment' || !edit.dirty) {
            edit.previewState = 'idle';
            edit.preview = null;
        } else if (edit.section === 'schedule' && edit.draft.delivery_time_slot_id === undefined) {
            edit.previewState = 'idle';  // the slot message says what is missing; nothing to check yet
        } else {
            edit.previewState = 'waiting';
            var token = edit.previewToken;
            previewTimer = window.setTimeout(function () { runPreview(token); }, PREVIEW_DELAY_MS);
        }
        if (options && options.wholeSection) renderSection(edit.section);
        else renderEditParts();
    }

    async function runPreview(token) {
        var sub = selected();
        var edit = state.edit;
        if (!sub || !edit || token !== edit.previewToken) return;
        edit.previewState = 'loading';
        renderEditParts();
        var bodies = { items: itemsBody, schedule: scheduleBody, address: addressBody };
        var body = bodies[edit.section](edit.draft);
        var response = null;
        var result = {};
        try {
            response = await apiRequest('/subscriptions/' + sub.id + '/preview', { method: 'POST', body: JSON.stringify(body) });
            result = await readJson(response);
        } catch (error) {
            response = null;
        }
        if (state.edit !== edit || token !== edit.previewToken) return;  // a newer draft is being checked
        if (response && response.ok && result.success) {
            edit.preview = result.data.preview;
            edit.previewState = 'ok';
            edit.previewMessage = '';
            // The method menu stays up once the backend has asked for one, even after the choice makes it offered.
            if (edit.section === 'address' && !edit.preview.address_change.payment_method_offered) {
                edit.methodChoices = edit.preview.address_change.payment_methods;
            }
        } else {
            edit.preview = null;
            edit.previewState = response && response.status >= 400 && response.status < 500 ? 'refused' : 'failed';
            edit.previewMessage = refusal(response, result, 'error.preview');
        }
        renderEditParts();
    }

    // The slots the chosen weekday (and frequency) offers, by the backend's own rule, and the delivery days. A time no
    // longer offered is dropped with a message, and Save waits for another time or Any time (UX brief "TIME SLOT").
    function slotsQuery(draft) {
        var weekday = WEEKDAY_FREQUENCIES[draft.frequency] && draft.delivery_day_of_week !== null ? draft.delivery_day_of_week : 'any';
        return SLOTS_URL + '?weekday=' + weekday + '&frequency=' + draft.frequency;
    }

    function slotMessage(draft) {
        return WEEKDAY_FREQUENCIES[draft.frequency] && draft.delivery_day_of_week !== null
            ? text('schedule.slot_missing', { day: weekdayName(draft.delivery_day_of_week) })
            : text('schedule.slot_missing_plain');
    }

    async function loadSlots() {
        var edit = state.edit;
        if (!edit || edit.section !== 'schedule') return;
        var query = slotsQuery(edit.draft);
        if (query === edit.slotsQuery && edit.slotsState === 'ok') {
            draftChanged();
            return;
        }
        edit.slotsQuery = query;
        edit.slotsState = 'loading';
        edit.slotsToken += 1;
        var token = edit.slotsToken;
        renderEditParts();
        var answer = null;
        try {
            var response = await apiRequest(query);
            var result = await readJson(response);
            if (response.ok && Array.isArray(result.time_slots)) answer = result;
        } catch (error) {
            answer = null;
        }
        if (state.edit !== edit || token !== edit.slotsToken) return;
        edit.slots = answer ? answer.time_slots : [];
        edit.slotsState = answer ? 'ok' : 'failed';
        if (answer) {
            edit.deliveryWeekdays = Array.isArray(answer.delivery_weekdays) ? answer.delivery_weekdays : null;
            var chosen = edit.draft.delivery_time_slot_id;
            var dropped = typeof chosen === 'number' && !edit.slots.some(function (slot) { return slot.id === chosen; });
            if (dropped) edit.draft.delivery_time_slot_id = undefined;
            // Undecided (a dropped time not yet replaced) keeps saying so, for the day now chosen.
            edit.slotMessage = edit.draft.delivery_time_slot_id === undefined ? slotMessage(edit.draft) : '';
            keepFocus(function () { $('#subs-edit-days').html(dayFieldset()); });
        }
        draftChanged();
    }

    async function loadAddresses() {
        addressesToken += 1;
        var token = addressesToken;
        state.addresses = { list: null, loading: true, failed: false };
        var list = null;
        try {
            // The customer's saved addresses (the Address editor's tiles).
            var response = await apiRequest('/auth/addresses');
            var result = await readJson(response);
            if (response.ok && result.success) list = (result.data && result.data.addresses) || [];
        } catch (error) {
            list = null;
        }
        if (token !== addressesToken) return;
        state.addresses = { list: list, loading: false, failed: list === null };
        var sub = selected();
        if (!sub || !editing(sub, 'address')) return;
        keepFocus(function () { $('#subs-edit-addresses').html(addressFieldset()); });
        var form = document.getElementById('subs-edit-address');
        if (form && !form.contains(document.activeElement)) {
            var field = firstField('address');
            if (field) field.focus();
        }
    }

    async function loadMethods(sub) {
        methodsToken += 1;
        var token = methodsToken;
        state.methods = { subId: sub.id, list: null, loading: true, failed: false };
        var list = null;
        try {
            var response = await apiRequest(METHODS_URL + sub.delivery_address_id);
            var result = await readJson(response);
            if (response.ok && result.success) list = (result.data && result.data.available_methods) || [];
        } catch (error) {
            list = null;
        }
        if (token !== methodsToken) return;
        state.methods = { subId: sub.id, list: list, loading: false, failed: list === null };
        if (state.selectedId !== sub.id) return;
        if (editing(sub, 'payment')) {
            keepFocus(function () { $('#subs-edit-methods').html(methodsFieldset(sub)); });
            var form = document.getElementById('subs-edit-payment');
            if (form && !form.contains(document.activeElement)) {
                var field = firstField('payment');
                if (field) field.focus();
            }
        } else {
            renderSection('payment');
        }
    }

    // ------------------------------------------------------------------ saving a section

    async function save() {
        var sub = selected();
        var edit = state.edit;
        if (!sub || !edit || !canSave()) return;
        edit.saving = true;
        edit.saveError = '';
        edit.guard = false;
        renderEditParts();
        var response = null;
        var result = {};
        try {
            // Each save sends back the state its screen was drawn from (spec 2026-10-27 §16 M2): the preview's for
            // items and schedule, the card's for the payment method. A stale one answers 409 "This changed".
            if (edit.section === 'items') {
                response = await apiRequest('/subscriptions/' + sub.id + '/items', {
                    method: 'PUT', body: JSON.stringify(withState(itemsBody(edit.draft), edit.preview.expected_state))
                });
            } else if (edit.section === 'schedule') {
                response = await apiRequest('/subscriptions/' + sub.id, {
                    method: 'PUT', body: JSON.stringify(withState(scheduleBody(edit.draft), edit.preview.expected_state))
                });
            } else if (edit.section === 'address') {
                response = await apiRequest('/subscriptions/' + sub.id, {
                    method: 'PUT', body: JSON.stringify(withState(addressBody(edit.draft), edit.preview.expected_state))
                });
            } else {
                response = await apiRequest('/subscriptions/' + sub.id + '/change-payment-method', {
                    method: 'POST',
                    body: JSON.stringify(withState({ payment_method: edit.draft.payment_method }, sub.allowed_actions.expected_state))
                });
            }
            result = await readJson(response);
        } catch (error) {
            response = null;
        }
        if (state.edit !== edit) return;
        var name = edit.section;
        var focusTargets = ['#subs-sec-' + name + ' [data-action="change"]', '#sub-h'];
        if (!(response && response.ok && result.success)) {
            // The screen the draft was made on changed meanwhile: the form closes on the latest, and the section says so.
            if (changedMeanwhile(response)) window.clearTimeout(previewTimer);
            if (await refreshWhenChanged(sub, response, result, name, focusTargets)) return;
            // Never reported as saved: the draft stays, the error stands next to Save until the next try.
            edit.saving = false;
            edit.saveError = refusal(response, result, 'error.save');
            renderEditParts();
            return;
        }
        window.clearTimeout(previewTimer);
        state.edit = null;
        await refresh(sub.id, { where: name, text: savedLine(name) }, focusTargets);
    }

    function savedLine(name) {
        return function (sub) {
            if (name === 'items') return text('edit.saved_items', { amount: sub.estimated_per_delivery_label });
            if (name === 'address') return text('edit.saved_address', { address: sub.delivery_address_label });
            if (name === 'payment') {
                return text('edit.saved_payment', { method: sub.payment_method_label }) +
                    (sub.kept_order_note ? ' ' + sub.kept_order_note : '');
            }
            var when = dateWindow(sub.upcoming_delivery);
            return when ? text('edit.saved_schedule', { date: when }) : text('edit.saved');
        };
    }

    // ------------------------------------------------------------------ the product picker

    function minimumOf(product) {
        return product.min_order_quantity || 1;
    }

    function candidate(productId) {
        return (state.edit.candidates || []).filter(function (product) { return product.id === productId; })[0];
    }

    function pickRow(product) {
        var images = product.images || [];
        var quantity = state.edit.picks[product.id];
        return '<li class="subs-item subs-pick" data-product-id="' + product.id + '">' +
            '<span class="subs-item__thumb">' + (images.length ? '<img src="' + esc(images[0]) + '" alt="" loading="lazy">' : bottle('thumb')) + '</span>' +
            '<div class="subs-item__text"><b>' + esc(product.product_name) + '</b>' +
            (product.unit_price_label ? '<small class="subs-pick__price">' + esc(product.unit_price_label) + '</small>' : '') +
            '</div>' +
            '<div class="subs-item__controls">' + stepper({ product_id: product.id, quantity: quantity }, minimumOf(product), 'pick-', product.product_name) +
            '<button type="button" class="subs-btn subs-btn--secondary subs-pick__add" data-action="picker-add" ' +
            'data-product-id="' + product.id + '" data-focus="pick-add-' + product.id + '">' +
            esc(text('picker.add', { quantity: String(quantity), name: product.product_name })) + '</button></div></li>';
    }

    function pickerContent(failed) {
        var candidates = state.edit.candidates;
        var list = failed
            ? '<p class="subs-error-line" role="alert">' + esc(text('picker.failed')) + '</p>'
            : (candidates.length
                ? '<ul class="subs-items subs-picker">' + candidates.map(pickRow).join('') + '</ul>'
                : '<p class="subs-hint">' + esc(text('picker.empty')) + '</p>');
        return '<div class="modal-header"><h2 class="modal-title subs-modal__title" id="subs-picker-title">' + esc(text('picker.title')) + '</h2>' +
            '<button type="button" class="close subs-modal__close" data-dismiss="modal"><span aria-hidden="true">&times;</span>' +
            '<span class="sr-only">' + esc(text('cancel.close')) + '</span></button></div>' +
            '<div class="modal-body">' + list + '</div>';
    }

    async function openPicker(button) {
        var edit = state.edit;
        if (!edit || edit.section !== 'items') return;
        setBusy(button, text('busy.loading'));
        var products = null;
        try {
            var response = await apiRequest(PRODUCTS_URL);
            var result = await readJson(response);
            if (response.ok && result.success) products = (result.data && result.data.items) || [];
        } catch (error) {
            products = null;
        }
        setIdle(button);
        if (state.edit !== edit) return;
        var inDraft = {};
        edit.draft.items.forEach(function (row) { inDraft[row.product_id] = true; });
        // The backend lists only what a subscription can take; the picker leaves out what the draft has.
        edit.candidates = (products || []).filter(function (product) { return !inDraft[product.id]; });
        edit.picks = {};
        edit.candidates.forEach(function (product) { edit.picks[product.id] = minimumOf(product); });
        $('#subs-picker-content').html(pickerContent(products === null));
        modalReturn = button;
        showModal('#subs-picker-modal');
    }

    function renderPick(productId) {
        var product = candidate(productId);
        if (!product) return;
        keepFocus(function () {
            $('#subs-picker-content .subs-pick[data-product-id="' + productId + '"]').replaceWith(pickRow(product));
        });
    }

    function addPicked(productId) {
        var edit = state.edit;
        var product = candidate(productId);
        if (!product) return;
        edit.draft.items.push({
            product_id: product.id,
            quantity: edit.picks[product.id],
            product_name: product.product_name,
            product: { images: product.images || [] },
            available: true,
            min_order_quantity: minimumOf(product),
            unit_price_label: null,
            line_total_label: null
        });
        // The new row joins the draft as unsaved; it goes through the same save bar (UX brief "PRODUCT PICKER").
        afterModal = function () {
            draftChanged({ wholeSection: true });
            var input = document.querySelector('#subs-edit-rows [data-focus="qty-' + product.id + '"]');
            if (input) input.focus();
        };
        hideModal('#subs-picker-modal');
    }

    // ------------------------------------------------------------------ the Cancel modal

    // What cancelling does, as the backend publishes it: its sentence, each order it keeps (worded by the reason it
    // is kept, so only a paid one is ever called paid), each order it cancels, and the coins line.
    function cancelContent(sub) {
        var plan = sub.allowed_actions.cancel;
        var lines = [plan.confirm_text].concat(plan.kept_lines || [], plan.cancel_lines || [], [plan.coins_line]);
        var alternatives = [];
        if (allowed(sub, 'pause')) alternatives.push(actionButton('secondary', 'cancel-pause', text('cancel.pause_instead')));
        if (allowed(sub, 'edit')) alternatives.push(actionButton('secondary', 'cancel-less-often', text('cancel.less_often')));
        return '<div class="modal-header"><h2 class="modal-title subs-modal__title" id="subs-cancel-title">' +
            esc(text('cancel.title', { name: sub.name })) + '</h2>' +
            '<button type="button" class="close subs-modal__close" data-dismiss="modal"><span aria-hidden="true">&times;</span>' +
            '<span class="sr-only">' + esc(text('cancel.close')) + '</span></button></div>' +
            '<div class="modal-body">' +
            '<ul class="subs-modal__list">' + lines.map(function (line) { return '<li>' + esc(line) + '</li>'; }).join('') + '</ul>' +
            (alternatives.length
                ? '<div class="subs-modal__alt"><p>' + esc(text('cancel.alternatives')) + '</p>' +
                    '<div class="subs-confirm__row">' + alternatives.join('') + '</div></div>'
                : '') +
            '<label class="subs-label" for="subs-cancel-reason">' + esc(text('cancel.reason')) + '</label>' +
            '<textarea class="form-control subs-input" id="subs-cancel-reason" rows="2" maxlength="500"></textarea>' +
            '<p class="subs-error-line" id="subs-cancel-error" role="alert" hidden></p>' +
            '</div>' +
            '<div class="modal-footer subs-modal__footer">' +
            '<button type="button" class="subs-btn subs-btn--secondary" data-action="cancel-keep" data-dismiss="modal">' + esc(plan.keep_label) + '</button>' +
            '<button type="button" class="subs-btn subs-btn--danger" data-action="cancel-confirm">' + esc(plan.confirm_label) + '</button>' +
            '</div>';
    }

    function showModal(selector) {
        if ($.fn.modal) $(selector).modal('show');
    }

    function hideModal(selector) {
        if ($.fn.modal) $(selector).modal('hide');
    }

    // Runs `next` once the modal is closed. The customer may have closed it while its request was in flight (Keep,
    // the ×, Esc or the backdrop). Bootstrap then ignores hide() and fires no hidden event, so `next` runs at once.
    // hide() removes `show` the moment it starts, so a modal still fading out counts as closed. Clearing modalReturn
    // stops its late hidden event from moving focus off what `next` focused.
    function afterClosing(selector, next) {
        if (!$(selector).hasClass('show')) {
            modalReturn = null;
            next();
            return;
        }
        afterModal = next;
        hideModal(selector);
    }

    async function cancelSubscription(button) {
        var sub = selected();
        if (!sub || isBusy(button)) return;
        var reason = String($('#subs-cancel-reason').val() || '').trim();
        setBusy(button, text('busy.cancelling'));
        $('#subs-cancel-error').prop('hidden', true).text('');
        var response = null;
        var result = {};
        try {
            response = await apiRequest('/subscriptions/' + sub.id + '/cancel', {
                method: 'POST', body: JSON.stringify(withState(reason ? { reason: reason } : {}, sub.allowed_actions.expected_state))
            });
            result = await readJson(response);
        } catch (error) {
            response = null;
        }
        if (response && response.ok && result.success) {
            afterClosing('#subs-cancel-modal', function () {
                refresh(sub.id, { where: 'hero', text: text('cancel.done') }, ['#sub-h']);
            });
            return;
        }
        if (changedMeanwhile(response)) {
            // Nothing was cancelled: the modal closes, and the card says what changed and shows the latest.
            afterClosing('#subs-cancel-modal', function () {
                refreshWhenChanged(sub, response, result, 'hero', HERO_FOCUS);
            });
            return;
        }
        setIdle(button);
        $('#subs-cancel-error').text(refusal(response, result, 'error.action')).prop('hidden', false);
    }

    // ------------------------------------------------------------------ the card's actions

    function isBusy(button) {
        return Boolean(button && button.getAttribute('aria-disabled') === 'true');
    }

    // An action in flight: its button says so and refuses a second tap (UX brief "ASYNC BUTTONS").
    function setBusy(button, label) {
        if (!button) return;
        $(button).data('subs-label', button.innerHTML).attr('aria-disabled', 'true').addClass('is-busy')
            .html('<span class="spinner-border spinner-border-sm" aria-hidden="true"></span>' + esc(label));
    }

    function setIdle(button) {
        if (!button) return;
        var label = $(button).data('subs-label');
        $(button).removeAttr('aria-disabled').removeClass('is-busy');
        if (label !== undefined) $(button).html(label);
    }

    function showHeroStatus(sub, message, error) {
        state.status = { subId: sub.id, where: 'hero', text: message, error: Boolean(error) };
        var $line = $('#subs-hero-status');
        $line.text(message).toggleClass('is-error', Boolean(error));
        if (error) $line.attr('role', 'alert');
        else {
            $line.removeAttr('role');
            announce(message);
        }
    }

    // Pay: the card's button and every Orders row's call this one function (UX brief "HERO PRIORITY"), through the
    // write path every order surface shares. It sends what this screen showed (`expected_amount`, spec 2026-10-27
    // §4.2); the backend answers 409 when the amount moved or the order can no longer be paid online.
    async function pay(button) {
        var sub = selected();
        if (!sub) return;
        var orderId = parseInt(button.getAttribute('data-order-id'), 10);
        var $row = $(button).closest('.subs-orow');
        $row.find('.subs-orow__error').remove();
        clearStatus();
        setBusy(button, text('busy.paying'));
        var result = await window.OrderActions.pay(orderId, { expected_amount: button.getAttribute('data-amount') });
        // On its way to Click: the button stays busy, so a second tap opens nothing more.
        if (result.ok) return;
        setIdle(button);
        if (await refreshWhenChanged(sub, result, result, 'hero', HERO_FOCUS)) return;
        var message = refusal(result, result, 'error.pay');
        if ($row.length) $row.append('<p class="subs-orow__error subs-error-line" role="alert">' + esc(message) + '</p>');
        else showHeroStatus(sub, message, true);
    }

    // Open the card's confirm (`kind` 'skip' or 'pause') or an Orders row's ('order', its order id). One is open at a
    // time, so opening one closes any other; opening the open one again closes it.
    function openConfirmFor(kind, orderId) {
        var sub = selected();
        if (!sub) return;
        var id = orderId === undefined ? null : orderId;
        var open = state.confirm;
        if (open && open.subId === sub.id && open.kind === kind && open.orderId === id) {
            closeConfirmPanel();
            return;
        }
        clearStatus();
        state.confirm = { subId: sub.id, kind: kind, orderId: id, reason: null, other: '', error: '' };
        renderConfirm(true);
    }

    // Close the open confirm and give focus back to the button that opened it.
    function closeConfirmPanel() {
        var open = state.confirm;
        state.confirm = null;
        renderConfirm(false);
        if (!open) return;
        $(open.kind === 'order'
            ? '#subs-orders [data-action="order-remove"][data-order-id="' + open.orderId + '"]'
            : '#subscription [data-action="' + open.kind + '"]').trigger('focus');
    }

    async function skip(button) {
        var sub = selected();
        var open = state.confirm;
        if (!sub || !open) return;
        var plan = sub.allowed_actions.skip;
        var body = { expected_date: plan.date };  // the date the button names (spec §12)
        if (open.reason) body.reason_code = open.reason;
        var words = String(open.other || '').trim();
        if (open.reason === 'other' && words) body.reason_text = words;
        open.error = '';
        setBusy(button, text('busy.skipping'));
        var response = null;
        var result = {};
        try {
            response = await apiRequest('/subscriptions/' + sub.id + '/skip-next-delivery', { method: 'POST', body: JSON.stringify(body) });
            result = await readJson(response);
        } catch (error) {
            response = null;
        }
        if (response && response.ok && result.success) {
            await refresh(sub.id, { where: 'hero', text: afterSkip }, HERO_FOCUS);
            return;
        }
        // Review Focus 1: the delivery the button named changed meanwhile. Nothing was skipped; show the latest.
        if (await refreshWhenChanged(sub, response, result, 'hero', HERO_FOCUS)) return;
        setIdle(button);
        open.error = refusal(response, result, 'error.action');
        renderConfirm(false);
    }

    function afterSkip(sub) {
        var when = dateWindow(sub.upcoming_delivery);
        return when ? text('skip.done', { date: when }) : text('skip.done_plain');
    }

    function startPause(button) {
        var sub = selected();
        if (!sub) return;
        // Two steps only when the backend says the pause needs a confirm (it cancels an order; UX brief "PAUSE").
        if (sub.allowed_actions.pause.needs_confirm) openConfirmFor('pause');
        else pause(button);
    }

    async function pause(button) {
        var sub = selected();
        if (!sub) return;
        if (state.confirm) state.confirm.error = '';
        clearStatus();
        setBusy(button, text('busy.pausing'));
        var response = null;
        var result = {};
        try {
            response = await apiRequest('/subscriptions/' + sub.id + '/pause', {
                method: 'POST', body: JSON.stringify(withState({}, sub.allowed_actions.expected_state))
            });
            result = await readJson(response);
        } catch (error) {
            response = null;
        }
        if (response && response.ok && result.success) {
            await refresh(sub.id, { where: 'hero', text: text('pause.done') }, HERO_FOCUS);
            return;
        }
        if (await refreshWhenChanged(sub, response, result, 'hero', HERO_FOCUS)) return;
        setIdle(button);
        if (state.confirm) {
            state.confirm.error = refusal(response, result, 'error.action');
            renderConfirm(false);
        } else {
            showHeroStatus(sub, refusal(response, result, 'error.action'), true);
        }
    }

    async function resume(button) {
        var sub = selected();
        if (!sub) return;
        clearStatus();
        setBusy(button, text('busy.resuming'));
        var response = null;
        var result = {};
        try {
            response = await apiRequest('/subscriptions/' + sub.id + '/resume', { method: 'POST', body: JSON.stringify({}) });
            result = await readJson(response);
        } catch (error) {
            response = null;
        }
        if (response && response.ok && result.success) {
            await refresh(sub.id, { where: 'hero', text: afterResume }, HERO_FOCUS);
            return;
        }
        setIdle(button);
        showHeroStatus(sub, refusal(response, result, 'error.action'), true);
    }

    function afterResume(sub) {
        var when = dateWindow(sub.upcoming_delivery);
        return when ? text('resume.done', { date: when }) : text('resume.done_plain');
    }

    // ------------------------------------------------------------------ Orders from this subscription

    // One Orders row. An order's row offers what that order's own `customer_actions` allows (a skipped row has
    // none): Pay, and Skip or Cancel with its inline confirm under the row (spec 2026-10-27 §5.3).
    function historyRow(row) {
        var actions = row.customer_actions;
        var id = esc(row.order_id);
        var when = row.order_id ? ' id="subs-orow-' + id + '-when"' : '';
        var content = '<span class="subs-orow__when"' + when + '>' + esc(row.date_label) + '</span>' + chip(row.status) +
            '<span class="subs-orow__why">' + esc(row.reason_label || '') + '</span>' +
            '<span class="subs-money subs-orow__amt">' + esc(row.amount_label || '') + '</span>';
        var main = row.order_id
            ? '<button type="button" class="subs-orow__open" data-action="open-order" data-order-id="' + id + '">' + content + '</button>'
            : '<div class="subs-orow__open">' + content + '</div>';
        var payButton = actions && actions.pay.allowed
            ? '<button type="button" class="subs-btn subs-btn--secondary subs-orow__pay" data-action="pay" data-order-id="' + id + '"' +
                ' data-amount="' + esc(actions.pay.expected_amount) + '">' +
                '<span aria-hidden="true">' + esc(text('orders.pay')) + '</span>' +
                '<span class="sr-only">' + esc(text('orders.pay_label', { number: row.order_number || '' })) + '</span></button>'
            : '';
        var remove = '';
        if (actions && actions.cancel.allowed) {
            var open = rowConfirmOpen(row.order_id);
            remove = '<button type="button" class="subs-btn subs-btn--secondary subs-orow__act" data-action="order-remove" ' +
                'data-order-id="' + id + '" aria-controls="subs-orow-confirm-' + id + '" aria-expanded="' + (open ? 'true' : 'false') + '" ' +
                'aria-describedby="subs-orow-' + id + '-when">' + esc(actions.cancel.label) + '</button>' +
                '<div class="subs-orow__confirm" id="subs-orow-confirm-' + id + '" data-order-id="' + id + '" tabindex="-1"' +
                (open ? '>' + rowConfirmPanel(row) : ' hidden>') + '</div>';
        }
        return '<li class="subs-orow' + (row.kind === 'skipped' ? ' subs-orow--skipped' : '') + '">' + main + payButton + remove + '</li>';
    }

    // The Orders row shown for this order, if any.
    function shownRow(orderId) {
        var rows = (state.history && state.history.rows) || [];
        return rows.filter(function (row) { return row.order_id === orderId; })[0] || null;
    }

    // An Orders row's Skip or Cancel, through the write path every order surface shares: it sends the `via` the row
    // showed and, for a skip, the reason picked. The backend's sentence for what it did lands in Orders.
    async function removeOrder(button) {
        var sub = selected();
        var open = state.confirm;
        var row = open && open.kind === 'order' ? shownRow(open.orderId) : null;
        if (!sub || !row) return;
        var cancel = row.customer_actions.cancel;
        var reason = {};
        if (open.reason) reason.reason_code = open.reason;
        var words = String(open.other || '').trim();
        if (open.reason === 'other' && words) reason.reason_text = words;
        open.error = '';
        setBusy(button, text(cancel.via === 'skip' ? 'busy.skipping' : 'busy.cancelling'));
        var result = await window.OrderActions.remove(row.order_id, cancel, reason);
        if (result.ok) {
            await refresh(sub.id, { where: 'orders', text: result.message }, ['#subs-orders']);
            return;
        }
        if (await refreshWhenChanged(sub, result, result, 'orders', ['#subs-orders'])) return;
        setIdle(button);
        open.error = refusal(result, result, 'error.action');
        renderConfirm(false);
    }

    function renderHistory(sub) {
        var history = state.history;
        var $list = $('#subs-orders');
        $list.html(history.rows.map(historyRow).join('')).attr('aria-busy', history.loading ? 'true' : 'false');
        var $empty = $('#subs-orders-empty');
        var upcoming = sub.upcoming_delivery;
        if (history.failed) {
            $empty.text(text('orders.load_failed')).prop('hidden', false);
        } else if (!history.loading && !history.rows.length) {
            $empty.text(upcoming && upcoming.will_place_on_label
                ? text('orders.empty_planned', { date: upcoming.will_place_on_label })
                : text('orders.empty')).prop('hidden', false);
        } else {
            $empty.prop('hidden', true).text('');
        }
        $('#subs-orders-more').prop('hidden', !history.hasNext);
    }

    async function fetchHistoryPage(subId, page) {
        var response = await apiRequest('/subscriptions/' + subId + '/history?page=' + page + '&per_page=' + HISTORY_PAGE_SIZE);
        var result = await response.json();
        if (!response.ok || !result.success) throw new Error(result.message || 'history');
        return { rows: (result.data && result.data.items) || [], meta: result.meta || {} };
    }

    async function loadHistory(sub, pages) {
        var history = state.history;
        var token = history.token;
        history.loading = true;
        history.failed = false;
        try {
            for (var i = 0; i < pages; i += 1) {
                if (history.page > 0 && !history.hasNext) break;
                var answer = await fetchHistoryPage(sub.id, history.page + 1);
                if (token !== state.history.token) return false;  // another subscription is shown now
                history.rows = history.rows.concat(answer.rows);
                history.page += 1;
                history.total = answer.meta.total || history.rows.length;
                history.hasNext = Boolean(answer.meta.has_next);
            }
            return true;
        } catch (error) {
            history.failed = true;
            return false;
        } finally {
            if (token === state.history.token) {
                history.loading = false;
                renderHistory(sub);
            }
        }
    }

    function startHistory(sub) {
        historyToken += 1;
        state.history = {
            subId: sub.id, rows: [], page: 0, total: 0, hasNext: false, loading: true, failed: false, token: historyToken
        };
        return loadHistory(sub, 1);
    }

    async function moreHistory() {
        var sub = selected();
        if (!sub || !state.history || state.history.loading) return;
        var $more = $('#subs-orders-more');
        $more.attr('aria-disabled', 'true');
        var ok = await loadHistory(sub, HISTORY_MORE_PAGES);
        $more.removeAttr('aria-disabled');
        if (!ok) return;
        announce(text('orders.showing', { shown: String(state.history.rows.length), total: String(state.history.total) }));
        // Focus stays on the button; once every row is shown it moves to the list it filled.
        if (!$more.prop('hidden')) $more.trigger('focus');
        else $('#subs-orders').trigger('focus');
    }

    // ------------------------------------------------------------------ selection and loading

    function selected() {
        for (var i = 0; i < state.subs.length; i += 1) {
            if (state.subs[i].id === state.selectedId) return state.subs[i];
        }
        return null;
    }

    function announce(message) {
        $('#subs-status').text(message);
    }

    function render(options) {
        var sub = selected();
        var $body = $('#subscription');
        if (!sub) {
            $('#subs-strip-nav').prop('hidden', true);
            $body.removeClass('is-readonly').html(emptyState()).attr('aria-busy', 'false');
            return;
        }
        if (lifecycle(sub) === 'cancelled' && strip(state.subs).live.length) state.showCancelled = true;
        renderStrip();
        $body.toggleClass('is-readonly', !allowed(sub, 'edit')).html(body(sub)).attr('aria-busy', 'false');
        followSavebar();
        startHistory(sub);
        // The payment menu of a subscription that can still change: Change on Payment depends on it.
        if (allowed(sub, 'edit')) loadMethods(sub);
        else state.methods = null;
        if (options && options.focus) {
            var heading = document.getElementById('sub-h');
            if (heading) {
                heading.focus();
                scrollIntoSight(heading, 'start');
            }
        }
    }

    function requestedId() {
        var raw = new URLSearchParams(window.location.search).get('sub');
        if (raw === null) return null;
        var id = parseInt(raw, 10);
        return isNaN(id) ? null : id;
    }

    function select(id, options) {
        var known = state.subs.some(function (sub) { return sub.id === id; });
        state.selectedId = known ? id : defaultSelection(state.subs);
        state.status = null;
        state.confirm = null;
        state.edit = null;
        render(options);
    }

    async function fetchList() {
        var response = await apiRequest(LIST_URL);
        var result = await response.json();
        if (!response.ok || !result.success) throw new Error(result.message || 'list');
        return (result.data && result.data.items) || [];
    }

    // After a write: read the published answers again and re-draw from them, with the action's result line
    // (`status.text` may be worded from the fresh subscription). Nothing changes before the server has answered.
    async function refresh(subId, status, focusTargets) {
        var subs;
        try {
            subs = await fetchList();
        } catch (error) {
            showLoadError();
            return;
        }
        state.subs = subs;
        var known = subs.some(function (sub) { return sub.id === subId; });
        state.selectedId = known ? subId : defaultSelection(subs);
        state.confirm = null;
        state.edit = null;
        state.status = null;
        var sub = selected();
        if (status && sub) {
            var message = typeof status.text === 'function' ? status.text(sub) : status.text;
            state.status = { subId: sub.id, where: status.where, text: message, error: Boolean(status.error) };
            if (!status.error) announce(message);
        }
        render({ focus: false });
        for (var i = 0; focusTargets && i < focusTargets.length; i += 1) {
            var target = document.querySelector(focusTargets[i]);
            if (target) {
                target.focus();
                break;
            }
        }
    }

    // A write's answer that the screen it was sent from changed meanwhile (spec 2026-10-27 §16 M2): what it echoed
    // (`expected_state`, `expected_amount`, `expected_via`, `expected_date`) no longer holds, or the Pay it named is
    // now refused. Either way nothing was written, and the backend answers 409.
    function changedMeanwhile(response) {
        return Boolean(response) && response.status === 409;
    }

    // On such an answer, read the latest and show the backend's own sentence for it (the refresh line, or the Pay
    // refusal) as an alert where the customer acted (`where`: 'hero' or a section). Returns whether it did.
    async function refreshWhenChanged(sub, response, result, where, focusTargets) {
        if (!changedMeanwhile(response)) return false;
        await refresh(sub.id, { where: where, text: refusal(response, result, 'error.action'), error: true }, focusTargets);
        return true;
    }

    function showLoadError() {
        var $body = $('#subscription');
        $body.attr('aria-busy', 'false').html('<div class="subs-error" role="alert"><p>' + esc(text('load_failed')) + '</p>' +
            '<button type="button" class="subs-btn subs-btn--secondary" data-action="retry-load">' + esc(text('retry')) + '</button></div>');
    }

    async function load() {
        var $body = $('#subscription');
        $body.attr('aria-busy', 'true');
        var timer = window.setTimeout(function () { $('#subs-skeleton').addClass('is-visible'); }, SKELETON_DELAY_MS);
        try {
            state.subs = await fetchList();
            select(requestedId(), { focus: false });
        } catch (error) {
            showLoadError();
        } finally {
            window.clearTimeout(timer);
        }
    }

    // ------------------------------------------------------------------ events

    // A body button's click, unless it is blocked or busy (aria-disabled, kept focusable; UX brief).
    function onBody(action, handler) {
        $(document).on('click', '#subscription [data-action="' + action + '"]', function (event) {
            if (isBusy(this)) {
                event.preventDefault();
                return;
            }
            handler.call(this, event);
        });
    }

    // A card action first closes a form that has nothing unsaved; with unsaved changes it points at the save bar.
    function leaveForm() {
        if (guardDirty()) return false;
        if (state.edit) closeEdit({ focus: false });
        return true;
    }

    $(document).on('click', '#subs-strip a.subs-card[data-sub-id]', function (event) {
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button > 0) return;
        event.preventDefault();
        var id = parseInt(this.getAttribute('data-sub-id'), 10);
        if (id === state.selectedId) return;
        if (guardDirty()) return;
        window.history.pushState({ sub: id }, '', subHref(id));
        select(id, { focus: true });
    });

    $(document).on('click', '#subs-strip [data-action="toggle-cancelled"]', function () {
        state.showCancelled = !state.showCancelled;
        renderStrip();
        $('#subs-strip [data-action="toggle-cancelled"]').trigger('focus');
    });

    onBody('open-order', function () {
        var id = parseInt(this.getAttribute('data-order-id'), 10);
        if (typeof window.showOrderDetail === 'function') window.showOrderDetail(id);
    });
    onBody('more-orders', function () { moreHistory(); });
    onBody('retry-load', function () { load(); });

    // The card.
    onBody('pay', function () { pay(this); });
    onBody('skip', function () { if (leaveForm()) openConfirmFor('skip'); });
    onBody('pause', function () { if (leaveForm()) startPause(this); });
    onBody('resume', function () { if (leaveForm()) resume(this); });
    onBody('confirm-keep', function () { closeConfirmPanel(); });
    onBody('skip-reason', function () {
        var code = this.getAttribute('data-reason');
        state.confirm.reason = state.confirm.reason === code ? null : code;
        renderConfirm(false);
        if (state.confirm.reason === 'other') $('#subs-skip-other').trigger('focus');
    });
    onBody('skip-confirm', function () { skip(this); });
    onBody('pause-confirm', function () { pause(this); });

    // Orders rows.
    onBody('order-remove', function () {
        if (leaveForm()) openConfirmFor('order', parseInt(this.getAttribute('data-order-id'), 10));
    });
    onBody('order-remove-confirm', function () { removeOrder(this); });
    // The order modal skipped, cancelled or paid an order (order-detail-modal.js): read the selected subscription
    // again, so the card and its Orders no longer show what changed. Unsaved changes wait for their own save.
    document.addEventListener('orders:changed', function () {
        if (state.selectedId !== null && !dirty()) refresh(state.selectedId, null, []);
    });
    $(document).on('input', '#subs-skip-other', function () {
        if (state.confirm) state.confirm.other = this.value;
    });

    // The sections.
    onBody('change', function () {
        var name = this.getAttribute('data-section');
        if (state.edit && state.edit.section === name) {
            if (!guardDirty()) closeEdit();
            return;
        }
        if (leaveForm()) openEdit(name);
    });
    onBody('discard', function () { closeEdit(); });
    onBody('save', function () { save(); });

    function rowOf(element) {
        var id = parseInt(element.getAttribute('data-product-id'), 10);
        return state.edit.draft.items.filter(function (row) { return row.product_id === id; })[0];
    }

    onBody('inc', function () {
        rowOf(this).quantity += 1;
        draftChanged();
    });
    // − stops at the product's minimum (aria-disabled there); removal is the separate button (UX brief "ITEMS").
    onBody('dec', function () {
        rowOf(this).quantity -= 1;
        draftChanged();
    });
    $(document).on('input', '#subscription .subs-stepper__input[data-action="qty"]', function () {
        var digits = this.value.replace(/[^0-9]/g, '');
        if (digits !== this.value) this.value = digits;
        var quantity = parseInt(digits, 10);
        if (!quantity) return;  // an empty or zero box waits; leaving it restores the quantity
        rowOf(this).quantity = quantity;
        draftChanged();
    });
    $(document).on('change', '#subscription .subs-stepper__input[data-action="qty"]', function () {
        this.value = rowOf(this).quantity;
    });
    // Removing the last product is shown but blocked, its reason beside it (UX brief "DISABLED VS HIDDEN").
    onBody('remove-item', function () {
        var id = parseInt(this.getAttribute('data-product-id'), 10);
        var rows = state.edit.draft.items;
        var index = rows.map(function (row) { return row.product_id; }).indexOf(id);
        rows.splice(index, 1);
        draftChanged({ wholeSection: true });
        var next = rows[Math.min(index, rows.length - 1)];
        var target = next ? document.querySelector('#subs-edit-rows [data-focus="remove-' + next.product_id + '"]') : null;
        if (!target || isBusy(target)) target = document.querySelector('#subs [data-focus="add-product"]');
        if (target) target.focus();
    });
    onBody('open-picker', function () { openPicker(this); });

    $(document).on('change', '#subscription input[name="subs-frequency"]', function () {
        state.edit.draft.frequency = this.value;
        keepFocus(function () {
            $('#subs-edit-freq').html(frequencyFieldset());
            $('#subs-edit-days').html(dayFieldset());
        });
        loadSlots();
    });
    $(document).on('change', '#subscription input[name="subs-weekday"]', function () {
        state.edit.draft.delivery_day_of_week = parseInt(this.value, 10);
        keepFocus(function () { $('#subs-edit-days').html(dayFieldset()); });
        loadSlots();
    });
    $(document).on('change', '#subscription #subs-dom', function () {
        state.edit.draft.delivery_day_of_month = this.value ? parseInt(this.value, 10) : null;
        draftChanged();
    });
    $(document).on('change', '#subscription input[name="subs-slot"]', function () {
        state.edit.draft.delivery_time_slot_id = this.value === ANY_TIME ? null : parseInt(this.value, 10);
        state.edit.slotMessage = '';
        draftChanged();
    });
    // A new address starts over: no method asked for yet, until its own preview says one is needed.
    $(document).on('change', '#subscription input[name="subs-address"]', function () {
        var edit = state.edit;
        edit.draft.delivery_address_id = parseInt(this.value, 10);
        edit.draft.payment_method = null;
        edit.methodChoices = null;
        draftChanged();
    });
    $(document).on('change', '#subscription input[name="subs-address-method"]', function () {
        state.edit.draft.payment_method = this.value;
        draftChanged();
    });
    $(document).on('change', '#subscription input[name="subs-method"]', function () {
        state.edit.draft.payment_method = this.value;
        keepFocus(function () { $('#subs-edit-methods').html(methodsFieldset(selected())); });
        draftChanged();
    });

    // The product picker.
    $(document).on('click', '#subs-picker-modal [data-action="pick-inc"], #subs-picker-modal [data-action="pick-dec"]', function () {
        if (isBusy(this)) return;
        var id = parseInt(this.getAttribute('data-product-id'), 10);
        state.edit.picks[id] += this.getAttribute('data-action') === 'pick-inc' ? 1 : -1;
        renderPick(id);
    });
    $(document).on('input', '#subs-picker-modal .subs-stepper__input', function () {
        var id = parseInt(this.getAttribute('data-product-id'), 10);
        var digits = this.value.replace(/[^0-9]/g, '');
        if (digits !== this.value) this.value = digits;
        var quantity = parseInt(digits, 10);
        if (!quantity) return;
        state.edit.picks[id] = quantity;
        $(this).closest('.subs-pick').find('[data-action="picker-add"]')
            .text(text('picker.add', { quantity: String(quantity), name: candidate(id).product_name }));
    });
    $(document).on('click', '#subs-picker-modal [data-action="picker-add"]', function () {
        addPicked(parseInt(this.getAttribute('data-product-id'), 10));
    });

    // A modal that opens drops what an earlier one left to run, so that never runs when this one closes.
    $(document).on('show.bs.modal', '#subs-cancel-modal, #subs-picker-modal', function () {
        afterModal = null;
    });
    // The Cancel modal (opened by Bootstrap's data-toggle on the footer's Cancel).
    $(document).on('show.bs.modal', '#subs-cancel-modal', function (event) {
        var sub = selected();
        if (!sub) return;
        modalReturn = event.relatedTarget || document.activeElement;
        $('#subs-cancel-content').html(cancelContent(sub));
    });
    $(document).on('shown.bs.modal', '#subs-cancel-modal', function () {
        $('#subs-cancel-modal [data-action="cancel-keep"]').trigger('focus');
    });
    $(document).on('shown.bs.modal', '#subs-picker-modal', function () {
        var first = document.querySelector('#subs-picker-modal [data-action="picker-add"]') ||
            document.querySelector('#subs-picker-modal .close');
        if (first) first.focus();
    });
    $(document).on('hidden.bs.modal', '#subs-cancel-modal, #subs-picker-modal', function () {
        var next = afterModal;
        afterModal = null;
        if (next) {
            next();
            return;
        }
        if (modalReturn && document.body.contains(modalReturn)) modalReturn.focus();
    });
    $(document).on('click', '#subs-cancel-modal [data-action="cancel-confirm"]', function () {
        cancelSubscription(this);
    });
    $(document).on('click', '#subs-cancel-modal [data-action="cancel-pause"]', function () {
        afterModal = function () {
            if (!leaveForm()) return;
            var button = document.querySelector('#subscription [data-action="pause"]');
            if (button) button.focus();
            startPause(button);
        };
        hideModal('#subs-cancel-modal');
    });
    $(document).on('click', '#subs-cancel-modal [data-action="cancel-less-often"]', function () {
        afterModal = function () {
            if (!leaveForm()) return;
            openEdit('schedule');
            scrollIntoSight(document.getElementById('subs-sec-schedule'), 'start');
        };
        hideModal('#subs-cancel-modal');
    });

    // Leaving with unsaved changes asks first, and only then.
    window.addEventListener('beforeunload', function (event) {
        if (!dirty()) return;
        event.preventDefault();
        event.returnValue = '';
    });

    // Back and forward between subscriptions. A same-page jump (the jump links, #subscription) also fires
    // popstate; it names the subscription already shown, so nothing is re-drawn.
    window.addEventListener('popstate', function () {
        if (!state.subs.length) return;
        var id = requestedId();
        var target = state.subs.some(function (sub) { return sub.id === id; }) ? id : defaultSelection(state.subs);
        if (target === state.selectedId) return;
        if (dirty()) {
            window.history.pushState({ sub: state.selectedId }, '', subHref(state.selectedId));
            guardDirty();
            return;
        }
        select(id, { focus: false });
    });

    $(function () {
        if (reducedMotion()) $.fx.off = true;
        load();
    });
})(jQuery);
