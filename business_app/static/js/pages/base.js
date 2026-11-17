(function () {
    var PAGE_DATA = getPageData('base-page-data');

    // Global variables (attached to window for legacy compatibility with page scripts)
    window.API_BASE_URL = PAGE_DATA.api_base_url;
    window.CURRENT_USER = PAGE_DATA.current_user;

    // A customer page reached through the same-site hop or the quiet refresh carries the server's `_hop`
    // marker (business_app/utils/page_auth.py HOP_MARKER). It has done its job once the page renders: drop it
    // from the address, keeping the rest of the query and the #fragment, so a bookmark never carries it.
    (function dropHopMarker() {
        var query = window.location.search.replace(/^\?/, '');
        if (!query) return;
        var kept = query.split('&').filter(function (part) { return part.split('=')[0] !== '_hop'; });
        if (kept.length === query.split('&').length) return;
        var search = kept.length ? '?' + kept.join('&') : '';
        window.history.replaceState(window.history.state, '', window.location.pathname + search + window.location.hash);
    })();

    function getAuthToken() { return null; }

    function setAuthToken(token, refreshToken) {
        console.warn('setAuthToken called but tokens are now handled via httpOnly cookies');
    }

    function removeAuthTokens() {
        localStorage.removeItem('access_token');
        localStorage.removeItem('refresh_token');
        localStorage.removeItem('user_session_id');
    }

    function deleteCookie(name, path, domain) {
        path = path || '/';
        var cookieString = name + '=; Max-Age=0; Path=' + path + ';';
        if (domain) cookieString += ' Domain=' + domain + ';';
        document.cookie = cookieString;
    }

    // Log out. The server ends the login and its answer clears the httpOnly cookies, which script cannot touch
    // (business_app/api/auth.py logout). The call proves the login with the refresh cookie: its readable CSRF
    // value lasts the whole login, while the 30-minute access cookie may have run out. A call that did not
    // succeed (offline, a 5xx, a rate limit) never counts as signed out: the browser goes to the server's
    // sign-out page, which always clears the cookies, instead of the login page.
    async function logout() {
        var signedOut = false;
        try {
            var headers = { 'X-Platform': 'web' };
            var csrfToken = getCookie('csrf_refresh_token') || getCookie('csrf_access_token');
            if (csrfToken) headers['X-CSRF-TOKEN'] = csrfToken;
            var response = await fetch(window.API_BASE_URL + '/auth/logout', {
                method: 'POST',
                headers: headers,
                credentials: 'include'
            });
            signedOut = response.ok;
        } catch (error) {
            console.error('Logout API call failed:', error);
        }

        removeAuthTokens();
        window.cart = [];
        localStorage.removeItem('cart');

        // The readable halves go now, whatever happens next: a customer page never renews a refresh cookie
        // whose readable half is gone (business_app/utils/page_auth.py _renewed_access_token).
        deleteCookie('csrf_access_token', '/', '.aqua-element.uz');
        deleteCookie('csrf_refresh_token', '/', '.aqua-element.uz');
        deleteCookie('csrf_access_token', '/');
        deleteCookie('csrf_refresh_token', '/');

        window.location.replace(signedOut ? PAGE_DATA.login_url : PAGE_DATA.logout_url);
    }

    // Cart management
    window.cart = JSON.parse(localStorage.getItem('cart') || '[]');

    function updateCartCount() {
        var count = window.cart.reduce(function (sum, item) { return sum + item.quantity; }, 0);
        // The header has ONE counter (#cart-count); [data-cart-count] marks any other place that shows it.
        var cartCounters = document.querySelectorAll('#cart-count, [data-cart-count]');

        cartCounters.forEach(function (counter) {
            counter.textContent = count;
            counter.style.display = count > 0 ? 'inline' : 'none';
        });
    }

    async function addToCart(productId, quantity) {
        quantity = quantity || 1;
        var existingItem = window.cart.find(function (item) { return item.product_id === productId; });
        if (existingItem) {
            existingItem.quantity += quantity;
        } else {
            window.cart.push({ product_id: productId, quantity: quantity });
        }
        localStorage.setItem('cart', JSON.stringify(window.cart));
        updateCartCount();
        showNotification(PAGE_DATA.i18n.product_added, 'success');

        if (window.CURRENT_USER) {
            try {
                await apiRequest('/cart/items', {
                    method: 'POST',
                    body: JSON.stringify({ product_id: productId, quantity: quantity })
                });
            } catch (error) {
                console.error('Failed to sync item to backend cart:', error);
            }
        }
    }

    function removeFromCart(productId) {
        window.cart = window.cart.filter(function (item) { return item.product_id !== productId; });
        localStorage.setItem('cart', JSON.stringify(window.cart));
        updateCartCount();
    }

    // Toasts. The container is in base.html and the look is in ae-ui.css; a page without base.html's markup gets a
    // bare container. #toast-container and the .toast-* class names are what page scripts and tests read.
    // The container is the ONE live region (polite, additions only): a toast is announced once, whatever its type.
    // A live region added together with its text is often not read at all, and a role="alert" toast inside the
    // polite container was read twice by some screen readers.
    function toastContainer() {
        var container = document.getElementById('toast-container');
        if (!container) {
            container = document.createElement('div');
            container.id = 'toast-container';
            container.className = 'ae-toasts';
            container.setAttribute('aria-live', 'polite');
            container.setAttribute('aria-atomic', 'false');
            container.setAttribute('aria-relevant', 'additions');
            document.body.appendChild(container);
        }
        return container;
    }
    toastContainer();

    var TOAST_ICONS = {
        success: 'fas fa-check',
        error: 'fas fa-exclamation',
        warning: 'fas fa-exclamation',
        info: 'fas fa-info'
    };

    // How long a toast stays when the caller names no duration: an error stays twice as long. Hovering or focusing a
    // toast pauses its clock (and the progress line), so nobody loses a message while reading it.
    var TOAST_DURATION = { error: 10000 };
    var TOAST_DEFAULT_DURATION = 5000;

    function showNotification(message, type, duration) {
        type = TOAST_ICONS[type] ? type : 'info';
        if (duration === undefined) duration = TOAST_DURATION[type] || TOAST_DEFAULT_DURATION;

        var container = toastContainer();
        var i18n = PAGE_DATA.i18n || {};

        var toast = document.createElement('div');
        toast.className = 'toast-notification toast-' + type;

        var iconDiv = document.createElement('div');
        iconDiv.className = 'toast-icon';
        iconDiv.setAttribute('aria-hidden', 'true');
        var icon = document.createElement('i');
        icon.className = TOAST_ICONS[type];
        iconDiv.appendChild(icon);

        var contentDiv = document.createElement('div');
        contentDiv.className = 'toast-content';
        if (i18n[type]) {
            var titleDiv = document.createElement('div');
            titleDiv.className = 'toast-title';
            titleDiv.textContent = i18n[type];
            contentDiv.appendChild(titleDiv);
        }
        var messageDiv = document.createElement('div');
        messageDiv.className = 'toast-message';
        messageDiv.textContent = message;
        contentDiv.appendChild(messageDiv);

        var closeButton = document.createElement('button');
        closeButton.type = 'button';
        closeButton.className = 'toast-close';
        closeButton.setAttribute('aria-label', i18n.close || '×');
        closeButton.innerHTML = '<i class="far fa-times" aria-hidden="true"></i>';

        var progressDiv = document.createElement('div');
        progressDiv.className = 'toast-progress';
        if (duration > 0) progressDiv.style.animation = 'shrinkProgress ' + duration + 'ms linear forwards';

        toast.appendChild(iconDiv);
        toast.appendChild(contentDiv);
        toast.appendChild(closeButton);
        toast.appendChild(progressDiv);

        container.appendChild(toast);

        requestAnimationFrame(function () { toast.classList.add('show'); });

        var closeToast = function () {
            toast.classList.remove('show');
            toast.classList.add('hide');
            setTimeout(function () {
                if (toast.parentNode) toast.parentNode.removeChild(toast);
            }, 300);
        };

        closeButton.addEventListener('click', function (e) {
            e.stopPropagation();
            closeToast();
        });

        toast.addEventListener('click', closeToast);

        if (duration > 0) {
            var remaining = duration;
            var startedAt = 0;
            var timer = null;
            var hovered = false;
            var focused = false;
            var run = function () {
                if (timer || remaining <= 0) return;
                startedAt = Date.now();
                timer = setTimeout(closeToast, remaining);
                progressDiv.style.animationPlayState = 'running';
            };
            var hold = function () {
                if (!timer) return;
                clearTimeout(timer);
                timer = null;
                remaining -= Date.now() - startedAt;
                progressDiv.style.animationPlayState = 'paused';
            };
            var sync = function () { if (hovered || focused) hold(); else run(); };
            toast.addEventListener('mouseenter', function () { hovered = true; sync(); });
            toast.addEventListener('mouseleave', function () { hovered = false; sync(); });
            toast.addEventListener('focusin', function () { focused = true; sync(); });
            toast.addEventListener('focusout', function (e) {
                focused = toast.contains(e.relatedTarget);
                sync();
            });
            run();
        }

        return toast;
    }

    function getCookie(name) {
        var value = '; ' + document.cookie;
        var parts = value.split('; ' + name + '=');
        if (parts.length === 2) return parts.pop().split(';').shift();
        return null;
    }

    // The request as the storefront sends it: the httpOnly cookies, `X-Platform: web`, and for a write the
    // readable `csrf_access_token` in X-CSRF-TOKEN, read when the request is built (a renewal sets a new one).
    function apiFetchOptions(options) {
        var headers = Object.assign(
            { 'Content-Type': 'application/json' },
            options.headers || {}
        );

        headers['X-Platform'] = 'web';

        var method = (options.method || 'GET').toUpperCase();
        if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) {
            var csrfToken = getCookie('csrf_access_token');
            if (csrfToken) headers['X-CSRF-TOKEN'] = csrfToken;
        }

        return Object.assign({}, options, {
            headers: headers,
            credentials: 'include'
        });
    }

    // The backend marks the 401s that mean "this login lapsed, sign in again" with `reauth: true` (the JWT
    // layer's answers, business_app/__init__.py setup_jwt_handlers). Any other 401, such as a wrong password, is
    // the caller's answer. The body is read from a clone, so the caller can still read it.
    async function asksToSignInAgain(response) {
        try {
            var body = await response.clone().json();
            return Boolean(body) && body.reauth === true;
        } catch (error) {
            return false;
        }
    }

    async function apiRequest(url, options) {
        options = options || {};
        var response = await fetch(window.API_BASE_URL + url, apiFetchOptions(options));

        if (response.status === 401 && !url.includes('/auth/refresh-cookie') && await asksToSignInAgain(response)) {
            try {
                // The httpOnly refresh cookie renews the access cookie; its CSRF value is the readable one.
                var refreshHeaders = {};
                var refreshCsrf = getCookie('csrf_refresh_token');
                if (refreshCsrf) refreshHeaders['X-CSRF-TOKEN'] = refreshCsrf;
                var refreshResponse = await fetch(window.API_BASE_URL + '/auth/refresh-cookie', {
                    method: 'POST',
                    headers: refreshHeaders,
                    credentials: 'include'
                });

                if (refreshResponse.ok) {
                    return fetch(window.API_BASE_URL + url, apiFetchOptions(options));
                }
            } catch (error) {
                console.error('Token refresh failed:', error);
            }

            var currentPath = window.location.pathname + window.location.search;
            window.location.href = PAGE_DATA.login_url + '?next=' + encodeURIComponent(currentPath);

            throw new Error('Session expired. Redirecting to login...');
        }

        return response;
    }

    async function validateSession() {
        var token = getAuthToken();
        if (!token) return false;

        try {
            var response = await apiRequest('/auth/validate-token', { method: 'POST' });
            var result = await response.json();
            return response.ok && result.success;
        } catch (error) {
            console.error('Session validation failed:', error);
            return false;
        }
    }

    function startSessionMonitoring() {
        setInterval(async function () {
            var isValid = await validateSession();
            if (!isValid) {
                showNotification(PAGE_DATA.i18n.session_expired, 'warning');
                setTimeout(logout, 3000);
            }
        }, 15 * 60 * 1000);
    }

    async function syncCartFromDatabase() {
        try {
            var response = await apiRequest('/cart/');

            if (response.ok) {
                var data = await response.json();

                if (data.success) {
                    var dbCart = [];

                    if (data.data.cart && data.data.cart.cart_items) {
                        dbCart = data.data.cart.cart_items.map(function (item) {
                            return { product_id: item.product_id, quantity: item.quantity };
                        });
                    }

                    localStorage.setItem('cart', JSON.stringify(dbCart));
                    window.cart = dbCart;
                } else {
                    console.error('SYNC: API returned success=false', data);
                }
            } else {
                console.error('SYNC: API request failed');
            }
        } catch (error) {
            console.error('Error syncing cart from database:', error);
        }
    }

    function optimizeNonCriticalImages() {
        var criticalSelectors = [
            '.logo img',
            '.footer-logo img',
            '.nav-logo img',
            '.banner-style-two img',
            '.page-title img',
            '.main-product-img'
        ];
        var criticalImages = new Set();
        criticalSelectors.forEach(function (selector) {
            document.querySelectorAll(selector).forEach(function (img) { criticalImages.add(img); });
        });

        document.querySelectorAll('img').forEach(function (img) {
            if (criticalImages.has(img)) return;
            if (!img.hasAttribute('loading')) img.setAttribute('loading', 'lazy');
            if (!img.hasAttribute('decoding')) img.setAttribute('decoding', 'async');
        });
    }

    // ---------------------------------------------------------------- window.AE: shared page helpers
    // One copy for every page script (no per-page escapeHtml / money / date code).
    var NBSP = '\u00a0';
    var DATE_LOCALES = { ru: 'ru-RU', uz: 'uz-Latn-UZ', en: 'en-GB' };
    var DISPLAY_TIMEZONE = 'Asia/Tashkent';
    var STATUS_TONES = {
        ok: 'ok', success: 'ok', warn: 'warn', warning: 'warn', danger: 'danger',
        info: 'info', progress: 'progress', muted: 'muted', neutral: 'muted'
    };

    function escapeHtml(value) {
        return String(value === null || value === undefined ? '' : value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    // An amount as the server prints it (business_app/utils/display_labels.py money): whole so'm, half away from
    // zero, thousands grouped by a no-break space, in the language's template `web.display_labels.money`
    // ("{amount} сум"), published in the page data. Without the template: "36 000 UZS", as the server falls back.
    function formatMoney(amount) {
        var number = Number(amount) || 0;
        var whole = (number < 0 ? -1 : 1) * Math.round(Math.abs(number));
        var grouped = String(whole).replace(/\B(?=(\d{3})+(?!\d))/g, NBSP);
        var template = PAGE_DATA.money_template;
        if (typeof template === 'string' && template.indexOf('{amount}') !== -1) {
            return template.replace('{amount}', grouped);
        }
        return grouped + NBSP + 'UZS';
    }

    // A date in the server's own words (page data `date_words`: web.display_labels.* and web.integration.date_year),
    // so it reads exactly as the dates the server prints: "30 сентября 2026", "30 September 2026",
    // "2026-yil 30-sentabr"; the weekday and the time follow when asked for.
    // `{name}` placeholders filled from `values`; a name without a value stays as written. The one fill every page
    // script uses (AE.fill).
    function fill(template, values) {
        return String(template === null || template === undefined ? '' : template).replace(/\{(\w+)\}/g, function (match, name) {
            return Object.prototype.hasOwnProperty.call(values || {}, name) ? values[name] : match;
        });
    }

    function formatWithWords(date, settings) {
        var words = PAGE_DATA.date_words;
        var parts = {};
        new Intl.DateTimeFormat('en-GB', {
            timeZone: settings.timeZone, year: 'numeric', month: 'numeric', day: 'numeric',
            hour: '2-digit', minute: '2-digit', hourCycle: 'h23', weekday: 'short'
        }).formatToParts(date).forEach(function (part) { parts[part.type] = part.value; });
        var weekdayIndex = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].indexOf(parts.weekday);
        var values = { day: String(+parts.day), month: words.months[+parts.month - 1], year: parts.year };
        var withYear = typeof words.date_year === 'string' && words.date_year.indexOf('{day}') !== -1;
        var text = settings.year && withYear
            ? fill(words.date_year, values)
            : fill(words.day_month, values) + (settings.year ? ', ' + parts.year : '');
        if (settings.weekday && weekdayIndex >= 0) text += ', ' + words.weekdays[weekdayIndex];
        if (settings.hour || settings.minute) text += ' ' + parts.hour + ':' + parts.minute;
        return text;
    }

    // A date in the page's language and Tashkent time. `iso` is an ISO string ("2026-11-13" or a full timestamp) or
    // a Date; `options` are Intl.DateTimeFormat options (default: day, long month, year). A date-only string is read
    // as that calendar day, never shifted by a time zone. Prefer a label the backend publishes where one exists.
    function formatDate(iso, options) {
        if (iso === null || iso === undefined || iso === '') return '';
        var date;
        var dateOnly = typeof iso === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(iso);
        if (dateOnly) {
            var parts = iso.split('-');
            date = new Date(Date.UTC(+parts[0], +parts[1] - 1, +parts[2], 12));
        } else {
            date = iso instanceof Date ? iso : new Date(iso);
        }
        if (isNaN(date.getTime())) return String(iso);
        var lang = (document.documentElement.lang || PAGE_DATA.language || 'uz').slice(0, 2);
        var settings = Object.assign(
            { day: 'numeric', month: 'long', year: 'numeric' },
            options || {},
            { timeZone: dateOnly ? 'UTC' : DISPLAY_TIMEZONE }
        );
        try {
            // Uzbek always (no browser has its month names); Russian and English for a plain day-month(-year) date,
            // the one the server prints. A weekday or a time in ru/en stays with Intl.
            var plain = settings.month === 'long' && !settings.weekday && !settings.hour && !settings.minute;
            var words = PAGE_DATA.date_words;
            var withYear = words && typeof words.date_year === 'string' && words.date_year.indexOf('{day}') !== -1;
            if (words && words.months && (lang === 'uz' || (plain && (withYear || !settings.year)))) {
                return formatWithWords(date, settings);
            }
            return new Intl.DateTimeFormat(DATE_LOCALES[lang] || lang, settings).format(date);
        } catch (error) {
            return date.toISOString().slice(0, 10);
        }
    }

    // A line's units and unit money as the server priced it (a cart or quote line): "4 × 20 000 сум". A courtesy line
    // names its kept units first, in `words.courtesy_part` ("{qty} × {price} (your price, kept for you)"), then the
    // units at today's price. Server fields only (`regular_quantity` is published): nothing is subtracted here.
    function linePriceText(line, words) {
        if (!line.courtesy_quantity) return line.quantity + ' × ' + formatMoney(line.unit_price);
        var kept = fill(words.courtesy_part, { qty: line.courtesy_quantity, price: formatMoney(line.courtesy_unit_price) });
        return kept + (line.regular_quantity > 0 ? ' + ' + line.regular_quantity + ' × ' + formatMoney(line.unit_price) : '');
    }

    // The dated coming price ("From 1 February: 20 000 so'm.") from a price block the API served
    // ({new_price, effective_date}), in `template` (landing.offer.upcoming_price, the server's own sentence); '' when
    // no change is coming.
    function upcomingPriceText(upcoming, template) {
        if (!upcoming) return '';
        return fill(template, {
            date: formatDate(upcoming.effective_date, { year: undefined }),
            price: formatMoney(upcoming.new_price)
        });
    }

    // A status pill's HTML (.ae-status). The label is the backend's published wording; the tone is its colour.
    function statusPill(label, tone) {
        var cssTone = STATUS_TONES[tone] || 'muted';
        return '<span class="ae-status ae-status--' + cssTone + '">' + escapeHtml(label) + '</span>';
    }

    // A product image that failed to load shows the placeholder instead (once, so a missing placeholder never loops).
    function imgFallback(img) {
        if (!img || img.dataset.aeFallback === 'done') return;
        img.dataset.aeFallback = 'done';
        img.src = img.getAttribute('data-fallback') || PAGE_DATA.image_placeholder || '/static/images/product-placeholder.svg';
    }

    window.AE = Object.assign(window.AE || {}, {
        escapeHtml: escapeHtml,
        fill: fill,
        formatMoney: formatMoney,
        formatDate: formatDate,
        linePriceText: linePriceText,
        upcomingPriceText: upcomingPriceText,
        statusPill: statusPill,
        imgFallback: imgFallback
    });

    // Server-rendered and script-rendered images that opt in with data-fallback fall back on error.
    document.addEventListener('error', function (event) {
        var target = event.target;
        if (target && target.tagName === 'IMG' && target.hasAttribute('data-fallback')) imgFallback(target);
    }, true);
    // base.js runs after the page's images: one that already failed fired its error before the listener existed.
    // A broken image is complete with no width; one still loading (or lazy and not started) is not complete.
    function fallbackBrokenImages() {
        document.querySelectorAll('img[data-fallback]').forEach(function (img) {
            if (img.complete && img.naturalWidth === 0 && img.getAttribute('src')) imgFallback(img);
        });
    }
    fallbackBrokenImages();

    // ---------------------------------------------------------------- Shell: menus, drawer, footer
    // Header menus: a [data-ae-menu-toggle] button opens its .ae-menu (nav dropdowns, language, account, search).
    // One is open at a time; Escape or a click elsewhere closes it and Escape returns focus to the button.
    function setMenu(menu, open) {
        if (!menu) return;
        menu.classList.toggle('is-open', open);
        var toggle = menu.querySelector('[data-ae-menu-toggle]');
        if (toggle) toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
        if (open) {
            var field = menu.querySelector('.ae-search__panel input[type="search"]');
            if (field) setTimeout(function () { field.focus({ preventScroll: true }); }, 30);
        }
    }

    function closeMenus(except) {
        document.querySelectorAll('.ae-menu.is-open').forEach(function (menu) {
            if (menu !== except) setMenu(menu, false);
        });
    }

    function initMenus() {
        document.addEventListener('click', function (event) {
            var toggle = event.target.closest('[data-ae-menu-toggle]');
            if (toggle) {
                var menu = toggle.closest('.ae-menu');
                var open = !menu.classList.contains('is-open');
                closeMenus(menu);
                setMenu(menu, open);
                return;
            }
            if (!event.target.closest('.ae-menu')) closeMenus();
        });
        document.addEventListener('keydown', function (event) {
            if (event.key !== 'Escape') return;
            var menu = document.querySelector('.ae-menu.is-open');
            if (!menu) return;
            setMenu(menu, false);
            var toggle = menu.querySelector('[data-ae-menu-toggle]');
            // preventScroll: the header is sticky, and a focus that "scrolls it into view" moves the page.
            if (toggle) toggle.focus({ preventScroll: true });
        });
        // Tabbing out of an open menu closes it.
        document.addEventListener('focusin', function (event) {
            document.querySelectorAll('.ae-menu.is-open').forEach(function (menu) {
                if (!menu.contains(event.target)) setMenu(menu, false);
            });
        });
    }

    // Mobile drawer: a real dialog. Focus moves in and is kept there, Escape and the backdrop close it, focus goes
    // back to the menu button.
    function initDrawer() {
        var drawer = document.querySelector('[data-ae-drawer]');
        var opener = document.querySelector('[data-ae-drawer-open]');
        if (!drawer || !opener) return;
        var panel = drawer.querySelector('.ae-drawer__panel');
        var lastFocus = null;
        var FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), summary, [tabindex]:not([tabindex="-1"])';

        function focusables() {
            return Array.prototype.filter.call(panel.querySelectorAll(FOCUSABLE), function (el) {
                return el.offsetWidth > 0 || el.offsetHeight > 0 || el === document.activeElement;
            });
        }

        function open() {
            lastFocus = document.activeElement;
            closeMenus();
            drawer.classList.add('is-open');
            document.body.classList.add('ae-drawer-open');
            opener.setAttribute('aria-expanded', 'true');
            var close = drawer.querySelector('.ae-drawer__close');
            setTimeout(function () { (close || panel).focus({ preventScroll: true }); }, 50);
        }

        function close() {
            if (!drawer.classList.contains('is-open')) return;
            drawer.classList.remove('is-open');
            document.body.classList.remove('ae-drawer-open');
            opener.setAttribute('aria-expanded', 'false');
            // preventScroll: focus going back into the sticky header must not move the page.
            if (lastFocus && typeof lastFocus.focus === 'function') lastFocus.focus({ preventScroll: true });
        }

        opener.addEventListener('click', open);
        drawer.querySelectorAll('[data-ae-drawer-close]').forEach(function (el) { el.addEventListener('click', close); });
        drawer.addEventListener('keydown', function (event) {
            if (event.key === 'Escape') { close(); return; }
            if (event.key !== 'Tab') return;
            var items = focusables();
            if (!items.length) return;
            var first = items[0];
            var last = items[items.length - 1];
            if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus({ preventScroll: true }); }
            else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus({ preventScroll: true }); }
        });
        // The drawer is the compact bar's menu: leaving that width closes it.
        if (window.matchMedia) {
            var wide = window.matchMedia('(min-width: 1280px)');
            var onWide = function (event) { if (event.matches) close(); };
            if (wide.addEventListener) wide.addEventListener('change', onWide);
            else if (wide.addListener) wide.addListener(onWide);
        }
    }

    // Footer link columns: collapsed accordions on phones; from 768px always open, and the summary is only the
    // column's heading: out of the tab order, and neither a click nor a screen reader's activation closes it.
    function initFooterColumns() {
        var columns = document.querySelectorAll('[data-ae-footer-col]');
        if (!columns.length || !window.matchMedia) return;
        var phone = window.matchMedia('(max-width: 767px)');
        var apply = function () {
            columns.forEach(function (column) {
                column.open = !phone.matches;
                var summary = column.querySelector('summary');
                if (!summary) return;
                if (phone.matches) summary.removeAttribute('tabindex');
                else summary.tabIndex = -1;
            });
        };
        columns.forEach(function (column) {
            var summary = column.querySelector('summary');
            if (summary) summary.addEventListener('click', function (event) { if (!phone.matches) event.preventDefault(); });
            column.addEventListener('toggle', function () { if (!phone.matches && !column.open) column.open = true; });
        });
        apply();
        if (phone.addEventListener) phone.addEventListener('change', apply);
        else if (phone.addListener) phone.addListener(apply);
    }

    // Expose globals for page scripts and inline handlers already converted via data-action
    window.getAuthToken = getAuthToken;
    window.setAuthToken = setAuthToken;
    window.removeAuthTokens = removeAuthTokens;
    window.logout = logout;
    window.updateCartCount = updateCartCount;
    window.addToCart = addToCart;
    window.removeFromCart = removeFromCart;
    window.showNotification = showNotification;
    window.getCookie = getCookie;
    window.apiRequest = apiRequest;
    window.validateSession = validateSession;
    window.startSessionMonitoring = startSessionMonitoring;
    window.syncCartFromDatabase = syncCartFromDatabase;

    document.addEventListener('DOMContentLoaded', async function () {
        optimizeNonCriticalImages();
        initMenus();
        initDrawer();
        initFooterColumns();

        if (PAGE_DATA.is_authenticated) {
            window.cartSyncPromise = syncCartFromDatabase();
            await window.cartSyncPromise;
        } else {
            window.cartSyncPromise = Promise.resolve();
        }

        updateCartCount();

        if (getAuthToken()) {
            startSessionMonitoring();
        }

        document.querySelectorAll('[data-action="logout"]').forEach(function (el) {
            el.addEventListener('click', function (e) {
                e.preventDefault();
                logout();
            });
        });

        // The product card's default button (partials/product_card.html): it adds the smallest order the backend
        // publishes on the card (data-qty = offer facts smallest_order_quantity), never one bottle below it.
        document.body.addEventListener('click', function (e) {
            var btn = e.target.closest('[data-action="add-to-cart-shop"]');
            if (btn) {
                addToCart(parseInt(btn.dataset.productId, 10), parseInt(btn.dataset.qty, 10) || 1);
            }
        });
    });
})();
