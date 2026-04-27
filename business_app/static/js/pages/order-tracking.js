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

    if (PAGE_DATA.auto_refresh) {
        setInterval(function () { location.reload(); }, 30000);
    }

    function cancelOrder() {
        if (!confirm(PAGE_DATA.i18n.confirm_cancel)) return;

        var reason = prompt(PAGE_DATA.i18n.prompt_reason);

        fetch('/api/v1/orders/' + PAGE_DATA.order_id + '/cancel', {
            method: 'POST',
            headers: {
                'Authorization': 'Bearer ' + localStorage.getItem('access_token'),
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ reason: reason || 'Customer request' })
        })
        .then(function (response) { return response.json(); })
        .then(function (data) {
            if (data.success) {
                showNotification(PAGE_DATA.i18n.cancel_success, 'success');
                setTimeout(function () { location.reload(); }, 1500);
            } else {
                showNotification(data.message || PAGE_DATA.i18n.cancel_failed, 'error');
            }
        })
        .catch(function (err) {
            console.error('Error cancelling order:', err);
            showNotification(PAGE_DATA.i18n.error, 'error');
        });
    }

    document.addEventListener('DOMContentLoaded', function () {
        var btn = document.querySelector('[data-action="cancel-order"]');
        if (btn) btn.addEventListener('click', cancelOrder);
    });
})();
