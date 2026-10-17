import React from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter } from 'react-router-dom';
import { message } from 'antd';

import Users from '../../pages/Users';
import adminService from '../../services/adminService';
import staffService from '../../services/staffService';

vi.mock('../../services/adminService');
vi.mock('../../services/staffService');

vi.mock('../../components/AddressMapPicker', () => ({
  default: () => <div data-testid="address-map-picker" />,
}));

vi.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    get: vi.fn(),
  },
}));

vi.mock('../../hooks/useResponsive', () => ({
  default: () => ({
    isMobileDevice: false,
    isTabletDevice: false,
    isTouchDevice: false,
    getFontSize: (mobile, _tablet, desktop) => desktop || mobile,
  }),
}));

// The stub returns each key's inline English fallback (what the page renders before the DB rows
// load), filling {{placeholders}} from the call's options like i18next does.
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key, fallback, options) => {
      const text = typeof fallback === 'string' && fallback ? fallback : key;
      return options && typeof options === 'object'
        ? text.replace(/\{\{(\w+)\}\}/g, (_match, name) => String(options[name]))
        : text;
    },
  }),
}));

vi.mock('antd', async () => {
  const actual = await vi.importActual('antd');
  return {
    ...actual,
    Dropdown: ({ menu, children }) => (
      <div>
        {children}
        {menu?.items
          ?.filter((item) => item && item.type !== 'divider' && item.onClick)
          .map((item) => (
            <button
              key={item.key}
              disabled={item.disabled}
              onClick={item.onClick}
              type="button"
            >
              {typeof item.label === 'string' ? item.label : item.key}
            </button>
          ))}
      </div>
    ),
    message: {
      success: vi.fn(),
      error: vi.fn(),
      info: vi.fn(),
      warning: vi.fn(),
      loading: vi.fn(),
      destroy: vi.fn(),
      open: vi.fn(),
    },
  };
});

const createWrapper = () => {
  const queryClient = new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
      },
    },
  });

  return ({ children }) => (
    <BrowserRouter>
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    </BrowserRouter>
  );
};

// The reorder-reminders block of `notification_settings` exactly as the backend publishes it
// (`EffectiveSetting.to_dict(include_reason=True)`, Task 21a), with the `state` it publishes for
// staff (final review deferred 507): 'on', 'customer_off' or 'admin_off'.
const reminderSetting = (overrides = {}) => ({
  enabled: true,
  changed_by_kind: null,
  changed_via: null,
  changed_at: null,
  reason: null,
  state: 'on',
  ...overrides,
});

const publishedSettings = ({ delivery = true, reminders = reminderSetting() } = {}) => ({
  delivery_telegram_status_updates_enabled: delivery,
  delivery_telegram_status_updates_source: 'default',
  telegram_connected: true,
  bot_active: true,
  updated_at: null,
  reorder_reminders: reminders,
});

// What axios rejects with for a 400 from `validation_error_response(message, error_code=...)`:
// the localized text is `errors[0]`, `message` stays "Validation failed", and the machine code
// (only the reminder field's refusals carry one) sits under `data`.
const validationRefusal = ({ errors, errorCode }) => {
  const error = new Error('Request failed with status code 400');
  error.response = {
    status: 400,
    data: {
      success: false,
      message: 'Validation failed',
      errors,
      ...(errorCode ? { data: { error_code: errorCode } } : {}),
    },
  };
  return error;
};

const DELIVERY_SWITCH = 'Telegram delivery updates (in transit, arrived)';
const REMINDER_SWITCH = 'Reorder reminders';

