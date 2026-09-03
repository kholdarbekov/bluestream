(function () {
    var PAGE_DATA = getPageData();
    var currentUserReferralCode = '';
    var currentUserTier = 'Bronze';

    function escapeHtml(str) {
        if (str === null || str === undefined) return '';
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function formatDate(dateString) {
        var date = new Date(dateString);
        return date.toLocaleDateString('en-US', {
            year: 'numeric',
            month: 'short',
            day: 'numeric'
        });
    }

    function attachImageErrorHandlers(container) {
        container.querySelectorAll('img[data-hide-on-error]').forEach(function (img) {
            img.addEventListener('error', function () {
                this.style.display = 'none';
            });
        });
    }

    async function loadMembershipTiers() {
        var container = document.getElementById('membershipTiersContainer');
        try {
            var response = await apiRequest('/loyalty/tiers');
            var result = await response.json();

            if (response.ok && result.success) {
                renderMembershipTiers(result.data.tiers, result.data.tier_discount_condition);
            } else {
                container.innerHTML = '<div class="col-12 text-center py-4"><p class="text-muted">' + escapeHtml(PAGE_DATA.i18n.tiers_failed) + '</p></div>';
            }
        } catch (error) {
            console.error('Failed to load membership tiers:', error);
            container.innerHTML = '<div class="col-12 text-center py-4"><p class="text-muted">' + escapeHtml(PAGE_DATA.i18n.tiers_failed) + '</p></div>';
        }
    }

    // `discountCondition` is a preformatted, already-translated sentence from
    // GET /loyalty/tiers. The rate is a COD-rail benefit; the browser never
    // holds its own copy of that rule.
    function renderMembershipTiers(tiers, discountCondition) {
        var container = document.getElementById('membershipTiersContainer');

        if (!tiers || tiers.length === 0) {
            container.innerHTML = '<div class="col-12 text-center py-4"><p class="text-muted">' + escapeHtml(PAGE_DATA.i18n.no_tiers) + '</p></div>';
            return;
        }

        var html = tiers.map(function (tier) {
            var tierClass = tier.name.toLowerCase();
            var icon = tier.icon || 'fa-medal';
            var color = tier.color || '#CD7F32';
            var isCurrentTier = tier.name === currentUserTier;

            var benefitsHtml = (tier.benefits || []).map(function (benefit) {
                return '<li>' + escapeHtml(benefit) + '</li>';
            }).join('');

            var multiplierBadge = tier.points_multiplier >= 1
                ? '<span class="badge" style="background: ' + escapeHtml(color) + '; color: white;">' +
                  escapeHtml(String(tier.points_multiplier)) + 'x ' + escapeHtml(PAGE_DATA.i18n.aquacoins) + '</span>'
                : '';

            var discountBlock = tier.discount_percentage > 0
                ? '<div class="tier-discount" style="color: ' + escapeHtml(color) + ';">' +
                  '<i class="far fa-tag"></i> ' + escapeHtml(String(tier.discount_percentage)) + '% ' + escapeHtml(PAGE_DATA.i18n.discount) +
                  (discountCondition
                      ? '<small class="d-block text-muted">' + escapeHtml(discountCondition) + '</small>'
                      : '') +
                  '</div>'
                : '';

            return '<div class="col-md-3">' +
                '<div class="tier-card ' + escapeHtml(tierClass) + ' ' + (isCurrentTier ? 'current-tier' : '') + '" ' +
                'style="border-left-color: ' + escapeHtml(color) + ';">' +
                '<div class="tier-header">' +
                '<i class="far ' + escapeHtml(icon) + '" style="color: ' + escapeHtml(color) + ';"></i>' +
                '<h6>' + escapeHtml(tier.name) + '</h6>' +
                '<p>' + escapeHtml(tier.points_range) + ' ' + escapeHtml(PAGE_DATA.i18n.aquacoins) + '</p>' +
                multiplierBadge +
                '</div>' +
                '<div class="tier-benefits"><ul>' + benefitsHtml + '</ul></div>' +
                discountBlock +
                '</div></div>';
        }).join('');

        container.innerHTML = html;
    }

    function updateLoyaltyOverview(loyalty) {
        currentUserTier = loyalty.current_tier || 'Bronze';

        document.getElementById('currentPoints').textContent = loyalty.current_balance || 0;
        document.getElementById('pointsThisMonth').textContent = '+' + (loyalty.points_this_month || 0) + ' ' + PAGE_DATA.i18n.this_month;
        document.getElementById('currentTier').textContent = currentUserTier;

        if (loyalty.tier_valid_until) {
            var date = new Date(loyalty.tier_valid_until);
            var dateStr = date.toLocaleDateString();
            document.getElementById('tierGuaranteeText').textContent = PAGE_DATA.i18n.guaranteed_until + ' ' + dateStr;
        } else {
            document.getElementById('tierGuaranteeText').textContent = '';
        }

        if (loyalty.requalification) {
            var needed = loyalty.requalification.points_needed_to_keep || 0;
            var reqText = document.getElementById('tierRequalificationText');
            if (needed > 0) {
                reqText.textContent = PAGE_DATA.i18n.earn + ' ' + needed + ' ' + PAGE_DATA.i18n.more_coins_to_keep;
            } else {
                reqText.textContent = PAGE_DATA.i18n.status_secured;
                reqText.className = 'text-success mt-1';
            }
        }

        if (loyalty.tier_progress) {
            var tierProgressPercent = (loyalty.tier_progress.current / loyalty.tier_progress.next_tier_points) * 100;
            document.getElementById('tierProgress').style.width = Math.min(tierProgressPercent, 100) + '%';
            document.getElementById('tierProgressText').textContent =
                (loyalty.tier_progress.points_needed || loyalty.tier_progress.next_tier_points) + ' ' + PAGE_DATA.i18n.coins_to_next_tier;
        }

        var currentTierLower = (loyalty.current_tier || 'Bronze').toLowerCase();
        document.querySelectorAll('.tier-card').forEach(function (card) {
            card.classList.remove('current-tier');
            if (card.classList.contains(currentTierLower)) {
                card.classList.add('current-tier');
            }
        });

        var streakWidget = document.getElementById('currentStreak') &&
            document.getElementById('currentStreak').closest('.loyalty-card');
        if (Array.isArray(loyalty.streak_progress) && loyalty.streak_progress.length) {
            if (streakWidget) streakWidget.style.display = '';
            // Show count of active rules in the header number slot
            document.getElementById('currentStreak').textContent = loyalty.streak_progress.length;
            // Render per-rule rows into the streakNextText element
            var rulesHtml = '<ul class="streak-rules-list list-unstyled text-left mb-0 mt-1">' +
                loyalty.streak_progress.map(function (r) {
                    var pct = Math.min((r.current_orders / r.required_orders) * 100, 100);
                    return '<li class="streak-rule mb-2">' +
                        '<div class="d-flex justify-content-between align-items-baseline">' +
                        '<span class="streak-rule__name font-weight-bold small">' + escapeHtml(r.name) + '</span>' +
                        '<span class="streak-rule__meta text-muted small">' +
                            escapeHtml(String(r.current_orders)) + '/' + escapeHtml(String(r.required_orders)) +
                            ' · ' + escapeHtml(String(r.window_days)) + 'd → +' + escapeHtml(String(r.bonus_points)) +
                        '</span>' +
                        '</div>' +
                        '<div class="progress mt-1" style="height:4px;">' +
                        '<div class="progress-bar bg-danger" role="progressbar" style="width:' + pct + '%"></div>' +
                        '</div></li>';
                }).join('') +
                '</ul>';
            var streakProgressBar = document.getElementById('streakProgress');
            if (streakProgressBar) streakProgressBar.closest('.mt-2').style.display = 'none';
            var streakNextText = document.getElementById('streakNextText');
            if (streakNextText) {
                streakNextText.innerHTML = rulesHtml;
                streakNextText.className = '';
            }
        } else {
            if (streakWidget) streakWidget.style.display = 'none';
        }
    }

    async function loadLoyaltyData() {
        try {
            var response = await apiRequest('/loyalty/account');
            var result = await response.json();

            if (response.ok && result.success) {
                updateLoyaltyOverview(result.data);
            } else {
                console.error('Failed to load loyalty data:', result.message);
                updateLoyaltyOverview({
                    current_balance: 0,
                    current_tier: 'Bronze',
                    points_this_month: 0,
                    tier_progress: { current: 0, next_tier_points: 1000 },
                    available_rewards_count: 0
                });
            }
        } catch (error) {
            console.error('Failed to load loyalty data:', error);
        }
    }

    function getTransactionIcon(type) {
        switch (type) {
            case 'earned': return 'plus';
            case 'bonus': return 'gift';
            case 'redeemed': return 'minus';
            case 'expired': return 'clock';
            default: return 'circle';
        }
    }

    function displayPointsHistory(transactions) {
        var historyList = document.getElementById('pointsHistoryList');

        if (!transactions || transactions.length === 0) {
            historyList.innerHTML = '<div class="text-center py-4"><p class="text-muted">' + escapeHtml(PAGE_DATA.i18n.no_history) + '</p></div>';
            return;
        }

        var html = transactions.map(function (transaction) {
            return '<div class="points-history-item">' +
                '<div class="points-icon ' + escapeHtml(transaction.transaction_type || 'earned') + '">' +
                '<i class="far fa-' + getTransactionIcon(transaction.transaction_type) + '"></i></div>' +
                '<div class="points-details">' +
                '<h6>' + escapeHtml(transaction.description) + '</h6>' +
                '<small class="text-muted">' + escapeHtml(formatDate(transaction.created_at)) + '</small>' +
                (transaction.order_number ? '<br><small class="text-primary">' + escapeHtml(PAGE_DATA.i18n.order) + ' #' + escapeHtml(transaction.order_number) + '</small>' : '') +
                '</div>' +
                '<div class="points-amount ' + (transaction.points > 0 ? 'positive' : 'negative') + '">' +
                (transaction.points > 0 ? '+' : '') + transaction.points + ' ' + escapeHtml(PAGE_DATA.i18n.aquacoins) +
                '</div></div>';
        }).join('');

        historyList.innerHTML = html;
    }

    async function loadPointsHistory() {
        var filter = (document.getElementById('historyFilter') || {}).value || '';
        var historyList = document.getElementById('pointsHistoryList');

        try {
            var response = await apiRequest('/loyalty/history' + (filter ? '?type=' + filter : ''));
            var result = await response.json();

            if (response.ok && result.success) {
                displayPointsHistory(result.data.items);
            } else {
                historyList.innerHTML = '<div class="text-center py-4"><p class="text-muted">' + escapeHtml(PAGE_DATA.i18n.no_history) + '</p></div>';
            }
        } catch (error) {
            console.error('Failed to load points history:', error);
            historyList.innerHTML = '<div class="text-center py-4"><p class="text-muted">' + escapeHtml(PAGE_DATA.i18n.history_failed) + '</p></div>';
        }
    }

    function displayRewards(rewards) {
        var rewardsGrid = document.getElementById('rewardsGrid');

        if (!rewards || rewards.length === 0) {
            rewardsGrid.innerHTML = '<div class="text-center py-4"><p class="text-muted">' + escapeHtml(PAGE_DATA.i18n.no_rewards) + '</p></div>';
            return;
        }

        // Reward-type vocabulary matches the backend whitelist (discount, free_product).
        // free_delivery and voucher were removed (never applied/redeemable).
        var typeLabelMap = {
            'discount': PAGE_DATA.i18n.type_discount,
            'free_product': PAGE_DATA.i18n.type_free_product
        };

        var html = rewards.map(function (reward) {
            var pointsCost = reward.points_cost || reward.points_required || 0;
            var rewardType = reward.reward_type || 'discount';
            var isSystemReward = reward.is_system_reward || false;
            var typeLabel = typeLabelMap[rewardType] || rewardType;

            var imageBlock = reward.image_url
                ? '<img src="' + escapeHtml(reward.image_url) + '" alt="' + escapeHtml(reward.name) + '" style="width:100%; height:100%; object-fit:cover;" data-hide-on-error="1">'
                : (isSystemReward
                    ? '<i class="fas fa-truck" style="font-size: 3rem; color: #52c41a;"></i>'
                    : '<i class="far fa-gift" style="font-size: 3rem; color: #1890ff;"></i>');
            var imageStyle = reward.image_url ? '' : 'display: flex; align-items: center; justify-content: center;';

            if (isSystemReward) {
                return '<div class="reward-card system-reward">' +
                    '<span class="reward-type-badge ' + escapeHtml(rewardType) + '">' + escapeHtml(typeLabel) + '</span>' +
                    '<span class="system-reward-badge"><i class="fas fa-check-circle"></i> ' + escapeHtml(PAGE_DATA.i18n.auto_applied) + '</span>' +
                    '<div class="reward-image" style="' + imageStyle + '">' + imageBlock + '</div>' +
                    '<div class="reward-content">' +
                    '<h5>' + escapeHtml(reward.name) + '</h5>' +
                    '<p>' + escapeHtml(reward.description || '') + '</p>' +
                    '<div class="system-reward-info">' +
                    '<i class="fas fa-magic"></i>' +
                    '<span>' + escapeHtml(PAGE_DATA.i18n.applied_auto_order) + '</span>' +
                    '</div>' +
                    '<div class="reward-points-cost" style="border-top: none; padding-top: 0.75rem;">' +
                    '<span class="points-badge" style="background: linear-gradient(135deg, #52c41a 0%, #389e0d 100%);">' +
                    '<i class="far fa-star"></i> ' + pointsCost.toLocaleString() + ' ' + escapeHtml(PAGE_DATA.i18n.aquacoins) +
                    '</span></div></div></div>';
            }

            return '<div class="reward-card">' +
                '<span class="reward-type-badge ' + escapeHtml(rewardType) + '">' + escapeHtml(typeLabel) + '</span>' +
                '<div class="reward-image" style="' + imageStyle + '">' + imageBlock + '</div>' +
                '<div class="reward-content">' +
                '<h5>' + escapeHtml(reward.name) + '</h5>' +
                '<p>' + escapeHtml(reward.description || '') + '</p>' +
                '<div class="reward-points-cost">' +
                '<span class="points-badge"><i class="far fa-star"></i> ' +
                pointsCost.toLocaleString() + ' ' + escapeHtml(PAGE_DATA.i18n.aquacoins) + '</span>' +
                '</div></div></div>';
        }).join('');

        rewardsGrid.innerHTML = html;
        attachImageErrorHandlers(rewardsGrid);
    }

    async function loadAvailableRewards() {
        var rewardsGrid = document.getElementById('rewardsGrid');

        try {
            var response = await apiRequest('/loyalty/rewards');
            var result = await response.json();

            if (response.ok && result.success) {
                displayRewards(result.data.rewards);
            } else {
                rewardsGrid.innerHTML = '<div class="text-center py-4"><p class="text-muted">' + escapeHtml(PAGE_DATA.i18n.no_rewards) + '</p></div>';
            }
        } catch (error) {
            console.error('Failed to load rewards:', error);
            rewardsGrid.innerHTML = '<div class="text-center py-4"><p class="text-muted">' + escapeHtml(PAGE_DATA.i18n.rewards_failed) + '</p></div>';
        }
    }

    async function generateReferralCode() {
        try {
            var response = await apiRequest('/loyalty/referral');
            var result = await response.json();

            if (response.ok && result.success) {
                currentUserReferralCode = result.data.referral_code;
                document.getElementById('referralCodeText').textContent = result.data.referral_code;
                document.getElementById('referralCount').textContent =
                    (result.data.statistics && result.data.statistics.total_referrals) || 0;
                document.getElementById('referralPoints').textContent =
                    (result.data.statistics && result.data.statistics.points_earned_from_referrals) || 0;
                $('#referralModal').modal('show');
            } else {
                showNotification(result.message || PAGE_DATA.i18n.referral_failed, 'error');
            }
        } catch (error) {
            console.error('Failed to generate referral code:', error);
            showNotification(PAGE_DATA.i18n.referral_failed, 'error');
        }
    }

    function copyReferralCode() {
        navigator.clipboard.writeText(currentUserReferralCode).then(function () {
            showNotification(PAGE_DATA.i18n.referral_copied, 'success');
        }).catch(function () {
            showNotification(PAGE_DATA.i18n.referral_copy_failed, 'error');
        });
    }

    function shareReferralCode() {
        if (navigator.share) {
            navigator.share({
                title: PAGE_DATA.i18n.share_title,
                text: PAGE_DATA.i18n.share_text_prefix + ': ' + currentUserReferralCode,
                url: window.location.origin + '?ref=' + currentUserReferralCode
            });
        } else {
            copyReferralCode();
        }
    }

    document.addEventListener('DOMContentLoaded', async function () {
        loadMembershipTiers();
        await loadLoyaltyData();
        loadAvailableRewards();
        loadPointsHistory();

        var historyFilter = document.getElementById('historyFilter');
        if (historyFilter) {
            historyFilter.addEventListener('change', loadPointsHistory);
        }

        document.body.addEventListener('click', function (e) {
            var target = e.target.closest('[data-action]');
            if (!target) return;

            switch (target.dataset.action) {
                case 'generate-referral':
                    generateReferralCode();
                    break;
                case 'copy-referral':
                    copyReferralCode();
                    break;
                case 'share-referral':
                    shareReferralCode();
                    break;
            }
        });
    });
})();
