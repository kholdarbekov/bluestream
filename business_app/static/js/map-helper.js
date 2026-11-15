/**
 * AEMap: loads the vendored Leaflet (static/vendor/leaflet) on demand, once per page.
 *
 *   AEMap.load({js, css, images}) -> Promise<L>
 *
 * Pages that show a map only after a click (the address wizard, an order's map) call this on first use instead of
 * putting Leaflet in <head> on every visit. `images` is the folder of the marker icons, so pins never come from a CDN.
 * Map containers get the `ae-map` class: address-wizard.css stops the theme's global `img` transition from delaying
 * the tiles there.
 */
(function (window, document) {
    'use strict';

    var pending = null;

    // One tag per file. A tag that failed is taken out again, so the next load() (the customer reopening the form after
    // a network blip) inserts a fresh one: a failed <script> never fires again, and waiting on it hung forever.
    function addOnce(tag, attrs) {
        return new Promise(function (resolve, reject) {
            var key = attrs.src || attrs.href;
            var found = document.querySelector(tag + '[data-ae-map="' + key + '"]');
            if (found && found.getAttribute('data-loaded') === '1') { resolve(); return; }
            var el = found || document.createElement(tag);
            el.addEventListener('load', function () { el.setAttribute('data-loaded', '1'); resolve(); });
            el.addEventListener('error', function () {
                if (el.parentNode) el.parentNode.removeChild(el);
                reject(new Error('Failed to load ' + key));
            });
            if (!found) {
                Object.keys(attrs).forEach(function (name) { el.setAttribute(name, attrs[name]); });
                el.setAttribute('data-ae-map', key);
                (tag === 'link' ? document.head : document.body).appendChild(el);
            }
        });
    }

    function load(assets) {
        if (window.L && window.L.map) return Promise.resolve(window.L);
        if (pending) return pending;
        pending = Promise.all([
            addOnce('link', { rel: 'stylesheet', href: assets.css }),
            addOnce('script', { src: assets.js })
        ]).then(function () {
            var L = window.L;
            // Leaflet 1.9 builds each default icon URL as imagePath + the file name (marker-icon.png, …): the folder
            // goes in imagePath, never a full URL in iconUrl (that came out doubled: .../images//static/.../marker-icon.png).
            if (assets.images) L.Icon.Default.imagePath = assets.images.replace(/\/?$/, '/');
            return L;
        }).catch(function (error) {
            pending = null;
            throw error;
        });
        return pending;
    }

    window.AEMap = { load: load };
})(window, document);
