// The cabinet frame (cabinet_base.html). Below 992px the account links are a horizontally scrollable tab strip:
// the current page's tab is brought into view, so on /my-subscriptions "Subscriptions" is not off-screen. Only the
// strip scrolls (scrollLeft), never the page.
(function () {
    function centreCurrentTab() {
        var list = document.querySelector('.ae-cabinet-nav__list');
        var current = list && list.querySelector('[aria-current="page"]');
        if (!list || !current || list.scrollWidth <= list.clientWidth) return;
        var item = current.parentElement || current;
        list.scrollLeft = Math.max(0, item.offsetLeft - (list.clientWidth - item.offsetWidth) / 2);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', centreCurrentTab);
    } else {
        centreCurrentTab();
    }
})();
