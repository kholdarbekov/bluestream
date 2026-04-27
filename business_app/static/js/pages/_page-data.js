(function (window) {
    'use strict';
    var cache = {};

    window.getPageData = function (elementId) {
        var id = elementId || 'page-data';
        if (Object.prototype.hasOwnProperty.call(cache, id)) return cache[id];
        var el = document.getElementById(id);
        if (!el) { cache[id] = {}; return cache[id]; }
        try {
            cache[id] = JSON.parse(el.textContent);
        } catch (err) {
            console.error('getPageData: failed to parse #' + id + ' JSON', err);
            cache[id] = {};
        }
        return cache[id];
    };
})(window);