describe('Users page notification settings modal flow', () => {
  beforeEach(() => {
    vi.clearAllMocks();

    adminService.getUsers.mockResolvedValue({
      data: {
        items: [
          {
            id: 11,
            first_name: 'Alice',
            last_name: 'Tester',
            email: 'alice@example.com',
            phone: '+998901234567',
            status: 'active',
            role: 'customer',
            user_type: 'individual',
            telegram_id: '998901234567',
            is_bot_active: true,
            created_at: '2026-03-01T10:00:00+00:00',
            last_login: '2026-03-02T10:00:00+00:00',
          },
        ],
      },
      meta: {
        total: 1,
        page: 1,
        per_page: 20,
      },
    });

    adminService.getUserAddresses.mockResolvedValue({
      data: {
        addresses: [],
      },
    });

    adminService.getUserNotificationSettings.mockResolvedValue({
      data: { notification_settings: publishedSettings() },
    });

    // The real 21a body: the whole published block (delivery + reorder_reminders), not a fragment.
    adminService.updateUserNotificationSettings.mockResolvedValue({
      data: {
        notification_settings: {
          ...publishedSettings({ delivery: false }),
          delivery_telegram_status_updates_source: 'explicit',
          updated_at: '2026-03-05T10:00:00+00:00',
        },
      },
      message: 'updated',
    });

    adminService.updateUserStatus.mockResolvedValue({});
    adminService.createUser.mockResolvedValue({});
    adminService.updateUser.mockResolvedValue({});
    adminService.createUserAddress.mockResolvedValue({});
    adminService.updateUserAddress.mockResolvedValue({});
    adminService.deleteUserAddress.mockResolvedValue({});
    adminService.unlockUserAccount.mockResolvedValue({});
    staffService.getCustomerCodStatement.mockResolvedValue({
      data: {
        data: {
          active_cod_debt_count: 0,
          total_outstanding_amount: 0,
          cod_restricted: false,
          items: [],
        },
      },
    });
  });

  // Opens Alice's details modal, where the notification settings card lives.
  const openAlice = async (user) => {
    render(<Users />, { wrapper: createWrapper() });
    await screen.findByText('alice@example.com');
    await user.click(screen.getByText('ui.users.view_details'));
    await waitFor(() => {
      expect(adminService.getUserNotificationSettings).toHaveBeenCalledWith(11);
    });
  };

  const sentPayload = () => adminService.updateUserNotificationSettings.mock.calls[0][1];

  // antd hides a closed modal (display: none) once its leave motion ends; the content stays mounted.
  const expectReasonModalClosed = async () => {
    await waitFor(() => {
      expect(screen.getByPlaceholderText('Enter reason')).not.toBeVisible();
    });
  };

  it('keeps Confirm disabled until a reason is typed, then sends the delivery-only payload', async () => {
    const user = userEvent.setup();
    await openAlice(user);

    await user.click(await screen.findByRole('switch', { name: DELIVERY_SWITCH }));

    const confirmButton = await screen.findByRole('button', { name: 'Confirm' });
    expect(confirmButton).toBeDisabled();

    // Whitespace is not a reason.
    await user.type(screen.getByPlaceholderText('Enter reason'), '   ');
    expect(confirmButton).toBeDisabled();
    await user.click(confirmButton);
    expect(adminService.updateUserNotificationSettings).not.toHaveBeenCalled();

    // The reason goes out trimmed: padding is not part of it.
    await user.type(screen.getByPlaceholderText('Enter reason'), '  Customer requested disable via phone  ');
    expect(confirmButton).toBeEnabled();
    await user.click(confirmButton);

    await waitFor(() => {
      expect(adminService.updateUserNotificationSettings).toHaveBeenCalledWith(11, {
        delivery_telegram_status_updates_enabled: false,
        reason: 'Customer requested disable via phone',
      });
    });
    expect(adminService.updateUserNotificationSettings).toHaveBeenCalledTimes(1);
    // The delivery switch must not drag the reminder field along.
    expect(Object.keys(sentPayload()).sort()).toEqual(['delivery_telegram_status_updates_enabled', 'reason']);
  });

  it('sends the reminder-only payload from the reorder reminders switch', async () => {
    const user = userEvent.setup();
    await openAlice(user);

    await user.click(await screen.findByRole('switch', { name: REMINDER_SWITCH }));
    expect(screen.getByText('Please provide a reason for turning reorder reminders off.')).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: 'Confirm' })).toBeDisabled();
    await user.type(screen.getByPlaceholderText('Enter reason'), 'Customer moved to Samarkand');
    await user.click(await screen.findByRole('button', { name: 'Confirm' }));

    await waitFor(() => {
      expect(adminService.updateUserNotificationSettings).toHaveBeenCalledWith(11, {
        reorder_reminders_enabled: false,
        reason: 'Customer moved to Samarkand',
      });
    });
    expect(adminService.updateUserNotificationSettings).toHaveBeenCalledTimes(1);
    // The reminder switch must not drag the delivery field along.
    expect(Object.keys(sentPayload()).sort()).toEqual(['reason', 'reorder_reminders_enabled']);
  });

  // Every option a human can pick: each switch, both directions. The reason differs per case so
  // a payload that swapped fields or reused a stale reason cannot pass.
  it.each([
    {
      name: 'reorder reminders off',
      switchName: REMINDER_SWITCH,
      published: publishedSettings(),
      field: 'reorder_reminders_enabled',
      enabled: false,
      reason: 'Asked to stop in the support chat',
    },
    {
      name: 'reorder reminders back on',
      switchName: REMINDER_SWITCH,
      published: publishedSettings({
        reminders: reminderSetting({
          enabled: false,
          changed_by_kind: 'admin',
          state: 'admin_off',
          changed_via: 'admin_ui',
          changed_at: '2026-10-12T06:00:00+00:00',
          reason: 'Moved away',
        }),
      }),
      field: 'reorder_reminders_enabled',
      enabled: true,
      reason: 'Back in Tashkent since October',
      prompt: 'Please provide a reason for turning reorder reminders on.',
    },
    {
      name: 'Telegram delivery updates off',
      switchName: DELIVERY_SWITCH,
      published: publishedSettings(),
      field: 'delivery_telegram_status_updates_enabled',
      enabled: false,
      reason: 'Complained about night pings',
      prompt: 'Please provide a reason for disabling Telegram delivery updates.',
    },
    {
      name: 'Telegram delivery updates back on',
      switchName: DELIVERY_SWITCH,
      published: publishedSettings({ delivery: false }),
      field: 'delivery_telegram_status_updates_enabled',
      enabled: true,
      reason: 'Wants updates again',
      prompt: 'Please provide a reason for enabling Telegram delivery updates.',
    },
  ])('sends exactly one field and the reason: $name', async ({ switchName, published, field, enabled, reason, prompt }) => {
    adminService.getUserNotificationSettings.mockResolvedValue({ data: { notification_settings: published } });
    const user = userEvent.setup();
    await openAlice(user);

    await user.click(await screen.findByRole('switch', { name: switchName }));
    if (prompt) {
      expect(screen.getByText(prompt)).toBeInTheDocument();
    }
    await user.type(screen.getByPlaceholderText('Enter reason'), reason);
    await user.click(await screen.findByRole('button', { name: 'Confirm' }));

    await waitFor(() => {
      expect(adminService.updateUserNotificationSettings).toHaveBeenCalledTimes(1);
    });
    expect(adminService.updateUserNotificationSettings).toHaveBeenCalledWith(11, { [field]: enabled, reason });
    expect(Object.keys(sentPayload()).sort()).toEqual([field, 'reason'].sort());
  });

  it('shows the published reminder setting with its source, route and date', async () => {
    adminService.getUserNotificationSettings.mockResolvedValue({
      data: {
        notification_settings: publishedSettings({
          reminders: reminderSetting({
            enabled: false,
            changed_by_kind: 'customer',
            state: 'customer_off',
            changed_via: 'telegram_button',
            changed_at: '2026-10-10T06:00:00+00:00',
          }),
        }),
      },
    });
    const user = userEvent.setup();
    await openAlice(user);

    // The page renders the published fields; the i18n stub returns each key's fallback.
    expect(await screen.findByTestId('reorder-reminders-source')).toHaveTextContent(
      'Reminders off · customer · telegram_button · Oct 10, 2026'
    );
    expect(screen.getByRole('switch', { name: REMINDER_SWITCH })).not.toBeChecked();
    // A customer's own off carries no staff reason.
    expect(screen.queryByText(/^Reason:/)).not.toBeInTheDocument();
  });

  it('shows an admin-off with the staff reason, and does not re-derive the setting from the kind', async () => {
    adminService.getUserNotificationSettings.mockResolvedValue({
      data: {
        notification_settings: publishedSettings({
          reminders: reminderSetting({
            enabled: false,
            changed_by_kind: 'admin',
            state: 'admin_off',
            changed_via: 'admin_ui',
            changed_at: '2026-10-12T06:00:00+00:00',
            reason: 'Customer moved to Samarkand',
          }),
        }),
      },
    });
    const user = userEvent.setup();
    await openAlice(user);

    expect(await screen.findByTestId('reorder-reminders-source')).toHaveTextContent(
      'Reminders off · admin · admin_ui · Oct 12, 2026'
    );
    expect(screen.getByText('Reason: Customer moved to Samarkand')).toBeInTheDocument();
    expect(screen.getByRole('switch', { name: REMINDER_SWITCH })).not.toBeChecked();
  });

  it('shows a customer who switched reminders back on as on, by the customer', async () => {
    adminService.getUserNotificationSettings.mockResolvedValue({
      data: {
        notification_settings: publishedSettings({
          reminders: reminderSetting({
            enabled: true,
            changed_by_kind: 'customer',
            changed_via: 'web_settings',
            changed_at: '2026-10-11T06:00:00+00:00',
          }),
        }),
      },
    });
    const user = userEvent.setup();
    await openAlice(user);

    expect(await screen.findByTestId('reorder-reminders-source')).toHaveTextContent(
      'Reminders on · customer · web_settings · Oct 11, 2026'
    );
    expect(screen.getByRole('switch', { name: REMINDER_SWITCH })).toBeChecked();
  });

  it('shows "never changed" when no row exists anywhere', async () => {
    const user = userEvent.setup();
    await openAlice(user);

    expect(await screen.findByTestId('reorder-reminders-source')).toHaveTextContent('Reminders on · never changed');
    expect(screen.getByRole('switch', { name: REMINDER_SWITCH })).toBeChecked();
  });

  it('tolerates a payload without the reminder block', async () => {
    const withoutReminders = publishedSettings();
    delete withoutReminders.reorder_reminders;
    adminService.getUserNotificationSettings.mockResolvedValue({ data: { notification_settings: withoutReminders } });
    const user = userEvent.setup();
    await openAlice(user);

    expect(await screen.findByRole('switch', { name: REMINDER_SWITCH })).toBeChecked();
    expect(screen.getByTestId('reorder-reminders-source')).toBeEmptyDOMElement();
  });

  it('replaces the shown setting with the one the PUT publishes', async () => {
    adminService.updateUserNotificationSettings.mockResolvedValue({
      data: {
        notification_settings: publishedSettings({
          reminders: reminderSetting({
            enabled: false,
            changed_by_kind: 'admin',
            state: 'admin_off',
            changed_via: 'admin_ui',
            changed_at: '2026-10-16T05:00:00+00:00',
            reason: 'Customer moved to Samarkand',
          }),
        }),
      },
      message: 'Notification settings updated by staff',
    });
    const user = userEvent.setup();
    await openAlice(user);

    await user.click(await screen.findByRole('switch', { name: REMINDER_SWITCH }));
    await user.type(screen.getByPlaceholderText('Enter reason'), 'Customer moved to Samarkand');
    await user.click(await screen.findByRole('button', { name: 'Confirm' }));

    await waitFor(() => {
      expect(message.success).toHaveBeenCalledWith('Notification settings updated by staff');
    });
    expect(screen.getByTestId('reorder-reminders-source')).toHaveTextContent(
      'Reminders off · admin · admin_ui · Oct 16, 2026'
    );
    expect(screen.getByText('Reason: Customer moved to Samarkand')).toBeInTheDocument();
    expect(screen.getByRole('switch', { name: REMINDER_SWITCH })).not.toBeChecked();
    await expectReasonModalClosed();
    // While it fades out the modal still asks about reminders: it must not flip to the delivery text.
    expect(screen.getByText('Please provide a reason for turning reorder reminders off.')).toBeInTheDocument();
    expect(screen.queryByText('Please provide a reason for disabling Telegram delivery updates.')).not.toBeInTheDocument();
  });

  it('disables both switches, showing neither as on, when no setting was published', async () => {
    adminService.getUserNotificationSettings.mockRejectedValue(new Error('Network Error'));
    const user = userEvent.setup();
    await openAlice(user);

    const reminders = await screen.findByRole('switch', { name: REMINDER_SWITCH });
    const delivery = screen.getByRole('switch', { name: DELIVERY_SWITCH });
    for (const toggle of [reminders, delivery]) {
      expect(toggle).toBeDisabled();
      expect(toggle).not.toBeChecked();
    }
    await user.click(reminders);
    await user.click(delivery);
    expect(screen.queryByPlaceholderText('Enter reason')).not.toBeInTheDocument();
    expect(adminService.updateUserNotificationSettings).not.toHaveBeenCalled();
  });

  describe('excluding a customer who switched reminders off (staff off)', () => {
    const customerOff = (overrides = {}) =>
      publishedSettings({
        reminders: reminderSetting({
          enabled: false,
          changed_by_kind: 'customer',
          state: 'customer_off',
          changed_via: 'telegram_button',
          changed_at: '2026-10-10T06:00:00+00:00',
          ...overrides,
        }),
      });
    const EXCLUDE = 'Exclude (staff off)';

    it.each([
      {
        name: 'an admin-off',
        reminders: { enabled: false, changed_by_kind: 'admin', changed_via: 'admin_ui', reason: 'Moved', state: 'admin_off' },
      },
      {
        name: 'a customer who switched back on',
        reminders: { enabled: true, changed_by_kind: 'customer', changed_via: 'web_settings', state: 'on' },
      },
      { name: 'a never-changed setting', reminders: {} },
      // The page reads the published `state` and never re-derives customer-off from `enabled` +
      // `changed_by_kind` (final review deferred 507): with no state published, nothing is offered.
      {
        name: 'a customer-off published without its state',
        reminders: { enabled: false, changed_by_kind: 'customer', changed_via: 'telegram_button', state: undefined },
      },
    ])('is not offered for $name', async ({ reminders }) => {
      adminService.getUserNotificationSettings.mockResolvedValue({
        data: { notification_settings: publishedSettings({ reminders: reminderSetting(reminders) }) },
      });
      const user = userEvent.setup();
      await openAlice(user);

      await screen.findByRole('switch', { name: REMINDER_SWITCH });
      expect(screen.queryByRole('button', { name: EXCLUDE })).not.toBeInTheDocument();
    });

    it('is not offered when the payload carries no reminder block', async () => {
      const withoutReminders = publishedSettings();
      delete withoutReminders.reorder_reminders;
      adminService.getUserNotificationSettings.mockResolvedValue({ data: { notification_settings: withoutReminders } });
      const user = userEvent.setup();
      await openAlice(user);

      await screen.findByRole('switch', { name: REMINDER_SWITCH });
      expect(screen.queryByRole('button', { name: EXCLUDE })).not.toBeInTheDocument();
    });

    it('sends the reminder-off payload with the reason and redraws as a staff off', async () => {
      adminService.getUserNotificationSettings.mockResolvedValue({ data: { notification_settings: customerOff() } });
      adminService.updateUserNotificationSettings.mockResolvedValue({
        data: {
          notification_settings: publishedSettings({
            reminders: reminderSetting({
              enabled: false,
              changed_by_kind: 'admin',
              state: 'admin_off',
              changed_via: 'admin_ui',
              changed_at: '2026-10-16T05:00:00+00:00',
              reason: 'Asked us to leave him out',
            }),
          }),
        },
        message: 'Notification settings updated by staff',
      });
      const user = userEvent.setup();
      await openAlice(user);

      expect(await screen.findByTestId('reorder-reminders-source')).toHaveTextContent(
        'Reminders off · customer · telegram_button · Oct 10, 2026'
      );
      await user.click(await screen.findByRole('button', { name: EXCLUDE }));
      expect(
        screen.getByText('Please provide a reason for excluding this customer from reorder reminders (staff off).')
      ).toBeInTheDocument();
      expect(await screen.findByRole('button', { name: 'Confirm' })).toBeDisabled();
      await user.type(screen.getByPlaceholderText('Enter reason'), 'Asked us to leave him out');
      await user.click(screen.getByRole('button', { name: 'Confirm' }));

      await waitFor(() => {
        expect(adminService.updateUserNotificationSettings).toHaveBeenCalledWith(11, {
          reorder_reminders_enabled: false,
          reason: 'Asked us to leave him out',
        });
      });
      expect(adminService.updateUserNotificationSettings).toHaveBeenCalledTimes(1);
      expect(Object.keys(sentPayload()).sort()).toEqual(['reason', 'reorder_reminders_enabled']);

      await waitFor(() => {
        expect(screen.getByTestId('reorder-reminders-source')).toHaveTextContent(
          'Reminders off · admin · admin_ui · Oct 16, 2026'
        );
      });
      expect(screen.getByText('Reason: Asked us to leave him out')).toBeInTheDocument();
      // It is a staff off now, so the action is gone and the switch still reads off.
      expect(screen.queryByRole('button', { name: EXCLUDE })).not.toBeInTheDocument();
      expect(screen.getByRole('switch', { name: REMINDER_SWITCH })).not.toBeChecked();
      await expectReasonModalClosed();
    });

    it('keeps the reason modal open with its text when the server refuses', async () => {
      adminService.getUserNotificationSettings.mockResolvedValue({ data: { notification_settings: customerOff() } });
      adminService.updateUserNotificationSettings.mockRejectedValue(
        validationRefusal({ errors: ["A reason is required to change a customer's reorder reminders"], errorCode: 'REASON_REQUIRED' })
      );
      const user = userEvent.setup();
      await openAlice(user);

      await user.click(await screen.findByRole('button', { name: EXCLUDE }));
      await user.type(screen.getByPlaceholderText('Enter reason'), 'Asked us to leave him out');
      await user.click(await screen.findByRole('button', { name: 'Confirm' }));

      await waitFor(() => {
        expect(message.error).toHaveBeenCalledWith('Reason is required');
      });
      expect(screen.getByPlaceholderText('Enter reason')).toHaveValue('Asked us to leave him out');
      expect(screen.getByRole('button', { name: EXCLUDE })).toBeInTheDocument();
    });
  });

  describe('refusals from the server', () => {
    // Fill the modal for a reminder switch-off and confirm; the PUT rejects with `rejection`.
    const confirmReminderOffRefusedWith = async (rejection) => {
      adminService.updateUserNotificationSettings.mockRejectedValue(rejection);
      const user = userEvent.setup();
      await openAlice(user);
      await user.click(await screen.findByRole('switch', { name: REMINDER_SWITCH }));
      await user.type(screen.getByPlaceholderText('Enter reason'), 'Customer moved to Samarkand');
      await user.click(await screen.findByRole('button', { name: 'Confirm' }));
      await waitFor(() => {
        expect(adminService.updateUserNotificationSettings).toHaveBeenCalledTimes(1);
      });
    };

    const expectNothingChanged = () => {
      expect(message.success).not.toHaveBeenCalled();
      // The modal stays open with the typed reason, so the admin can fix it and retry.
      expect(screen.getByRole('button', { name: 'Confirm' })).toBeEnabled();
      expect(screen.getByPlaceholderText('Enter reason')).toHaveValue('Customer moved to Samarkand');
      expect(screen.getByRole('switch', { name: REMINDER_SWITCH })).toBeChecked();
    };

    it('shows the page copy for a REASON_REQUIRED refusal, not the untranslated server literal', async () => {
      // set()'s own message is an English literal (settings.py), so the page's localized copy wins.
      await confirmReminderOffRefusedWith(
        validationRefusal({
          errors: ["A reason is required to change a customer's reorder reminders"],
          errorCode: 'REASON_REQUIRED',
        })
      );

      await waitFor(() => {
        expect(message.error).toHaveBeenCalledWith('Reason is required');
      });
      expect(message.error).toHaveBeenCalledTimes(1);
      expectNothingChanged();
    });

    it('shows the server message of a refusal that carries no error_code (blank reason on the delivery rule)', async () => {
      // The delivery rule's message is localized on the server (get_translation); no code comes with it.
      await confirmReminderOffRefusedWith(validationRefusal({ errors: ['Необходимо указать причину'] }));

      await waitFor(() => {
        expect(message.error).toHaveBeenCalledWith('Необходимо указать причину');
      });
      expect(message.error).toHaveBeenCalledTimes(1);
      expectNothingChanged();
    });

    it('falls back to the page copy for REASON_REQUIRED when the server sent no message text', async () => {
      await confirmReminderOffRefusedWith(validationRefusal({ errors: [], errorCode: 'REASON_REQUIRED' }));

      await waitFor(() => {
        expect(message.error).toHaveBeenCalledWith('Reason is required');
      });
      expect(message.error).toHaveBeenCalledTimes(1);
      expectNothingChanged();
    });

    it('falls back to the generic failure copy when the request never got a response', async () => {
      await confirmReminderOffRefusedWith(new Error('Network Error'));

      await waitFor(() => {
        expect(message.error).toHaveBeenCalledWith('Failed to update notification settings');
      });
      expect(message.error).toHaveBeenCalledTimes(1);
      expectNothingChanged();
    });
  });
});
