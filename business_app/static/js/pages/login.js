(function () {
    var PAGE_DATA = getPageData();

    function togglePassword(inputId, button) {
        var input = document.getElementById(inputId);
        var icon = button.querySelector('i');

        if (input.type === 'password') {
            input.type = 'text';
            icon.classList.remove('fa-eye');
            icon.classList.add('fa-eye-slash');
        } else {
            input.type = 'password';
            icon.classList.remove('fa-eye-slash');
            icon.classList.add('fa-eye');
        }
    }

    document.querySelectorAll('.password-toggle').forEach(function (btn) {
        btn.addEventListener('click', function () {
            togglePassword(this.dataset.target, this);
        });
    });

    function isValidRedirectUrl(url) {
        if (!url || typeof url !== 'string') return false;
        if (!url.startsWith('/') || url.startsWith('//')) return false;
        if (url.includes('://')) return false;
        return true;
    }

    var form = document.getElementById('loginForm');
    if (!form) return;

    form.addEventListener('submit', async function (e) {
        e.preventDefault();

        var submitBtn = this.querySelector('.btn-auth');
        submitBtn.classList.add('loading');

        var formData = new FormData(this);
        var data = {
            identifier: formData.get('identifier'),
            password: formData.get('password'),
            remember_me: formData.get('remember_me') === 'on'
        };

        try {
            var response = await apiRequest('/auth/login', {
                method: 'POST',
                body: JSON.stringify(data)
            });

            var result = await response.json();

            if (response.ok) {
                setAuthToken(result.access_token);

                var localCart = JSON.parse(localStorage.getItem('cart') || '[]');
                if (localCart.length > 0) {
                    try {
                        var csrfToken = getCookie('csrf_access_token');
                        var syncHeaders = {
                            'Content-Type': 'application/json',
                            'X-Platform': 'web'
                        };
                        if (csrfToken) syncHeaders['X-CSRF-TOKEN'] = csrfToken;

                        var syncResponse = await fetch(API_BASE_URL + '/cart/sync', {
                            method: 'POST',
                            headers: syncHeaders,
                            credentials: 'include',
                            body: JSON.stringify({ cart_items: localCart })
                        });

                        if (syncResponse.ok) {
                            var syncData = await syncResponse.json();
                            if (syncData.success && syncData.data.cart) {
                                localStorage.setItem('cart', JSON.stringify(syncData.data.cart.cart_items || []));
                            }
                        }
                    } catch (error) {
                        console.error('Cart sync failed:', error);
                    }
                }

                showNotification(PAGE_DATA.i18n.login_success, 'success');

                var urlParams = new URLSearchParams(window.location.search);
                var nextUrl = urlParams.get('next');

                if (nextUrl && isValidRedirectUrl(nextUrl)) {
                    window.location.href = nextUrl;
                } else {
                    window.location.href = PAGE_DATA.my_account_url;
                }
            } else {
                submitBtn.classList.remove('loading');
                showNotification(result.message || PAGE_DATA.i18n.login_failed, 'error');
            }
        } catch (error) {
            submitBtn.classList.remove('loading');
            showNotification(PAGE_DATA.i18n.network_error, 'error');
        }
    });
})();
