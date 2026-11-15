/* Public coverage map + address checker (/coverage). Reads #coverage-data (polygon, districts, center, checkUrl).
 * Every check asks the public check-delivery endpoint, which decides the zone server-side (shared.constants
 * is_within_tashkent): the address form sends ?address= (geocoded there); a pin dropped on the map and "my location"
 * send ?lat=&lng=. The script draws the polygon the backend publishes but never decides the zone itself.
 * Every outcome shows one of the server-rendered #coverage-result-* panels (translated in the template); the
 * script never writes text of its own. The map is optional: without Leaflet the address check still works.
 * Phones: the answer sits a screen below the map, so an answer that is out of view is scrolled into view, and a
 * district chip brings the map into view before moving it; one finger scrolls the page, two fingers move the map. */
(function () {
  var dataEl = document.getElementById('coverage-data');
  var form = document.getElementById('coverage-form');
  if (!dataEl || !form) return;
  var DATA = JSON.parse(dataEl.textContent);
  var mapEl = document.getElementById('coverage-map');
  var hasMap = typeof L !== 'undefined' && mapEl;
  var media = function (query) { return !!(window.matchMedia && window.matchMedia(query).matches); };
  var reduceMotion = media('(prefers-reduced-motion: reduce)');

  var input = document.getElementById('coverage-address');
  var inputError = document.getElementById('coverage-address-error');
  var checkBtn = document.getElementById('coverage-check-btn');
  var locateBtn = document.getElementById('coverage-locate-btn');
  var STATES = ['checking', 'ok', 'no', 'notfound', 'error', 'geo'];
  var TAP_DELAY = 350; // ms: a burst of taps on the map sends one request, for the last pin
  var tapTimer = null;
  var latest = 0; // only the newest check may show its answer

  /* Show the one result panel for `state` (one of STATES), or none; returns that panel. */
  function show(state) {
    var shown = null;
    STATES.forEach(function (name) {
      var el = document.getElementById('coverage-result-' + name);
      if (!el) return;
      el.hidden = name !== state;
      if (name === state) shown = el;
    });
    return shown;
  }

  /* Scroll `el` into view when it is not already fully visible below the sticky header. */
  function reveal(el) {
    if (!el || typeof el.scrollIntoView !== 'function') return;
    var box = el.getBoundingClientRect();
    var top = parseFloat(window.getComputedStyle(el).scrollMarginTop) || 0;
    var viewport = window.innerHeight || document.documentElement.clientHeight;
    if (box.top >= top && box.bottom <= viewport) return;
    el.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'nearest' });
  }

  /* A final answer: its panel, scrolled into view on phones. */
  function answer(state) { reveal(show(state)); }

  function setBusy(button, busy) {
    if (!button) return;
    button.disabled = busy;
    if (busy) { button.setAttribute('aria-busy', 'true'); } else { button.removeAttribute('aria-busy'); }
  }

  function setAddressError(on) {
    if (!inputError) return;
    inputError.hidden = !on;
    if (on) {
      input.setAttribute('aria-invalid', 'true');
      input.setAttribute('aria-describedby', inputError.id);
    } else {
      input.removeAttribute('aria-invalid');
      input.removeAttribute('aria-describedby');
    }
  }

  var map = null;
  var marker = null;
  var districtMarkers = {};
  var chips = Array.prototype.slice.call(document.querySelectorAll('[data-district]'));

  function selectChip(key) {
    chips.forEach(function (chip) {
      chip.setAttribute('aria-pressed', chip.getAttribute('data-district') === key ? 'true' : 'false');
    });
  }

  function moveTo(latlng, zoom) {
    if (!map) return;
    if (reduceMotion) { map.setView(latlng, zoom, { animate: false }); } else { map.flyTo(latlng, zoom, { duration: 0.6 }); }
  }

  /* Put the pin at (lat, lng) and centre the map on it. */
  function pin(lat, lng) {
    selectChip(null);
    if (!map) return;
    if (marker) { marker.setLatLng([lat, lng]); } else { marker = L.marker([lat, lng]).addTo(map); }
    moveTo([lat, lng], 14);
  }

  /* Ask the endpoint (`query` without the "?") and show its answer. `pinAnswer`: put the pin where the endpoint
   * located an address (a dropped pin or a position is already on the map). */
  function ask(query, button, pinAnswer) {
    var mine = ++latest;
    clearTimeout(tapTimer);
    setBusy(button, true);
    show('checking');
    return fetch(DATA.checkUrl + '?' + query, { headers: { Accept: 'application/json' } })
      .then(function (r) {
        if (!r.ok) throw new Error('check-delivery answered ' + r.status);
        return r.json();
      })
      .then(function (b) {
        if (mine !== latest) return;
        if (typeof b.is_deliverable !== 'boolean' || b.latitude == null || b.longitude == null) {
          answer('notfound');
          return;
        }
        if (pinAnswer) pin(b.latitude, b.longitude);
        answer(b.is_deliverable ? 'ok' : 'no');
      })
      .catch(function () { if (mine === latest) answer('error'); })
      .then(function () { setBusy(button, false); });
  }

  function coords(lat, lng) {
    return 'lat=' + encodeURIComponent(lat) + '&lng=' + encodeURIComponent(lng);
  }

  if (hasMap) {
    // Phones: one finger scrolls the page (the map took every swipe over its 360px); two fingers pan and zoom.
    var touchOnly = !!(L.Browser && L.Browser.mobile) || media('(pointer: coarse)');
    // Leaflet's default marker images live in the vendored images/ dir. Leaflet 1.9 prefixes imagePath to the
    // icon file names itself (full URLs in iconUrl came out doubled: .../images//static/vendor/.../marker-icon.png).
    L.Icon.Default.imagePath = '/static/vendor/leaflet/images/';
    map = L.map('coverage-map', { scrollWheelZoom: false, dragging: !touchOnly });
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
      maxZoom: 18
    }).addTo(map);
    var poly = L.polygon(DATA.polygon, {
      color: '#0077B6', weight: 2.5, fillColor: '#00D1F9', fillOpacity: 0.16
    }).addTo(map);
    (DATA.districts || []).forEach(function (d) {
      if (!d.center) return;
      var dot = L.circleMarker(d.center, { radius: 5, color: '#fff', weight: 1.5, fillColor: '#002C8F', fillOpacity: 1 });
      // The tooltip's focus listeners made each dot a ringless Tab stop (12 before the address field); the chips
      // are the keyboard path to a district, so the dots leave the Tab order (hover still names them). The path
      // exists only once the layer is on the map, which waits for the map's first view.
      dot.on('add', function () {
        var el = dot.getElement();
        if (el) el.setAttribute('tabindex', '-1');
      });
      dot.addTo(map).bindTooltip(d.name, { direction: 'top' });
      districtMarkers[d.key] = dot;
    });
    map.fitBounds(poly.getBounds(), { padding: [16, 16] });

    map.on('click', function (e) {
      var lat = e.latlng.lat, lng = e.latlng.lng;
      pin(lat, lng);
      latest += 1; // an answer still on its way is for an older pin
      show('checking');
      clearTimeout(tapTimer);
      tapTimer = setTimeout(function () { ask(coords(lat, lng), null, false); }, TAP_DELAY);
    });

    if (touchOnly) {
      Array.prototype.forEach.call(document.querySelectorAll('[data-map-hint]'), function (hint) {
        hint.hidden = hint.getAttribute('data-map-hint') !== 'touch';
      });
    }
  }

  chips.forEach(function (chip) {
    chip.addEventListener('click', function () {
      var key = chip.getAttribute('data-district');
      var dot = districtMarkers[key];
      selectChip(key);
      if (dot) {
        reveal(mapEl);
        moveTo(dot.getLatLng(), 13);
        dot.openTooltip();
      }
    });
  });

  if (locateBtn) {
    locateBtn.addEventListener('click', function () {
      if (!navigator.geolocation) { answer('geo'); return; }
      setBusy(locateBtn, true);
      navigator.geolocation.getCurrentPosition(function (pos) {
        var la = pos.coords.latitude, ln = pos.coords.longitude;
        pin(la, ln);
        ask(coords(la, ln), locateBtn, false);
      }, function () {
        setBusy(locateBtn, false);
        answer('geo');
      }, { timeout: 15000, maximumAge: 60000 });
    });
  }

  input.addEventListener('input', function () { setAddressError(false); });

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var address = (input.value || '').trim();
    if (!address) {
      // Spaces only pass `required`: say what is missing, and drop the answer to an earlier address.
      latest += 1;
      clearTimeout(tapTimer);
      show(null);
      setAddressError(true);
      input.focus();
      return;
    }
    setAddressError(false);
    ask('address=' + encodeURIComponent(address), checkBtn, true);
  });
})();
