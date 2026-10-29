// The same-site hop page (business_app/templates/frontend/same_site_hop.html, business_app/utils/page_auth.py).
// It reloads the target the server wrote into `data-hop-target`, which already passed the server's one
// "send me back to" rule (shared/safe_paths.py safe_next_path), and keeps the #fragment, which never reaches the
// server. The page's plain meta refresh does the same, without the fragment, a second later.
(function () {
    var target = document.documentElement.getAttribute('data-hop-target');
    if (target) window.location.replace(target + window.location.hash);
})();
