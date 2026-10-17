import { formatDate } from './dateUtils';

const SEPARATOR = ' · ';

/**
 * The `state` the backend publishes on the staff setting shape (`EffectiveSetting.state`, final
 * review deferred 507): pages compare against these and never re-derive a customer-off from
 * `enabled` + `changed_by_kind`. Held to the backend's SETTING_STATES by
 * tests/unit/test_lapsed_customers_ui_contract.py.
 */
export const REMINDER_SETTING_STATE = { ON: 'on', CUSTOMER_OFF: 'customer_off', ADMIN_OFF: 'admin_off' };

/**
 * One line naming the EFFECTIVE reorder-reminder setting the backend published (spec §8): on or
 * off, who changed it, through which route and when. Users.js and the Lapsed customers page both
 * render it, so the wording lives here once. It decides nothing: every part is a published field,
 * so an admin-off over a customer-off reads as the admin's change, exactly as published.
 *
 * `t` is the page's i18next `t`; each label is a `ui.reorder_reminders.*` row seeded in
 * scripts/seed_backend_translations.py, with the published value as the fallback for a vocabulary
 * entry the DB does not know yet.
 */
export const describeReminderSetting = (t, setting) => {
  if (!setting) return '';
  const state = setting.enabled
    ? t('ui.reorder_reminders.setting_on', 'Reminders on')
    : t('ui.reorder_reminders.setting_off', 'Reminders off');
  if (!setting.changed_by_kind) {
    return `${state}${SEPARATOR}${t('ui.reorder_reminders.source.default', 'never changed')}`;
  }
  return [
    state,
    t(`ui.reorder_reminders.source.${setting.changed_by_kind}`, setting.changed_by_kind),
    setting.changed_via ? t(`ui.reorder_reminders.via.${setting.changed_via}`, setting.changed_via) : null,
    setting.changed_at ? formatDate(setting.changed_at) : null,
  ]
    .filter(Boolean)
    .join(SEPARATOR);
};
