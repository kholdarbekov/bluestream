/* Public coverage map + address checker. Reads #coverage-data (polygon,
 * districts, center, checkUrl). Pin-drop and "use my location" run a local
 * point-in-polygon; the address box calls the public check-delivery endpoint. */
(function () {
  var dataEl = document.getElementById('coverage-data');
  if (!dataEl || typeof L === 'undefined') return;
  var DATA = JSON.parse(dataEl.textContent);

  // Leaflet's default marker images live in the vendored images/ dir.
  L.Icon.Default.mergeOptions({
    iconUrl: '/static/vendor/leaflet/images/marker-icon.png',
    iconRetinaUrl: '/static/vendor/leaflet/images/marker-icon-2x.png',
    shadowUrl: '/static/vendor/leaflet/images/marker-shadow.png'
  });

  function pointInPolygon(lat, lng, polygon) {
    var inside = false;
    for (var i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
      var xi = polygon[i][0], yi = polygon[i][1];
      var xj = polygon[j][0], yj = polygon[j][1];
      var intersect = ((yi > lng) !== (yj > lng)) &&
        (lat < (xj - xi) * (lng - yi) / (yj - yi) + xi);
      if (intersect) inside = !inside;
    }
    return inside;
  }

  var map = L.map('coverage-map', { scrollWheelZoom: false });
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
    { attribution: '&copy; OpenStreetMap', maxZoom: 18 }).addTo(map);
  var poly = L.polygon(DATA.polygon,
    { color: '#1390d6', weight: 2.5, fillColor: '#1390d6', fillOpacity: 0.14 }).addTo(map);
  (DATA.districts || []).forEach(function (d) {
    if (d.center) {
      L.circleMarker(d.center, { radius: 4.5, color: '#fff', weight: 1.4, fillColor: '#0b6aa2', fillOpacity: 1 })
        .addTo(map).bindTooltip(d.name, { direction: 'top' });
    }
  });
  map.fitBounds(poly.getBounds(), { padding: [20, 20] });

  var marker = null;
  var okEl = document.getElementById('coverage-result-ok');
  var noEl = document.getElementById('coverage-result-no');

  function showResult(isDeliverable) {
    okEl.classList.toggle('ok', isDeliverable === true);
    noEl.classList.toggle('no', isDeliverable === false);
    okEl.style.display = isDeliverable === true ? 'block' : 'none';
    noEl.style.display = isDeliverable === false ? 'block' : 'none';
  }

  function place(lat, lng, isDeliverable) {
    if (marker) { marker.setLatLng([lat, lng]); } else { marker = L.marker([lat, lng]).addTo(map); }
    map.setView([lat, lng], 14);
    showResult(isDeliverable);
  }

  map.on('click', function (e) {
    place(e.latlng.lat, e.latlng.lng, pointInPolygon(e.latlng.lat, e.latlng.lng, DATA.polygon));
  });

  document.getElementById('coverage-locate-btn').addEventListener('click', function () {
    if (!navigator.geolocation) return;
    navigator.geolocation.getCurrentPosition(function (pos) {
      var la = pos.coords.latitude, ln = pos.coords.longitude;
      place(la, ln, pointInPolygon(la, ln, DATA.polygon));
    });
  });

  document.getElementById('coverage-check-btn').addEventListener('click', function () {
    var address = (document.getElementById('coverage-address').value || '').trim();
    if (!address) return;
    fetch(DATA.checkUrl + '?address=' + encodeURIComponent(address))
      .then(function (r) { return r.json(); })
      .then(function (b) {
        if (b.is_deliverable === null || b.latitude == null) {
          noEl.textContent = 'Could not locate that address — drop a pin on the map instead.';
          showResult(false);
          return;
        }
        place(b.latitude, b.longitude, b.is_deliverable);
      })
      .catch(function () { showResult(false); });
  });
})();
