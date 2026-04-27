document.addEventListener('DOMContentLoaded', function () {
    var PAGE_DATA = getPageData();
    updateCartCount();
    if (PAGE_DATA.order_success) {
        showNotification(PAGE_DATA.i18n.order_success, 'success');
    }
});
