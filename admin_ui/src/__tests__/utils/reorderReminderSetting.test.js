import * as settingModule from '../../utils/reorderReminderSetting';
import { describeReminderSetting, REMINDER_SETTING_STATE } from '../../utils/reorderReminderSetting';

// The stub returns each key's inline fallback, as the page renders before the DB rows load.
const t = (key, fallback) => fallback || key;

// A recording `t`: which keys the label asked the DB for, with their fallbacks.
const recordingT = () => {
  const calls = [];
  const translate = (key, fallback) => {
    calls.push([key, fallback]);
    return `<${key}>`;
  };
  return { translate, calls };
};

const published = (overrides = {}) => ({
  enabled: true,
  changed_by_kind: null,
  changed_via: null,
  changed_at: null,
  reason: null,
  ...overrides,
});

describe('describeReminderSetting', () => {
  it('says nothing when no setting was published', () => {
    expect(describeReminderSetting(t, null)).toBe('');
    expect(describeReminderSetting(t, undefined)).toBe('');
  });

  it('names a setting nobody ever changed', () => {
    expect(describeReminderSetting(t, published())).toBe('Reminders on · never changed');
    expect(describeReminderSetting(t, published({ enabled: false }))).toBe('Reminders off · never changed');
  });

  it('names the state, the actor, the route and the date, exactly as published', () => {
    expect(
      describeReminderSetting(
        t,
        published({
          enabled: false,
          changed_by_kind: 'customer',
          changed_via: 'email_link',
          changed_at: '2026-10-10T06:00:00+00:00',
        })
      )
    ).toBe('Reminders off · customer · email_link · Oct 10, 2026');
    expect(
      describeReminderSetting(
        t,
        published({
          enabled: true,
          changed_by_kind: 'admin',
          changed_via: 'admin_ui',
          changed_at: '2026-10-12T06:00:00+00:00',
          reason: 'Back in Tashkent',
        })
      )
    ).toBe('Reminders on · admin · admin_ui · Oct 12, 2026');
  });

  it('does not take the state from the actor: an admin-off and a customer-on are what they say', () => {
    const adminOff = published({ enabled: false, changed_by_kind: 'admin', changed_via: 'worklist' });
    const customerOn = published({ enabled: true, changed_by_kind: 'customer', changed_via: 'web_settings' });
    expect(describeReminderSetting(t, adminOff)).toMatch(/^Reminders off · admin · worklist/);
    expect(describeReminderSetting(t, customerOn)).toMatch(/^Reminders on · customer · web_settings/);
  });

  it('asks the DB for the state, actor and route labels, falling back to the published value', () => {
    const { translate, calls } = recordingT();
    describeReminderSetting(
      translate,
      published({
        enabled: false,
        changed_by_kind: 'admin',
        changed_via: 'worklist',
        changed_at: '2026-10-12T06:00:00+00:00',
      })
    );
    expect(calls).toEqual([
      ['ui.reorder_reminders.setting_off', 'Reminders off'],
      ['ui.reorder_reminders.source.admin', 'admin'],
      ['ui.reorder_reminders.via.worklist', 'worklist'],
    ]);
  });

  it('leaves out a route or date the setting does not carry instead of printing a dash', () => {
    expect(describeReminderSetting(t, published({ enabled: false, changed_by_kind: 'admin' }))).toBe(
      'Reminders off · admin'
    );
  });
});

describe('the published setting state', () => {
  // tests/unit/test_lapsed_customers_ui_contract.py holds these to the backend's SETTING_STATES.
  it('names the three states the backend publishes', () => {
    expect(REMINDER_SETTING_STATE).toEqual({ ON: 'on', CUSTOMER_OFF: 'customer_off', ADMIN_OFF: 'admin_off' });
  });

  it('keeps no JS copy of the customer-off rule: pages read the published `state`', () => {
    expect(settingModule.isCustomerOff).toBeUndefined();
  });
});
