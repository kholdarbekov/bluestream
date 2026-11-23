/**
 * Address wizard (markup: templates/frontend/partials/address_wizard.html, which documents the host contract).
 *
 *   AddressWizard.open({address, onSaved, makeDefault})
 *       Without `address`: a new address. With `address` (a row of GET /auth/addresses): edit it.
 *       onSaved(address, {created}) runs after a successful save with the row the API returned.
 *       makeDefault pre-ticks "Make this my main address" for a new address (e.g. the customer has none yet).
 *   document 'ae:address-saved'   CustomEvent, detail {address, created}, fired after every save.
 *   AddressWizard.ready           Promise of the geo config {center, polygon, districts} (GET /addresses/geo-config).
 *   AddressWizard.districtName(k) the localized name of a district key, '' when unknown.
 *
 * Rules kept here:
 * - The delivery zone is the backend's polygon (geo-config); a point outside it is refused on the map, and the
 *   backend re-checks on save (ensure_within_delivery_zone).
 * - Restoring a saved pin never geocodes: only a point the customer moves (map tap, drag, search, my location)
 *   asks the geocoder, and its text replaces the address field only while the field is empty or still holds the
 *   geocoder's previous text. Otherwise the wizard offers it as a suggestion.
 * - The district comes from the reverse-geocode answer's canonical `district_key`, never from matching names here.
 * - Duplicate-address guard: the body carries `point_source` (map_pin, gps, search; none for a saved point). A 409
 *   ADDRESS_POSSIBLE_DUPLICATE opens a pane of the customer's nearby saved addresses instead of an error: Use one
 *   (no request), Move its pin to the new point (POST /auth/addresses/<id>/move), or save anyway; each re-send
 *   adds `confirm_duplicate: true` to that one request only. The server decides what may move (`move_address_id`)
 *   and which pins are locked (`pin_move_refusal` on the row); the wizard only shows what it is told.
 */
