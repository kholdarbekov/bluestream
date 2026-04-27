/**
 * Cabinet Token Refresh Handler
 *
 * Official Flask-JWT-Extended approach for handling expired tokens:
 * https://flask-jwt-extended.readthedocs.io/en/stable/refreshing_tokens.html
 *
 * For page loads (not AJAX), we detect if the page rendered an error JSON and
 * attempt to refresh the token and reload.
 */
(function () {
    var PAGE_DATA = getPageData('cabinet-base-data');
    try {
        var body = document.body;
        var text = body ? body.textContent.trim() : '';

        if (text.startsWith('{') && text.includes('"error"') && text.includes('Token Expired')) {
            console.log('[JWT] Token expired on page load, attempting refresh...');

            fetch(PAGE_DATA.refresh_url, {
                method: 'POST',
                credentials: 'include',
                headers: {
                    'Content-Type': 'application/json'
                }
            })
            .then(function (response) {
                if (response.ok) {
                    console.log('[JWT] Token refreshed successfully, reloading page...');
                    window.location.reload();
                } else {
                    console.warn('[JWT] Token refresh failed, redirecting to login...');
                    window.location.href = PAGE_DATA.login_url + '?next=' + encodeURIComponent(window.location.pathname);
                }
            })
            .catch(function (err) {
                console.error('[JWT] Token refresh error:', err);
                window.location.href = PAGE_DATA.login_url + '?next=' + encodeURIComponent(window.location.pathname);
            });
            return;
        }
    } catch (err) {
        console.log('[JWT] Normal page load');
    }
})();
