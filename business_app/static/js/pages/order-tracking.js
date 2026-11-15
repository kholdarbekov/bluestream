// An order's own page (/order-tracking). Everything it prints is the server's; this script only:
// - hands the backend's `customer_actions` to order-actions.js, the one renderer every order surface shares (Pay,
//   Skip/Cancel/Decline and Confirm), and reloads the page from the server once the customer has read the toast;
// - reloads the page every 30 seconds while the order is live, but never under a customer who has an inline confirm
//   open or a write in flight;
// - draws the delivery address's pin on a small map when the address is pinned (the vendored Leaflet, loaded on
//   demand through map-helper.js, once the map box comes into view).
(function () {
    var PAGE_DATA = getPageData();

    function drawMap(box) {
        var pin = PAGE_DATA.map;
        if (!window.AEMap || !PAGE_DATA.leaflet) return;
        window.AEMap.load(PAGE_DATA.leaflet).then(function (L) {
            var map = L.map(box, { zoomControl: true, scrollWheelZoom: false, dragging: !L.Browser.mobile });
            map.setView([pin.lat, pin.lng], 16);
            L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
                attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
                maxZoom: 19
            }).addTo(map);
            L.marker([pin.lat, pin.lng], { title: pin.label || '', interactive: false, keyboard: false }).addTo(map);
        }).catch(function () {
            // No map without Leaflet; the address above the box still says where the order goes.
            box.hidden = true;
        });
    }

    function initMap() {
        var box = document.getElementById('orderMap');
        if (!box || !PAGE_DATA.map) return;
        if (!('IntersectionObserver' in window)) { drawMap(box); return; }
        var observer = new IntersectionObserver(function (entries) {
            if (!entries.some(function (entry) { return entry.isIntersecting; })) return;
            observer.disconnect();
            drawMap(box);
        }, { rootMargin: '200px' });
        observer.observe(box);
    }

    if (PAGE_DATA.auto_refresh) {
        setInterval(function () {
            if (window.OrderActions && window.OrderActions.isBusy()) return;
            window.location.reload();
        }, 30000);
    }

    document.addEventListener('DOMContentLoaded', function () {
        initMap();
        var container = document.getElementById('orderActions');
        if (!container || !PAGE_DATA.order) return;
        window.OrderActions.render(container, PAGE_DATA.order, {
            words: PAGE_DATA.order_actions,
            subscriptionsUrl: PAGE_DATA.subscriptions_url,
            onChanged: function (result) {
                setTimeout(function () { window.location.reload(); }, result.ok ? 1500 : 2500);
            }
        });
    });
})();