(function (window, document, $) {
    'use strict';

    var DATA = (typeof window.getPageData === 'function' ? window.getPageData('address-wizard-data') : {}) || {};
    var T = DATA.i18n || {};
    var FALLBACK_GEO = { center: { latitude: 41.2995, longitude: 69.2401 }, polygon: [], districts: [] };
    var TOTAL_STEPS = 3;

    var state = {
        step: 1,
        map: null,
        marker: null,
        location: null,         // {lat, lng} of the pin
        editingId: null,
        wasDefault: false,
        autoAddress: '',        // the geocoder text the wizard last put into #fullAddress
        autoDistrict: '',       // the district key the wizard last selected on its own
        suggestion: '',
        L: null,                // the Leaflet the loader returned
        pointSource: null,      // map_pin | gps | search; null for a point loaded from a saved address
        pinLocked: false,       // the edited address's pin may not move (row.pin_move_refusal)
        onSaved: null,
        geocodeSeq: 0,
        geo: FALLBACK_GEO
    };

    function el(id) { return document.getElementById(id); }
    function val(id) { var node = el(id); return node && node.value ? node.value.trim() : ''; }
    function setVal(id, value) { var node = el(id); if (node) node.value = value === null || value === undefined ? '' : value; }

    function notify(message, type) {
        if (typeof window.showNotification === 'function') window.showNotification(message, type || 'error');
    }

    function format(template, params) {
        return String(template || '').replace(/\{(\w+)\}/g, function (match, name) {
            return Object.prototype.hasOwnProperty.call(params, name) ? params[name] : match;
        });
    }

    async function readJson(response) {
        try { return await response.json(); } catch (error) { return {}; }
    }

    // ---- geo config (zone polygon + districts) -------------------------------------------------------------
    function districtName(key) {
        if (!key) return '';
        var found = (state.geo.districts || []).find(function (d) { return d.key === key; });
        return found ? found.name : '';
    }

    function setDistrict(key) {
        var select = el('districtSelect');
        if (!select) return;
        key = key || '';
        // A district saved as free text (older rows) is not one of the keys: keep it as its own option so a save
        // without touching the field does not wipe it.
        if (key && !Array.prototype.some.call(select.options, function (o) { return o.value === key; })) {
            var legacy = document.createElement('option');
            legacy.value = key;
            legacy.textContent = key;
            legacy.setAttribute('data-legacy', '1');
            select.appendChild(legacy);
        }
        select.value = key;
    }

    function populateDistricts() {
        var select = el('districtSelect');
        if (!select) return;
        var current = select.value;
        select.innerHTML = '';
        var placeholder = document.createElement('option');
        placeholder.value = '';
        placeholder.textContent = T.f_district_ph || '';
        select.appendChild(placeholder);
        (state.geo.districts || []).forEach(function (district) {
            var option = document.createElement('option');
            option.value = district.key;
            option.textContent = district.name;
            select.appendChild(option);
        });
        setDistrict(current);
    }

    async function loadGeo() {
        try {
            var lang = document.documentElement.lang || 'ru';
            var response = await window.apiRequest('/addresses/geo-config?lang=' + encodeURIComponent(lang));
            var result = await readJson(response);
            if (response.ok && result.success && result.data) {
                state.geo = {
                    center: result.data.center || FALLBACK_GEO.center,
                    polygon: result.data.polygon || [],
                    districts: result.data.districts || []
                };
            }
        } catch (error) {
            // The wizard still works without it: no outline, no district names, the backend still checks the zone.
        }
        populateDistricts();
        return state.geo;
    }

    function insideZone(lat, lng) {
        var polygon = state.geo.polygon;
        if (!polygon || !polygon.length) return true;
        var inside = false;
        for (var i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
            var xi = polygon[i][0], yi = polygon[i][1];
            var xj = polygon[j][0], yj = polygon[j][1];
            if (((yi > lng) !== (yj > lng)) && (lat < (xj - xi) * (lng - yi) / (yj - yi) + xi)) inside = !inside;
        }
        return inside;
    }

    // ---- status line and messages ----------------------------------------------------------------------------
    function setStatus(text, tone) {
        var box = el('locationStatus');
        var label = el('locationStatusText');
        if (!box || !label) return;
        box.classList.toggle('d-none', !text);
        box.classList.remove('is-ok', 'is-busy', 'is-warn');
        if (tone) box.classList.add('is-' + tone);
        label.textContent = text || '';
    }

    function showZoneAlert(show) {
        var alert = el('wizardZoneAlert');
        if (alert) alert.hidden = !show;
    }

    function showSuggestion(text) {
        state.suggestion = text || '';
        var box = el('wizardAddressSuggest');
        if (!box) return;
        el('wizardAddressSuggestText').textContent = state.suggestion;
        box.hidden = !state.suggestion;
    }

    // ---- map ---------------------------------------------------------------------------------------------------
    function ensureMap() {
        if (!window.AEMap || !DATA.leaflet) return Promise.reject(new Error('map helper missing'));
        return window.AEMap.load(DATA.leaflet).then(function (L) {
            var container = el('addressWizardMap');
            state.L = L;
            if (!state.map) {
                var center = state.geo.center || FALLBACK_GEO.center;
                state.map = L.map(container, { fadeAnimation: false, zoomSnap: 0.25 }).setView([center.latitude, center.longitude], 12);
                L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
                    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
                    maxZoom: 19
                }).addTo(state.map);
                // A tap on the whole-city view is rough: zoom in on it so the pin can be put on the right door.
                state.map.on('click', function (event) {
                    pickPoint(event.latlng.lat, event.latlng.lng, state.map.getZoom() < 15, 'map_pin');
                });
            }
            drawZone(L);
            state.map.invalidateSize();
            if (state.location) {
                placeMarker(state.location.lat, state.location.lng);
                state.map.setView([state.location.lat, state.location.lng], 16);
            }
            return state.map;
        });
    }

    function drawZone(L) {
        if (state.zoneLayer || !state.geo.polygon || !state.geo.polygon.length) return;
        state.zoneLayer = L.polygon(state.geo.polygon, {
            color: '#0077B6', weight: 2, fillColor: '#00D1F9', fillOpacity: 0.08, interactive: false
        }).addTo(state.map);
        if (!state.location) state.map.fitBounds(state.zoneLayer.getBounds(), { padding: [8, 8] });
    }

    function placeMarker(lat, lng) {
        if (!state.map || !state.L) return;
        if (state.marker) {
            state.marker.setLatLng([lat, lng]);
            return;
        }
        state.marker = state.L.marker([lat, lng], { draggable: !state.pinLocked, keyboard: true }).addTo(state.map);
        state.marker.on('dragend', function (event) {
            if (state.pinLocked) return;
            var pos = event.target.getLatLng();
            if (!insideZone(pos.lat, pos.lng)) {
                if (state.location) state.marker.setLatLng([state.location.lat, state.location.lng]);
                showZoneAlert(true);
                return;
            }
            setPoint(pos.lat, pos.lng, true, 'map_pin');
        });
    }

    function setPoint(lat, lng, geocode, source) {
        state.location = { lat: lat, lng: lng };
        state.pointSource = source || null;
        setVal('selectedLatitude', lat);
        setVal('selectedLongitude', lng);
        showZoneAlert(false);
        placeMarker(lat, lng);
        if (geocode) reverseGeocode(lat, lng);
        else setStatus(T.point_selected, 'ok');
    }

    /** A point the customer chose (tap, drag, search, my location): check the zone, pin it, ask the geocoder. */
    function pickPoint(lat, lng, zoom, source) {
        if (state.pinLocked) return false;
        if (!insideZone(lat, lng)) {
            showZoneAlert(true);
            return false;
        }
        setPoint(lat, lng, true, source);
        if (state.map && zoom) state.map.setView([lat, lng], 16);
        return true;
    }

    async function reverseGeocode(lat, lng) {
        var seq = ++state.geocodeSeq;
        setStatus(T.getting_address, 'busy');
        try {
            var response = await window.apiRequest('/addresses/reverse-geocode', {
                method: 'POST',
                body: JSON.stringify({ latitude: lat, longitude: lng })
            });
            var result = await readJson(response);
            if (seq !== state.geocodeSeq) return;
            if (response.ok && result.success && result.data) {
                applyGeocoded(result.data.formatted_address || '', result.data.district_key || '');
                setStatus(T.point_selected, 'ok');
            } else {
                setStatus(T.point_no_address, 'warn');
            }
        } catch (error) {
            if (seq === state.geocodeSeq) setStatus(T.point_no_address, 'warn');
        }
    }

    /** The "filled in from the map" help shows only while the field holds the geocoder's text. */
    function syncAddressHelp() {
        var help = el('fullAddressHelp');
        if (help) help.hidden = !(state.autoAddress && val('fullAddress') === state.autoAddress);
    }

    function applyGeocoded(text, districtKey) {
        var current = val('fullAddress');
        if (text) {
            if (!current || current === state.autoAddress) {
                setVal('fullAddress', text);
                state.autoAddress = text;
                showSuggestion('');
            } else if (text !== current) {
                showSuggestion(text);
            }
            syncAddressHelp();
        }
        var select = el('districtSelect');
        if (districtKey && select && (!select.value || select.value === state.autoDistrict)) {
            setDistrict(districtKey);
            state.autoDistrict = districtKey;
        }
    }

    async function searchAddress() {
        if (state.pinLocked) return;
        var query = val('addressSearchInput');
        if (!query) {
            setStatus(T.search_empty, 'warn');
            el('addressSearchInput').focus();
            return;
        }
        var button = el('searchAddressBtn');
        setBusy(button, true);
        try {
            var response = await window.apiRequest('/addresses/geocode', {
                method: 'POST',
                body: JSON.stringify({ address: query + ', Tashkent' })
            });
            var result = await readJson(response);
            if (response.ok && result.success && result.data) {
                pickPoint(result.data.latitude, result.data.longitude, true, 'search');
            } else {
                setStatus(response.status === 404 ? T.not_found : (result.message || T.not_found), 'warn');
            }
        } catch (error) {
            setStatus(T.search_failed, 'warn');
        } finally {
            setBusy(button, false);
        }
    }

    function useMyLocation() {
        if (state.pinLocked) return;
        var button = el('useMyLocationBtn');
        if (!navigator.geolocation) {
            setStatus(T.geo_unsupported, 'warn');
            return;
        }
        setBusy(button, true, T.locating);
        navigator.geolocation.getCurrentPosition(function (position) {
            setBusy(button, false, T.my_location);
            pickPoint(position.coords.latitude, position.coords.longitude, true, 'gps');
        }, function (error) {
            setBusy(button, false, T.my_location);
            setStatus(error && error.code === 1 ? T.geo_denied : T.geo_failed, 'warn');
        }, { enableHighAccuracy: true, timeout: 10000, maximumAge: 0 });
    }

    function setBusy(button, busy, label) {
        if (!button) return;
        button.disabled = busy;
        button.classList.toggle('is-loading', busy);
        button.setAttribute('aria-busy', busy ? 'true' : 'false');
        if (label) {
            var span = button.querySelector('span');
            if (span) span.textContent = label;
            else button.textContent = label;
        }
    }

    // ---- a pin that may not move -------------------------------------------------------------------------------
    /** `refusal` is the row's pin_move_refusal (WITH_DRIVER, SHARED_PLACE, OUTLET) or empty: read-only map and why. */
    function setPinLock(refusal) {
        state.pinLocked = !!refusal;
        ['searchAddressBtn', 'useMyLocationBtn'].forEach(function (id) {
            var button = el(id);
            if (button) button.disabled = state.pinLocked;
        });
        var note = el('addressWizardPinLocked');
        if (!note) return;
        var reason = refusal ? (T.pin_locked || {})[refusal] : '';
        note.textContent = reason || '';
        note.hidden = !reason;
    }

    // ---- the duplicate pane ------------------------------------------------------------------------------------
    function setDuplicateVisible(visible) {
        var pane = el('addressWizardDuplicate');
        if (!pane) return;
        pane.hidden = !visible;
        el('addressWizardForm').hidden = visible;
        document.querySelector('#addressWizardModal .ae-wizard__stepper').hidden = visible;
        document.querySelector('#addressWizardModal .modal-footer').hidden = visible;
        if (!visible) {
            el('addressWizardDuplicateList').innerHTML = '';
            el('addressWizardDuplicateActions').innerHTML = '';
            showDuplicateError('');
        }
    }

    function showDuplicateError(message) {
        var box = el('addressWizardDuplicateError');
        box.textContent = message || '';
        box.hidden = !message;
    }

    function setDuplicateBusy(busy) {
        document.querySelectorAll('#addressWizardDuplicate button').forEach(function (button) { button.disabled = busy; });
    }

    function duplicateButton(action, label, className, handler) {
        var button = document.createElement('button');
        button.type = 'button';
        button.className = 'ae-btn ' + className;
        button.setAttribute('data-action', action);
        button.textContent = label;
        button.addEventListener('click', handler);
        return button;
    }

    function textNode(tag, className, text) {
        var node = document.createElement(tag);
        node.className = className;
        node.textContent = text;
        return node;
    }

    function duplicateCard(candidate, moveId, editing) {
        var card = document.createElement('article');
        card.className = 'ae-dup__card';
        card.setAttribute('data-candidate-id', String(candidate.id));
        card.appendChild(textNode('h4', 'ae-dup__label', candidate.display_label || candidate.title || candidate.full_address || ''));
        if (candidate.title && candidate.full_address) card.appendChild(textNode('p', 'ae-dup__line', candidate.full_address));
        card.appendChild(textNode('p', 'ae-dup__distance', format(T.duplicate_distance, { distance: candidate.distance_m })));
        if (editing) return card;
        var canMove = candidate.id === moveId;
        if (canMove) card.appendChild(textNode('p', 'ae-dup__hint', T.duplicate_move_hint));
        var actions = document.createElement('div');
        actions.className = 'ae-dup__actions';
        actions.appendChild(duplicateButton('duplicate-use', T.duplicate_use, 'ae-btn--primary ae-btn--sm', function () { useCandidate(candidate); }));
        if (canMove) {
            actions.appendChild(duplicateButton('duplicate-move', T.duplicate_move, 'ae-btn--secondary ae-btn--sm', function () { moveCandidate(candidate); }));
        }
        card.appendChild(actions);
        return card;
    }

    function showDuplicatePane(details) {
        details = details || {};
        var editing = !!state.editingId;
        var list = el('addressWizardDuplicateList');
        list.innerHTML = '';
        (details.candidates || []).forEach(function (candidate) {
            list.appendChild(duplicateCard(candidate, details.move_address_id, editing));
        });
        var actions = el('addressWizardDuplicateActions');
        actions.innerHTML = '';
        if (editing) {
            actions.appendChild(duplicateButton('duplicate-anyway', T.duplicate_save_anyway, 'ae-btn--primary', function () { saveAnyway(); }));
        } else {
            actions.appendChild(duplicateButton('duplicate-new', T.duplicate_new, 'ae-btn--secondary', function () { saveAnyway(); }));
        }
        // Back to the map step with everything typed kept, so the pin can be fixed (both modes).
        actions.appendChild(duplicateButton('duplicate-back', T.back, 'ae-btn--ghost', function () {
            setDuplicateVisible(false);
            goToStep(1);
        }));
        showDuplicateError('');
        setDuplicateVisible(true);
        var body = document.querySelector('#addressWizardModal .modal-body');
        if (body) body.scrollTop = 0;
        // The Save button that had the focus is hidden now: the pane's title takes it, so it is announced.
        var title = el('addressWizardDuplicateTitle');
        if (title) title.focus({ preventScroll: true });
    }

    /** The customer chose a saved address over the new point: nothing is sent, nothing was saved. */
    function useCandidate(candidate) {
        var onSaved = state.onSaved;
        $('#addressWizardModal').modal('hide');
        if (onSaved) onSaved(candidate, { created: false });
    }

    /** One re-send of the save, after the customer saw the duplicates: `send(body)` makes the request, and
     *  `confirm_duplicate` rides this one body only (payload() never keeps it). */
    async function sendConfirmed(send, created) {
        showDuplicateError('');
        setDuplicateBusy(true);
        try {
            var response = await send(JSON.stringify(payload({ confirm_duplicate: true })));
            var result = await readJson(response);
            if (!response.ok || result.success === false) {
                setDuplicateBusy(false);
                return { failed: result.message || T.save_failed };
            }
            finishSave((result.data && result.data.address) || null, created);
            return { failed: null };
        } catch (error) {
            setDuplicateBusy(false);
            return { failed: T.network_error };
        }
    }

    async function saveAnyway() {
        var created = !state.editingId;
        var outcome = await sendConfirmed(function (body) {
            return created
                ? window.apiRequest('/auth/addresses', { method: 'POST', body: body })
                : window.apiRequest('/auth/addresses/' + state.editingId, { method: 'PUT', body: body });
        }, created);
        if (outcome.failed) showDuplicateError(outcome.failed);
    }

    async function moveCandidate(candidate) {
        var outcome = await sendConfirmed(function (body) {
            return window.apiRequest('/auth/addresses/' + candidate.id + '/move', { method: 'POST', body: body });
        }, false);
        if (!outcome.failed) return;
        // The server refused this move (a pin rule, too far): its words stay on screen and Move is gone.
        var move = document.querySelector('#addressWizardDuplicate [data-candidate-id="' + candidate.id + '"] [data-action="duplicate-move"]');
        if (move) move.remove();
        var hint = document.querySelector('#addressWizardDuplicate [data-candidate-id="' + candidate.id + '"] .ae-dup__hint');
        if (hint) hint.remove();
        showDuplicateError(outcome.failed);
    }

    // ---- steps -------------------------------------------------------------------------------------------------
    function goToStep(step) {
        if (step < 1 || step > TOTAL_STEPS) return;
        state.step = step;
        document.querySelectorAll('#addressWizardModal .ae-wizard__step').forEach(function (section) {
            section.classList.toggle('is-active', Number(section.dataset.step) === step);
        });
        document.querySelectorAll('#addressWizardModal [data-step-indicator]').forEach(function (item) {
            var index = Number(item.dataset.stepIndicator);
            item.classList.toggle('is-done', index < step);
            item.classList.toggle('is-current', index === step);
            if (index === step) item.setAttribute('aria-current', 'step');
            else item.removeAttribute('aria-current');
        });
        var labels = T.step_labels || [];
        if (el('wizardStepLabel')) el('wizardStepLabel').textContent = labels[step - 1] || '';
        if (el('wizardStepCount')) el('wizardStepCount').textContent = format(T.step_count, { n: step, total: TOTAL_STEPS });
        if (el('wizardProgressBar')) el('wizardProgressBar').style.width = (step / TOTAL_STEPS * 100).toFixed(1) + '%';

        el('wizardPrevBtn').hidden = step === 1;
        el('wizardNextBtn').hidden = step === TOTAL_STEPS;
        el('wizardSaveBtn').hidden = step !== TOTAL_STEPS;

        if (step === 1 && isOpen()) {
            ensureMap().catch(function () { setStatus(T.map_failed, 'warn'); });
        }
        if (step === 3) renderSummary();
        var body = document.querySelector('#addressWizardModal .modal-body');
        if (body) body.scrollTop = 0;
    }

    function validateStep(step) {
        if (step === 1 && !state.location) {
            setStatus(T.pick_point, 'warn');
            return false;
        }
        if (step === 2) {
            var ok = !!val('addressTitle');
            el('addressTitleError').hidden = ok;
            el('addressTitleField').classList.toggle('ae-field--invalid', !ok);
            el('addressTitle').setAttribute('aria-invalid', ok ? 'false' : 'true');
            if (!ok) el('addressTitle').focus();
            return ok;
        }
        return true;
    }

    function renderSummary() {
        var labels = T.summary || {};
        var select = el('districtSelect');
        var district = select && select.value ? (districtName(select.value) || select.value) : '';
        var rows = [
            ['title', val('addressTitle'), true],
            ['address', val('fullAddress'), true],
            ['district', district],
            ['street', val('streetAddress')],
            ['floor', val('floorNumber')],
            ['apartment', val('apartmentNumber')],
            ['landmark', val('landmark')]
        ];
        var list = el('wizardSummary');
        list.innerHTML = '';
        rows.forEach(function (row) {
            if (!row[1] && !row[2]) return;
            var wrap = document.createElement('div');
            wrap.className = 'ae-dl__row';
            var dt = document.createElement('dt');
            dt.textContent = labels[row[0]] || '';
            var dd = document.createElement('dd');
            dd.textContent = row[1] || T.not_set || '';
            if (!row[1]) dd.className = 'is-empty';
            wrap.appendChild(dt);
            wrap.appendChild(dd);
            list.appendChild(wrap);
        });
    }

    // ---- open / fill / save ------------------------------------------------------------------------------------
    function isOpen() {
        var modal = el('addressWizardModal');
        return !!modal && modal.classList.contains('show');
    }

    function resetForm() {
        var form = el('addressWizardForm');
        if (form) form.reset();
        setVal('selectedLatitude', '');
        setVal('selectedLongitude', '');
        setDistrict('');
        Array.prototype.slice.call(el('districtSelect').querySelectorAll('option[data-legacy]')).forEach(function (o) { o.remove(); });
        document.querySelectorAll('#addressWizardModal .quick-title-btn').forEach(function (chip) {
            chip.setAttribute('aria-pressed', 'false');
        });
        el('addressTitleError').hidden = true;
        el('addressTitleField').classList.remove('ae-field--invalid');
        el('addressTitle').removeAttribute('aria-invalid');
        el('isDefault').disabled = false;
        setStatus('');
        showZoneAlert(false);
        showSuggestion('');
        setPinLock('');
        setDuplicateVisible(false);
        state.location = null;
        state.pointSource = null;
        state.autoAddress = '';
        state.autoDistrict = '';
        state.geocodeSeq++;
        syncAddressHelp();
        if (state.marker && state.map) {
            state.map.removeLayer(state.marker);
        }
        state.marker = null;
    }

    function fillForm(address) {
        setVal('addressTitle', address.title);
        setVal('fullAddress', address.full_address);
        setVal('streetAddress', address.street_address);
        setDistrict(address.district || '');
        setVal('floorNumber', address.floor_number);
        setVal('apartmentNumber', address.apartment_number);
        setVal('landmark', address.landmark);
        setVal('deliveryInstructions', address.delivery_instructions);
        el('isDefault').checked = !!address.is_default;
        // There is always one main address: the current one stays main until another one takes over.
        el('isDefault').disabled = !!address.is_default;
        el('isBusiness').checked = !!address.is_business;
        syncChips();
        if (address.latitude && address.longitude) {
            state.location = { lat: Number(address.latitude), lng: Number(address.longitude) };
            setVal('selectedLatitude', address.latitude);
            setVal('selectedLongitude', address.longitude);
        }
    }

    function syncChips() {
        var title = val('addressTitle');
        document.querySelectorAll('#addressWizardModal .quick-title-btn').forEach(function (chip) {
            chip.setAttribute('aria-pressed', chip.dataset.title === title ? 'true' : 'false');
        });
    }

    function open(options) {
        options = options || {};
        var address = options.address || null;
        resetForm();
        state.editingId = address ? address.id : null;
        state.wasDefault = !!(address && address.is_default);
        state.onSaved = typeof options.onSaved === 'function' ? options.onSaved : null;
        // Where the keyboard was (the button that opened the form): it goes back there when the form closes.
        state.returnFocus = document.activeElement;
        if (address) {
            fillForm(address);
            setPinLock(address.pin_move_refusal);
        } else if (options.makeDefault) {
            el('isDefault').checked = true;
        }
        el('addressWizardModalTitle').textContent = address ? T.title_edit : T.title_new;
        goToStep(address && state.location ? 2 : 1);
        $('#addressWizardModal').modal('show');
    }

    function payload(extra) {
        // Optional fields left empty are sent as null, so an edit clears them instead of storing ''.
        function opt(id) { return val(id) || null; }
        return Object.assign({
            title: val('addressTitle'),
            full_address: val('fullAddress'),
            street_address: opt('streetAddress'),
            district: el('districtSelect').value || null,
            city: 'Tashkent',
            country: 'Uzbekistan',
            floor_number: opt('floorNumber'),
            apartment_number: opt('apartmentNumber'),
            delivery_instructions: opt('deliveryInstructions'),
            landmark: opt('landmark'),
            latitude: state.location.lat,
            longitude: state.location.lng,
            is_default: el('isDefault').checked,
            is_business: el('isBusiness').checked,
            point_source: state.pointSource
        }, extra || {});
    }

    /** The usual end of a save: close, tell the customer, tell the page (the 'ae:address-saved' event and onSaved). */
    function finishSave(saved, created) {
        var onSaved = state.onSaved;
        $('#addressWizardModal').modal('hide');
        notify(T.saved, 'success');
        document.dispatchEvent(new CustomEvent('ae:address-saved', { detail: { address: saved, created: created } }));
        if (onSaved) onSaved(saved, { created: created });
    }

    async function save() {
        if (!state.location) { goToStep(1); validateStep(1); return; }
        if (!validateStep(2)) { goToStep(2); validateStep(2); return; }
        var button = el('wizardSaveBtn');
        var label = button.textContent;
        setBusy(button, true, T.saving);
        var created = !state.editingId;
        try {
            var body = JSON.stringify(payload());
            var response = created
                ? await window.apiRequest('/auth/addresses', { method: 'POST', body: body })
                : await window.apiRequest('/auth/addresses/' + state.editingId, { method: 'PUT', body: body });
            var result = await readJson(response);
            if (response.status === 409 && result.error_code === 'ADDRESS_POSSIBLE_DUPLICATE') {
                showDuplicatePane(result.details);
                return;
            }
            if (!response.ok || result.success === false) {
                notify(result.message || T.save_failed, 'error');
                return;
            }
            finishSave((result.data && result.data.address) || null, created);
        } catch (error) {
            notify(T.network_error, 'error');
        } finally {
            setBusy(button, false, label);
        }
    }

    function bind() {
        el('wizardNextBtn').addEventListener('click', function () {
            if (validateStep(state.step)) goToStep(state.step + 1);
        });
        el('wizardPrevBtn').addEventListener('click', function () { goToStep(state.step - 1); });
        el('wizardSaveBtn').addEventListener('click', save);
        el('searchAddressBtn').addEventListener('click', searchAddress);
        el('useMyLocationBtn').addEventListener('click', useMyLocation);
        el('addressSearchInput').addEventListener('keydown', function (event) {
            if (event.key === 'Enter') { event.preventDefault(); searchAddress(); }
        });
        el('addressTitle').addEventListener('input', function () {
            syncChips();
            if (val('addressTitle')) {
                el('addressTitleError').hidden = true;
                el('addressTitleField').classList.remove('ae-field--invalid');
                el('addressTitle').removeAttribute('aria-invalid');
            }
        });
        document.querySelectorAll('#addressWizardModal .quick-title-btn').forEach(function (chip) {
            chip.addEventListener('click', function () {
                setVal('addressTitle', chip.dataset.title);
                el('addressTitle').dispatchEvent(new Event('input'));
            });
        });
        el('wizardAddressSuggestBtn').addEventListener('click', function () {
            setVal('fullAddress', state.suggestion);
            state.autoAddress = state.suggestion;
            showSuggestion('');
            syncAddressHelp();
        });
        el('fullAddress').addEventListener('input', syncAddressHelp);
        el('addressWizardForm').addEventListener('submit', function (event) { event.preventDefault(); });

        var $modal = $('#addressWizardModal');
        $modal.on('shown.bs.modal', function () {
            if (state.step === 1) ensureMap().catch(function () { setStatus(T.map_failed, 'warn'); });
        });
        $modal.on('hidden.bs.modal', function () {
            state.onSaved = null;
            state.geocodeSeq++;
            var back = state.returnFocus;
            state.returnFocus = null;
            if (back && back !== document.body && document.body.contains(back) && back.focus) back.focus();
        });
        // The static backdrop keeps a stray click outside from closing the form, but Escape closes it, as on every
        // dialog: Bootstrap 4.4 turns Escape on a static modal into the same "shake" (hidePrevented), so the key
        // that caused it is remembered here (this handler runs before Bootstrap's own) and the shake becomes a hide.
        var escapeKey = false;
        $modal.on('keydown', function (event) { escapeKey = event.key === 'Escape' || event.which === 27; });
        $modal.on('hidePrevented.bs.modal', function (event) {
            if (!escapeKey) return;
            escapeKey = false;
            event.preventDefault();
            $modal.modal('hide');
        });
    }

    var AddressWizard = {
        ready: null,
        open: open,
        districtName: districtName,
        // Kept for older callers.
        openForNew: function (options) { open(options); },
        openForEdit: function (address, options) { open(Object.assign({}, options || {}, { address: address })); }
    };

    function init() {
        var modal = el('addressWizardModal');
        if (!modal) return;
        // Bootstrap modals must sit directly in <body>, outside the host page's layout boxes.
        if (modal.parentNode !== document.body) document.body.appendChild(modal);
        bind();
        AddressWizard.ready = loadGeo();
    }

    window.AddressWizard = AddressWizard;
    window.showAddAddressModal = function (options) { open(options); };
    window.editAddressWizard = function (address, options) { AddressWizard.openForEdit(address, options); };

    AddressWizard.ready = Promise.resolve(FALLBACK_GEO);
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
})(window, document, window.jQuery);
