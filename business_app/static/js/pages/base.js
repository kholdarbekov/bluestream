(function () {
    var PAGE_DATA = getPageData('base-page-data');

    // Global variables (attached to window for legacy compatibility with page scripts)
    window.API_BASE_URL = PAGE_DATA.api_base_url;
    window.CURRENT_USER = PAGE_DATA.current_user;

    function getAuthToken() { return null; }

    function setAuthToken(token, refreshToken) {
        console.warn('setAuthToken called but tokens are now handled via httpOnly cookies');
    }

    function getRefreshToken() { return null; }

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

    async function logout() {
        try {
            await apiRequest('/auth/logout', {
                method: 'POST',
                credentials: 'include'
            });
        } catch (error) {
            console.error('Logout API call failed:', error);
        }

        removeAuthTokens();
        window.cart = [];
        localStorage.removeItem('cart');

        deleteCookie('csrf_access_token', '/', '.aqua-element.uz');
        deleteCookie('csrf_refresh_token', '/', '.aqua-element.uz');
        deleteCookie('csrf_access_token', '/');
        deleteCookie('csrf_refresh_token', '/');

        window.location.replace(PAGE_DATA.login_url);
    }

    async function logoutAll() {
        var token = getAuthToken();

        try {
            if (token) {
                await apiRequest('/auth/logout-all', { method: 'POST' });
            }
        } catch (error) {
            console.error('Logout all API call failed:', error);
        }

        removeAuthTokens();
        window.cart = [];
        localStorage.removeItem('cart');

        showNotification(PAGE_DATA.i18n.logged_out, 'success');

        setTimeout(function () {
            window.location.href = PAGE_DATA.login_url;
        }, 1000);
    }

    async function refreshToken() {
        var refreshTokenValue = getRefreshToken();

        if (!refreshTokenValue) {
            logout();
            return null;
        }

        try {
            var response = await fetch(window.API_BASE_URL + '/auth/refresh-token', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ refresh_token: refreshTokenValue })
            });

            var result = await response.json();

            if (response.ok && result.success) {
                setAuthToken(result.data.access_token);
                return result.data.access_token;
            }
            logout();
            return null;
        } catch (error) {
            console.error('Token refresh failed:', error);
            logout();
            return null;
        }
    }

    // Cart management
    window.cart = JSON.parse(localStorage.getItem('cart') || '[]');

    function updateCartCount() {
        var count = window.cart.reduce(function (sum, item) { return sum + item.quantity; }, 0);
        var cartCounters = document.querySelectorAll('#cart-count, #cart-count-sticky, .cart-box span');

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

    // Toast styles — injected once
    (function () {
        var toastContainer = document.getElementById('toast-container');
        if (!toastContainer) {
            toastContainer = document.createElement('div');
            toastContainer.id = 'toast-container';
            toastContainer.style.cssText =
                'position: fixed; top: 20px; right: 20px; z-index: 99999; ' +
                'display: flex; flex-direction: column; gap: 10px; max-width: 400px; pointer-events: none;';
            document.body.appendChild(toastContainer);
        }

        var styleId = 'toast-notification-styles';
        if (!document.getElementById(styleId)) {
            var style = document.createElement('style');
            style.id = styleId;
            style.textContent =
                '.toast-notification { display: flex; align-items: flex-start; padding: 16px 20px; border-radius: 12px; background: #ffffff; box-shadow: 0 10px 40px rgba(0, 0, 0, 0.15), 0 4px 12px rgba(0, 0, 0, 0.1); pointer-events: auto; transform: translateX(120%); opacity: 0; transition: all 0.4s cubic-bezier(0.68, -0.55, 0.265, 1.55); cursor: pointer; min-width: 300px; max-width: 400px; backdrop-filter: blur(10px); border: 1px solid rgba(255, 255, 255, 0.2); }' +
                '.toast-notification.show { transform: translateX(0); opacity: 1; }' +
                '.toast-notification.hide { transform: translateX(120%); opacity: 0; }' +
                '.toast-notification .toast-icon { flex-shrink: 0; width: 24px; height: 24px; margin-right: 14px; display: flex; align-items: center; justify-content: center; border-radius: 50%; font-size: 14px; }' +
                '.toast-notification .toast-content { flex: 1; margin-right: 10px; }' +
                '.toast-notification .toast-title { font-weight: 600; font-size: 14px; margin-bottom: 4px; color: #1a1a2e; }' +
                '.toast-notification .toast-message { font-size: 13px; color: #666; line-height: 1.4; }' +
                '.toast-notification .toast-close { flex-shrink: 0; width: 20px; height: 20px; display: flex; align-items: center; justify-content: center; border-radius: 50%; background: rgba(0, 0, 0, 0.05); color: #999; font-size: 12px; transition: all 0.2s ease; cursor: pointer; }' +
                '.toast-notification .toast-close:hover { background: rgba(0, 0, 0, 0.1); color: #333; }' +
                '.toast-notification .toast-progress { position: absolute; bottom: 0; left: 0; height: 3px; border-radius: 0 0 12px 12px; width: 100%; transform-origin: left; }' +
                '.toast-notification.toast-success { border-left: 4px solid #10b981; background: linear-gradient(135deg, #ffffff 0%, #ecfdf5 100%); }' +
                '.toast-notification.toast-success .toast-icon { background: #10b981; color: white; }' +
                '.toast-notification.toast-success .toast-progress { background: #10b981; }' +
                '.toast-notification.toast-error { border-left: 4px solid #ef4444; background: linear-gradient(135deg, #ffffff 0%, #fef2f2 100%); }' +
                '.toast-notification.toast-error .toast-icon { background: #ef4444; color: white; }' +
                '.toast-notification.toast-error .toast-progress { background: #ef4444; }' +
                '.toast-notification.toast-warning { border-left: 4px solid #f59e0b; background: linear-gradient(135deg, #ffffff 0%, #fffbeb 100%); }' +
                '.toast-notification.toast-warning .toast-icon { background: #f59e0b; color: white; }' +
                '.toast-notification.toast-warning .toast-progress { background: #f59e0b; }' +
                '.toast-notification.toast-info { border-left: 4px solid #3b82f6; background: linear-gradient(135deg, #ffffff 0%, #eff6ff 100%); }' +
                '.toast-notification.toast-info .toast-icon { background: #3b82f6; color: white; }' +
                '.toast-notification.toast-info .toast-progress { background: #3b82f6; }' +
                '@media (max-width: 480px) { #toast-container { left: 10px; right: 10px; max-width: calc(100% - 20px); } .toast-notification { min-width: auto; max-width: 100%; } }' +
                '@keyframes shrinkProgress { from { transform: scaleX(1); } to { transform: scaleX(0); } }';
            document.head.appendChild(style);
        }
    })();

    function showNotification(message, type, duration) {
        type = type || 'info';
        duration = duration === undefined ? 5000 : duration;

        var container = document.getElementById('toast-container');
        if (!container) return;

        var icons = { success: '✓', error: '✕', warning: '⚠', info: 'ℹ' };
        var titles = {
            success: PAGE_DATA.i18n.success,
            error: PAGE_DATA.i18n.error,
            warning: PAGE_DATA.i18n.warning,
            info: PAGE_DATA.i18n.info
        };

        var toast = document.createElement('div');
        toast.className = 'toast-notification toast-' + type;

        var iconDiv = document.createElement('div');
        iconDiv.className = 'toast-icon';
        iconDiv.textContent = icons[type] || icons.info;

        var contentDiv = document.createElement('div');
        contentDiv.className = 'toast-content';
        var titleDiv = document.createElement('div');
        titleDiv.className = 'toast-title';
        titleDiv.textContent = titles[type] || titles.info;
        var messageDiv = document.createElement('div');
        messageDiv.className = 'toast-message';
        messageDiv.textContent = message;
        contentDiv.appendChild(titleDiv);
        contentDiv.appendChild(messageDiv);

        var closeDiv = document.createElement('div');
        closeDiv.className = 'toast-close';
        closeDiv.textContent = '✕';

        var progressDiv = document.createElement('div');
        progressDiv.className = 'toast-progress';
        progressDiv.style.animation = 'shrinkProgress ' + duration + 'ms linear forwards';

        toast.appendChild(iconDiv);
        toast.appendChild(contentDiv);
        toast.appendChild(closeDiv);
        toast.appendChild(progressDiv);

        container.appendChild(toast);

        requestAnimationFrame(function () { toast.classList.add('show'); });

        var closeToast = function () {
            toast.classList.remove('show');
            toast.classList.add('hide');
            setTimeout(function () {
                if (toast.parentNode) toast.parentNode.removeChild(toast);
            }, 400);
        };

        closeDiv.addEventListener('click', function (e) {
            e.stopPropagation();
            closeToast();
        });

        toast.addEventListener('click', closeToast);

        if (duration > 0) setTimeout(closeToast, duration);

        return toast;
    }

    function getCookie(name) {
        var value = '; ' + document.cookie;
        var parts = value.split('; ' + name + '=');
        if (parts.length === 2) return parts.pop().split(';').shift();
        return null;
    }

    async function apiRequest(url, options) {
        options = options || {};
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

        var fetchOptions = Object.assign({}, options, {
            headers: headers,
            credentials: 'include'
        });

        var response = await fetch(window.API_BASE_URL + url, fetchOptions);

        if (response.status === 401 && !url.includes('/auth/refresh-token')) {
            try {
                var refreshResponse = await fetch(window.API_BASE_URL + '/auth/refresh-token', {
                    method: 'POST',
                    credentials: 'include'
                });

                if (refreshResponse.ok) {
                    return fetch(window.API_BASE_URL + url, fetchOptions);
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

    // Expose globals for page scripts and inline handlers already converted via data-action
    window.getAuthToken = getAuthToken;
    window.setAuthToken = setAuthToken;
    window.getRefreshToken = getRefreshToken;
    window.removeAuthTokens = removeAuthTokens;
    window.logout = logout;
    window.logoutAll = logoutAll;
    window.refreshToken = refreshToken;
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

        document.body.addEventListener('click', function (e) {
            var btn = e.target.closest('[data-action="add-to-cart-shop"]');
            if (btn) {
                addToCart(parseInt(btn.dataset.productId, 10));
            }
        });
    });
})();
