(function () {
    var PAGE_DATA = getPageData();
    var map, deliveryMarker;

    function initMap() {
        if (!PAGE_DATA.map.enabled) return;
        var deliveryLocation = { lat: PAGE_DATA.map.lat, lng: PAGE_DATA.map.lng };

        map = new google.maps.Map(document.getElementById('tracking-map'), {
            zoom: 15,
            center: deliveryLocation,
            mapTypeControl: true,
            streetViewControl: false,
            fullscreenControl: true
        });

        deliveryMarker = new google.maps.Marker({
            position: deliveryLocation,
            map: map,
            title: PAGE_DATA.i18n.delivery_location,
            icon: { url: 'https://maps.google.com/mapfiles/ms/icons/red-dot.png' }
        });

        var infoWindow = new google.maps.InfoWindow({
            content: '<div style="padding: 10px;">' +
                     '<h4 style="margin: 0 0 5px 0;">' + PAGE_DATA.i18n.delivery_location + '</h4>' +
                     '<p style="margin: 0; color: #666;">' + PAGE_DATA.map.address + '</p>' +
                     '</div>'
        });

        deliveryMarker.addListener('click', function () {
            infoWindow.open(map, deliveryMarker);
        });
    }

    if (PAGE_DATA.map.enabled) {
        document.addEventListener('DOMContentLoaded', initMap);
    }

    // The page refreshes itself every 30 seconds while the order is live, but never under a customer who has
    // an inline confirm open or a write in flight.
    if (PAGE_DATA.auto_refresh) {
        setInterval(function () {
            if (window.OrderActions && window.OrderActions.isBusy()) return;
            window.location.reload();
        }, 30000);
    }

    // Pay, Skip/Cancel/Decline and Confirm: the backend's `customer_actions`, the same answer GET /orders/<id>
    // publishes, drawn by the one script every order surface shares (order-actions.js). After a write the page
    // reloads from the server, once the customer has read the toast.
    document.addEventListener('DOMContentLoaded', function () {
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
