// /my-loyalty (templates/frontend/loyalty.html). The server renders the summary, the streak rules, the ways to earn
// and the levels; this script reads only what is per request: the rewards (GET /loyalty/rewards), the AquaCoins history
// (GET /loyalty/points/history, filtered by the chip's `group`, paged with "Show more") and, when asked, the referral code
// (GET /loyalty/referral). An account the loyalty program does not cover gets no page data and no script.
(function () {
    var PAGE_DATA = getPageData();
    var LOYALTY = PAGE_DATA.loyalty;
    if (!LOYALTY) return;
    var I18N = LOYALTY.i18n || {};
    var TXN = LOYALTY.txn || {};
    var NBSP = ' ';
    var PER_PAGE = 20;

    var history = { group: '', page: 0, pages: 0, total: 0, shown: 0, loading: false };
    var referral = { code: '', link: '' };

    // base.js (loaded on every page before this script) owns escaping; there is no unescaped fallback.
    function escapeHtml(value) {
        return window.AE.escapeHtml(value);
    }

    // `{name}` placeholders, filled by base.js (AE.fill, the one fill every page script shares).
    var fill = window.AE.fill;

    // A whole number grouped by a no-break space, as the server prints it (12 000).
    function formatPoints(value) {
        var number = Math.round(Math.abs(Number(value) || 0));
        return String(number).replace(/\B(?=(\d{3})+(?!\d))/g, NBSP);
    }

    function formatDate(iso) {
        return window.AE && typeof window.AE.formatDate === 'function' ? window.AE.formatDate(iso) : String(iso || '').slice(0, 10);
    }

    // The 403 every loyalty read answers when the account is not (or no longer) covered: the server draws that state.
    function isNotAvailable(response, result) {
        return response.status === 403 && result && result.data && result.data.code === 'loyalty_not_available';
    }

    // `request` is the pending apiRequest call; each caller names its route literally.
    async function readJson(request) {
        var response = await request;
        var result = null;
        try { result = await response.json(); } catch (error) { result = null; }
        if (isNotAvailable(response, result)) {
            window.location.reload();
            return null;
        }
        if (!response.ok || !result || !result.success) throw new Error('loyalty request failed: ' + response.status);
        return result;
    }

    // ---------------------------------------------------------------- rewards

    function rewardMedia(reward) {
        var icon = reward.reward_type === 'free_product' ? 'fa-tint' : 'fa-tag';
        var tile = '<span class="ae-icon-tile ae-acl__reward-icon" aria-hidden="true"><i class="far ' + icon + '"></i></span>';
        if (!reward.image_url) return tile;
        // A picture that fails to load gives way to the type's icon (no empty block).
        return '<img class="ae-acl__reward-img" src="' + escapeHtml(reward.image_url) + '" alt="" loading="lazy" data-reward-img>' + tile;
    }

    function rewardState(reward) {
        if (reward.is_system_reward) {
            return '<span class="ae-acl__reward-state is-ready"><i class="far fa-check-circle" aria-hidden="true"></i>' + escapeHtml(I18N.reward_auto) + '</span>';
        }
        if (reward.can_redeem) {
            return '<span class="ae-acl__reward-state is-ready"><i class="far fa-check-circle" aria-hidden="true"></i>' + escapeHtml(I18N.reward_ready) + '</span>';
        }
        return '<span class="ae-acl__reward-state">' +
            escapeHtml(fill(I18N.reward_missing, { pts: formatPoints(reward.points_needed) })) + '</span>';
    }

    function renderRewards(rewards) {
        var grid = document.getElementById('rewardsGrid');
        if (!grid) return;
        grid.removeAttribute('aria-busy');
        if (!rewards || !rewards.length) {
            grid.innerHTML = '<p class="ae-acl__note">' + escapeHtml(I18N.rewards_empty) + '</p>';
            return;
        }
        var typeLabels = { discount: I18N.type_discount, free_product: I18N.type_free_product };
        grid.innerHTML = rewards.map(function (reward) {
            var cost = reward.points_cost || reward.points_required || 0;
            var type = typeLabels[reward.reward_type];
            return '<article class="ae-acl__reward' + (reward.can_redeem || reward.is_system_reward ? ' is-ready' : '') + '">' +
                '<div class="ae-acl__reward-media">' + rewardMedia(reward) + '</div>' +
                '<div class="ae-acl__reward-body">' +
                (type ? '<span class="ae-acl__reward-type">' + escapeHtml(type) + '</span>' : '') +
                '<h3 class="ae-acl__reward-name">' + escapeHtml(reward.name) + '</h3>' +
                (reward.description ? '<p class="ae-acl__reward-text">' + escapeHtml(reward.description) + '</p>' : '') +
                '<div class="ae-acl__reward-foot">' +
                '<span class="ae-badge ae-badge--gold ae-acl__reward-cost">' + formatPoints(cost) + NBSP + escapeHtml(LOYALTY.unit) + '</span>' +
                rewardState(reward) +
                '</div></div></article>';
        }).join('');
        grid.querySelectorAll('img[data-reward-img]').forEach(function (img) {
            img.addEventListener('error', function () {
                img.remove();
            }, { once: true });
        });
    }

    async function loadRewards() {
        var grid = document.getElementById('rewardsGrid');
        try {
            var result = await readJson(apiRequest('/loyalty/rewards'));
            if (result) renderRewards(result.data.rewards);
        } catch (error) {
            if (!grid) return;
            grid.removeAttribute('aria-busy');
            grid.innerHTML = '<p class="ae-acl__note">' + escapeHtml(I18N.rewards_failed) + '</p>';
        }
    }

    // ---------------------------------------------------------------- history

    var TXN_ICONS = {
        order_earn: 'fa-shopping-bag', referral: 'fa-user-friends', welcome: 'fa-hand-sparkles',
        birthday: 'fa-birthday-cake', streak: 'fa-fire', surprise: 'fa-gift', bonus: 'fa-gift',
        redeem: 'fa-exchange-alt', refund: 'fa-undo', expired: 'fa-hourglass-end', adjustment: 'fa-sliders-h'
    };

    function historyRow(txn) {
        var category = txn.display_category || 'other';
        var points = Number(txn.points) || 0;
        var sign = points > 0 ? '+' : (points < 0 ? '−' : '');
        var tone = points > 0 ? 'is-plus' : (points < 0 ? 'is-minus' : '');
        return '<li class="ae-acl__txn">' +
            '<span class="ae-icon-tile ae-icon-tile--sm ae-acl__txn-icon ' + tone + '" aria-hidden="true"><i class="far ' + (TXN_ICONS[category] || 'fa-circle') + '"></i></span>' +
            '<div class="ae-acl__txn-body">' +
            '<p class="ae-acl__txn-title">' + escapeHtml(TXN[category] || TXN.other || '') + '</p>' +
            '<p class="ae-acl__txn-meta"><time datetime="' + escapeHtml(txn.created_at || '') + '">' + escapeHtml(formatDate(txn.created_at)) + '</time>' +
            (txn.order_number ? '<span class="ae-acl__txn-order">' + escapeHtml(fill(I18N.txn_order, { number: txn.order_number })) + '</span>' : '') +
            '</p>' +
            '</div>' +
            '<span class="ae-acl__txn-amount ' + tone + '">' + sign + formatPoints(points) +
            '<span class="visually-hidden"> ' + escapeHtml(LOYALTY.unit) + '</span></span>' +
            '</li>';
    }

    function setHistoryState(html) {
        var state = document.getElementById('historyState');
        if (!state) return;
        state.innerHTML = html || '';
        state.hidden = !html;
    }

    function updateMore() {
        var more = document.getElementById('historyMore');
        var info = document.getElementById('historyInfo');
        if (!more) return;
        more.hidden = !(history.page < history.pages);
        if (info) info.textContent = more.hidden ? '' : fill(I18N.history_shown, { shown: history.shown, total: history.total });
    }

    async function loadHistory(append) {
        if (history.loading) return;
        var list = document.getElementById('pointsHistoryList');
        if (!list) return;
        var page = append ? history.page + 1 : 1;
        var group = history.group;
        history.loading = true;
        var moreButton = document.querySelector('[data-action="history-more"]');
        if (moreButton && append) moreButton.setAttribute('aria-busy', 'true');
        try {
            var query = '?page=' + page + '&per_page=' + PER_PAGE + (group ? '&group=' + encodeURIComponent(group) : '');
            var result = await readJson(apiRequest('/loyalty/points/history' + query));
            if (!result || group !== history.group) return;
            var items = (result.data && result.data.items) || [];
            var meta = result.meta || {};
            history.page = Number(meta.page) || page;
            history.pages = Number(meta.pages) || 0;
            history.total = Number(meta.total) || 0;
            history.shown = (append ? history.shown : 0) + items.length;
            list.removeAttribute('aria-busy');
            var html = items.map(historyRow).join('');
            if (append) {
                list.insertAdjacentHTML('beforeend', html);
            } else {
                list.innerHTML = html;
            }
            setHistoryState(history.total ? '' : '<p class="ae-acl__note">' + escapeHtml(group ? I18N.history_empty_filtered : I18N.history_empty) + '</p>');
            updateMore();
        } catch (error) {
            list.removeAttribute('aria-busy');
            if (!append) list.innerHTML = '';
            setHistoryState('<p class="ae-acl__note">' + escapeHtml(I18N.history_failed) + '</p>' +
                '<button type="button" class="ae-btn ae-btn--secondary ae-btn--sm" data-action="history-retry" data-append="' + (append ? '1' : '') + '">' + escapeHtml(I18N.retry) + '</button>');
        } finally {
            history.loading = false;
            if (moreButton) moreButton.removeAttribute('aria-busy');
        }
    }

    // A chip names a group of the backend's display categories (loyalty_serializers.HISTORY_GROUPS), so a row is
    // listed under the chip its label belongs to; the page never classifies a row itself.
    function selectHistoryGroup(button) {
        var group = button.getAttribute('data-group') || '';
        document.querySelectorAll('#historyFilter [data-group]').forEach(function (chip) {
            chip.setAttribute('aria-pressed', chip === button ? 'true' : 'false');
        });
        if (group === history.group && history.page) return;
        history.group = group;
        history.page = 0;
        history.pages = 0;
        history.loading = false;
        loadHistory(false);
    }

    // --------------------------------------------------------------- referral

    async function showReferralCode(button) {
        button.setAttribute('aria-busy', 'true');
        try {
            var result = await readJson(apiRequest('/loyalty/referral'));
            if (!result) return;
            var data = result.data || {};
            var stats = data.statistics || {};
            referral.code = data.referral_code || '';
            referral.link = data.referral_link || '';
            document.getElementById('referralCodeText').textContent = referral.code;
            document.getElementById('referralCount').textContent = formatPoints(stats.total_referrals);
            document.getElementById('referralPoints').textContent = formatPoints(stats.points_earned_from_referrals);
            document.getElementById('referralCard').hidden = false;
            button.hidden = true;
            document.getElementById('referralCodeText').focus();
        } catch (error) {
            showNotification(I18N.referral_failed, 'error');
        } finally {
            button.removeAttribute('aria-busy');
        }
    }

    function copyReferralCode() {
        if (referral.code) copyText(referral.code);
    }

    function copyText(text) {
        if (!navigator.clipboard) {
            showNotification(I18N.referral_copy_failed, 'error');
            return;
        }
        navigator.clipboard.writeText(text).then(function () {
            showNotification(I18N.referral_copied, 'success');
        }).catch(function () {
            showNotification(I18N.referral_copy_failed, 'error');
        });
    }

    // The link is the backend's (`referral_link`, built from the site's address), never one the page puts together.
    // Without a share sheet, Share copies the same message with the link, so a friend gets what Share would send.
    function shareReferralCode() {
        if (!referral.code) return;
        var text = fill(I18N.share_text, { code: referral.code });
        if (navigator.share) {
            navigator.share({
                title: I18N.share_title,
                text: text,
                url: referral.link || undefined
            }).catch(function () { /* the visitor closed the share sheet */ });
        } else {
            copyText(referral.link ? text + ' ' + referral.link : text);
        }
    }

    // ------------------------------------------------------------------ start

    document.addEventListener('click', function (event) {
        var chip = event.target.closest('#historyFilter [data-group]');
        if (chip) {
            selectHistoryGroup(chip);
            return;
        }
        var target = event.target.closest('[data-action]');
        if (!target) return;
        switch (target.getAttribute('data-action')) {
            case 'generate-referral':
                showReferralCode(target);
                break;
            case 'copy-referral':
                copyReferralCode();
                break;
            case 'share-referral':
                shareReferralCode();
                break;
            case 'history-more':
                loadHistory(true);
                break;
            case 'history-retry':
                loadHistory(target.getAttribute('data-append') === '1');
                break;
        }
    });

    function start() {
        loadRewards();
        loadHistory(false);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', start);
    } else {
        start();
    }
})();
